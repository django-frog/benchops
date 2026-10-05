"""Deployment command logic: git-based sync of a local app to a remote bench.

Pipeline:
  1. local    pre-local hooks, snapshot commit, asset manifests
  2. remote   inspect + lock (one agent call)
  3. local    assess: ancestry, HEAD drift, branch switch, unpushed, Frappe version
  4. both     upload one package (git bundle + dist + manifests), preview the file plan
  5. local    render the plan, confirm
  6. remote   pre-remote hooks, apply (backup, checkout, dist, manifests, record),
              post-remote hooks; the lock is always released
"""

import posixpath
import re
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import typer
from invoke.exceptions import UnexpectedExit
from rich.console import Console

from benchops.assets import collect_app_manifests, resolve_bench_path
from benchops.base import BaseCommand
from benchops.gitlocal import GitError, LocalRepo, Snapshot
from benchops.runner import BenchOpsConnectionError, LocalRunner
from benchops.sync import RemoteAgent, RemoteAgentError, build_package, dist_digest

console = Console()

PLAN_LIST_LIMIT = 15
_VERSION_RE = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.M)


class DeployAborted(Exception):
    """A deploy stopped on purpose, before anything on the remote changed."""


def local_frappe_version(bench: Path) -> str | None:
    try:
        match = _VERSION_RE.search((bench / "apps" / "frappe" / "frappe" / "__init__.py").read_text())
    except OSError:
        return None
    return match.group(1) if match else None


def assess(
    state: dict,
    repo: LocalRepo,
    snap: Snapshot,
    deployer: str,
    local_frappe: str | None,
    adopt: bool,
    force: bool,
) -> list[str]:
    """Check the remote state against local history. Returns warnings for the
    plan; raises DeployAborted for anything that would lose someone's work."""
    warnings: list[str] = []
    record = state.get("record")

    if not record:
        if not adopt:
            raise DeployAborted(
                "The server has no BenchOps deploy record for this app. For the first git-based "
                "deploy, re-run with --adopt to review the files already on the server."
            )
    else:
        if state.get("head") != record.get("snapshot"):
            message = "The app's HEAD on the server moved outside BenchOps (e.g. a manual git pull or checkout)."
            if not force:
                raise DeployAborted(message + " Re-run with --force to replace it.")
            warnings.append(message)

        base = record.get("base")
        if base and not repo.is_ancestor(base, snap.base):
            message = (
                f"The server runs {record.get('branch')}@{base[:10]} (deployed by {record.get('deployer')}), "
                "which is not in your branch's history."
            )
            if not force:
                raise DeployAborted(message + " Pull or rebase onto it first, or re-run with --force.")
            warnings.append(message)

        if record.get("branch") and record["branch"] != snap.branch:
            warnings.append(
                f"Switching the server from '{record['branch']}' (deployed by {record.get('deployer')}) "
                f"to '{snap.branch}'."
            )
        if record.get("deployer") != deployer and record.get("uncommitted"):
            warnings.append(
                f"This replaces {len(record['uncommitted'])} uncommitted file(s) deployed by {record.get('deployer')}."
            )

    if not repo.is_pushed(snap.base):
        warnings.append(f"{snap.base[:10]} is not pushed to any remote yet.")

    remote_frappe = state.get("frappe_version")
    if local_frappe and remote_frappe and local_frappe != remote_frappe:
        warnings.append(
            f"Frappe versions differ (local {local_frappe}, server {remote_frappe}); "
            "assets were built against the local one."
        )
    return warnings


def required_steps(app: str, changes: list[str]) -> list[str]:
    """Suggest post-deploy bench steps from the paths that changed."""
    steps = []
    if any(Path(p).name in ("pyproject.toml", "requirements.txt", "setup.py") for p in changes):
        steps.append("bench setup requirements --python")
    if any(
        (p.endswith(".json") and not p.startswith(f"{app}/public/"))
        or Path(p).name == "patches.txt"
        or p == f"{app}/hooks.py"
        for p in changes
    ):
        steps.append("bench migrate")
    if any(p.endswith(".py") for p in changes):
        steps.append("bench restart")
    return steps


def _print_list(title: str, items: list, style: str, fmt=str) -> None:
    if not items:
        return
    console.print(f"[{style}]{title}: {len(items)}[/{style}]")
    for item in items[:PLAN_LIST_LIMIT]:
        console.print(f"    {fmt(item)}")
    if len(items) > PLAN_LIST_LIMIT:
        console.print(f"    … and {len(items) - PLAN_LIST_LIMIT} more")


