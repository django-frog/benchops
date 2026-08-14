"""Deployment command logic."""

import os
import subprocess
import tempfile
from pathlib import Path

import typer
from rich.console import Console

from benchops.auth import AuthManager
from benchops.config import ConfigManager
from benchops.runner import LocalRunner, RemoteConnectionError, RemoteRunner
from benchops.sync import create_tarball, extract_and_cleanup, transfer_tarball

console = Console()


class DeployCommand:
    """Encapsulates the deployment pipeline logic."""

    def __init__(self, app_name: str, server_alias: str) -> None:
        self.app_name = app_name
        self.server_alias = server_alias
        self.config = ConfigManager()
        self.auth = AuthManager()

    def _resolve_app_dir(self) -> Path:
        """Locate the local application directory."""
        cwd = Path.cwd()
        for candidate in (cwd / "apps" / self.app_name, Path(self.app_name)):
            if candidate.is_dir():
                return candidate
        raise FileNotFoundError(
            f"Local app directory '{self.app_name}' not found (looked in '{cwd / 'apps'}' and '{cwd}')."
        )

    def execute(self) -> None:
        """Execute the full deployment pipeline."""
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
        local_runner = LocalRunner()

        try:
            app_dir = self._resolve_app_dir()
            console.print("[yellow]Starting local builds...[/yellow]")
            local_runner.run(["bench", "build", "--app", self.app_name], cwd=str(app_dir.parent))

            with tempfile.TemporaryDirectory() as tmp_dir:
                tarball = create_tarball(str(app_dir), os.path.join(tmp_dir, f"{self.app_name}.tar.gz"))
                console.print(f"[green]Created tarball: {tarball}[/green]")

                console.print(
                    f"[yellow]Connecting to {server_config['user']}@{server_config['host']}:{server_config['port']}...[/yellow]"
                )
                remote_dest_dir = f"{server_config['bench_path']}/apps"
                remote_tar_path = transfer_tarball(remote_runner, tarball, remote_dest_dir)
                console.print(f"[green]Transferred tarball to {remote_tar_path}[/green]")

                extract_and_cleanup(remote_runner, remote_tar_path, remote_dest_dir)
                console.print("[green]Extracted on remote server.[/green]")

            console.print("[yellow]Starting remote operations...[/yellow]")
            post_commands = server_config.get("post_commands", [])
            for cmd in post_commands:
                console.print(f"[cyan]Executing: {cmd}[/cyan]")
                remote_runner.run(cmd, cwd=server_config["bench_path"])

            console.print(f"[green]Successfully deployed '{self.app_name}' to '{self.server_alias}'.[/green]")

        except (subprocess.CalledProcessError, RemoteConnectionError, FileNotFoundError) as exc:
            console.print(f"[red]Deployment failed: {exc}[/red]")
            raise typer.Exit(1)
        finally:
            remote_runner.close()
