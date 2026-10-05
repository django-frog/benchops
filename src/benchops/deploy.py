"""Deployment command logic."""

import os
import posixpath
import subprocess
import tempfile
from pathlib import Path

import typer
from invoke.exceptions import UnexpectedExit
from rich.console import Console

from benchops.assets import collect_app_manifests, resolve_bench_path, ship_manifests
from benchops.base import BaseCommand
from benchops.runner import BenchOpsConnectionError, LocalRunner
from benchops.sync import create_tarball, extract_and_cleanup, transfer_tarball

console = Console()


class DeployCommand(BaseCommand):
    """Encapsulates the deployment pipeline logic."""

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
        server_config = self._get_server_config()
        local_runner = LocalRunner()

        with self._get_remote_runner(server_config) as remote_runner:
            try:
                app_dir = self._resolve_app_dir()
                console.print("[yellow]Starting local pre-deploy commands...[/yellow]")
                pre_local_commands = server_config.get("pre_local_commands", [])
                for cmd in pre_local_commands:
                    console.print(f"[cyan]Executing: {cmd}[/cyan]")
                    local_runner.run(cmd, cwd=str(app_dir.parent))

                manifests = collect_app_manifests(resolve_bench_path(app_dir), self.app_name)
                asset_count = sum(len(entries) for entries in manifests.values())

                with tempfile.TemporaryDirectory() as tmp_dir:
                    tarball = create_tarball(str(app_dir), os.path.join(tmp_dir, f"{self.app_name}.tar.gz"))
                    console.print(f"[green]Created tarball: {tarball}[/green]")

                    console.print(
                        f"[yellow]Connecting to {server_config['user']}@{server_config['host']}:{server_config['port']}...[/yellow]"
                    )
                    pre_remote_commands = server_config.get("pre_remote_commands", [])
                    for cmd in pre_remote_commands:
                        cmd = self._interpolate_cmd(cmd)
                        console.print(f"[cyan]Executing: {cmd}[/cyan]")
                        remote_runner.run(cmd, cwd=server_config["bench_path"])

                    # Use posixpath to safely join remote paths
                    remote_dest_dir = posixpath.join(server_config['bench_path'], "apps")
                    remote_tar_path = transfer_tarball(remote_runner, tarball, remote_dest_dir)
                    console.print(f"[green]Transferred tarball to {remote_tar_path}[/green]")

                    extract_and_cleanup(remote_runner, remote_tar_path, remote_dest_dir)
                    console.print("[green]Extracted on remote server.[/green]")

                    if asset_count:
                        ship_manifests(
                            remote_runner, manifests, self.app_name, tmp_dir, server_config["bench_path"]
                        )
                        console.print(f"[green]Shipped {asset_count} built asset entries.[/green]")
                    else:
                        console.print(
                            f"[yellow]No built assets found for '{self.app_name}' in the local manifest; "
                            "leaving the remote manifest untouched.[/yellow]"
                        )

                console.print("[yellow]Starting remote post-deploy commands...[/yellow]")
                post_remote_commands = server_config.get("post_remote_commands", [])
                for cmd in post_remote_commands:
                    cmd = self._interpolate_cmd(cmd)
                    console.print(f"[cyan]Executing: {cmd}[/cyan]")
                    remote_runner.run(cmd, cwd=server_config["bench_path"])

                console.print(f"[green]Successfully deployed '{self.app_name}' to '{self.server_alias}'.[/green]")

            except (subprocess.CalledProcessError, BenchOpsConnectionError, FileNotFoundError, UnexpectedExit) as exc:
                console.print(f"[red]Deployment failed: {exc}[/red]")
                raise typer.Exit(1)
