"""`benchops clone`: clone every repository of a project in parallel.

The run is planned before anything is written: each repository's access is
checked against its git host (with the user's token, so private hosts and
roles are respected), each target folder is inspected, and conflicts are
reported instead of resolved by guessing:

* two repositories that would land in the same folder,
* a folder that already holds a different repository,
* a folder (or file) that exists and isn't a clone at all,
* another `benchops clone` writing into the same directory (a lock file).

Each repository is cloned into a temporary folder next to its target and
renamed into place only when complete, so an interrupted or failed clone
never leaves a half-written folder behind, and a folder that appears while
cloning is never overwritten.
"""

import fnmatch
import hashlib
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from benchops.auth import KeyringUnavailableError
from benchops.config import CONFIG_PATH, ConfigManager
from benchops.projects import RepoSpec, explicit_specs, load_manifest, validate_project
from benchops.vcs.credentials import Redactor, TokenStore, credential_helper_command, git_env, token_env_var
from benchops.vcs.providers import Access, AuthError, Provider, ProviderError, make_provider, provider_kind_for
from benchops.vcs.remotes import Remote, https_url, parse_remote, ssh_url

console = Console()

RUNS_DIR = CONFIG_PATH.parent / "clone-runs"
LOCK_NAME = ".benchops-clone.lock"
TMP_PREFIX = ".benchops-tmp-"
DEFAULT_JOBS = 4
MAX_JOBS_PER_HOST = 8
RETRIES = 2
RETRY_BACKOFF_SECONDS = 2
LS_REMOTE_TIMEOUT_SECONDS = 60
EXPIRY_WARNING_DAYS = 7

TRANSIENT_ERRORS = (
    "could not resolve host", "connection timed out", "connection reset", "early eof",
    "rpc failed", "the remote end hung up unexpectedly", "operation too slow", "returned error: 5",
    "gnutls", "ssl_read", "tls connection", "temporary failure", "connection refused", "unexpected disconnect",
)
ACCESS_ERRORS = (
    "authentication failed", "could not read username", "terminal prompts disabled", "permission denied",
    "repository not found", "not found", "does not appear to be a git repository", "access denied",
    "returned error: 401", "returned error: 403", "not authorized", "403 forbidden", "invalid username or password",
)


class Action(StrEnum):
    CLONE = "clone"
    UPDATE = "update"
    SKIP = "skip"
    CONFLICT = "conflict"
    NO_ACCESS = "no access"
    ERROR = "error"


class Outcome(StrEnum):
    CLONED = "cloned"
    UPDATED = "updated"
    SKIPPED = "skipped"
    NO_ACCESS = "no access"
    CONFLICT = "conflict"
    FAILED = "failed"
    CANCELLED = "cancelled"


FAILED_OUTCOMES = {Outcome.CONFLICT, Outcome.FAILED, Outcome.CANCELLED}
ACTION_STYLES = {
    Action.CLONE: "green", Action.UPDATE: "cyan", Action.SKIP: "dim",
    Action.CONFLICT: "red", Action.NO_ACCESS: "yellow", Action.ERROR: "red",
}
OUTCOME_MARKS = {
    Outcome.CLONED: "[green]✔[/green]", Outcome.UPDATED: "[cyan]⟳[/cyan]", Outcome.SKIPPED: "[dim]↷[/dim]",
    Outcome.NO_ACCESS: "[yellow]⊘[/yellow]", Outcome.CONFLICT: "[red]✖[/red]",
    Outcome.FAILED: "[red]✖[/red]", Outcome.CANCELLED: "[yellow]■[/yellow]",
}


@dataclass
class HostAuth:
    provider: Provider
    token: str | None = None
    source: str | None = None
    api_ok: bool = True

    @property
    def host(self) -> str:
        return self.provider.host

    def env(self) -> dict[str, str]:
        return git_env(self.host, self.provider.git_username(), self.token)


@dataclass
class PlanItem:
    spec: RepoSpec
    target: Path
    action: Action
    url: str = ""
    reason: str = ""
    access: Access | None = None
    branch: str | None = None
    outcome: Outcome | None = None
    detail: str = ""


