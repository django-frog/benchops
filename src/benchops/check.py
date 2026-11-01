"""`benchops server check`: walk the whole path to a server one step at a
time and say exactly which step fails and how to fix it — instead of a
deploy failing somewhere in the middle with a transport error."""

import shlex
import subprocess
import time

import boto3
import botocore.exceptions
import typer
from rich.console import Console
from rich.markup import escape

from benchops.auth import KeyringUnavailableError
from benchops.base import BaseCommand
from benchops.runner import BenchOpsCommandError, BenchOpsConnectionError, RemoteRunner
from benchops.ssm_shell import SsmShellRunner
from benchops.ssm import PLUGIN, SsmError, SsmTunnel, explain_client_error, list_managed_instances, require_plugin, session_for

console = Console()


class CheckFailed(Exception):
    """A check step failed; the message is shown as its fix."""


class CheckSkipped(Exception):
    """A check step could not run, but later steps can still decide."""


class CheckCommand(BaseCommand):
    """Diagnoses the connection to a configured server, step by step."""

    def __init__(self, server_alias: str) -> None:
        super().__init__(server_alias=server_alias)
        self.failed = False

    def _step(self, label: str, check) -> object:
        """Run one check, print ✓/✗ with its detail; return its result or None."""
        started = time.monotonic()
        try:
            detail, result = check()
        except CheckFailed as exc:
            console.print(f"[red]✗[/red] {label}")
            console.print(f"    [red]{escape(str(exc))}[/red]")
            self.failed = True
            return None
        except CheckSkipped as exc:
            console.print(f"[yellow]![/yellow] {label} [dim]— skipped: {escape(str(exc))}[/dim]")
            return True
        elapsed = time.monotonic() - started
        timing = f" [dim]({elapsed:.1f}s)[/dim]" if elapsed >= 0.5 else ""
        console.print(f"[green]✓[/green] {label}" + (f" [dim]— {escape(detail)}[/dim]" if detail else "") + timing)
        return result if result is not None else True

    def execute(self) -> None:
        server_config = self._get_server_config()
        connection_type = server_config.get("connection_type", "ssh")
        console.print(f"[bold]Checking '{self.server_alias}'[/bold] [dim]({connection_type})[/dim]")

        if connection_type == "ssh":
            self._check_ssh(server_config, None)
        else:
            session = self._check_aws(server_config)
            if session is None:
                raise typer.Exit(1)
            if connection_type == "ssm":
                self._check_shell(server_config, session)
            else:
                tunnel = self._check_tunnel(server_config, session)
                if tunnel is None:
                    raise typer.Exit(1)
                try:
                    self._check_ssh(server_config, tunnel)
                finally:
                    tunnel.close()

        if self.failed:
            raise typer.Exit(1)
        console.print(f"[green]'{self.server_alias}' is ready.[/green]")

    def _check_aws(self, server_config: dict) -> boto3.Session | None:
        """Plugin, credentials and registration: what every SSM transport needs."""
        instance_id = server_config.get("instance_id")
        if not instance_id:
            console.print("[red]✗ No instance_id configured. Re-run 'benchops server add' to pick the instance.[/red]")
            return None

        def plugin():
            try:
                path = require_plugin()
            except SsmError as exc:
                raise CheckFailed(str(exc)) from exc
            try:
                version = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                version = ""
            return (f"{PLUGIN} {version}".strip(), None)

        if not self._step("Session Manager plugin installed", plugin):
            return None

        def credentials():
            try:
                session = session_for(server_config)
                identity = session.client("sts").get_caller_identity()
            except SsmError as exc:
                raise CheckFailed(str(exc)) from exc
            except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as exc:
                raise CheckFailed(explain_client_error(exc, instance_id, "sts:GetCallerIdentity")) from exc
            region = session.region_name or "default region"
            return (f"{identity['Arn']} in {region}", session)

        session = self._step("AWS credentials", credentials)
        if not session:
            return None

        def registered():
            try:
                found = list_managed_instances(session, [instance_id])
            except SsmError as exc:
                # Read access to the inventory is a nice-to-have; the tunnel
                # step below is the one that decides.
                raise CheckSkipped(str(exc)) from exc
            if not found:
                raise CheckFailed(
                    f"'{instance_id}' is not managed by SSM in {session.region_name}. Check instance_id and "
                    "aws_region, and that the instance has the SSM agent and an AmazonSSMManagedInstanceCore role."
                )
            instance = found[0]
            if instance.ping_status != "Online":
                raise CheckFailed(
                    f"SSM agent on '{instance_id}' is {instance.ping_status}: the instance is stopped or its agent "
                    "cannot reach SSM. Check Systems Manager → Fleet Manager."
                )
            return (f"{instance.name or instance_id}, {instance.platform}, agent Online", None)

        if not self._step("Instance registered with SSM", registered):
            return None
        return session

    def _check_shell(self, server_config: dict, session: boto3.Session) -> None:
        instance_id = server_config["instance_id"]
        user = server_config["user"]

        def shell():
            try:
                runner = SsmShellRunner(session, instance_id, user).open()
            except SsmError as exc:
                raise CheckFailed(str(exc)) from exc
            except BenchOpsConnectionError as exc:
                raise CheckFailed(str(exc)) from exc
            detail = f"{runner.session_user}@{runner.hostname}"
            if runner.session_user != user:
                detail += f", commands run as '{user}' via sudo"
            return (detail, runner)

        runner = self._step("Shell session (ssm:StartSession)", shell)
        if not runner:
            return
        try:
            self._step("Bench directory", lambda: self._bench(runner, server_config))
        finally:
            runner.close()

    def _bench(self, runner, server_config: dict):
        bench_path = server_config["bench_path"]
        quoted = shlex.quote(bench_path)
        try:
            runner.capture(f"test -d {quoted}/apps && test -d {quoted}/sites && test -x {quoted}/env/bin/python")
        except BenchOpsCommandError as exc:
            raise CheckFailed(
                f"'{bench_path}' is not a bench (needs apps/, sites/ and env/) or '{server_config['user']}' cannot read it."
            ) from exc
        except BenchOpsConnectionError as exc:
            raise CheckFailed(str(exc)) from exc
        return (bench_path, None)

    def _check_tunnel(self, server_config: dict, session: boto3.Session) -> SsmTunnel | None:
        instance_id = server_config["instance_id"]

        def tunnel():
            try:
                opened = SsmTunnel(session, instance_id, int(server_config.get("port", 22))).open()
            except SsmError as exc:
                raise CheckFailed(str(exc)) from exc
            return (f"127.0.0.1:{opened.local_port} → {instance_id}:{opened.remote_port}", opened)

        opened = self._step("Port-forwarding session (ssm:StartSession)", tunnel)
        return opened or None

    def _check_ssh(self, server_config: dict, tunnel: SsmTunnel | None) -> None:
        try:
            password = self.auth.get_password(self.server_alias)
        except KeyringUnavailableError as exc:
            password = None
            console.print(f"[yellow]![/yellow] Keyring unavailable, skipping stored password: {escape(str(exc))}")
        key_path = server_config.get("private_key_path")
        if not password and not key_path:
            console.print(
                "[red]✗[/red] SSH login\n    [red]No credentials configured. Run "
                f"'benchops auth setup-keys {self.server_alias}' (ssm-ssh) or 'benchops server set-auth "
                f"{self.server_alias}'.[/red]"
            )
            self.failed = True
            return

        if tunnel is not None:
            runner = RemoteRunner("127.0.0.1", tunnel.local_port, server_config["user"], password, key_path)
        else:
            runner = RemoteRunner(server_config["host"], int(server_config["port"]), server_config["user"], password, key_path)

        user = server_config["user"]

        def login():
            try:
                hostname = runner.capture("hostname").strip()
            except (BenchOpsConnectionError, BenchOpsCommandError) as exc:
                hint = (
                    f"Run 'benchops auth setup-keys {self.server_alias}' to install the BenchOps key for '{user}'."
                    if tunnel is not None
                    else f"Check the host/port and run 'benchops server set-auth {self.server_alias}'."
                )
                raise CheckFailed(f"{exc}\n    {hint}") from exc
            return (f"{user}@{hostname} with {'key ' + key_path if key_path else 'password'}", None)

        try:
            if self._step("SSH login", login):
                self._step("Bench directory", lambda: self._bench(runner, server_config))
        finally:
            runner.connection.close()
