"""Uninstallation command logic."""

import typer
from invoke.exceptions import UnexpectedExit
from rich.console import Console

from benchops.auth import AuthManager
from benchops.config import ConfigManager
from benchops.runner import RemoteConnectionError, RemoteRunner

console = Console()

class UninstallCommand:
    """Encapsulates the one-time application uninstallation logic."""

    def __init__(self, app_name: str, server_alias: str, site: str) -> None:
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

    def execute(self) -> None:
        """Execute the remote uninstall hooks."""
        server_config = self.config.get_server(self.server_alias)
        if server_config is None:
            console.print(f"[red]Error: Server '{self.server_alias}' not found in configuration.[/red]")
            raise typer.Exit(1)

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

        remote_runner = RemoteRunner(
            host=server_config["host"],
            port=int(server_config["port"]),
            user=server_config["user"],
            password=password,
            key_path=key_path,
        )

        try:
            uninstall_commands = server_config.get("uninstall_remote_commands", [])

            if not uninstall_commands:
                console.print(
                    f"[yellow]No uninstall-remote hooks configured for '{self.server_alias}'. "
                    "Use 'benchops server edit-hooks' to add them.[/yellow]"
                )
                return

            console.print(
                f"[yellow]Connecting to {server_config['user']}@{server_config['host']}:{server_config['port']}...[/yellow]"
            )

            for cmd in uninstall_commands:
                cmd = self._interpolate_cmd(cmd)
                console.print(f"[cyan]Executing: {cmd}[/cyan]")
                remote_runner.run(cmd, cwd=server_config["bench_path"])

            console.print(f"[green]Successfully executed uninstall hooks for '{self.app_name}' on '{self.server_alias}'.[/green]")

        except (RemoteConnectionError, UnexpectedExit) as exc:
            console.print(f"[red]Uninstallation failed: {exc}[/red]")
            raise typer.Exit(1)
        finally:
            remote_runner.close()
