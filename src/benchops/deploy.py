"""Deployment command logic: ship commits + staged changes onto a remote bench.

A deploy applies the developer's commits and what they staged with `git add`
on top of the server's app, and touches nothing else there — people working
directly on the server keep their uncommitted work, and `git status` on the
server shows the deploy as "Changes to be committed" next to their changes.

Pipeline:
  1. local    pre-local hooks, staged state, build decision + build,
              build output checks, asset manifests
  2. remote   inspect + lock
  3. local    assess: history, branch switch, unpushed, Frappe version
  4. both     upload one package (git bundle + build outputs + manifests),
              preview: files to write, overlaps with work on the server
  5. local    render the plan; overlaps need an explicit yes (default no)
  6. remote   pre-remote hooks, apply (backup overlaps, write deploy set,
              move HEAD, build outputs, record), cache clear, post-remote
              hooks; the lock is always released
"""

import posixpath
import re
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import typer
from invoke.exceptions import UnexpectedExit
from rich.console import Console

from benchops.assets import collect_app_manifests, resolve_bench_path
from benchops.base import BaseCommand
from benchops.build import (
    BuildError,
    check_html_asset_references,
    existing_outputs,
    frontend_source_dirs,
    load_build_outputs,
    outputs_digest,
    run_build,
)
from benchops.gitlocal import GitError, LocalRepo, StagedState
from benchops.runner import BenchOpsConnectionError, LocalRunner
from benchops.sync import RemoteAgent, RemoteAgentError, build_package

console = Console()

PLAN_LIST_LIMIT = 15
_BENCH_BUILD_RE = re.compile(r"\bbench\b.*\sbuild\b")
_VERSION_RE = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.M)


class DeployAborted(Exception):
    """A deploy stopped on purpose, before anything on the remote changed."""


@dataclass
class LocalSide:
    """Everything prepared on the developer's machine before connecting."""

    app_dir: Path
    bench: Path
    repo: LocalRepo
    deployer: str
    staged: StagedState
    output_specs: list[dict]
    outputs: list[str] = field(default_factory=list)  # build outputs to ship, if they changed
    digest: str | None = None
    manifests: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def local_frappe_version(bench: Path) -> str | None:
    try:
        match = _VERSION_RE.search((bench / "apps" / "frappe" / "frappe" / "__init__.py").read_text())
    except OSError:
        return None
    return match.group(1) if match else None


def assess(state: dict, repo: LocalRepo, staged: StagedState, local_frappe: str | None, force: bool) -> list[str]:
    """Check the server's state against local history. Returns warnings for
    the plan; raises DeployAborted when deploying would drop someone's commits."""
    warnings: list[str] = []
    head, remote_branch = state["head"], state.get("branch")

    if not repo.is_ancestor(head, staged.base):
        message = (
            f"The server is at {remote_branch or '(detached)'}@{head[:10]}, which is not in your history "
            "(a commit made on the server, or commits you haven't pulled)."
        )
        if not force:
            raise DeployAborted(message + " Pull it first, or re-run with --force.")
        warnings.append(message + " Its HEAD is backed up under refs/benchops/previous-head/.")

    if staged.branch is None:
        warnings.append("You are on a detached HEAD; the server's HEAD will be detached too.")
    elif remote_branch and remote_branch != staged.branch:
        warnings.append(f"Switching the server from branch '{remote_branch}' to '{staged.branch}'.")
    if not repo.is_pushed(staged.base):
        warnings.append(f"{staged.base[:10]} is not pushed to any remote yet.")

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
        (p.endswith(".json") and p.startswith(f"{app}/") and not p.startswith(f"{app}/public/"))
        or Path(p).name == "patches.txt"
        or p == f"{app}/hooks.py"
        for p in changes
    ):
        steps.append("bench migrate")
    if any(p.endswith(".py") for p in changes):
        steps.append("bench restart")
    return steps


def unstaged_frontend_changes(staged: StagedState, source_dirs: list[str]) -> list[str]:
    """Unstaged or untracked files that `bench build` would compile even
    though their source isn't part of the deploy."""
    paths = [p for _, p in staged.unstaged] + staged.untracked
    return sorted(p for p in paths if any(p.startswith(d + "/") for d in source_dirs))


def _print_list(title: str, items: list, style: str, fmt=str) -> None:
    if not items:
        return
    console.print(f"[{style}]{title}: {len(items)}[/{style}]")
    for item in items[:PLAN_LIST_LIMIT]:
        console.print(f"    {fmt(item)}")
    if len(items) > PLAN_LIST_LIMIT:
        console.print(f"    … and {len(items) - PLAN_LIST_LIMIT} more")


