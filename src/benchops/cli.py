"""CLI entry point for benchops."""

from enum import Enum
from pathlib import Path

import typer
from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.table import Table

from benchops.auth import AuthManager, KeyringUnavailableError
from benchops.config import ConfigManager
from benchops.deploy import DeployCommand
from benchops.execute import ExecuteCommand
from benchops.install import InstallCommand
from benchops.logs import LogsCommand, LogType
from benchops.runner import BenchOpsConnectionError
from benchops.status import StatusCommand
from benchops.uninstall import UninstallCommand


class HookPhase(str, Enum):
    """Lifecycle phases for command hooks."""
    pre_local = "pre-local"
    pre_remote = "pre-remote"
    post_remote = "post-remote"
    install_remote = "install-remote"
    uninstall_remote = "uninstall-remote"


class ConnectionType(str, Enum):
    """Supported transports for reaching a configured server."""
    ssh = "ssh"
    ssm = "ssm"


app = typer.Typer(
    name="benchops",
    help="A CLI tool to synchronize local Frappe development environments with remote servers.",
    no_args_is_help=True,
)
server_app = typer.Typer(help="Manage configured remote servers.")
app.add_typer(server_app, name="server")
auth_app = typer.Typer(help="Bootstrap and manage BenchOps SSH trust.")
app.add_typer(auth_app, name="auth")

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
    connection_type: ConnectionType = typer.Option(
        ConnectionType.ssh,
        prompt="Connection type",
        help="How benchops should reach this server ('ssh' for direct SSH, 'ssm' for AWS Systems Manager).",
    ),
    instance_id: str | None = typer.Option(
        None, help="EC2 instance ID (only used when connection_type is 'ssm')."
    ),
    aws_profile: str | None = typer.Option(
        None, help="Named AWS CLI profile to use (only used when connection_type is 'ssm')."
    ),
    aws_region: str | None = typer.Option(
        None, help="AWS region the instance lives in (only used when connection_type is 'ssm')."
    ),
) -> None:
    """Add or update a configured server."""
    if connection_type == ConnectionType.ssm and not instance_id:
        instance_id = typer.prompt("EC2 instance ID")

    try:
        ConfigManager().add_server(
            alias,
            host,
            port,
            user,
            bench_path,
            connection_type=connection_type.value,
            instance_id=instance_id,
            aws_profile=aws_profile,
            aws_region=aws_region,
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
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
    table.add_column("Type")
    table.add_column("Host")
    table.add_column("Port")
    table.add_column("User")
    table.add_column("Bench Path")
    table.add_column("Instance ID")
    table.add_column("Pre-Local", justify="center")
    table.add_column("Pre-Remote", justify="center")
    table.add_column("Post-Remote", justify="center")
    table.add_column("Install-Remote", justify="center")
    table.add_column("Uninstall-Remote", justify="center")

    for alias, config in sorted(servers.items()):
        table.add_row(
            alias,
            config.get("connection_type", "ssh"),
            config.get("host", ""),
            str(config.get("port", "")),
            config.get("user", ""),
            config.get("bench_path", ""),
            config.get("instance_id") or "[dim]—[/dim]",
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

    auth = AuthManager()

    if auth_type == "password":
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        try:
            auth.set_password(alias, password)
        except KeyringUnavailableError as exc:
            console.print(f"[red]Error: Failed to save password: {exc}[/red]")
            raise typer.Exit(1)

        # A server should only carry one active credential at a time; drop the
        # key path so a stale key can't be combined with the new password.
        try:
            config.clear_private_key(alias)
        except ValueError as exc:
            console.print(f"[red]Error: {exc}[/red]")
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

        # Switching to key-based auth: clear any stale password from the
        # keyring so a future connection can't silently fall back to it.
        try:
            auth.delete_password(alias)
        except KeyringUnavailableError as exc:
            console.print(
                f"[yellow]Warning: Could not clear the old password from the system "
                f"keyring for '{alias}': {exc}[/yellow]"
            )

        console.print(f"[green]Private key path saved for server '{alias}'.[/green]")


@server_app.command("remove")
def remove_server(
    alias: str = typer.Argument(..., help="Alias of the configured server to remove."),
) -> None:
    """Remove a configured server."""
    try:
        ConfigManager().remove_server(alias)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Server '{alias}' has been removed from the configuration.[/green]")

    try:
        AuthManager().delete_password(alias)
    except KeyringUnavailableError as exc:
        console.print(
            f"[yellow]Warning: Could not clear the stored credential from the system "
            f"keyring for '{alias}': {exc}[/yellow]"
        )


@auth_app.command("setup-keys")
def setup_keys(
    alias: str = typer.Argument(..., help="Alias of the configured server to provision."),
) -> None:
    """Bootstrap SSH trust for a server: generate (or reuse) the BenchOps
    keypair and install its public key via an AWS SSM RunCommand.

    This is the one bootstrap step allowed to establish an SSH credential
    without an already-open port 22 connection — it authenticates entirely
    through the SSM control plane (IAM), never over SSH itself.
    """
    config = ConfigManager()
    server_config = config.get_server(alias)
    if server_config is None:
        console.print(f"[red]Error: No server found with alias '{alias}'.[/red]")
        raise typer.Exit(1)

    if server_config.get("connection_type") != "ssm":
        console.print(
            f"[red]Error: '{alias}' is not configured with connection_type 'ssm'. "
            "Key provisioning via SSM requires connection_type = 'ssm'.[/red]"
        )
        raise typer.Exit(1)

    instance_id = server_config.get("instance_id")
    if not instance_id:
        console.print(f"[red]Error: Server '{alias}' has no instance_id configured.[/red]")
        raise typer.Exit(1)

    auth = AuthManager()
    private_path, public_path = auth.generate_keypair()
    console.print(f"[cyan]Using BenchOps keypair: {private_path}[/cyan]")

    console.print(f"[yellow]Installing public key on '{alias}' ({instance_id}) via SSM...[/yellow]")
    try:
        auth.provision_public_key(
            instance_id=instance_id,
            remote_user=server_config["user"],
            public_key=public_path.read_text(),
            aws_profile=server_config.get("aws_profile"),
            aws_region=server_config.get("aws_region"),
        )
    except BenchOpsConnectionError as exc:
        console.print(f"[red]Error: Failed to provision the SSH key via SSM: {exc}[/red]")
        raise typer.Exit(1)

    config.update_server_key(alias, str(private_path))
    console.print(f"[green]SSH trust established for '{alias}'. Private key: {private_path}[/green]")


@app.command("deploy")
def deploy(
    app_name: str = typer.Argument(..., help="Name of the local Frappe app directory to sync."),
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
    site: str | None = typer.Option(None, help="Specific site to target for remote commands (e.g., test-16.akwad.qa)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the deploy plan without asking for confirmation."),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Allow replacing files that have different uncommitted changes on the server."
    ),
    force: bool = typer.Option(
        False, "--force", help="Deploy even if the server is on a commit that is not in your history."
    ),
    break_lock: bool = typer.Option(False, "--break-lock", help="Take over a stale deploy lock left on the server."),
    skip_build: bool = typer.Option(
        False, "--skip-build", help="Don't run 'yarn install' and 'bench build' locally; ship the existing build."
    ),
) -> None:
    """Deploy your commits and staged changes (git add) to a remote server."""
    command = DeployCommand(
        server_alias=server_alias,
        app_name=app_name,
        site=site,
        yes=yes,
        overwrite=overwrite,
        force=force,
        break_lock=break_lock,
        skip_build=skip_build,
    )
    command.execute()


@app.command("status")
def status(
    app_name: str = typer.Argument(..., help="Name of the Frappe app."),
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
    files: bool = typer.Option(False, "--files", help="List every changed file instead of counts."),
) -> None:
    """Show what a server runs for an app: who deployed what, and what changed there since."""
    StatusCommand(server_alias=server_alias, app_name=app_name, files=files).execute()


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
    command = InstallCommand(server_alias=server_alias, app_name=app_name, site=site)
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
    command = UninstallCommand(server_alias=server_alias, app_name=app_name, site=site)
    command.execute()


@app.command("logs")
def logs(
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
    log_type: LogType | None = typer.Option(
        None,
        "--type",
        help="Specific log to tail (frappe.log, web.error.log, or worker.error.log). "
        "Defaults to tailing all three together.",
    ),
) -> None:
    """Tail bench log files on a remote server in real time. Press Ctrl+C to stop."""
    command = LogsCommand(server_alias=server_alias, log_type=log_type)
    command.execute()


@app.command("execute")
def execute(
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
    method: str = typer.Argument(..., help="Dotted path of the Python method to run (e.g. frappe.clear_cache)."),
    site: str = typer.Option(..., "--site", help="Site to execute the method against."),
    args: str | None = typer.Option(
        None, "--args", help='JSON array of positional arguments, e.g. \'[1, "two"]\'.'
    ),
    kwargs: str | None = typer.Option(
        None, "--kwargs", help='JSON object of keyword arguments, e.g. \'{"key": "value"}\'.'
    ),
) -> None:
    """Run a single Python method on a remote site via `bench execute` — never an interactive shell."""
    command = ExecuteCommand(server_alias=server_alias, site=site, method=method, args=args, kwargs=kwargs)
    command.execute()