class GitFailed(Exception):
    """A failed git command: `message` is for people, `output` (all of
    stderr) is what the failure is classified on."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.output = (output or message).lower()

    @property
    def transient(self) -> bool:
        return any(marker in self.output for marker in TRANSIENT_ERRORS)

    @property
    def access_denied(self) -> bool:
        return any(marker in self.output for marker in ACCESS_ERRORS)


def summarize_git_error(output: str) -> str:
    """The lines of git's stderr that say what went wrong, without the
    progress chatter and boilerplate advice around them."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    meaningful = [line for line in lines if line.lower().startswith(("fatal:", "error:", "remote:"))]
    return " ".join((meaningful or lines[-1:])[:3])


class LockHeld(Exception):
    pass


class CloneLock:
    """An exclusive lock file in the target directory, so two clone runs
    can't race to create the same folders. A lock left by a crashed run on
    this machine is detected and taken over."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / LOCK_NAME
        self.token = uuid.uuid4().hex

    def _info(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _is_stale(self, info: dict) -> bool:
        # Signal 0 only probes on POSIX; on Windows os.kill would terminate
        # the process, so there a leftover lock has to be removed by hand.
        if platform.system() == "Windows" or info.get("host") != socket.gethostname():
            return False
        try:
            os.kill(int(info.get("pid", 0)), 0)
        except ProcessLookupError:
            return True
        except (PermissionError, ValueError, OverflowError):
            return False
        return False

    def __enter__(self) -> "CloneLock":
        payload = json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(),
            "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "token": self.token,
        })
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                info = self._info()
                if self._is_stale(info):
                    self.path.unlink(missing_ok=True)
                    continue
                raise LockHeld(
                    f"Another 'benchops clone' is writing into {self.path.parent} "
                    f"(pid {info.get('pid', '?')} on {info.get('host', '?')}, started {info.get('started', '?')}). "
                    f"If it is no longer running, delete {self.path}."
                ) from None
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
            return self
        raise LockHeld(f"Could not take the clone lock {self.path}.")

    def __exit__(self, *exc) -> None:
        if self._info().get("token") == self.token:
            self.path.unlink(missing_ok=True)


def _remove_tree(path: Path) -> None:
    def make_writable(func, target, _exc):
        # Git marks pack files read-only, which stops rmtree on Windows.
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable)


def same_remote(a: Remote, b: Remote) -> bool:
    if a.is_local or b.is_local:
        return a.is_local and b.is_local and Path(a.path).resolve() == Path(b.path).resolve()
    return a.host == b.host and a.path.lower() == b.path.lower()


def run_key(project: str, into: Path) -> str:
    return hashlib.sha1(f"{project}|{into.resolve()}".encode()).hexdigest()[:16]


class CloneCommand:
    """Plans and runs the bulk clone of one project."""

    def __init__(
        self,
        project: str,
        into: Path | None = None,
        jobs: int = DEFAULT_JOBS,
        branch: str | None = None,
        protocol: str | None = None,
        depth: int | None = None,
        partial: bool = False,
        only: list[str] | None = None,
        skip: list[str] | None = None,
        update: bool = False,
        dry_run: bool = False,
        yes: bool = False,
        retry_failed: bool = False,
        persist_credentials: bool = False,
        config: ConfigManager | None = None,
        tokens: TokenStore | None = None,
        runs_dir: Path | None = None,
    ) -> None:
        self.project_ref = project
        self.into = (into or Path.cwd()).expanduser()
        self.jobs = max(1, jobs)
        self.branch = branch
        self.protocol = protocol
        self.depth = depth
        self.partial = partial
        self.only = only or []
        self.skip = skip or []
        self.update = update
        self.dry_run = dry_run
        self.yes = yes
        self.retry_failed = retry_failed
        self.persist_credentials = persist_credentials
        self.config = config or ConfigManager()
        self.tokens = tokens or TokenStore()
        self.runs_dir = runs_dir or RUNS_DIR
        self.redact = Redactor()
        self.hosts: dict[str, HostAuth] = {}
        self.project: dict = {}
        self._cancel = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._procs_lock = threading.Lock()
        self._host_slots: dict[str, threading.Semaphore] = {}

    # --- output helpers ----------------------------------------------------

    def _fail(self, message: str) -> None:
        console.print(f"[red]Error: {self.redact(message)}[/red]")
        raise typer.Exit(1)

    # --- resolving the project ---------------------------------------------

    def _load_project(self) -> tuple[str, dict]:
        candidate = Path(self.project_ref).expanduser()
        if self.project_ref.endswith(".toml") or candidate.is_file():
            if not candidate.is_file():
                self._fail(f"Manifest '{self.project_ref}' does not exist.")
            try:
                return str(candidate.resolve()), load_manifest(candidate)
            except ValueError as exc:
                self._fail(str(exc))
        stored = self.config.get_project(self.project_ref)
        if stored is None:
            self._fail(
                f"No project named '{self.project_ref}'. Add one with 'benchops project add', "
                "or pass the path to a manifest (.toml)."
            )
        try:
            return self.project_ref, validate_project(stored, source=f"project '{self.project_ref}'")
        except ValueError as exc:
            self._fail(str(exc))

    def _host(self, host: str) -> HostAuth:
        if host in self.hosts:
            return self.hosts[host]
        if host == "file":
            auth = HostAuth(make_provider("generic", "file"), api_ok=False)
        else:
            stored = self.config.get_git_host(host) or {}
            configured = stored.get("provider") or (self.project.get("provider") if host == self.project.get("host") else None)
            try:
                token, source = self.tokens.get(host)
            except KeyringUnavailableError as exc:
                console.print(
                    f"[yellow]Warning: {exc}. Set {token_env_var(host)} to use a token without a keyring.[/yellow]"
                )
                token, source = None, None
            provider = make_provider(
                provider_kind_for(host, configured), host, token=token,
                api_url=stored.get("api_url"), username=stored.get("username"),
            )
            auth = HostAuth(provider, token, source)
            self.redact.add(token, provider.git_username())
        self.hosts[host] = auth
        return auth

    def _authenticate(self) -> None:
        """Check every token before anything starts: a rejected token stops
        the run, rather than surfacing as a wall of per-repo failures."""
        for host, auth in sorted(self.hosts.items()):
            if host == "file":
                continue
            provider = auth.provider
            if not auth.token:
                note = "only public repositories" if provider.has_api else "using your git credentials / SSH keys"
                console.print(f"[dim]{host}: no token ({note}). Run 'benchops git login {host}' for private ones.[/dim]")
                continue
            if not provider.has_api:
                console.print(f"[dim]{host}: token from {auth.source} (generic git host, access checked with git).[/dim]")
                continue
            try:
                identity = provider.whoami()
            except AuthError:
                self._fail(
                    f"{host} rejected the token from {auth.source} (expired or revoked?). "
                    f"Run 'benchops git login {host}' to replace it."
                )
            except ProviderError as exc:
                auth.api_ok = False
                console.print(f"[yellow]Warning: {exc} Access will be checked with git only.[/yellow]")
                continue
            self.redact.add(auth.token, provider.git_username())
            expires = ""
            if identity.expires:
                days = (identity.expires - date.today()).days
                expires = f", expires {identity.expires.isoformat()}"
                if days <= EXPIRY_WARNING_DAYS:
                    identity.warnings.append(f"The token expires in {max(days, 0)} day(s).")
            console.print(f"[cyan]{host}: signed in as {identity.username or '?'} (token from {auth.source}{expires}).[/cyan]")
            for warning in identity.warnings:
                console.print(f"[yellow]  Warning: {warning}[/yellow]")

    def _discover(self) -> list[RepoSpec]:
        group = self.project.get("group")
        if not group:
            return []
        host = self.project["host"]
        auth = self.hosts[host]
        try:
            repos = auth.provider.list_group(group)
        except ProviderError as exc:
            self._fail(f"Could not list the repositories of '{group}' on {host}: {exc}")
        pattern = self.project.get("match")
        return [
            RepoSpec(host=host, path=repo.path, dir=repo.name, branch=self.project.get("branch"), explicit=False)
            for repo in repos
            if not pattern or fnmatch.fnmatch(repo.name, pattern)
        ]

    def _merge(self, explicit: list[RepoSpec], discovered: list[RepoSpec]) -> list[RepoSpec]:
        specs: dict[tuple[str, str], RepoSpec] = {}
        for spec in explicit:
            if spec.key in specs:
                console.print(f"[yellow]Warning: {spec.label} is listed twice; using the first entry.[/yellow]")
                continue
            specs[spec.key] = spec
        for spec in discovered:
            specs.setdefault(spec.key, spec)
        return list(specs.values())

    @staticmethod
    def _matches(spec: RepoSpec, pattern: str) -> bool:
        return any(fnmatch.fnmatch(value.lower(), pattern.lower()) for value in (spec.dir, spec.path, spec.label))

    def _select(self, specs: list[RepoSpec]) -> list[RepoSpec]:
        for pattern in self.only:
            if not any(self._matches(spec, pattern) for spec in specs):
                self._fail(f"--only '{pattern}' matches no repository in this project.")
        if self.only:
            specs = [s for s in specs if any(self._matches(s, p) for p in self.only)]
        return [s for s in specs if not any(self._matches(s, p) for p in self.skip)]

    # --- run records (for --retry-failed) ----------------------------------

    def _run_record_path(self, project_key: str) -> Path:
        return self.runs_dir / f"{run_key(project_key, self.into)}.json"

    def _retry_selection(self, project_key: str, specs: list[RepoSpec]) -> list[RepoSpec]:
        try:
            record = json.loads(self._run_record_path(project_key).read_text())
        except (OSError, ValueError):
            self._fail(f"There is no previous clone of this project into {self.into} to retry.")
        failed = {d for d, outcome in record.get("items", {}).items() if outcome in FAILED_OUTCOMES}
        if not failed:
            console.print("[green]The previous run had no failures; nothing to retry.[/green]")
            raise typer.Exit(0)
        return [s for s in specs if s.dir in failed]

    def _save_run(self, project_key: str, plan: list[PlanItem]) -> None:
        record = {
            "project": project_key,
            "into": str(self.into.resolve()),
            "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": {item.spec.dir: str(item.outcome) for item in plan if item.outcome},
        }
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            self._run_record_path(project_key).write_text(json.dumps(record, indent=2))
        except OSError as exc:
            console.print(f"[yellow]Warning: could not save the run record ({exc}); --retry-failed won't work.[/yellow]")

    # --- preflight ---------------------------------------------------------

    def _git(self, args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
             timeout: float | None = None) -> str:
        """Run git, registered so Ctrl+C can stop it; raises GitFailed with
        the redacted stderr."""
        if self._cancel.is_set():
            raise GitFailed("cancelled")
        proc = subprocess.Popen(
            ["git", *args], cwd=cwd, env=env or git_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        with self._procs_lock:
            self._procs.add(proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise GitFailed(f"git {args[0]} timed out after {timeout:.0f}s (connection timed out)") from None
        finally:
            with self._procs_lock:
                self._procs.discard(proc)
        if proc.returncode != 0:
            if self._cancel.is_set():
                raise GitFailed("cancelled")
            output = self.redact(stderr or stdout or "")
            raise GitFailed(summarize_git_error(output) or f"git {args[0]} failed ({proc.returncode})", output)
        return stdout

    def _origin(self, target: Path) -> Remote | None:
        try:
            url = self._git(["remote", "get-url", "origin"], cwd=target).strip()
            return parse_remote(url)
        except (GitFailed, ValueError):
            return None

    def _disk_state(self, target: Path, spec: RepoSpec) -> tuple[str, str]:
        """('missing'|'empty'|'same'|'conflict', reason)."""
        if not target.exists() and not target.is_symlink():
            return "missing", ""
        if not target.is_dir():
            return "conflict", "a file with this name already exists"
        if not any(target.iterdir()):
            return "empty", ""
        if not (target / ".git").exists():
            return "conflict", "folder exists, is not empty and is not a git clone"
        origin = self._origin(target)
        if origin is None:
            return "conflict", "folder is a git clone without an 'origin' remote"
        if not same_remote(origin, Remote(spec.host, spec.path)):
            where = origin.path if origin.is_local else f"{origin.host}/{origin.path}"
            return "conflict", f"folder already holds a different repository ({where})"
        return "same", ""

    def _clone_url(self, spec: RepoSpec, auth: HostAuth, https: str | None, ssh: str | None) -> str:
        if spec.host == "file":
            return spec.url or spec.path
        protocol = self.protocol or (None if spec.url else self.project.get("protocol", "https"))
        if protocol is None:
            return spec.url
        if protocol == "ssh":
            return ssh or ssh_url(spec.host, spec.path)
        return https or https_url(spec.host, spec.path)

    def _check(self, spec: RepoSpec) -> PlanItem:
        target = self.into / spec.dir
        item = PlanItem(spec, target, Action.CLONE, branch=self.branch or spec.branch)

        state, reason = self._disk_state(target, spec)
        if state == "conflict":
            item.action, item.reason = Action.CONFLICT, reason
            return item
        if state == "same":
            item.action = Action.UPDATE if self.update else Action.SKIP
            item.reason = "" if self.update else "already cloned"
            return item

        auth = self.hosts[spec.host]
        https = ssh = None
        api_checked = False
        if auth.provider.has_api and auth.api_ok:
            try:
                result = auth.provider.repo(spec.path)
            except ProviderError as exc:
                item.reason = f"API check failed ({exc}); checked with git"
            else:
                api_checked = True
                item.access = result.access
                if result.repo:
                    https, ssh = result.repo.https_url, result.repo.ssh_url
                    item.branch = item.branch or result.repo.default_branch
                if result.access == Access.NONE:
                    item.action, item.reason = Action.NO_ACCESS, result.reason
                    return item
                if result.access < spec.min_access:
                    item.action = Action.SKIP
                    item.reason = f"needs {spec.min_access.label} access, you have {result.access.label}"
                    return item
                item.reason = result.reason

        item.url = self._clone_url(spec, auth, https, ssh)
        requested = self.branch or spec.branch
        refs = ["HEAD"] + ([f"refs/heads/{requested}"] if requested else [])
        try:
            output = self._git(["ls-remote", "--symref", item.url, *refs], env=auth.env(),
                               timeout=LS_REMOTE_TIMEOUT_SECONDS)
        except GitFailed as exc:
            if exc.access_denied:
                item.action = Action.NO_ACCESS
                item.reason = (
                    "the API lists it, but git refused the token (check its repository read scope)"
                    if api_checked else f"git refused access: {exc.message}"
                )
            else:
                item.action, item.reason = Action.ERROR, f"unreachable: {exc.message}"
            return item

        if item.access is None:
            item.access = Access.READ
        heads = {line.split("\t", 1)[1] for line in output.splitlines() if "\t" in line and not line.startswith("ref:")}
        if requested and f"refs/heads/{requested}" not in heads:
            item.action, item.reason = Action.ERROR, f"branch '{requested}' doesn't exist on the remote"
            return item
        if not item.branch:
            for line in output.splitlines():
                if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                    item.branch = line.split("\t", 1)[0].removeprefix("ref: refs/heads/")
        return item

    def _preflight(self, specs: list[RepoSpec]) -> list[PlanItem]:
        by_folder: dict[str, list[RepoSpec]] = {}
        for spec in specs:
            # Case-insensitive: on macOS and Windows "HRMS" and "hrms" are one folder.
            by_folder.setdefault(spec.dir.lower(), []).append(spec)

        plan: dict[RepoSpec, PlanItem] = {}
        to_check = []
        for spec in specs:
            clashing = [s for s in by_folder[spec.dir.lower()] if s is not spec]
            if clashing:
                others = ", ".join(s.label for s in clashing)
                plan[spec] = PlanItem(spec, self.into / spec.dir, Action.CONFLICT,
                                      reason=f"same folder as {others} (set 'dir' for one of them)")
            else:
                to_check.append(spec)

        with console.status(f"Checking {len(to_check)} repositories..."):
            with ThreadPoolExecutor(max_workers=min(16, max(1, len(to_check)))) as pool:
                for spec, item in zip(to_check, pool.map(self._check, to_check), strict=True):
                    plan[spec] = item
        return [plan[spec] for spec in specs]

    def _render_plan(self, plan: list[PlanItem]) -> None:
        table = Table(title=f"Clone plan → {self.into}")
        table.add_column("Repository", style="bold")
        table.add_column("Folder")
        table.add_column("Branch")
        table.add_column("Access")
        table.add_column("Action")
        table.add_column("Note", style="dim")
        for item in plan:
            style = ACTION_STYLES[item.action]
            table.add_row(*(self.redact(cell) for cell in (
                item.spec.label, item.spec.dir, item.branch or "[dim]default[/dim]",
                item.access.label if item.access is not None else "[dim]—[/dim]",
                f"[{style}]{item.action}[/{style}]", item.reason,
            )))
        console.print(table)

    # --- running -----------------------------------------------------------

    def _clone_args(self, item: PlanItem) -> list[str]:
        args = ["clone", "--origin", "origin"]
        requested = self.branch or item.spec.branch
        if requested:
            args += ["--branch", requested]
        if self.depth:
            args += ["--depth", str(self.depth)]
            if not requested:
                args.append("--single-branch")
        elif self.partial:
            args.append("--filter=blob:none")
        return args

    def _wants_helper(self, item: PlanItem) -> bool:
        auth = self.hosts[item.spec.host]
        # A partial clone fetches file contents on demand, so later git
        # commands need the token too.
        return bool(auth.token) and item.url.startswith("https://") and (self.persist_credentials or self.partial)

    def _install_helper(self, item: PlanItem) -> None:
        key = f"credential.https://{item.spec.host}.helper"
        # The empty entry resets helpers inherited from global config, so a
        # stale stored password can't be tried before the BenchOps token.
        self._git(["config", "--add", key, ""], cwd=item.target)
        self._git(["config", "--add", key, credential_helper_command()], cwd=item.target)

    def _clone(self, item: PlanItem) -> None:
        auth = self.hosts[item.spec.host]
        tmp = self.into / f"{TMP_PREFIX}{item.spec.dir}-{uuid.uuid4().hex[:8]}"
        try:
            self._git([*self._clone_args(item), "--", item.url, str(tmp)], env=auth.env())
            if item.target.exists() or item.target.is_symlink():
                if item.target.is_dir() and not item.target.is_symlink() and not any(item.target.iterdir()):
                    item.target.rmdir()
                else:
                    item.outcome, item.detail = Outcome.CONFLICT, "the folder appeared while cloning; left it untouched"
                    return
            try:
                os.rename(tmp, item.target)
            except OSError:
                item.outcome, item.detail = Outcome.CONFLICT, "the folder appeared while cloning; left it untouched"
                return
            if self._wants_helper(item):
                self._install_helper(item)
                item.detail = "credential helper set"
            item.outcome = Outcome.CLONED
        finally:
            _remove_tree(tmp)

    def _update(self, item: PlanItem) -> None:
        auth = self.hosts[item.spec.host]
        self._git(["fetch", "--prune", "origin"], cwd=item.target, env=auth.env())
        item.outcome = Outcome.UPDATED
        if self._git(["status", "--porcelain"], cwd=item.target).strip():
            item.detail = "fetched; not merged: local changes"
            return
        try:
            self._git(["rev-parse", "--abbrev-ref", "@{u}"], cwd=item.target)
        except GitFailed:
            item.detail = "fetched; branch has no upstream"
            return
        before = self._git(["rev-parse", "HEAD"], cwd=item.target).strip()
        try:
            self._git(["merge", "--ff-only", "@{u}"], cwd=item.target)
        except GitFailed:
            item.detail = "fetched; not merged: branch has diverged"
            return
        after = self._git(["rev-parse", "HEAD"], cwd=item.target).strip()
        item.detail = "fast-forwarded" if before != after else "up to date"

    def _process(self, item: PlanItem) -> None:
        slot = self._host_slots[item.spec.host]
        with slot:
            for attempt in range(RETRIES + 1):
                if self._cancel.is_set():
                    item.outcome = Outcome.CANCELLED
                    return
                try:
                    (self._clone if item.action == Action.CLONE else self._update)(item)
                    return
                except GitFailed as exc:
                    if self._cancel.is_set():
                        item.outcome = Outcome.CANCELLED
                        return
                    if exc.transient and attempt < RETRIES:
                        time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    # Access was confirmed in preflight, so a refusal now (a
                    # revoked token, or a local "Permission denied" on disk)
                    # is a failure that must show in the exit code.
                    item.outcome, item.detail = Outcome.FAILED, exc.message
                    return
                except OSError as exc:
                    item.outcome, item.detail = Outcome.FAILED, str(exc)
                    return

    def _stop_running(self) -> None:
        self._cancel.set()
        with self._procs_lock:
            for proc in list(self._procs):
                try:
                    proc.terminate()
                except OSError:
                    pass

    def _run(self, items: list[PlanItem]) -> None:
        per_host = min(self.jobs, MAX_JOBS_PER_HOST)
        for item in items:
            self._host_slots.setdefault(item.spec.host, threading.Semaphore(per_host))

        columns = (SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn())
        with Progress(*columns, console=console, transient=True) as progress:
            task = progress.add_task("Cloning", total=len(items))
            pool = ThreadPoolExecutor(max_workers=self.jobs)
            futures = {pool.submit(self._process, item): item for item in items}
            try:
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        future.result()
                    except Exception as exc:  # a bug in one worker must not hide the others' results
                        item.outcome, item.detail = Outcome.FAILED, self.redact(str(exc))
                    progress.console.print(self._result_line(item))
                    progress.advance(task)
            except KeyboardInterrupt:
                progress.console.print("[yellow]Interrupted: stopping running clones and cleaning up...[/yellow]")
                self._stop_running()
                pool.shutdown(wait=True, cancel_futures=True)
                for item in items:
                    item.outcome = item.outcome or Outcome.CANCELLED
                raise
            finally:
                pool.shutdown(wait=True)

    def _result_line(self, item: PlanItem) -> str:
        detail = f" [dim]({self.redact(item.detail)})[/dim]" if item.detail else ""
        return f"{OUTCOME_MARKS[item.outcome]} {item.spec.dir}: {item.outcome}{detail}"

    def _summarize(self, plan: list[PlanItem]) -> None:
        counts: dict[Outcome, int] = {}
        for item in plan:
            counts[item.outcome] = counts.get(item.outcome, 0) + 1
        problems = [i for i in plan if i.outcome in FAILED_OUTCOMES | {Outcome.NO_ACCESS}]
        if problems:
            console.print("\n[bold]Needs attention:[/bold]")
            for item in problems:
                console.print(f"  {self._result_line(item)}")
        console.print("\n" + "  ·  ".join(f"{OUTCOME_MARKS[o]} {counts[o]} {o}" for o in Outcome if counts.get(o)))
        if any(i.outcome in FAILED_OUTCOMES for i in plan):
            console.print(f"[dim]Fix the problems above, then: benchops clone {self.project_ref} --retry-failed[/dim]")

    # --- entry point -------------------------------------------------------

    def execute(self) -> None:
        if shutil.which("git") is None:
            self._fail("git is not installed or not on PATH.")
        if self.depth and self.partial:
            self._fail("--depth and --partial can't be combined; pick one.")
        if self.into.exists() and not self.into.is_dir():
            self._fail(f"'{self.into}' exists and is not a directory.")

        project_key, self.project = self._load_project()
        explicit = explicit_specs(self.project)
        for host in {spec.host for spec in explicit} | ({self.project["host"]} if self.project.get("group") else set()):
            self._host(host)
        self._authenticate()

        specs = self._select(self._merge(explicit, self._discover()))
        if self.retry_failed:
            specs = self._retry_selection(project_key, specs)
        if not specs:
            self._fail("No repositories to clone after applying the filters.")
        for spec in specs:
            self._host(spec.host)

        plan = self._preflight(specs)
        self._render_plan(plan)
        for item in plan:
            if item.action in (Action.CLONE, Action.UPDATE):
                continue
            item.outcome = {
                Action.SKIP: Outcome.SKIPPED, Action.NO_ACCESS: Outcome.NO_ACCESS,
                Action.CONFLICT: Outcome.CONFLICT, Action.ERROR: Outcome.FAILED,
            }[item.action]
            item.detail = item.reason

        work = [item for item in plan if item.outcome is None]
        if self.dry_run:
            console.print("[dim]Dry run: nothing was cloned.[/dim]")
            return
        if work and not self.yes:
            verb = " / ".join(sorted({str(i.action) for i in work}))
            if not typer.confirm(f"Proceed ({verb} {len(work)} of {len(plan)} repositories)?", default=True):
                console.print("[yellow]Cancelled. Nothing was cloned.[/yellow]")
                raise typer.Exit(1)

        if work:
            try:
                self.into.mkdir(parents=True, exist_ok=True)
                with CloneLock(self.into):
                    self._run(work)
            except LockHeld as exc:
                self._fail(str(exc))
            except OSError as exc:
                self._fail(f"Can't write into {self.into}: {exc.strerror or exc}.")
            except KeyboardInterrupt:
                self._save_run(project_key, plan)
                self._summarize(plan)
                raise typer.Exit(130)

        self._save_run(project_key, plan)
        self._summarize(plan)
        if any(item.outcome in FAILED_OUTCOMES for item in plan):
            raise typer.Exit(1)
