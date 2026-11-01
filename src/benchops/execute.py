"""Safe, single-method ad-hoc execution on a remote site.

Runs exactly one bench-mediated Python call per invocation
(`bench --site <site> execute <method>`) and returns — there is no
interactive shell, no REPL, and no persistent session on the target
instance. Whatever transport reached the instance (direct SSH or an SSM
tunnel) is torn down immediately afterward by the same RemoteRunner context
manager used everywhere else.
"""

import json
import shlex

import typer
from rich.console import Console
from rich.markup import escape

from benchops.base import BaseCommand
from benchops.runner import BenchOpsCommandError, BenchOpsConnectionError

console = Console()

# Bench subcommands people reach for, and the Python method that answers
# the same question through `bench execute`.
BENCH_COMMAND_EQUIVALENTS = {
    "list-apps": "frappe.get_installed_apps",
    "version": "frappe.utils.change_log.get_versions",
    "clear-cache": "frappe.clear_cache",
}


def method_problem(method: str) -> str | None:
    """Why `method` can't be a dotted Python path, or None if it can be.

    `bench execute` falls back to eval() when the import fails, so a bench
    subcommand like `list-apps` turns into a confusing NameError remotely.
    """
    parts = method.split(".")
    if len(parts) >= 2 and all(part.isidentifier() for part in parts):
        return None
    message = f"'{method}' is not a Python method path. `execute` runs a method by its dotted path, not a bench command."
    equivalent = BENCH_COMMAND_EQUIVALENTS.get(method)
    if equivalent:
        return f"{message} For '{method}', use: {equivalent}"
    return f"{message} For example: frappe.get_installed_apps"


class ExecuteCommand(BaseCommand):
    """Runs a single Python method on a remote site via `bench execute`."""

    def __init__(
        self,
        server_alias: str,
        site: str,
        method: str,
        args: str | None = None,
        kwargs: str | None = None,
    ) -> None:
        super().__init__(server_alias=server_alias, site=site)
        self.method = method
        self.args = args
        self.kwargs = kwargs

    def _validate_json(self) -> None:
        for label, value in (("--args", self.args), ("--kwargs", self.kwargs)):
            if value is None:
                continue
            try:
                json.loads(value)
            except json.JSONDecodeError as exc:
                console.print(f"[red]Error: {label} must be valid JSON: {exc}[/red]")
                raise typer.Exit(1)

    def _build_command(self) -> str:
        parts = ["bench", "--site", self.site, "execute", self.method]
        if self.args:
            parts += ["--args", self.args]
        if self.kwargs:
            parts += ["--kwargs", self.kwargs]
        return " ".join(shlex.quote(part) for part in parts)

    def execute(self) -> None:
        problem = method_problem(self.method)
        if problem:
            console.print(f"[red]Error:[/red] {escape(problem)}")
            raise typer.Exit(1)
        self._validate_json()
        server_config = self._get_server_config()
        command = self._build_command()

        try:
            with self._get_remote_runner(server_config) as remote_runner:
                output = remote_runner.capture(command, cwd=server_config["bench_path"])
        except BenchOpsConnectionError as exc:
            console.print(f"[red]Failed to reach '{self.server_alias}': {exc}[/red]")
            raise typer.Exit(1)
        except BenchOpsCommandError as exc:
            console.print(f"[red]'{self.method}' failed on '{self.server_alias}' (exit code {exc.exit_code}):[/red]")
            console.print((exc.stderr or exc.stdout).rstrip())
            raise typer.Exit(exc.exit_code)

        output = output.strip()
        if not output:
            console.print("[dim](no output)[/dim]")
            return

        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            console.print(output)
        else:
            console.print_json(data=parsed)
