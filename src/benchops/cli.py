"""CLI entry point for benchops."""

import os
import subprocess
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from benchops.auth import AuthManager
from benchops.config import ConfigManager
from benchops.runner import LocalRunner, RemoteConnectionError, RemoteRunner
from benchops.sync import create_tarball, extract_and_cleanup, transfer_tarball

app = typer.Typer(
    name="benchops",
    help="A CLI tool to synchronize local Frappe development environments with remote servers.",
    no_args_is_help=True,
)
server_app = typer.Typer(help="Manage configured remote servers.")
app.add_typer(server_app, name="server")

console = Console()


def _resolve_app_dir(app_name: str) -> Path:
    cwd = Path.cwd()
    for candidate in (cwd / "apps" / app_name, Path(app_name)):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"Local app directory '{app_name}' not found (looked in '{cwd / 'apps'}' and '{cwd}')."
    )


@app.command()
def init() -> None:
    """Initialize the benchops configuration."""
    ConfigManager().init_config()
    console.print("[green]benchops initialized successfully.[/green]")


@server_app.command("add")
def add_server(
    alias: str = typer.Option(..., prompt="Server alias", help="Alias for the server."),
    host: str = typer.Option(..., prompt="Server host", help="Hostname or IP address."),
    port: int = typer.Option(22, prompt="SSH port", help="SSH port (default: 22)."),
    user: str = typer.Option(..., prompt="SSH user", help="SSH username."),
    bench_path: str = typer.Option(..., prompt="Remote bench path", help="Path to the bench directory on the server."),
) -> None:
    """Add or update a configured server."""
    ConfigManager().add_server(alias, host, port, user, bench_path)
    console.print(f"[green]Server '{alias}' saved to configuration.[/green]")


@server_app.command("list")
def list_servers() -> None:
    """List all configured servers."""
    servers = ConfigManager().list_servers()
    if not servers:
        console.print("[yellow]No servers configured yet. Run 'benchops server add' to add one.[/yellow]")
        return
    table = Table(title="Configured Servers")
    table.add_column("Alias", style="bold cyan", no_wrap=True)
    table.add_column("Host")
    table.add_column("Port")
    table.add_column("User")
    table.add_column("Bench Path")
    for alias, config in sorted(servers.items()):
        table.add_row(
            alias,
            config["host"],
            str(config["port"]),
            config["user"],
            config["bench_path"],
        )
    console.print(table)


@server_app.command("set-auth")
def set_auth(
    alias: str = typer.Argument(..., help="Alias of the configured server."),
) -> None:
    """Set authentication credentials (password or SSH key) for a server."""
    config = ConfigManager()
    if config.get_server(alias) is None:
        console.print(f"[red]Error: No server found with alias '{alias}'.[/red]")
        raise typer.Exit(1)

    while True:
        auth_type = typer.prompt("Authentication type [password/key]").strip().lower()
        if auth_type in ("password", "key"):
            break
        console.print("[red]Invalid choice. Enter 'password' or 'key'.[/red]")

    if auth_type == "password":
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        try:
            AuthManager().set_password(alias, password)
        except Exception as exc:
            console.print(f"[red]Error: Failed to save password: {exc}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Password saved for server '{alias}'.[/green]")
    else:
        key_path = typer.prompt("Absolute path to the SSH private key", default="~/.ssh/id_rsa")
        try:
            config.update_server_key(alias, key_path)
        except ValueError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Private key path saved for server '{alias}'.[/green]")


@app.command("deploy")
def deploy(
    app_name: str = typer.Argument(..., help="Name of the local Frappe app directory to sync."),
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
) -> None:
    """Deploy a local Frappe app to a remote server."""
    config = ConfigManager().get_server(server_alias)
    if config is None:
        console.print(f"[red]Error: Server '{server_alias}' not found in configuration.[/red]")
        raise typer.Exit(1)

    try:
        password = AuthManager().get_password(server_alias)
    except Exception:
        password = None
    key_path = config.get("private_key_path")

    if not password and not key_path:
        console.print(
            f"[red]Error: No authentication configured for server '{server_alias}'. "
            "Run 'benchops server set-auth' first.[/red]"
        )
        raise typer.Exit(1)

    remote_runner = RemoteRunner(
        host=config["host"],
        port=int(config["port"]),
        user=config["user"],
        password=password,
        key_path=key_path,
    )
    local_runner = LocalRunner()

    try:
        app_dir = _resolve_app_dir(app_name)
        console.print("[yellow]Starting local builds...[/yellow]")
        local_runner.run(["bench", "build", "--app", app_name], cwd=str(app_dir.parent))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tarball = create_tarball(str(app_dir), os.path.join(tmp_dir, f"{app_name}.tar.gz"))
            console.print(f"[green]Created tarball: {tarball}[/green]")

            console.print(
                f"[yellow]Connecting to {config['user']}@{config['host']}:{config['port']}...[/yellow]"
            )
            remote_dest_dir = f"{config['bench_path']}/apps"
            remote_tar_path = transfer_tarball(remote_runner, tarball, remote_dest_dir)
            console.print(f"[green]Transferred tarball to {remote_tar_path}[/green]")

            extract_and_cleanup(remote_runner, remote_tar_path, remote_dest_dir)
            console.print("[green]Extracted on remote server.[/green]")

        console.print("[yellow]Starting remote operations...[/yellow]")
        remote_runner.run("bench --site all migrate", cwd=config["bench_path"])
        remote_runner.run("bench clear-cache", cwd=config["bench_path"])

        console.print(f"[green]Successfully deployed '{app_name}' to '{server_alias}'.[/green]")
    except (subprocess.CalledProcessError, RemoteConnectionError, FileNotFoundError) as exc:
        console.print(f"[red]Deployment failed: {exc}[/red]")
        raise typer.Exit(1)
    finally:
        remote_runner.close()