def _change(item) -> str:
    return f"{item[0]}  {item[1]}"


class DeployCommand(BaseCommand):
    """Encapsulates the deployment pipeline logic."""

    def __init__(
        self,
        server_alias: str,
        app_name: str,
        site: str | None = None,
        yes: bool = False,
        force: bool = False,
        break_lock: bool = False,
        skip_build: bool = False,
        overwrite: bool = False,
    ) -> None:
        super().__init__(server_alias, app_name, site)
        self.yes = yes
        self.force = force
        self.break_lock = break_lock
        self.skip_build = skip_build
        self.overwrite = overwrite

    def _resolve_app_dir(self) -> Path:
        """Locate the local application directory."""
        cwd = Path.cwd()
        for candidate in (cwd / "apps" / self.app_name, Path(self.app_name)):
            if candidate.is_dir():
                return candidate
        raise FileNotFoundError(
            f"Local app directory '{self.app_name}' not found (looked in '{cwd / 'apps'}' and '{cwd}')."
        )

    def _run_hooks(self, runner, key: str, label: str, server_config: dict, cwd: str) -> None:
        commands = server_config.get(key, [])
        if commands:
            console.print(f"[yellow]Running {label} hooks...[/yellow]")
        for cmd in commands:
            cmd = self._interpolate_cmd(cmd)
            console.print(f"[cyan]Executing: {cmd}[/cyan]")
            runner.run(cmd, cwd=cwd)

    # ------------------------------------------------------------------ local side

    def _build_mode(self, local: LocalSide) -> str:
        """"build" (build, then ship), "existing" (ship the build on disk), or
        "none" (ship no build outputs; the server keeps its current ones)."""
        if self.skip_build:
            console.print("[yellow]Skipping the local build (--skip-build); shipping the existing build.[/yellow]")
            return "existing"
        risky = unstaged_frontend_changes(local.staged, frontend_source_dirs(local.app_dir, self.app_name))
        if not risky:
            return "build"
        _print_list(
            "⚠ Frontend files with changes that are NOT staged (their source won't be deployed, "
            "but the build compiles what is on disk)",
            risky,
            "bold yellow",
        )
        if self.yes:
            console.print("[yellow]Building anyway (--yes); the shipped build includes these changes.[/yellow]")
            return "build"
        if typer.confirm("Build anyway, including these unstaged changes?", default=True):
            return "build"
        console.print("[yellow]Not building: the server keeps its current build outputs.[/yellow]")
        return "none"

    def _prepare_local(self, server_config: dict) -> LocalSide:
        app = self.app_name
        app_dir = self._resolve_app_dir()
        bench = resolve_bench_path(app_dir)
        repo = LocalRepo.open(app_dir)
        local_runner = LocalRunner()
        self._run_hooks(local_runner, "pre_local_commands", "pre-local", server_config, str(app_dir.parent))

        build_outputs = load_build_outputs(app_dir, app)
        deployer = f"{repo.user_name()}@{socket.gethostname()}"
        local = LocalSide(
            app_dir=app_dir,
            bench=bench,
            repo=repo,
            deployer=deployer,
            staged=repo.staged_state(deployer, build_outputs),
            output_specs=[
                {"path": rel, "dir": (app_dir / rel).is_dir() or not (app_dir / rel).exists()}
                for rel in build_outputs
            ],
        )

        mode = self._build_mode(local)
        if mode == "build":
            if any(_BENCH_BUILD_RE.search(cmd) for cmd in server_config.get("pre_local_commands", [])):
                console.print(
                    "[yellow]Warning: a pre-local hook runs 'bench build', and BenchOps builds the app "
                    "itself; remove the hook to avoid building twice.[/yellow]"
                )
            console.print(f"[yellow]Building '{app}' locally...[/yellow]")
            run_build(local_runner, app_dir, bench, app)
        if mode != "none":
            local.outputs = existing_outputs(app_dir, build_outputs)
            check_html_asset_references(app_dir, app, local.outputs)
            local.digest = outputs_digest(app_dir, local.outputs)
            local.manifests = collect_app_manifests(bench, app)

        local.warnings = [
            f"{rel} is a build output but is tracked in git; it is shipped from the build, not from git. "
            f"Untrack it: git rm -r --cached {rel} (and add it to .gitignore)."
            for rel in build_outputs
            if repo.is_tracked(rel)
        ]
        return local

    # ------------------------------------------------------------------ plan

    def _render_plan(self, local: LocalSide, state: dict, preview: dict, warnings: list[str], shipped: list[str]) -> None:
        staged, repo = local.staged, local.repo
        console.rule(f"Deploy plan: {self.app_name} → {self.server_alias}")
        console.print(f"Your side:  {staged.branch or '(detached)'} @ {staged.base[:10]}")
        behind = repo.commits_between(state["head"], staged.base)
        position = f" — {behind} commit(s) behind you" if behind else (" — same commit" if behind == 0 else "")
        console.print(f"Staging:    {state.get('branch') or '(detached)'} @ {state['head'][:10]}{position}")
        console.print(f"Shipping:   {behind or 0} commit(s) + {len(staged.staged)} staged file(s)")
        _print_list("Staged on your machine", staged.staged, "cyan", _change)
        not_shipped = [p for _, p in staged.unstaged] + staged.untracked
        _print_list("Not shipped (not staged on your machine)", sorted(set(not_shipped)), "dim")
        _print_list("Files written on staging", preview["writes"], "cyan", _change)

        overlaps = preview["overlaps"]
        if overlaps:
            console.print()
            _print_list(
                "⚠ OVERWRITE — these files have DIFFERENT changes on staging and will be replaced by your version",
                overlaps,
                "bold red",
                lambda o: f"{o[0]}   ({o[1]})",
            )
            console.print("[red]    Staging's versions are backed up first (refs/benchops/overwritten/…).[/red]")
            console.print()
        _print_list(
            "Previously staged on the server, not in this deploy (kept on disk, now unstaged)",
            preview["restage"],
            "yellow",
        )
        _print_list("Left untouched on staging (uncommitted work there)", preview["untouched"], "dim")
        if preview.get("branch_conflict"):
            warnings.append(
                f"Branch '{staged.branch}' on the server points at {preview['branch_conflict'][:10]}, which is not "
                "in your history; it is backed up under refs/benchops/branch-backup/ before moving."
            )
        if shipped:
            _print_list("Build outputs (replaced on the server)", shipped, "cyan")
        else:
            console.print("Build outputs: unchanged")
        steps = required_steps(self.app_name, preview["changes"])
        if steps:
            console.print(f"Changes suggest: {', '.join(steps)}")
        for warning in warnings:
            console.print(f"[bold yellow]⚠ {warning}[/bold yellow]")
        console.rule()

    def _confirm(self, preview: dict) -> None:
        if preview["overlaps"] and not self.overwrite:
            if self.yes:
                raise DeployAborted(
                    f"{len(preview['overlaps'])} file(s) would overwrite different changes on staging. "
                    "Re-run without --yes to review them, or add --overwrite."
                )
            if not typer.confirm("Overwrite these files on staging with your version?", default=False):
                raise DeployAborted("Nothing was changed on staging.")
        elif not self.yes and not typer.confirm("Proceed with deploy?", default=False):
            raise DeployAborted("Cancelled.")

    # ------------------------------------------------------------------ run

    def execute(self) -> None:
        """Execute the full deployment pipeline."""
        server_config = self._get_server_config()
        bench_path = server_config["bench_path"]
        app = self.app_name

        try:
            local = self._prepare_local(server_config)
        except (subprocess.CalledProcessError, BenchOpsConnectionError, FileNotFoundError, GitError, BuildError) as exc:
            console.print(f"[red]Deployment failed: {exc}[/red]")
            raise typer.Exit(1)
        staged, repo = local.staged, local.repo

        with self._get_remote_runner(server_config) as remote_runner:
            agent = RemoteAgent(remote_runner, bench_path)
            token = uuid.uuid4().hex
            package_remote = posixpath.join(bench_path, "apps", app, ".git", "benchops", f"package-{token}.tar.gz")
            try:
                state = agent.call(
                    "inspect",
                    app=app,
                    break_lock=self.break_lock,
                    build_outputs=local.output_specs,
                    lock={"token": token, "owner": local.deployer, "started": time.strftime("%Y-%m-%d %H:%M:%S")},
                )
                if state.get("converted_snapshot"):
                    console.print(
                        "[cyan]Converted the server from the old snapshot layout: HEAD is back on the real "
                        "commit, and the previous deploy's uncommitted files now show in its git status.[/cyan]"
                    )
                warnings = local.warnings + assess(
                    state, repo, staged, local_frappe_version(local.bench), self.force
                )

                record = state.get("record") or {}
                ship_build = local.digest is not None and local.digest != record.get("build_digest")
                shipped = local.outputs if ship_build else []

                with tempfile.TemporaryDirectory() as tmp:
                    ref = f"refs/benchops/deploy/{self.server_alias}/{app}"
                    repo.update_ref(ref, staged.commit)
                    known = {state["head"], record.get("base"), record.get("staged_commit"), record.get("snapshot")}
                    bundle = Path(tmp) / "staged.bundle"
                    known.discard(staged.commit)  # an identical redeploy recreates the same commit
                    repo.create_bundle(str(bundle), ref, sorted(c for c in known if c and repo.has_commit(c)))
                    package = build_package(
                        Path(tmp) / "package.tar.gz",
                        {"bundle_ref": ref},
                        bundle_path=bundle,
                        app_dir=local.app_dir,
                        build_outputs=shipped,
                        manifests=local.manifests if ship_build else None,
                    )
                    console.print("[yellow]Uploading deploy package...[/yellow]")
                    remote_runner.put(str(package), package_remote)

                preview = agent.call(
                    "preview",
                    app=app,
                    token=token,
                    package=package_remote,
                    branch=staged.branch,
                    build_outputs=local.output_specs,
                )
                if preview["up_to_date"] and not shipped:
                    console.print(
                        f"[green]'{app}' on '{self.server_alias}' is already up to date "
                        f"({staged.branch or 'detached'} @ {staged.base[:10]} + your staged files).[/green]"
                    )
                    return

                self._render_plan(local, state, preview, warnings, shipped)
                self._confirm(preview)
                self._run_hooks(remote_runner, "pre_remote_commands", "pre-remote", server_config, bench_path)

                result = agent.call(
                    "apply",
                    app=app,
                    token=token,
                    fingerprint=preview["fingerprint"],
                    branch=staged.branch,
                    overlaps=[path for path, _ in preview["overlaps"]],
                    restage=preview["restage"],
                    build_outputs=local.output_specs,
                    record={
                        "app": app,
                        "branch": staged.branch,
                        "deployer": local.deployer,
                        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "staged": [path for _, path in staged.staged],
                        "build_digest": local.digest if ship_build else record.get("build_digest"),
                    },
                )
                for backup in result.get("backups", []):
                    console.print(f"[cyan]Backed up on the server as {backup}.[/cyan]")
                for warning in result.get("warnings", []):
                    console.print(f"[yellow]Warning: {warning}[/yellow]")
                console.print(
                    f"[green]Staging is now on {staged.branch or '(detached)'} @ {staged.base[:10]}; "
                    "your staged files show there as 'Changes to be committed' (verified).[/green]"
                )
                if result.get("build_outputs"):
                    self._clear_remote_cache(remote_runner, bench_path)

                self._run_hooks(remote_runner, "post_remote_commands", "post-remote", server_config, bench_path)
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

    def _clear_remote_cache(self, runner, bench_path: str) -> None:
        """New build outputs change asset hashes; clear Frappe's caches on
        every site so pages and boot info stop pointing at the old ones."""
        console.print("[yellow]Clearing caches on the server (new build outputs)...[/yellow]")
        try:
            runner.run("bench --site all clear-cache", cwd=bench_path)
        except (subprocess.CalledProcessError, UnexpectedExit, BenchOpsConnectionError) as exc:
            console.print(
                f"[yellow]Warning: 'bench --site all clear-cache' failed ({exc}); "
                "run it on the server, or the old asset paths may still be served.[/yellow]"
            )

    def _describe_agent_error(self, exc: RemoteAgentError) -> str:
        if exc.error == "not_repo":
            return (
                f"apps/{self.app_name} on the server is not a git repository. Install it with "
                "'bench get-app', or run 'git init' there and commit, before deploying."
            )
        if exc.error == "locked":
            lock = exc.data.get("lock") or {}
            return (
                f"a deploy of '{self.app_name}' is in progress by {lock.get('owner')} since {lock.get('started')}. "
                "If it is stale, re-run with --break-lock."
            )
        if exc.error == "staging_changed":
            return (
                "a file this deploy writes was changed on the server while the plan was shown "
                "(e.g. a Desk save); nothing was applied, re-run the deploy."
            )
        if exc.error == "lock_lost":
            return "the deploy lock was taken over by someone else (--break-lock); nothing was applied."
        return exc.error
