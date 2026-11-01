"""Restore a Frappe site from a backup in S3, on a remote server or on your
local bench.

Pipeline:
  1. local    pick the backup in S3: the newest one of the site, or --at
  2. target   check the bench, the site, the database root password and disk space
  3. plan     show what gets replaced and ask; replacing a site asks for its name
  4. target   download the backup, verify sizes and gzip
  5. target   safety backup of the site being replaced (or `bench new-site` with
              --new-site), `bench --site <site> restore`, then post-restore hooks
  6. target   remove the downloads, also when a step fails

The target is a RemoteTarget (--server) or a LocalTarget (--local). A remote
server downloads straight from S3 through presigned URLs signed with your
own AWS credentials, which expire after an hour: the server needs no AWS
access and the data never passes through your machine. The URLs reach the
server in a curl config file inside a private temp dir, never on a command
line. A local restore downloads with your AWS credentials directly.

A database root password or admin password is never echoed; it is passed to
bench as an argument, so while bench runs it is visible in the process list.
Prefer `root_password` in the bench's common_site_config.json, which needs
nothing passed at all.
"""

import gzip
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn

from benchops.backups import BackupFile, BackupSet, BackupSourceError, S3Backups, human_size, select_backup, site_slug
from benchops.base import BaseCommand
from benchops.runner import BenchOpsCommandError, BenchOpsConnectionError, LocalRunner
from benchops.security import secure_file

console = Console()

ROOT_PASSWORD_ENV = "BENCHOPS_DB_ROOT_PASSWORD"
SITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WORKDIR_PREFIX = ".benchops-restore."
WORKDIR_TEMPLATE = WORKDIR_PREFIX + "XXXXXX"
# The dump is decompressed next to itself and the archives are extracted
# into the site, so a restore needs a few times the download size.
DISK_HEADROOM = 3
GZIP_CHUNK = 1024 * 1024


class RestoreAborted(Exception):
    """The restore stopped before anything on the target was changed."""


class RestoreFailed(Exception):
    """A step exited non-zero; carries what is left behind."""


def build_curl_config(downloads: list[tuple[str, str]]) -> str:
    """A curl --config file fetching each (url, output path)."""

    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return "".join(f"url = {quote(url)}\noutput = {quote(path)}\n" for url, path in downloads)


def _join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def build_new_site_command(site: str, admin_password: str, root_password: str | None) -> str:
    parts = ["bench", "new-site", site, "--admin-password", admin_password]
    if root_password:
        parts += ["--mariadb-root-password", root_password]
    return _join(parts)


def build_restore_command(
    site: str,
    database: str,
    public_files: str | None = None,
    private_files: str | None = None,
    root_password: str | None = None,
    admin_password: str | None = None,
) -> str:
    """`bench restore`; the --mariadb-* names work on Frappe v13 to v15."""
    parts = ["bench", "--site", site, "restore", database]
    if public_files:
        parts += ["--with-public-files", public_files]
    if private_files:
        parts += ["--with-private-files", private_files]
    if root_password:
        parts += ["--mariadb-root-password", root_password]
    if admin_password:
        parts += ["--admin-password", admin_password]
    return _join(parts)


def root_password_from(common_config: dict, site_config: dict) -> str | None:
    """The root password bench itself would use, site config first."""
    return site_config.get("root_password") or common_config.get("root_password")


