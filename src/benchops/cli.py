"""CLI entry point for benchops."""

import sys
from enum import Enum
from pathlib import Path

import typer
from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from benchops.auth import AuthManager, KeyringUnavailableError
from benchops.backups import BackupSourceError, S3Backups, human_size
from benchops.clone import DEFAULT_JOBS, CloneCommand
from benchops.check import CheckCommand
from benchops.config import ConfigManager
from benchops.deploy import DeployCommand
from benchops.execute import ExecuteCommand
from benchops.install import InstallCommand
from benchops.logs import LogsCommand, LogType
from benchops.projects import load_manifest, validate_project
from benchops.restore import RestoreCommand
from benchops.runner import BenchOpsConnectionError
from benchops.ssm import SsmError, list_managed_instances, session_for
from benchops.status import StatusCommand
from benchops.uninstall import UninstallCommand
from benchops.vcs.credentials import TokenStore, parse_credential_request, token_env_var
from benchops.vcs.providers import PROVIDERS, AuthError, ProviderError, make_provider, provider_kind_for
from benchops.version import installed_version, render_version_report, update_notice


class HookPhase(str, Enum):
    """Lifecycle phases for command hooks."""
    pre_local = "pre-local"
    pre_remote = "pre-remote"
    post_remote = "post-remote"
    install_remote = "install-remote"
    uninstall_remote = "uninstall-remote"
    post_restore = "post-restore"


class LocalHookPhase(str, Enum):
    """Hook phases for the bench on this machine."""
    post_restore = "post-restore"


class ConnectionType(str, Enum):
    """Supported transports for reaching a configured server."""
    ssh = "ssh"
    ssm = "ssm"


class GitProvider(str, Enum):
    """Git hosting providers bulk clone can talk to."""
    github = "github"
    gitlab = "gitlab"
    gitea = "gitea"
    generic = "generic"


class CloneProtocol(str, Enum):
    https = "https"
    ssh = "ssh"


app = typer.Typer(
    name="benchops",
    help="A CLI tool to synchronize local Frappe development environments with remote servers.",
    no_args_is_help=True,
)
server_app = typer.Typer(help="Manage configured remote servers.")
app.add_typer(server_app, name="server")
auth_app = typer.Typer(help="Bootstrap and manage BenchOps SSH trust.")
app.add_typer(auth_app, name="auth")
git_app = typer.Typer(help="Manage git hosting tokens used by 'benchops clone'.")
app.add_typer(git_app, name="git")
project_app = typer.Typer(help="Manage clone projects: named groups of repositories.")
app.add_typer(project_app, name="project")
backup_app = typer.Typer(help="Browse site backups that Frappe uploaded to S3.")
app.add_typer(backup_app, name="backup")
backup_source_app = typer.Typer(help="Manage S3 backup sources: the buckets Frappe uploads backups to.")
backup_app.add_typer(backup_source_app, name="source")
local_app = typer.Typer(help="Hooks for the bench on this machine (used by 'benchops restore --local').")
app.add_typer(local_app, name="local")

console = Console()
err_console = Console(stderr=True)


def _show_version(value: bool) -> None:
    if value:
        console.print(f"benchops {installed_version()}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", callback=_show_version, is_eager=True, help="Show the installed version and exit."
    ),
) -> None:
    """A CLI tool to synchronize local Frappe development environments with remote servers."""
    # On stderr, so it never mixes into a command's own output. Git runs the
    # credential helper in the background, where a notice is just noise.
    if ctx.invoked_subcommand != "version" and sys.argv[1:3] != ["git", "credential"]:
        notice = update_notice()
        if notice:
            err_console.print(f"[yellow]{notice}[/yellow]")