class DeployCommand(BaseCommand):
    """Encapsulates the deployment pipeline logic."""

    def __init__(
        self,
        server_alias: str,
        app_name: str,
        site: str | None = None,
        adopt: bool = False,
        yes: bool = False,
        force: bool = False,
        break_lock: bool = False,
    ) -> None:
        super().__init__(server_alias, app_name, site)
        self.adopt = adopt
        self.yes = yes
        self.force = force
        self.break_lock = break_lock

    def _resolve_app_dir(self) -> Path:
        """Locate the local application directory."""
        cwd = Path.cwd()
        for candidate in (cwd / "apps" / self.app_name, Path(self.app_name)):
            if candidate.is_dir():
                return candidate
        raise FileNotFoundError(
            f"Local app directory '{self.app_name}' not found (looked in '{cwd / 'apps'}' and '{cwd}')."
        )

    def _run_hooks(self, runner, key: str, label: str, server_config: dict, cwd: str, interpolate: bool) -> None:
        commands = server_config.get(key, [])
        if commands:
            console.print(f"[yellow]Running {label} hooks...[/yellow]")
        for cmd in commands:
            if interpolate:
                cmd = self._interpolate_cmd(cmd)
            console.print(f"[cyan]Executing: {cmd}[/cyan]")
            runner.run(cmd, cwd=cwd)

    def _classify_staging_files(self, preview: dict) -> tuple[list[str], list[str]]:
        """Split untracked server files into kept (staging-owned) and deleted.

        After adoption every new untracked file was created on the server, so
        it is kept. During adoption they may also be leftovers from the old
        archive-based deploys, so the operator decides."""
        keep = set(preview["staging_owned"])
        candidates = preview["staging_new"]
        if not (self.adopt and candidates) or self.yes:
            return sorted(keep | set(candidates)), []

        _print_list("Files on the server that are not in your snapshot", candidates, "bold yellow")
        console.print(
            "These are either staging-only files (e.g. created in Desk) or leftovers from earlier deploys."
        )
        choice = typer.prompt("Keep all [k], delete all [d], or choose per file [c]", default="k").strip().lower()
        if choice == "d":
            return sorted(keep), list(candidates)
        if choice == "c":
            delete = [p for p in candidates if not typer.confirm(f"Keep {p}?", default=True)]
            return sorted(keep | (set(candidates) - set(delete))), delete
        return sorted(keep | set(candidates)), []

    def _render_plan(
        self, snap: Snapshot, state: dict, preview: dict, warnings: list[str], delete: list[str], ship_dist: bool
    ) -> None:
        record = state.get("record") or {}
        console.rule(f"Deploy plan: {self.app_name} → {self.server_alias}")
        console.print(f"Base:      {snap.branch} @ {snap.base[:10]}")
        if record:
            console.print(
                f"Server:    {record.get('branch')} @ {str(record.get('base'))[:10]} "
                f"(deployed by {record.get('deployer')})"
            )
        _print_list("Uncommitted (shipped in the snapshot)", snap.uncommitted, "cyan", lambda c: f"{c[0]}  {c[1]}")
        console.print(f"Changed files on the server: {len(preview['changes'])}")
        _print_list("Removed from the server", preview["deletions"], "cyan")
        _print_list(
            "Staging edits to deployed files (overwritten, backed up)",
            preview["staging_edits"], "yellow", lambda c: f"{c[0]}  {c[1]}",
        )
        if any(path.endswith("modules.txt") for _, path in preview["staging_edits"]):
            warnings.append(
                "modules.txt was edited on the server and will be overwritten; add any module "
                "created there to your local modules.txt, or its DocTypes stop syncing on migrate."
            )
        if not self.adopt:
            _print_list("Staging-only files (kept, not in git)", preview["staging_owned"] + preview["staging_new"], "dim")
            _print_list("Staging-only files now tracked locally (local wins)", preview["collisions"], "yellow")
        _print_list("Staging-only files left inside folders this deploy removes", preview["orphans"], "yellow")
        _print_list("Deleted on the server (adoption)", delete, "red")
        console.print(f"Built assets: {'shipped' if ship_dist else 'unchanged'}")
        steps = required_steps(self.app_name, preview["changes"])
        if steps:
            console.print(f"Changes suggest: {', '.join(steps)}")
        for warning in warnings:
            console.print(f"[bold yellow]⚠ {warning}[/bold yellow]")
        console.rule()

    def execute(self) -> None:
        """Execute the full deployment pipeline."""
        server_config = self._get_server_config()
        bench_path = server_config["bench_path"]
        app = self.app_name

        try:
            app_dir = self._resolve_app_dir()
            bench = resolve_bench_path(app_dir)
            repo = LocalRepo.open(app_dir)

            self._run_hooks(LocalRunner(), "pre_local_commands", "pre-local", server_config, str(app_dir.parent), False)

            deployer = f"{repo.user_name()}@{socket.gethostname()}"
            snap = repo.snapshot(app, deployer)
            manifests = collect_app_manifests(bench, app)
            dist_dir = app_dir / app / "public" / "dist"
            if repo.is_tracked(f"{app}/public/dist"):
                console.print(
                    f"[yellow]Warning: {app}/public/dist is tracked in git; add it to .gitignore, "
                    "build output is shipped separately.[/yellow]"
                )
            digest = dist_digest(dist_dir)
        except (subprocess.CalledProcessError, BenchOpsConnectionError, FileNotFoundError, GitError) as exc:
            console.print(f"[red]Deployment failed: {exc}[/red]")
            raise typer.Exit(1)

        with self._get_remote_runner(server_config) as remote_runner:
            agent = RemoteAgent(remote_runner, bench_path)
            token = uuid.uuid4().hex
            package_remote = posixpath.join(bench_path, "apps", app, ".git", "benchops", f"package-{token}.tar.gz")
            try:
                state = agent.call(
                    "inspect",
                    app=app,
                    adopt=self.adopt,
                    break_lock=self.break_lock,
                    lock={"token": token, "owner": deployer, "started": time.strftime("%Y-%m-%d %H:%M:%S")},
                )
                warnings = assess(state, repo, snap, deployer, local_frappe_version(bench), self.adopt, self.force)

                record = state.get("record") or {}
                ship_code = self.adopt or state.get("tree") != snap.tree or record.get("base") != snap.base
                ship_dist = digest is not None and digest != record.get("dist_digest")
                if not (ship_code or ship_dist or state["staging_edits"]):
                    console.print(f"[green]'{app}' on '{self.server_alias}' is already up to date ({snap.tree[:10]}).[/green]")
                    return

                with tempfile.TemporaryDirectory() as tmp:
                    meta: dict = {}
                    bundle = None
                    if ship_code:
                        ref = f"refs/benchops/deploy/{self.server_alias}/{app}"
                        repo.update_ref(ref, snap.commit)
                        known = {record.get("snapshot"), record.get("base"), state.get("head")}
                        bundle = Path(tmp) / "snapshot.bundle"
                        repo.create_bundle(str(bundle), ref, sorted(c for c in known if c and repo.has_commit(c)))
                        meta["bundle_ref"] = ref
                    package = build_package(
                        Path(tmp) / "package.tar.gz",
                        meta,
                        bundle_path=bundle,
                        dist_dir=dist_dir if ship_dist else None,
                        manifests=manifests if ship_dist else None,
                    )
                    console.print("[yellow]Uploading deploy package...[/yellow]")
                    remote_runner.put(str(package), package_remote)

                preview = agent.call("preview", app=app, token=token, package=package_remote)
                keep, delete = self._classify_staging_files(preview)
                self._render_plan(snap, state, preview, warnings, delete, ship_dist)
                if not self.yes and not typer.confirm("Proceed with deploy?", default=False):
                    raise DeployAborted("Cancelled.")

                self._run_hooks(remote_runner, "pre_remote_commands", "pre-remote", server_config, bench_path, True)

                result = agent.call(
                    "apply",
                    app=app,
                    token=token,
                    fingerprint=preview["fingerprint"],
                    new=preview["new"],
                    keep=keep,
                    delete=delete,
                    record={
                        "app": app,
                        "branch": snap.branch,
                        "base": snap.base,
                        "tree": snap.tree,
                        "deployer": deployer,
                        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "uncommitted": [path for _, path in snap.uncommitted],
                        "dist_digest": digest if digest is not None else record.get("dist_digest"),
                    },
                )
                if result.get("backup_ref"):
                    console.print(f"[cyan]Staging edits backed up on the server as {result['backup_ref']}.[/cyan]")
                for warning in result.get("warnings", []):
                    console.print(f"[yellow]Warning: {warning}[/yellow]")
                console.print(f"[green]Server now runs tree {result['tree'][:10]} (verified).[/green]")

                self._run_hooks(remote_runner, "post_remote_commands", "post-remote", server_config, bench_path, True)
                console.print(f"[green]Successfully deployed '{app}' to '{self.server_alias}'.[/green]")

            except DeployAborted as exc:
                console.print(f"[yellow]Deploy aborted: {exc}[/yellow]")
                raise typer.Exit(1)
            except RemoteAgentError as exc:
                console.print(f"[red]Deployment failed: {self._describe_agent_error(exc)}[/red]")
                raise typer.Exit(1)
            except (subprocess.CalledProcessError, BenchOpsConnectionError, GitError, UnexpectedExit) as exc:
                console.print(f"[red]Deployment failed: {exc}[/red]")
                raise typer.Exit(1)
            finally:
                # Only removes the lock if it carries our token, so a lock held
                # by someone else's deploy is never released by this one.
                try:
                    agent.call("release", app=app, token=token, package=package_remote)
                except (RemoteAgentError, BenchOpsConnectionError) as exc:
                    console.print(f"[yellow]Warning: could not release the deploy lock: {exc}[/yellow]")

    def _describe_agent_error(self, exc: RemoteAgentError) -> str:
        if exc.error == "not_repo":
            return (
                f"apps/{self.app_name} on the server is not a git repository. "
                "Re-run with --adopt for the first git-based deploy."
            )
        if exc.error == "locked":
            lock = exc.data.get("lock") or {}
            return (
                f"a deploy of '{self.app_name}' is in progress by {lock.get('owner')} since {lock.get('started')}. "
                "If it is stale, re-run with --break-lock."
            )
        if exc.error == "staging_changed":
            return "files changed on the server while the plan was shown (e.g. a Desk save); nothing was applied, re-run the deploy."
        if exc.error == "lock_lost":
            return "the deploy lock was taken over by someone else (--break-lock); nothing was applied."
        return exc.error
