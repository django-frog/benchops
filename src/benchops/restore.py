"""Restore a Frappe site on a remote server from a backup in S3.

Pipeline:
  1. local    pick the backup in S3: the newest one of the site, or --at
  2. remote   check the target site, the database root password, curl and disk space
  3. plan     show what gets replaced and ask; replacing a site asks for its name
  4. remote   download straight from S3 through presigned URLs, verify sizes and gzip
  5. remote   safety backup of the site being replaced (or `bench new-site` with
              --new-site), `bench --site <site> restore`, then post-restore hooks
  6. remote   remove the downloads, also when a step fails

The URLs are signed with your own AWS credentials and expire after an hour,
so the server needs no AWS access and the data never passes through your
machine. They reach the server in a curl config file inside a private temp
dir, never on a command line. A database root password or admin password
is never echoed; it is passed to bench as an argument, so while bench runs
it is visible in the server's process list. Prefer `root_password` in the
bench's common_site_config.json, which needs nothing passed at all.
"""

import json
import os
import posixpath
import re
import secrets
import shlex
import tempfile
from pathlib import Path

import typer
from invoke.exceptions import UnexpectedExit
from rich.console import Console

from benchops.backups import BackupFile, BackupSet, BackupSourceError, S3Backups, human_size, select_backup, site_slug
from benchops.base import BaseCommand
from benchops.runner import BenchOpsCommandError, BenchOpsConnectionError
from benchops.security import secure_file

console = Console()

ROOT_PASSWORD_ENV = "BENCHOPS_DB_ROOT_PASSWORD"
SITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WORKDIR_TEMPLATE = ".benchops-restore.XXXXXX"
# The dump is decompressed next to itself and the archives are extracted
# into the site, so a restore needs a few times the download size.
DISK_HEADROOM = 3


class RestoreAborted(Exception):
    """The restore stopped before anything on the server was changed."""


class RestoreFailed(Exception):
    """A remote step exited non-zero; carries what is left behind."""


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


