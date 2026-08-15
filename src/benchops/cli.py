"""CLI entry point for benchops."""

from enum import Enum
from pathlib import Path

import typer
from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.table import Table

from benchops.auth import AuthManager
from benchops.config import ConfigManager
from benchops.deploy import DeployCommand


class HookPhase(str, Enum):
    """Lifecycle phases for command hooks."""
    pre_local = "pre-local"
    pre_remote = "pre-remote"
    post_remote = "post-remote"


app = typer.Typer(
    name="benchops",
    help="A CLI tool to synchronize local Frappe development environments with remote servers.",
    no_args_is_help=True,
)
server_app = typer.Typer(help="Manage configured remote servers.")
app.add_typer(server_app, name="server")

console = Console()


def _render_hook_count(config: dict, key: str) -> str:
    commands = config.get(key) or []
    if commands:
        return f"[green]{len(commands)} cmds[/green]"
    return "[dim]None[/dim]"


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
    table.add_column("Pre-Local", justify="center")
    table.add_column("Pre-Remote", justify="center")
    table.add_column("Post-Remote", justify="center")

    for alias, config in sorted(servers.items()):
        table.add_row(
            alias,
            config.get("host", ""),
            str(config.get("port", "")),
            config.get("user", ""),
            config.get("bench_path", ""),
            _render_hook_count(config, "pre_local_commands"),
            _render_hook_count(config, "pre_remote_commands"),
            _render_hook_count(config, "post_remote_commands"),
            _render_hook_count(config, "install_remote_commands"),
            _render_hook_count(config, "uninstall_remote_commands"),
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

        expanded_path = Path(key_path).expanduser()
        if not expanded_path.is_file():
            console.print(f"[red]Error: '{key_path}' is not a valid file. Please point directly to the private key file (e.g., ~/.ssh/id_rsa).[/red]")
            raise typer.Exit(1)

        try:
            config.update_server_key(alias, str(expanded_path))
        except ValueError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Private key path saved for server '{alias}'.[/green]")


@server_app.command("remove")
def remove_server(
    alias: str = typer.Argument(..., help="Alias of the configured server to remove."),
) -> None:
    """Remove a configured server."""
    try:
        ConfigManager().remove_server(alias)
        AuthManager().delete_password(alias)
        console.print(f"[green]Server '{alias}' has been removed from the configuration.[/green]")
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)


@app.command("deploy")
def deploy(
    app_name: str = typer.Argument(..., help="Name of the local Frappe app directory to sync."),
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
    site: str | None = typer.Option(None, help="Specific site to target for remote commands (e.g., test-16.akwad.qa)."),
) -> None:
    """Deploy a local Frappe app to a remote server."""
    command = DeployCommand(app_name, server_alias, site)
    command.execute()


@server_app.command("add-hook")
def add_hook(
    alias: str = typer.Argument(..., help="Alias of the configured server."),
    phase: HookPhase = typer.Argument(..., help="The lifecycle phase to attach the command to."),
    cmd: str = typer.Argument(..., help="The command string to execute (enclose in quotes)."),
) -> None:
    """Add a lifecycle command hook to a server."""
    try:
        ConfigManager().add_hook(alias, phase.value, cmd)
        console.print(f"[green]Successfully added command to {phase.value} hooks for '{alias}'.[/green]")
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)


@server_app.command("clear-hooks")
def clear_hooks(
    alias: str = typer.Argument(..., help="Alias of the configured server."),
    phase: HookPhase = typer.Argument(..., help="The lifecycle phase to clear hooks from."),
) -> None:
    """Clear all lifecycle command hooks for a specific phase."""
    try:
        ConfigManager().clear_hooks(alias, phase.value)
        console.print(f"[green]Successfully cleared {phase.value} hooks for '{alias}'.[/green]")
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)


@server_app.command("edit-hooks")
def edit_hooks(
    alias: str = typer.Argument(..., help="Alias of the configured server."),
    phase: HookPhase = typer.Argument(..., help="The lifecycle phase to edit."),
) -> None:
    """Open an embedded multiline editor to write commands."""
    config_mgr = ConfigManager()

    try:
        existing_commands = config_mgr.get_hooks(alias, phase.value)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)

    initial_text = "\n".join(existing_commands)
    if initial_text:
        initial_text += "\n"

    console.print(f"[cyan]Editing {phase.value} hooks for '{alias}'...[/cyan]")

    try:
        edited_text = prompt(
            "",
            default=initial_text,
            multiline=True,
            bottom_toolbar=HTML(" Press <b>[Esc]</b> then <b>[Enter]</b> to save | <b>[Ctrl+C]</b> to cancel "),
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Edit cancelled. No changes were made.[/yellow]")
        return

    new_commands = []
    for line in edited_text.splitlines():
        cleaned_line = line.strip()
        if cleaned_line and not cleaned_line.startswith("#"):
            new_commands.append(cleaned_line)

    try:
        config_mgr.set_hooks(alias, phase.value, new_commands)
        console.print(f"[green]Successfully saved {len(new_commands)} commands to {phase.value} hooks for '{alias}'.[/green]")
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)


@app.command("install")
def install(
    app_name: str = typer.Argument(..., help="Name of the local Frappe app."),
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
    site: str = typer.Option(
        ...,
        prompt="Target site",
        help="Specific site to install the app on (e.g., test-16.akwad.qa)."
    ),
) -> None:
    """Execute one-time installation hooks for a Frappe app on a remote server."""
    command = InstallCommand(app_name, server_alias, site)
    command.execute()

@app.command("uninstall")
def uninstall(
    app_name: str = typer.Argument(..., help="Name of the local Frappe app."),
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
    site: str = typer.Option(
        ...,
        prompt="Target site",
        help="Specific site to uninstall the app from (e.g., test-16.akwad.qa)."
    ),
) -> None:
    """Execute one-time uninstallation hooks for a Frappe app on a remote server."""
    command = UninstallCommand(app_name, server_alias, site)
    command.execute()