@app.command("version")
def version_command(
    show_all: bool = typer.Option(False, "--all", help="Show the release notes of every version."),
) -> None:
    """Show the installed version, whether a newer one is available, and what changed."""
    render_version_report(console, show_all=show_all)


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
    connection_type: ConnectionType = typer.Option(
        ConnectionType.ssh,
        prompt="Connection type",
        help="How benchops should reach this server ('ssh' for direct SSH, 'ssm' for AWS Systems Manager).",
    ),
    user: str = typer.Option(..., prompt="SSH user", help="SSH username."),
    bench_path: str = typer.Option(..., prompt="Remote bench path", help="Path to the bench directory on the server."),
    host: str | None = typer.Option(
        None, help="Hostname or IP address (asked for ssh; for ssm it is only a label and defaults to the instance)."
    ),
    port: int | None = typer.Option(None, help="SSH port (default: 22; for ssm, sshd's port on the instance)."),
    instance_id: str | None = typer.Option(
        None, help="EC2 instance ID (ssm only). Omit it to pick from the instances registered with SSM."
    ),
    aws_profile: str | None = typer.Option(
        None, help="AWS profile to use (ssm only). aws-vault profiles work too."
    ),
    aws_region: str | None = typer.Option(
        None, help="AWS region the instance lives in (ssm only; default: the profile's region)."
    ),
) -> None:
    """Add or update a configured server. Updating keeps its hooks and key."""
    if connection_type == ConnectionType.ssh:
        if not host:
            host = typer.prompt("Server host")
        if port is None:
            port = typer.prompt("SSH port", default=22, type=int)
    else:
        if not instance_id:
            instance_id = _pick_ssm_instance(aws_profile, aws_region)
        host = host or instance_id
        port = port or 22

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

    server = ConfigManager().get_server(alias) or {}
    if not server.get("private_key_path"):
        auth_step = (
            f"benchops auth setup-keys {alias}"
            if connection_type == ConnectionType.ssm
            else f"benchops server set-auth {alias}"
        )
        console.print(f"[dim]Next: '{auth_step}', then 'benchops server check {alias}'.[/dim]")
    else:
        console.print(f"[dim]Next: 'benchops server check {alias}'.[/dim]")


def _pick_ssm_instance(aws_profile: str | None, aws_region: str | None) -> str:
    """Let the user pick an instance registered with SSM, falling back to
    typing the ID when the instances can't be listed."""
    try:
        instances = list_managed_instances(session_for({"aws_profile": aws_profile, "aws_region": aws_region}))
    except SsmError as exc:
        console.print(f"[yellow]Could not list SSM-managed instances: {escape(str(exc))}[/yellow]")
        return typer.prompt("EC2 instance ID").strip()
    if not instances:
        console.print("[yellow]No instances are registered with SSM in this account/region.[/yellow]")
        return typer.prompt("EC2 instance ID").strip()

    table = Table(title="Instances registered with SSM")
    table.add_column("#", justify="right")
    table.add_column("Instance ID", style="bold cyan")
    table.add_column("Name")
    table.add_column("Platform")
    table.add_column("Agent")
    for number, instance in enumerate(instances, 1):
        status = "[green]Online[/green]" if instance.ping_status == "Online" else f"[red]{instance.ping_status}[/red]"
        table.add_row(str(number), instance.instance_id, instance.name, instance.platform, status)
    console.print(table)

    while True:
        choice = typer.prompt("Instance (number or ID)").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(instances):
            return instances[int(choice) - 1].instance_id
        if choice.startswith(("i-", "mi-")):
            return choice
        console.print(f"[red]Enter a number from 1 to {len(instances)}, or an instance ID.[/red]")


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
    table.add_column("Post-Restore", justify="center")

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
            _render_hook_count(config, "post_restore_commands"),
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


