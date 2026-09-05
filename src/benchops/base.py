"""Base command logic for shared configuration and authentication."""

import typer
from rich.console import Console

from benchops.auth import AuthManager, KeyringUnavailableError
from benchops.config import ConfigManager
from benchops.runner import BenchOpsConnectionError, RemoteRunner, Runner

console = Console()


class BaseCommand:
    """Base class providing shared logic for CLI commands."""

    def __init__(self, server_alias: str, app_name: str | None = None, site: str | None = None) -> None:
        self.server_alias = server_alias
        self.app_name = app_name
        self.site = site
        self.config = ConfigManager()
        self.auth = AuthManager()

    def _interpolate_cmd(self, cmd: str) -> str:
        """Replace {site} and {app} placeholders in the command."""
        if "{site}" in cmd:
            if not self.site:
                console.print(
                    f"[red]Error: Command '{cmd}' requires a --site argument, but none was provided.[/red]"
                )
                raise typer.Exit(1)
            cmd = cmd.replace("{site}", self.site)

        if "{app}" in cmd:
            if not self.app_name:
                console.print(
                    f"[red]Error: Command '{cmd}' requires an app name, but none was provided.[/red]"
                )
                raise typer.Exit(1)
            cmd = cmd.replace("{app}", self.app_name)

        return cmd

    def _get_server_config(self) -> dict:
        """Retrieve and validate the server configuration."""
        server_config = self.config.get_server(self.server_alias)
        if server_config is None:
            console.print(f"[red]Error: Server '{self.server_alias}' not found in configuration.[/red]")
            raise typer.Exit(1)
        return server_config

    def _get_remote_runner(self, server_config: dict) -> Runner:
        """Instantiate an authenticated runner for the server's connection_type.

        Returns the abstract Runner interface rather than a concrete class so
        that call sites never need to know whether traffic is going over a
        direct SSH connection or a tunneled SSM session.
        """
        connection_type = server_config.get("connection_type", "ssh")
        if connection_type not in ("ssh", "ssm"):
            console.print(
                f"[red]Error: Unknown connection_type '{connection_type}' for '{self.server_alias}'.[/red]"
            )
            raise typer.Exit(1)

        try:
            password = self.auth.get_password(self.server_alias)
        except KeyringUnavailableError as exc:
            console.print(
                f"[red]Error: Could not access the system keyring to retrieve credentials "
                f"for '{self.server_alias}': {exc}[/red]"
            )
            raise typer.Exit(1)

        key_path = server_config.get("private_key_path")

        if not password and not key_path:
            console.print(
                f"[red]Error: No authentication configured for server '{self.server_alias}'. "
                "Run 'benchops server set-auth' or 'benchops auth setup-keys' first.[/red]"
            )
            raise typer.Exit(1)

        if connection_type == "ssm":
            instance_id = server_config.get("instance_id")
            if not instance_id:
                console.print(
                    f"[red]Error: Server '{self.server_alias}' has connection_type 'ssm' "
                    "but no instance_id configured.[/red]"
                )
                raise typer.Exit(1)

            try:
                return RemoteRunner.via_ssm(
                    instance_id=instance_id,
                    user=server_config["user"],
                    port=int(server_config.get("port", 22)),
                    password=password,
                    key_path=key_path,
                    aws_profile=server_config.get("aws_profile"),
                    aws_region=server_config.get("aws_region"),
                )
            except BenchOpsConnectionError as exc:
                console.print(
                    f"[red]Error: Could not establish an SSM tunnel to '{self.server_alias}': {exc}[/red]"
                )
                raise typer.Exit(1)

        return RemoteRunner(
            host=server_config["host"],
            port=int(server_config["port"]),
            user=server_config["user"],
            password=password,
            key_path=key_path,
        )
