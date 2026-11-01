"""Base command logic for shared configuration and authentication."""

import typer
from rich.console import Console
from rich.markup import escape

from benchops.auth import AuthManager, KeyringUnavailableError
from benchops.config import ConfigManager
from benchops.config import VALID_CONNECTION_TYPES
from benchops.runner import BenchOpsConnectionError, RemoteRunner, Runner
from benchops.ssm_shell import SsmShellRunner

console = Console()


class BaseCommand:
    """Base class providing shared logic for CLI commands."""

    def __init__(self, server_alias: str, app_name: str | None = None, site: str | None = None) -> None:
        self.server_alias = server_alias
        self.app_name = app_name
        self.site = site
        self.config = ConfigManager()
        self.auth = AuthManager()
        # What --json prints; commands that support it fill in more.
        self.report: dict = {}

    def _error(self, message: str, hint: str | None = None) -> None:
        """Print an error, record it in the report and exit with status 1.
        `message` is plain text; `hint` is printed dimmed and not recorded."""
        console.print(f"[red]Error: {escape(message)}[/red]" + (f"\n[dim]{hint}[/dim]" if hint else ""))
        self.report.update(ok=False, message=f"Error: {message}")
        raise typer.Exit(1)

    def _interpolate_cmd(self, cmd: str) -> str:
        """Replace {site} and {app} placeholders in the command."""
        if "{site}" in cmd:
            if not self.site:
                self._error(f"Command '{cmd}' requires a --site argument, but none was provided.")
            cmd = cmd.replace("{site}", self.site)

        if "{app}" in cmd:
            if not self.app_name:
                self._error(f"Command '{cmd}' requires an app name, but none was provided.")
            cmd = cmd.replace("{app}", self.app_name)

        return cmd

    def _get_server_config(self) -> dict:
        """Retrieve and validate the server configuration."""
        server_config = self.config.get_server(self.server_alias)
        if server_config is None:
            self._error(f"Server '{self.server_alias}' not found in configuration.")
        return server_config

    def _get_remote_runner(self, server_config: dict) -> Runner:
        """Instantiate an authenticated runner for the server's connection_type.

        Returns the abstract Runner interface rather than a concrete class so
        that call sites never need to know whether traffic is going over SSH,
        an SSM shell session, or SSH tunneled through SSM.
        """
        connection_type = server_config.get("connection_type", "ssh")
        if connection_type not in VALID_CONNECTION_TYPES:
            self._error(f"Unknown connection_type '{connection_type}' for '{self.server_alias}'.")

        if connection_type in ("ssm", "ssm-ssh") and not server_config.get("instance_id"):
            self._error(
                f"Server '{self.server_alias}' has connection_type '{connection_type}' but no instance_id configured."
            )

        if connection_type == "ssm":
            # Authenticated by IAM alone; no SSH credentials involved.
            try:
                return SsmShellRunner.connect(server_config)
            except BenchOpsConnectionError as exc:
                self._error(
                    f"Could not open an SSM session to '{self.server_alias}': {exc}",
                    hint=f"Run 'benchops server check {self.server_alias}' to diagnose.",
                )

        try:
            password = self.auth.get_password(self.server_alias)
        except KeyringUnavailableError as exc:
            self._error(
                f"Could not access the system keyring to retrieve credentials for '{self.server_alias}': {exc}"
            )

        key_path = server_config.get("private_key_path")

        if not password and not key_path:
            self._error(
                f"No authentication configured for server '{self.server_alias}'. "
                "Run 'benchops server set-auth' or 'benchops auth setup-keys' first."
            )

        if connection_type == "ssm-ssh":
            try:
                return RemoteRunner.via_ssm(server_config, password=password, key_path=key_path)
            except BenchOpsConnectionError as exc:
                self._error(
                    f"Could not open an SSM tunnel to '{self.server_alias}': {exc}",
                    hint=f"Run 'benchops server check {self.server_alias}' to diagnose.",
                )

        return RemoteRunner(
            host=server_config["host"],
            port=int(server_config["port"]),
            user=server_config["user"],
            password=password,
            key_path=key_path,
        )
