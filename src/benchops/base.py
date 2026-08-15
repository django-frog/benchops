"""Base command logic for shared configuration and authentication."""

import typer
from rich.console import Console

from benchops.auth import AuthManager
from benchops.config import ConfigManager
from benchops.runner import RemoteRunner

console = Console()


class BaseCommand:
    """Base class providing shared logic for CLI commands."""

    def __init__(self, app_name: str, server_alias: str, site: str | None = None) -> None:
        self.app_name = app_name
        self.server_alias = server_alias
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
            cmd = cmd.replace("{app}", self.app_name)

        return cmd

    def _get_server_config(self) -> dict:
        """Retrieve and validate the server configuration."""
        server_config = self.config.get_server(self.server_alias)
        if server_config is None:
            console.print(f"[red]Error: Server '{self.server_alias}' not found in configuration.[/red]")
            raise typer.Exit(1)
        return server_config

    def _get_remote_runner(self, server_config: dict) -> RemoteRunner:
        """Instantiate an authenticated RemoteRunner."""
        try:
            password = self.auth.get_password(self.server_alias)
        except Exception:
            password = None

        key_path = server_config.get("private_key_path")

        if not password and not key_path:
            console.print(
                f"[red]Error: No authentication configured for server '{self.server_alias}'. "
                "Run 'benchops server set-auth' first.[/red]"
            )
            raise typer.Exit(1)

        return RemoteRunner(
            host=server_config["host"],
            port=int(server_config["port"]),
            user=server_config["user"],
            password=password,
            key_path=key_path,
        )