@server_app.command("check")
def check_server(
    alias: str = typer.Argument(..., help="Alias of the configured server."),
) -> None:
    """Test the connection to a server step by step (AWS credentials, SSM, SSH login, bench) and explain any failure."""
    CheckCommand(server_alias=alias).execute()


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

    console.print(
        f"[yellow]Installing public key for '{server_config['user']}' on '{alias}' ({instance_id}) via SSM...[/yellow]"
    )
    try:
        auth.provision_public_key(server_config, public_key=public_path.read_text())
    except BenchOpsConnectionError as exc:
        console.print(f"[red]Error: Failed to provision the SSH key via SSM:[/red] {escape(str(exc))}")
        raise typer.Exit(1)

    config.update_server_key(alias, str(private_path))
    console.print(f"[green]SSH trust established for '{alias}'. Private key: {private_path}[/green]")
    console.print(f"[dim]Verify with 'benchops server check {alias}'.[/dim]")


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
    label: str | None = typer.Option(
        None, "--label", help="Tag the deployed drafts, e.g. a task ID (TASK-142); shown by 'benchops status'."
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
        label=label,
    )
    command.execute()


@app.command("sync")
def sync(
    app_name: str = typer.Argument(..., help="Name of the local Frappe app directory to sync."),
    server_alias: str = typer.Argument(..., help="Alias of the target server."),
    site: str | None = typer.Option(None, help="Specific site to target for remote commands (e.g., test-16.akwad.qa)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the sync plan without asking for confirmation."),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Allow replacing files that have different uncommitted changes on the server."
    ),
    force: bool = typer.Option(
        False, "--force", help="Sync even if the server is on a commit that is not in your history."
    ),
    break_lock: bool = typer.Option(False, "--break-lock", help="Take over a stale deploy lock left on the server."),
    skip_build: bool = typer.Option(
        False, "--skip-build", help="Don't build locally, even if the commits touch frontend source."
    ),
) -> None:
    """Bring the server up to your latest commits (no staged files); deployed drafts those commits contain become committed."""
    command = DeployCommand(
        server_alias=server_alias,
        app_name=app_name,
        site=site,
        yes=yes,
        overwrite=overwrite,
        force=force,
        break_lock=break_lock,
        skip_build=skip_build,
        sync=True,
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


def _edit_commands(existing_commands: list[str]) -> list[str] | None:
    """Edit commands in the embedded editor, one per line; None if cancelled.
    Blank lines and # comments are dropped."""
    initial_text = "\n".join(existing_commands)
    if initial_text:
        initial_text += "\n"

    try:
        edited_text = prompt(
            "",
            default=initial_text,
            multiline=True,
            bottom_toolbar=HTML(" Press <b>[Esc]</b> then <b>[Enter]</b> to save | <b>[Ctrl+C]</b> to cancel "),
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Edit cancelled. No changes were made.[/yellow]")
        return None

    new_commands = []
    for line in edited_text.splitlines():
        cleaned_line = line.strip()
        if cleaned_line and not cleaned_line.startswith("#"):
            new_commands.append(cleaned_line)
    return new_commands


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

    console.print(f"[cyan]Editing {phase.value} hooks for '{alias}'...[/cyan]")
    new_commands = _edit_commands(existing_commands)
    if new_commands is None:
        return

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


@app.command("restore")
def restore(
    source: str = typer.Argument(..., help="Backup source to restore from (see 'benchops backup source list')."),
    legacy_source: str | None = typer.Argument(None, hidden=True),
    site: str = typer.Option(..., "--site", help="Site to restore."),
    server_alias: str | None = typer.Option(None, "--server", help="Restore on this configured server."),
    local: bool = typer.Option(False, "--local", help="Restore on the bench on this machine."),
    bench: Path | None = typer.Option(
        None, "--bench", help="Local bench root for --local (default: the current directory)."
    ),
    from_site: str | None = typer.Option(
        None, "--from-site", help="Site whose backup to restore, when it differs from --site (e.g. production's)."
    ),
    at: str | None = typer.Option(
        None, "--at", help="Backup to restore: 'latest' (default) or a timestamp prefix like 20261019 or 20261019_0200."
    ),
    new_site: bool = typer.Option(False, "--new-site", help="Create --site from the backup instead of replacing it."),
    with_files: bool = typer.Option(True, "--files/--no-files", help="Restore public and private files too."),
    admin_password: str | None = typer.Option(
        None, "--admin-password", help="Set the Administrator password after restoring (default: the backup's)."
    ),
    skip_safety_backup: bool = typer.Option(
        False, "--skip-safety-backup", help="Don't back up the site before replacing it."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Restore without asking for confirmation."),
) -> None:
    """Restore a site from a backup in S3, on a server (--server) or on your local bench (--local)."""
    if legacy_source is not None:
        # 0.18.0 took the server first: `benchops restore <server> <source>`.
        console.print(
            f"[red]Error: The server is now an option: benchops restore {legacy_source} --server {source} "
            "(or --local).[/red]"
        )
        raise typer.Exit(1)
    if bool(server_alias) == local:
        console.print("[red]Error: Pass either --server <alias> or --local.[/red]")
        raise typer.Exit(1)
    if bench is not None and not local:
        console.print("[red]Error: --bench only applies with --local.[/red]")
        raise typer.Exit(1)
    RestoreCommand(
        source_name=source,
        site=site,
        server_alias=server_alias,
        bench=bench,
        from_site=from_site,
        at=at,
        new_site=new_site,
        with_files=with_files,
        admin_password=admin_password,
        safety_backup=not skip_safety_backup,
        yes=yes,
    ).execute()


@backup_source_app.command("add")
def backup_source_add(
    name: str = typer.Argument(..., help="Name for this source, used as 'benchops backup list <name>'."),
    bucket: str = typer.Option(..., "--bucket", help="Bucket name set in Frappe's S3 Backup Settings."),
    prefix: str | None = typer.Option(None, "--prefix", help="Key prefix in front of the backup folders, if any."),
    aws_profile: str | None = typer.Option(None, help="Named AWS CLI profile (default: the default AWS credentials)."),
    aws_region: str | None = typer.Option(None, help="Bucket region (default: looked up from the bucket)."),
    endpoint_url: str | None = typer.Option(None, help="Endpoint of an S3-compatible store (MinIO, Wasabi, ...)."),
) -> None:
    """Add or update an S3 backup source, after checking it can be read."""
    entry = {"bucket": bucket, "prefix": prefix, "aws_profile": aws_profile,
             "aws_region": aws_region, "endpoint_url": endpoint_url}
    try:
        sets = S3Backups(entry).list_sets()
    except BackupSourceError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
    ConfigManager().set_backup_source(name, bucket, prefix, aws_profile, aws_region, endpoint_url)
    sites = len({b.site_slug for b in sets})
    console.print(f"[green]Backup source '{name}' saved: {len(sets)} backup(s) of {sites} site(s) found.[/green]")


@backup_source_app.command("list")
def backup_source_list() -> None:
    """List S3 backup sources."""
    sources = ConfigManager().list_backup_sources()
    if not sources:
        console.print("[yellow]No backup sources yet. Run 'benchops backup source add'.[/yellow]")
        return
    table = Table(title="Backup Sources")
    table.add_column("Name", style="bold cyan")
    table.add_column("Location")
    table.add_column("AWS Profile")
    table.add_column("Region / Endpoint")
    for name, source in sorted(sources.items()):
        location = f"s3://{source['bucket']}/{source.get('prefix') or ''}".rstrip("/")
        table.add_row(name, location, source.get("aws_profile") or "[dim]default[/dim]",
                      source.get("endpoint_url") or source.get("aws_region") or "[dim]auto[/dim]")
    console.print(table)


@backup_source_app.command("remove")
def backup_source_remove(name: str = typer.Argument(..., help="Backup source name.")) -> None:
    """Remove a backup source (nothing in S3 is touched)."""
    try:
        ConfigManager().remove_backup_source(name)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Backup source '{name}' removed.[/green]")


@backup_app.command("list")
def backup_list(
    source: str = typer.Argument(..., help="Backup source name."),
    site: str | None = typer.Option(None, "--site", help="Only backups of this site."),
    limit: int = typer.Option(10, "--limit", "-n", help="Show this many of the newest backups (0: all)."),
) -> None:
    """List the backups in a source, newest first."""
    entry = ConfigManager().get_backup_source(source)
    if entry is None:
        console.print(f"[red]Error: Backup source '{source}' not found.[/red]")
        raise typer.Exit(1)
    try:
        sets = S3Backups(entry).list_sets(site)
    except BackupSourceError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
    if not sets:
        console.print("[yellow]No Frappe backups found" + (f" for '{site}'." if site else ".") + "[/yellow]")
        return

    def size(part) -> str:
        return human_size(part.size) if part else "[dim]—[/dim]"

    shown = sets[:limit] if limit > 0 else sets
    table = Table(title=f"Backups in '{source}'" + (f" ({len(shown)} of {len(sets)})" if len(shown) < len(sets) else ""))
    table.add_column("Stamp (--at)", style="bold cyan", no_wrap=True)
    table.add_column("Taken")
    table.add_column("Site")
    table.add_column("Database", justify="right")
    table.add_column("Public Files", justify="right")
    table.add_column("Private Files", justify="right")
    for backup in shown:
        database = size(backup.database) if backup.restorable else "[red]missing[/red]"
        table.add_row(backup.stamp, f"{backup.taken_at:%Y-%m-%d %H:%M}", backup.site_slug, database,
                      size(backup.public_files), size(backup.private_files))
    console.print(table)


@local_app.command("add-hook")
def local_add_hook(
    phase: LocalHookPhase = typer.Argument(..., help="The phase to attach the command to."),
    cmd: str = typer.Argument(..., help="The command string to execute (enclose in quotes)."),
) -> None:
    """Add a command hook for the local bench."""
    ConfigManager().add_local_hook(phase.value, cmd)
    console.print(f"[green]Successfully added command to local {phase.value} hooks.[/green]")


@local_app.command("clear-hooks")
def local_clear_hooks(phase: LocalHookPhase = typer.Argument(..., help="The phase to clear.")) -> None:
    """Clear the local bench's hooks for a phase."""
    ConfigManager().set_local_hooks(phase.value, [])
    console.print(f"[green]Successfully cleared local {phase.value} hooks.[/green]")


@local_app.command("edit-hooks")
def local_edit_hooks(phase: LocalHookPhase = typer.Argument(..., help="The phase to edit.")) -> None:
    """Open the embedded multiline editor for the local bench's hooks."""
    config_mgr = ConfigManager()
    console.print(f"[cyan]Editing local {phase.value} hooks...[/cyan]")
    new_commands = _edit_commands(config_mgr.get_local_hooks(phase.value))
    if new_commands is None:
        return
    config_mgr.set_local_hooks(phase.value, new_commands)
    console.print(f"[green]Successfully saved {len(new_commands)} commands to local {phase.value} hooks.[/green]")


@local_app.command("list-hooks")
def local_list_hooks() -> None:
    """Show the local bench's hooks."""
    config_mgr = ConfigManager()
    for phase in LocalHookPhase:
        commands = config_mgr.get_local_hooks(phase.value)
        console.print(f"[bold]{phase.value}[/bold]" + ("" if commands else "  [dim]none[/dim]"))
        for cmd in commands:
            console.print(f"  {cmd}")


def _normalize_host(value: str) -> str:
    """Accept a bare host or any repository URL on it."""
    value = value.strip()
    if "://" in value:
        value = value.split("://", 1)[1].split("/", 1)[0]
    elif "@" in value:
        value = value.split(":", 1)[0]
    return value.rsplit("@", 1)[-1].strip("/").lower()


def _split_list(values: list[str] | None) -> list[str]:
    return [item.strip() for value in values or [] for item in value.split(",") if item.strip()]


@git_app.command("login")
def git_login(
    host: str = typer.Argument(..., help="Git host, e.g. github.com or gitlab.akwad.qa."),
    provider: GitProvider | None = typer.Option(
        None, help="What the host runs (default: detected for github.com / gitlab.com, otherwise asked)."
    ),
    api_url: str | None = typer.Option(None, help="API base URL, if not the provider's default for this host."),
    username: str | None = typer.Option(
        None, help="Username git sends with the token (only needed for some generic hosts, e.g. Bitbucket)."
    ),
    token_stdin: bool = typer.Option(False, "--token-stdin", help="Read the token from standard input."),
) -> None:
    """Save a personal access token for a git host (in the system keyring) and verify it."""
    host = _normalize_host(host)
    kind = provider.value if provider else None
    if kind is None:
        detected = provider_kind_for(host, (ConfigManager().get_git_host(host) or {}).get("provider"))
        kind = detected if detected != "generic" else typer.prompt(
            f"What does {host} run? [{'/'.join(PROVIDERS)}]", default="gitlab"
        ).strip().lower()
        if kind not in PROVIDERS:
            console.print(f"[red]Error: Unknown provider '{kind}'.[/red]")
            raise typer.Exit(1)

    token = sys.stdin.readline().strip() if token_stdin else typer.prompt(f"Token for {host}", hide_input=True).strip()
    if not token:
        console.print("[red]Error: The token is empty.[/red]")
        raise typer.Exit(1)

    client = make_provider(kind, host, token=token, api_url=api_url, username=username)
    if client.has_api:
        try:
            identity = client.whoami()
        except AuthError:
            console.print(f"[red]Error: {host} rejected this token. Nothing was saved.[/red]")
            raise typer.Exit(1)
        except ProviderError as exc:
            console.print(
                f"[red]Error: {exc} Nothing was saved. Check --provider, or pass --api-url if the API "
                "lives elsewhere.[/red]"
            )
            raise typer.Exit(1)
        username = username or (identity.username if kind == "gitea" else None)
        expires = f", expires {identity.expires.isoformat()}" if identity.expires else ""
        console.print(f"[green]Token works: signed in to {host} as {identity.username}{expires}.[/green]")
        for warning in identity.warnings:
            console.print(f"[yellow]Warning: {warning}[/yellow]")
    else:
        console.print(f"[dim]{host} is a generic git host; the token is checked when you clone.[/dim]")

    ConfigManager().set_git_host(host, kind, api_url=api_url, username=username)
    try:
        TokenStore().set(host, token)
    except KeyringUnavailableError as exc:
        console.print(f"[red]Error: {exc}. Set {token_env_var(host)} in your environment instead.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Token for {host} saved to the system keyring.[/green]")


@git_app.command("whoami")
def git_whoami(host: str = typer.Argument(..., help="Git host to check.")) -> None:
    """Show who the stored token for a git host belongs to, its scopes and its expiry."""
    host = _normalize_host(host)
    stored = ConfigManager().get_git_host(host) or {}
    try:
        token, source = TokenStore().get(host)
    except KeyringUnavailableError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
    if not token:
        console.print(f"[yellow]No token for {host}. Run 'benchops git login {host}'.[/yellow]")
        raise typer.Exit(1)
    client = make_provider(provider_kind_for(host, stored.get("provider")), host, token=token,
                           api_url=stored.get("api_url"), username=stored.get("username"))
    if not client.has_api:
        console.print(f"{host}: generic git host, token from {source}.")
        return
    try:
        identity = client.whoami()
    except AuthError:
        console.print(f"[red]{host} rejects the token from {source} (expired or revoked?).[/red]")
        raise typer.Exit(1)
    except ProviderError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]{host}[/bold] ({client.kind}, token from {source})")
    console.print(f"  User     {identity.username}" + (f" ({identity.name})" if identity.name else ""))
    if identity.scopes is not None:
        console.print(f"  Scopes   {', '.join(identity.scopes) or '(none)'}")
    console.print(f"  Expires  {identity.expires.isoformat() if identity.expires else 'never / unknown'}")
    for warning in identity.warnings:
        console.print(f"[yellow]  Warning: {warning}[/yellow]")


@git_app.command("logout")
def git_logout(host: str = typer.Argument(..., help="Git host to forget.")) -> None:
    """Delete the stored token and settings for a git host."""
    host = _normalize_host(host)
    try:
        TokenStore().delete(host)
    except KeyringUnavailableError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
    try:
        ConfigManager().remove_git_host(host)
    except ValueError:
        pass
    console.print(f"[green]Forgot the token for {host}.[/green]")


@git_app.command("hosts")
def git_hosts() -> None:
    """List git hosts with a stored token."""
    hosts = ConfigManager().list_git_hosts()
    if not hosts:
        console.print("[yellow]No git hosts yet. Run 'benchops git login <host>'.[/yellow]")
        return
    store = TokenStore()
    table = Table(title="Git Hosts")
    table.add_column("Host", style="bold cyan")
    table.add_column("Provider")
    table.add_column("API URL")
    table.add_column("Token")
    for host, entry in sorted(hosts.items()):
        try:
            source = store.get(host)[1] or "[red]missing[/red]"
        except KeyringUnavailableError:
            source = "[yellow]keyring unavailable[/yellow]"
        table.add_row(host, entry.get("provider", ""), entry.get("api_url") or "[dim]default[/dim]", source)
    console.print(table)


@git_app.command("credential", hidden=True)
def git_credential(operation: str = typer.Argument(...)) -> None:
    """Git credential helper protocol: answers 'get' with the stored token."""
    if operation != "get":
        return
    request = parse_credential_request(sys.stdin.read())
    host = request.get("host", "").lower()
    if request.get("protocol") != "https" or not host:
        return
    try:
        token, _ = TokenStore().get(host)
    except KeyringUnavailableError:
        return
    if not token:
        return
    stored = ConfigManager().get_git_host(host) or {}
    client = make_provider(provider_kind_for(host, stored.get("provider")), host, username=stored.get("username"))
    sys.stdout.write(f"username={client.git_username()}\npassword={token}\n")


@project_app.command("add")
def project_add(
    name: str = typer.Argument(..., help="Project name, used as 'benchops clone <name>'."),
    manifest: Path | None = typer.Option(None, help="Import the project from a manifest file (.toml)."),
    host: str | None = typer.Option(None, help="Git host the repository paths live on."),
    provider: GitProvider | None = typer.Option(None, help="What the host runs, if not detected."),
    group: str | None = typer.Option(None, help="Discover repositories from this org / group."),
    match: str | None = typer.Option(None, help="Only discovered repositories whose name matches this glob."),
    repo: list[str] | None = typer.Option(
        None, "--repo", help="A repository path on --host (group/app) or a full URL. Repeat or comma-separate."
    ),
    branch: str | None = typer.Option(None, help="Branch to clone for every repository (default: each repo's default)."),
    protocol: CloneProtocol = typer.Option(CloneProtocol.https, help="Clone over https (token) or ssh (your keys)."),
    replace: bool = typer.Option(False, "--replace", help="Overwrite an existing project with this name."),
) -> None:
    """Add a clone project from options or a manifest."""
    config = ConfigManager()
    if config.get_project(name) is not None and not replace:
        console.print(f"[red]Error: Project '{name}' already exists. Pass --replace to overwrite it.[/red]")
        raise typer.Exit(1)
    try:
        if manifest:
            project = load_manifest(manifest.expanduser())
        else:
            # Without --host every entry must be a full URL; with it, only
            # things that look like one (scheme, user@host:path, absolute path) are.
            repos = [
                {"url": item} if not host or "://" in item or "@" in item or Path(item).is_absolute() else {"path": item}
                for item in _split_list(repo)
            ]
            project = validate_project({
                "host": _normalize_host(host) if host else None,
                "provider": provider.value if provider else None,
                "group": group, "match": match, "branch": branch,
                "protocol": protocol.value, "repos": repos,
            })
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
    config.set_project(name, project)
    what = f"{len(project['repos'])} repositories" + (f" + discovery from '{project['group']}'" if project.get("group") else "")
    console.print(f"[green]Project '{name}' saved ({what}). Check access with 'benchops clone {name} --dry-run'.[/green]")


@project_app.command("list")
def project_list() -> None:
    """List clone projects."""
    projects = ConfigManager().list_projects()
    if not projects:
        console.print("[yellow]No projects yet. Run 'benchops project add'.[/yellow]")
        return
    table = Table(title="Clone Projects")
    table.add_column("Name", style="bold cyan")
    table.add_column("Host")
    table.add_column("Repos", justify="right")
    table.add_column("Discovery")
    table.add_column("Branch")
    for name, project in sorted(projects.items()):
        discovery = f"{project['group']}" + (f" ({project['match']})" if project.get("match") else "") if project.get("group") else "[dim]—[/dim]"
        table.add_row(name, project.get("host") or "[dim]per repo[/dim]", str(len(project.get("repos") or [])),
                      discovery, project.get("branch") or "[dim]default[/dim]")
    console.print(table)


@project_app.command("show")
def project_show(name: str = typer.Argument(..., help="Project name.")) -> None:
    """Show a project's settings and repositories."""
    project = ConfigManager().get_project(name)
    if project is None:
        console.print(f"[red]Error: No project named '{name}'.[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]{name}[/bold]")
    for key in ("host", "provider", "protocol", "branch", "group", "match"):
        if project.get(key):
            console.print(f"  {key:<9}{project[key]}")
    for repo in project.get("repos") or []:
        extras = ", ".join(f"{k}={v}" for k, v in repo.items() if k not in ("path", "url"))
        console.print(f"  • {repo.get('path') or repo.get('url')}" + (f"  [dim]({extras})[/dim]" if extras else ""))
    console.print(f"[dim]Your access to each repository: benchops clone {name} --dry-run[/dim]")


@project_app.command("remove")
def project_remove(name: str = typer.Argument(..., help="Project name.")) -> None:
    """Remove a clone project (cloned folders are left alone)."""
    try:
        ConfigManager().remove_project(name)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Project '{name}' removed.[/green]")


@app.command("clone")
def clone(
    project: str = typer.Argument(..., help="Project name (see 'benchops project list') or a manifest .toml file."),
    into: Path | None = typer.Option(None, "--into", help="Directory to clone into (default: current directory)."),
    jobs: int = typer.Option(DEFAULT_JOBS, "--jobs", "-j", min=1, max=32, help="Repositories cloned at the same time."),
    branch: str | None = typer.Option(None, help="Clone this branch of every repository."),
    protocol: CloneProtocol | None = typer.Option(None, help="Override the project's protocol (https or ssh)."),
    depth: int | None = typer.Option(None, min=1, help="Shallow clone with this many commits."),
    partial: bool = typer.Option(
        False, "--partial", help="Full history, file contents fetched on demand (much faster for big repos)."
    ),
    only: list[str] | None = typer.Option(None, "--only", help="Only these repos (folder, path or glob). Comma-separate or repeat."),
    skip: list[str] | None = typer.Option(None, "--skip", help="Leave out these repos (folder, path or glob)."),
    update: bool = typer.Option(False, "--update", help="Fetch (and fast-forward when clean) repos that are already cloned."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and your access, clone nothing."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Only retry what failed in the last run."),
    persist_credentials: bool = typer.Option(
        False, "--persist-credentials", help="Let later git pulls use the stored token (via a credential helper)."
    ),
) -> None:
    """Clone all repositories of a project in parallel, checking your access first."""
    CloneCommand(
        project=project, into=into, jobs=jobs, branch=branch, protocol=protocol.value if protocol else None,
        depth=depth, partial=partial, only=_split_list(only), skip=_split_list(skip), update=update,
        dry_run=dry_run, yes=yes, retry_failed=retry_failed, persist_credentials=persist_credentials,
    ).execute()


if __name__ == "__main__":
    app()
