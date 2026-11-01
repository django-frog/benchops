"""Remote bench log tailing."""

import posixpath
import shlex
from enum import Enum

import typer
from rich.console import Console

from benchops.base import BaseCommand
from benchops.runner import BenchOpsCommandError, BenchOpsConnectionError

console = Console()


class LogType(str, Enum):
    """A single bench log file that can be tailed on its own."""
    frappe = "frappe.log"
    web_error = "web.error.log"
    worker_error = "worker.error.log"


class LogsCommand(BaseCommand):
    """Tails one or more bench log files on the remote server: in real time
    until Ctrl+C, or — with `lines` — just the last lines, then exits."""

    def __init__(self, server_alias: str, log_type: LogType | None = None, lines: int | None = None) -> None:
        super().__init__(server_alias=server_alias)
        self.log_type = log_type
        self.lines = lines

    def _log_filenames(self) -> list[str]:
        if self.log_type is not None:
            return [self.log_type.value]
        return [member.value for member in LogType]

    def _log_paths(self, server_config: dict) -> list[str]:
        return [posixpath.join(server_config["bench_path"], "logs", name) for name in self._log_filenames()]

    def execute(self) -> None:
        if self.lines is not None:
            self._print_last_lines()
        else:
            self._follow()

    def _print_last_lines(self) -> None:
        """`tail -n N` once: bounded, non-interactive, exits by itself — for
        scripts and AI agents, which can't press Ctrl+C."""
        server_config = self._get_server_config()
        quoted = " ".join(shlex.quote(path) for path in self._log_paths(server_config))
        try:
            with self._get_remote_runner(server_config) as remote_runner:
                remote_runner.run(f"tail -n {int(self.lines)} -- {quoted}")
        except BenchOpsCommandError:
            # tail still prints the files it could read; it names the others.
            console.print(f"[yellow]Some log files could not be read on '{self.server_alias}' (see above).[/yellow]")
            raise typer.Exit(1)
        except BenchOpsConnectionError as exc:
            console.print(f"[red]Failed to read logs on '{self.server_alias}': {exc}[/red]")
            raise typer.Exit(1)

    def _follow(self) -> None:
        """Run `tail -n 100 -f` against the selected log path(s) until the
        user stops it with Ctrl+C."""
        server_config = self._get_server_config()
        filenames = self._log_filenames()
        log_paths = self._log_paths(server_config)
        command = "tail -n 100 -f " + " ".join(shlex.quote(path) for path in log_paths)

        console.print(
            f"[yellow]Tailing {', '.join(filenames)} on '{self.server_alias}'. "
            "Press Ctrl+C to stop.[/yellow]"
        )

        try:
            with self._get_remote_runner(server_config) as remote_runner:
                remote_runner.run(command, interactive=True)
        except KeyboardInterrupt:
            pass
        except BenchOpsConnectionError as exc:
            console.print(f"[red]Failed to tail logs on '{self.server_alias}': {exc}[/red]")
            raise typer.Exit(1)

        console.print("\n[yellow]Stopped tailing logs.[/yellow]")