class RestoreCommand(BaseCommand):
    """Restores a site on a remote server from a backup set in S3."""

    def __init__(
        self,
        server_alias: str,
        source_name: str,
        site: str,
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

    def _read_json(self, runner, path: str) -> dict:
        raw = runner.capture(f"cat {shlex.quote(path)} 2>/dev/null || true").strip()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _inspect(self, runner, bench: str) -> dict:
        site_dir = posixpath.join(bench, "sites", self.site)
        probe = (
            f"test -d {shlex.quote(site_dir)} && echo exists=1 || echo exists=0; "
            "command -v curl >/dev/null 2>&1 && echo curl=1 || echo curl=0; "
            f"df -Pk {shlex.quote(bench)} | awk 'NR==2 {{print \"free_kb=\" $4}}'"
        )
        facts = dict(line.split("=", 1) for line in runner.capture(probe).split() if "=" in line)
        common = self._read_json(runner, posixpath.join(bench, "sites", "common_site_config.json"))
        site = self._read_json(runner, posixpath.join(site_dir, "site_config.json"))
        free_kb = facts.get("free_kb", "")
        return {
            "exists": facts.get("exists") == "1",
            "curl": facts.get("curl") == "1",
            "free_bytes": int(free_kb) * 1024 if free_kb.isdigit() else None,
            "root_password_configured": bool(root_password_from(common, site)),
        }

    def _check(self, facts: dict) -> None:
        if not facts["curl"]:
            raise RestoreAborted(f"curl is not installed on '{self.server_alias}'; it downloads the backup.")
        if self.new_site and facts["exists"]:
            raise RestoreAborted(
                f"Site '{self.site}' already exists on '{self.server_alias}'. Drop --new-site to replace it."
            )
        if not self.new_site and not facts["exists"]:
            raise RestoreAborted(
                f"Site '{self.site}' does not exist on '{self.server_alias}'. Add --new-site to create it."
            )

    def _root_password(self, facts: dict) -> str | None:
        """None when bench finds the password in its own config."""
        if facts["root_password_configured"]:
            return None
        from_env = os.environ.get(ROOT_PASSWORD_ENV)
        if from_env:
            return from_env
        if self.yes:
            raise RestoreAborted(
                f"No root_password in the bench's site config on '{self.server_alias}'. "
                f"Set {ROOT_PASSWORD_ENV}, or run without --yes to be asked for it."
            )
        return typer.prompt(f"MariaDB root password on '{self.server_alias}'", hide_input=True)

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

    def _render_plan(self, backup: BackupSet, source: dict, server_config: dict, warnings: list[str]) -> None:
        location = f"s3://{source['bucket']}/{source.get('prefix') or ''}".rstrip("/")
        console.rule("Restore plan")
        console.print(f"Backup:  {backup.taken_at:%Y-%m-%d %H:%M:%S} of {backup.site_slug}  [dim]({location})[/dim]")
        for part in backup.files(self.with_files):
            console.print(f"         {part.name}  [dim]{human_size(part.size)}[/dim]")
        if not self.with_files:
            console.print("         [dim]database only (--no-files)[/dim]")
        console.print(f"Target:  {self.site} on '{self.server_alias}'  [dim]({server_config['bench_path']})[/dim]")
        if self.new_site:
            console.print("[bold green]Creates a new site[/bold green] from this backup.")
        else:
            replaced = "database and files" if self.with_files else "database"
            console.print(f"[bold red]REPLACES the {replaced} of {self.site}.[/bold red]")
            console.print(
                "Safety backup first: " + ("yes, into the site's private/backups" if self.safety_backup else
                                           "[bold yellow]no (--skip-safety-backup)[/bold yellow]")
            )
        hooks = server_config.get("post_restore_commands") or []
        if hooks:
            console.print(f"Then:    {len(hooks)} post-restore hook(s)")
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

    def _step(self, runner, label: str, command: str, cwd: str) -> None:
        """Run a streamed remote step without ever printing its command, which
        may carry passwords (invoke's UnexpectedExit message includes it)."""
        console.print(f"[cyan]{label}...[/cyan]")
        try:
            runner.run(command, cwd=cwd)
        except UnexpectedExit as exc:
            raise RestoreFailed(f"{label} failed (exit code {exc.result.exited}).") from None

    def _download(self, runner, s3: S3Backups, files: list[BackupFile], workdir: str) -> dict[str, str]:
        remote = {f.key: posixpath.join(workdir, f.name) for f in files}
        config = build_curl_config([(s3.presign(f.key), remote[f.key]) for f in files])
        remote_config = posixpath.join(workdir, "curl.conf")
        with tempfile.TemporaryDirectory() as tmp:
            local_config = Path(tmp) / "curl.conf"
            local_config.write_text(config)
            secure_file(local_config, 0o600)
            runner.put(str(local_config), remote_config)

        quoted = shlex.quote(remote_config)
        total = human_size(sum(f.size for f in files))
        self._step(
            runner,
            f"Downloading {len(files)} file(s), {total}, from S3",
            f"curl --fail --location --retry 3 --progress-bar --config {quoted}; "
            f"status=$?; rm -f {quoted}; exit $status",
            cwd=workdir,
        )

        sizes = runner.capture("stat -c %s " + _join(list(remote.values()))).split()
        if len(sizes) != len(files):
            raise RestoreFailed(f"Expected {len(files)} downloaded file(s), found {len(sizes)}. Nothing was restored.")
        for f, size in zip(files, sizes, strict=True):
            if int(size) != f.size:
                raise RestoreFailed(f"{f.name} is incomplete ({size} of {f.size} bytes). Nothing was restored.")
        for f in files:
            if f.name.endswith(".gz"):
                self._step(runner, f"Checking {f.name}", f"gzip -t {shlex.quote(remote[f.key])}", cwd=workdir)
        return remote

    def _restore(self, runner, bench: str, s3: S3Backups, backup: BackupSet, root_password: str | None) -> None:
        workdir = runner.capture(f"mktemp -d {shlex.quote(posixpath.join(bench, WORKDIR_TEMPLATE))}").strip()
        # It is removed with rm -rf below, so it must be the dir mktemp just made.
        if posixpath.dirname(workdir) != bench.rstrip("/") or not posixpath.basename(workdir).startswith(
            WORKDIR_TEMPLATE.split("X", 1)[0]
        ):
            raise RestoreFailed(f"Unexpected temp dir from mktemp: '{workdir}'. Nothing was changed.")
        try:
            remote = self._download(runner, s3, backup.files(self.with_files), workdir)

            def path(part: BackupFile | None) -> str | None:
                return remote[part.key] if part is not None and self.with_files else None

            if self.safety_backup:
                with_files = " --with-files" if self.with_files else ""
                self._step(
                    runner, f"Taking a safety backup of {self.site}",
                    f"bench --site {shlex.quote(self.site)} backup{with_files}", cwd=bench,
                )
            if self.new_site:
                # The restored database brings its own Administrator password
                # unless --admin-password is given; this one only lives until then.
                admin_password = self.admin_password or secrets.token_urlsafe(24)
                self._step(
                    runner, f"Creating site {self.site}",
                    build_new_site_command(self.site, admin_password, root_password), cwd=bench,
                )
            try:
                self._step(
                    runner, f"Restoring {self.site}",
                    build_restore_command(
                        self.site,
                        remote[backup.database.key],
                        path(backup.public_files),
                        path(backup.private_files),
                        root_password,
                        self.admin_password,
                    ),
                    cwd=bench,
                )
            except RestoreFailed as exc:
                if self.new_site:
                    hint = f" The new site may be half-created; remove it with 'bench drop-site {self.site}'."
                elif self.safety_backup:
                    hint = f" The safety backup is in sites/{self.site}/private/backups on the server."
                else:
                    hint = ""
                raise RestoreFailed(f"{exc}{hint}") from None

            hooks = self.config.get_hooks(self.server_alias, "post-restore")
            if hooks:
                console.print("[yellow]Running post-restore hooks...[/yellow]")
            for cmd in hooks:
                cmd = self._interpolate_cmd(cmd)
                try:
                    self._step(runner, f"Executing: {cmd}", cmd, cwd=bench)
                except RestoreFailed as exc:
                    raise RestoreFailed(
                        f"{self.site} was restored, but a post-restore hook failed: {exc}"
                    ) from None
        finally:
            try:
                runner.capture(f"rm -rf {shlex.quote(workdir)}")
            except (BenchOpsConnectionError, BenchOpsCommandError):
                console.print(f"[yellow]Could not remove {workdir} on the server; delete it by hand.[/yellow]")

    def execute(self) -> None:
        source = self._validate()
        server_config = self._get_server_config()
        bench = server_config["bench_path"]

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

        try:
            with self._get_remote_runner(server_config) as runner:
                facts = self._inspect(runner, bench)
                self._check(facts)
                self._render_plan(backup, source, server_config, self._warnings(backup, facts))
                root_password = self._root_password(facts)
                self._confirm()
                self._restore(runner, bench, s3, backup, root_password)
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
            console.print(f"[red]Restore failed on '{self.server_alias}'{detail}[/red]")
            raise typer.Exit(1)

        console.print(f"[green]Restored {self.site} on '{self.server_alias}' from the {backup.stamp} backup.[/green]")
        if self.new_site and not self.admin_password:
            console.print("[dim]Log in as Administrator with the password from the backup.[/dim]")
