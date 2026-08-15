"""Uninstallation command logic."""

import typer
from invoke.exceptions import UnexpectedExit
from rich.console import Console

from benchops.base import BaseCommand
from benchops.runner import RemoteConnectionError

console = Console()


class UninstallCommand(BaseCommand):
    """Encapsulates the one-time application uninstallation logic."""

    def execute(self) -> None:
        """Execute the remote uninstall hooks."""
        server_config = self._get_server_config()
        remote_runner = self._get_remote_runner(server_config)

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