def _parse_config(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _check_sizes(files: list[BackupFile], sizes: list[int]) -> None:
    if len(sizes) != len(files):
        raise RestoreFailed(f"Expected {len(files)} downloaded file(s), found {len(sizes)}. Nothing was restored.")
    for f, size in zip(files, sizes, strict=True):
        if size != f.size:
            raise RestoreFailed(f"{f.name} is incomplete ({size} of {f.size} bytes). Nothing was restored.")


# --------------------------------------------------------------------- targets
# A target is where the restore happens. Each one reports facts about its
# bench, gets the backup files into a private work dir, and runs bench steps.
# Facts: exists, problems (reasons the restore can't start), free_bytes,
# root_password_configured.


class RemoteTarget:
    """A configured server, reached through a Runner."""

    def __init__(self, runner, alias: str, bench: str, hooks: list[str]) -> None:
        self.runner = runner
        self.label = f"'{alias}'"
        self.bench = bench
        self.hooks = hooks

    def _read_config(self, path: str) -> dict:
        return _parse_config(self.runner.capture(f"cat {shlex.quote(path)} 2>/dev/null || true"))

    def inspect(self, site: str) -> dict:
        site_dir = posixpath.join(self.bench, "sites", site)
        probe = (
            f"test -d {shlex.quote(site_dir)} && echo exists=1 || echo exists=0; "
            "command -v curl >/dev/null 2>&1 && echo curl=1 || echo curl=0; "
            f"df -Pk {shlex.quote(self.bench)} | awk 'NR==2 {{print \"free_kb=\" $4}}'"
        )
        facts = dict(line.split("=", 1) for line in self.runner.capture(probe).split() if "=" in line)
        common = self._read_config(posixpath.join(self.bench, "sites", "common_site_config.json"))
        site_config = self._read_config(posixpath.join(site_dir, "site_config.json"))
        free_kb = facts.get("free_kb", "")
        problems = [] if facts.get("curl") == "1" else [f"curl is not installed on {self.label}; it downloads the backup."]
        return {
            "exists": facts.get("exists") == "1",
            "problems": problems,
            "free_bytes": int(free_kb) * 1024 if free_kb.isdigit() else None,
            "root_password_configured": bool(root_password_from(common, site_config)),
        }

    def make_workdir(self) -> str:
        workdir = self.runner.capture(f"mktemp -d {shlex.quote(posixpath.join(self.bench, WORKDIR_TEMPLATE))}").strip()
        # It is removed with rm -rf, so it must be the dir mktemp just made.
        if posixpath.dirname(workdir) != self.bench.rstrip("/") or not posixpath.basename(workdir).startswith(
            WORKDIR_PREFIX
        ):
            raise RestoreFailed(f"Unexpected temp dir from mktemp: '{workdir}'. Nothing was changed.")
        return workdir

    def step(self, label: str, command: str, cwd: str | None = None) -> None:
        """Run a streamed step without ever printing its command, which may
        carry passwords."""
        console.print(f"[cyan]{label}...[/cyan]")
        try:
            self.runner.run(command, cwd=cwd or self.bench)
        except BenchOpsCommandError as exc:
            raise RestoreFailed(f"{label} failed (exit code {exc.exit_code}).") from None

    def download(self, s3: S3Backups, files: list[BackupFile], workdir: str) -> dict[str, str]:
        remote = {f.key: posixpath.join(workdir, f.name) for f in files}
        config = build_curl_config([(s3.presign(f.key), remote[f.key]) for f in files])
        remote_config = posixpath.join(workdir, "curl.conf")
        with tempfile.TemporaryDirectory() as tmp:
            local_config = Path(tmp) / "curl.conf"
            local_config.write_text(config)
            secure_file(local_config, 0o600)
            self.runner.put(str(local_config), remote_config)

        quoted = shlex.quote(remote_config)
        total = human_size(sum(f.size for f in files))
        self.step(
            f"Downloading {len(files)} file(s), {total}, from S3",
            f"curl --fail --location --retry 3 --progress-bar --config {quoted}; "
            f"status=$?; rm -f {quoted}; exit $status",
            cwd=workdir,
        )

        sizes = self.runner.capture("stat -c %s " + _join(list(remote.values()))).split()
        _check_sizes(files, [int(size) for size in sizes])
        for f in files:
            if f.name.endswith(".gz"):
                self.step(f"Checking {f.name}", f"gzip -t {shlex.quote(remote[f.key])}", cwd=workdir)
        return remote

    def cleanup(self, workdir: str) -> None:
        try:
            self.runner.capture(f"rm -rf {shlex.quote(workdir)}")
        except (BenchOpsConnectionError, BenchOpsCommandError):
            console.print(f"[yellow]Could not remove {workdir} on {self.label}; delete it by hand.[/yellow]")


class LocalTarget:
    """The bench on this machine."""

    label = "your local bench"

    def __init__(self, bench: Path, hooks: list[str]) -> None:
        self.bench = str(bench)
        self.path = bench
        self.hooks = hooks
        self.runner = LocalRunner()

    def _read_config(self, path: Path) -> dict:
        try:
            return _parse_config(path.read_text())
        except OSError:
            return {}

    def inspect(self, site: str) -> dict:
        problems = []
        if not (self.path / "sites").is_dir() or not (self.path / "apps" / "frappe").is_dir():
            problems.append(f"{self.bench} is not a Frappe bench. Run from your bench root, or pass --bench.")
        if shutil.which("bench") is None:
            problems.append("The 'bench' command is not on PATH.")
        sites = self.path / "sites"
        try:
            free_bytes = shutil.disk_usage(self.path).free
        except OSError:
            free_bytes = None
        return {
            "exists": (sites / site).is_dir(),
            "problems": problems,
            "free_bytes": free_bytes,
            "root_password_configured": bool(root_password_from(
                self._read_config(sites / "common_site_config.json"),
                self._read_config(sites / site / "site_config.json"),
            )),
        }

    def make_workdir(self) -> str:
        return tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=self.bench)

    def step(self, label: str, command: str, cwd: str | None = None) -> None:
        """Like RemoteTarget.step: the errors LocalRunner raises name the
        command, so only the exit code is reported."""
        console.print(f"[cyan]{label}...[/cyan]")
        try:
            self.runner.run(command, cwd=cwd or self.bench)
        except subprocess.CalledProcessError as exc:
            raise RestoreFailed(f"{label} failed (exit code {exc.returncode}).") from None
        except BenchOpsConnectionError:
            raise RestoreFailed(f"{label} failed: the command could not be started.") from None

    def download(self, s3: S3Backups, files: list[BackupFile], workdir: str) -> dict[str, str]:
        local = {f.key: os.path.join(workdir, f.name) for f in files}
        columns = (TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn())
        with Progress(*columns, console=console) as progress:
            for f in files:
                task = progress.add_task(f.name, total=f.size)
                s3.download(f.key, local[f.key], lambda n, task=task: progress.advance(task, n))

        _check_sizes(files, [os.path.getsize(local[f.key]) for f in files if os.path.exists(local[f.key])])
        for f in files:
            if f.name.endswith(".gz"):
                console.print(f"[cyan]Checking {f.name}...[/cyan]")
                try:
                    with gzip.open(local[f.key], "rb") as stream:
                        while stream.read(GZIP_CHUNK):
                            pass
                except (OSError, EOFError) as exc:
                    raise RestoreFailed(f"{f.name} is corrupt ({exc}). Nothing was restored.") from None
        return local

    def cleanup(self, workdir: str) -> None:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------- command


class RestoreCommand(BaseCommand):
    """Restores a site from a backup set in S3, on a server or locally."""

    def __init__(
        self,
        source_name: str,
        site: str,
        server_alias: str | None = None,
        bench: Path | None = None,
        from_site: str | None = None,
        at: str | None = None,
        new_site: bool = False,
        with_files: bool = True,
        admin_password: str | None = None,
        safety_backup: bool = True,
        yes: bool = False,
    ) -> None:
        super().__init__(server_alias=server_alias, site=site.strip())
        self.source_name = source_name
        self.local = server_alias is None
        self.bench = (bench or Path.cwd()).expanduser().resolve()
        self.from_site = (from_site or site).strip()
        self.at = at
        self.new_site = new_site
        self.with_files = with_files
        self.admin_password = admin_password
        self.safety_backup = safety_backup and not new_site
        self.yes = yes

    # ------------------------------------------------------------------ checks

    def _validate(self) -> dict:
        for label, name in (("--site", self.site), ("--from-site", self.from_site)):
            if not SITE_NAME_RE.match(name):
                console.print(f"[red]Error: {label} '{name}' is not a valid site name.[/red]")
                raise typer.Exit(1)
        source = self.config.get_backup_source(self.source_name)
        if source is None:
            console.print(
                f"[red]Error: Backup source '{self.source_name}' not found. "
                "Add it with 'benchops backup source add'.[/red]"
            )
            raise typer.Exit(1)
        return source

    @contextmanager
    def _target(self, server_config: dict | None):
        if server_config is None:
            yield LocalTarget(self.bench, self.config.get_local_hooks("post-restore"))
            return
        with self._get_remote_runner(server_config) as runner:
            yield RemoteTarget(
                runner, self.server_alias, server_config["bench_path"],
                list(server_config.get("post_restore_commands") or []),
            )

    def _check(self, target, facts: dict) -> None:
        if facts["problems"]:
            raise RestoreAborted(" ".join(facts["problems"]))
        if self.new_site and facts["exists"]:
            raise RestoreAborted(
                f"Site '{self.site}' already exists on {target.label}. Drop --new-site to replace it."
            )
        if not self.new_site and not facts["exists"]:
            raise RestoreAborted(
                f"Site '{self.site}' does not exist on {target.label}. Add --new-site to create it."
            )

    def _root_password(self, target, facts: dict) -> str | None:
        """None when bench finds the password in its own config."""
        if facts["root_password_configured"]:
            return None
        from_env = os.environ.get(ROOT_PASSWORD_ENV)
        if from_env:
            return from_env
        if self.yes:
            raise RestoreAborted(
                f"No root_password in the bench's site config on {target.label}. "
                f"Set {ROOT_PASSWORD_ENV}, or run without --yes to be asked for it."
            )
        return typer.prompt(f"MariaDB root password on {target.label}", hide_input=True)

    # ------------------------------------------------------------------ plan

    def _warnings(self, backup: BackupSet, facts: dict) -> list[str]:
        warnings = []
        if self.with_files:
            for part, label in ((backup.public_files, "public"), (backup.private_files, "private")):
                if part is None:
                    warnings.append(f"This backup has no {label} files archive; the site keeps its current ones.")
        needed = sum(f.size for f in backup.files(self.with_files)) * DISK_HEADROOM
        if facts["free_bytes"] is not None and facts["free_bytes"] < needed:
            warnings.append(
                f"Only {human_size(facts['free_bytes'])} free on the bench's disk; "
                f"this restore may need about {human_size(needed)}."
            )
        return warnings

    def _render_plan(self, target, backup: BackupSet, source: dict, warnings: list[str]) -> None:
        location = f"s3://{source['bucket']}/{source.get('prefix') or ''}".rstrip("/")
        console.rule("Restore plan")
        console.print(f"Backup:  {backup.taken_at:%Y-%m-%d %H:%M:%S} of {backup.site_slug}  [dim]({location})[/dim]")
        for part in backup.files(self.with_files):
            console.print(f"         {part.name}  [dim]{human_size(part.size)}[/dim]")
        if not self.with_files:
            console.print("         [dim]database only (--no-files)[/dim]")
        console.print(f"Target:  {self.site} on {target.label}  [dim]({target.bench})[/dim]")
        if self.new_site:
            console.print("[bold green]Creates a new site[/bold green] from this backup.")
        else:
            replaced = "database and files" if self.with_files else "database"
            console.print(f"[bold red]REPLACES the {replaced} of {self.site}.[/bold red]")
            console.print(
                "Safety backup first: " + ("yes, into the site's private/backups" if self.safety_backup else
                                           "[bold yellow]no (--skip-safety-backup)[/bold yellow]")
            )
        if target.hooks:
            console.print(f"Then:    {len(target.hooks)} post-restore hook(s)")
        for warning in warnings:
            console.print(f"[bold yellow]⚠ {warning}[/bold yellow]")
        console.rule()

    def _confirm(self) -> None:
        if self.yes:
            return
        if self.new_site:
            if not typer.confirm(f"Create '{self.site}' from this backup?", default=False):
                raise RestoreAborted("Cancelled.")
            return
        typed = typer.prompt(f"Type the site name ({self.site}) to replace it", default="", show_default=False)
        if typed.strip() != self.site:
            raise RestoreAborted("The site name didn't match; nothing was changed.")

    # ------------------------------------------------------------------ run

    def _restore(self, target, s3: S3Backups, backup: BackupSet, root_password: str | None) -> None:
        workdir = target.make_workdir()
        try:
            paths = target.download(s3, backup.files(self.with_files), workdir)

            def path(part: BackupFile | None) -> str | None:
                return paths[part.key] if part is not None and self.with_files else None

            if self.safety_backup:
                with_files = " --with-files" if self.with_files else ""
                target.step(
                    f"Taking a safety backup of {self.site}",
                    f"bench --site {shlex.quote(self.site)} backup{with_files}",
                )
            if self.new_site:
                # The restored database brings its own Administrator password
                # unless --admin-password is given; this one only lives until then.
                admin_password = self.admin_password or secrets.token_urlsafe(24)
                target.step(f"Creating site {self.site}", build_new_site_command(self.site, admin_password, root_password))
            try:
                target.step(
                    f"Restoring {self.site}",
                    build_restore_command(
                        self.site,
                        paths[backup.database.key],
                        path(backup.public_files),
                        path(backup.private_files),
                        root_password,
                        self.admin_password,
                    ),
                )
            except RestoreFailed as exc:
                if self.new_site:
                    hint = f" The new site may be half-created; remove it with 'bench drop-site {self.site}'."
                elif self.safety_backup:
                    hint = f" The safety backup is in {target.bench}/sites/{self.site}/private/backups."
                else:
                    hint = ""
                raise RestoreFailed(f"{exc}{hint}") from None

            if target.hooks:
                console.print("[yellow]Running post-restore hooks...[/yellow]")
            for cmd in target.hooks:
                cmd = self._interpolate_cmd(cmd)
                try:
                    target.step(f"Executing: {cmd}", cmd)
                except RestoreFailed as exc:
                    raise RestoreFailed(
                        f"{self.site} was restored, but a post-restore hook failed: {exc}"
                    ) from None
        finally:
            target.cleanup(workdir)

    def execute(self) -> None:
        source = self._validate()
        server_config = None if self.local else self._get_server_config()

        try:
            s3 = S3Backups(source)
            backup = select_backup(s3.list_sets(self.from_site), self.at)
        except BackupSourceError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            if "No restorable backup" in str(exc):
                console.print(
                    f"[dim]Searched for backups of '{site_slug(self.from_site)}'. See what exists with "
                    f"'benchops backup list {self.source_name} --site {self.from_site}' (or without --site).[/dim]"
                )
            raise typer.Exit(1)

        where = "your local bench" if self.local else f"'{self.server_alias}'"
        try:
            with self._target(server_config) as target:
                facts = target.inspect(self.site)
                self._check(target, facts)
                self._render_plan(target, backup, source, self._warnings(backup, facts))
                root_password = self._root_password(target, facts)
                self._confirm()
                self._restore(target, s3, backup, root_password)
        except RestoreAborted as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(1)
        except RestoreFailed as exc:
            console.print(f"[red]Restore failed: {exc}[/red]")
            raise typer.Exit(1)
        except BackupSourceError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1)
        except (BenchOpsConnectionError, BenchOpsCommandError) as exc:
            detail = f": {(exc.stderr or exc.stdout).strip()}" if isinstance(exc, BenchOpsCommandError) else f": {exc}"
            console.print(f"[red]Restore failed on {where}{detail}[/red]")
            raise typer.Exit(1)

        console.print(f"[green]Restored {self.site} on {where} from the {backup.stamp} backup.[/green]")
        if self.new_site and not self.admin_password:
            console.print("[dim]Log in as Administrator with the password from the backup.[/dim]")
