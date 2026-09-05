"""Remote bench log tailing."""

import posixpath
import shlex
from enum import Enum

import typer
from rich.console import Console

from benchops.base import BaseCommand
from benchops.runner import BenchOpsConnectionError

console = Console()


class LogType(str, Enum):
    """A single bench log file that can be tailed on its own."""
    frappe = "frappe.log"
    web_error = "web.error.log"
    worker_error = "worker.error.log"


class LogsCommand(BaseCommand):
    """Tails one or more bench log files on the remote server in real time."""

    def __init__(self, server_alias: str, log_type: LogType | None = None) -> None:
        super().__init__(server_alias=server_alias)
        self.log_type = log_type

    def _log_filenames(self) -> list[str]:
        if self.log_type is not None:
            return [self.log_type.value]
        return [member.value for member in LogType]

    def execute(self) -> None:
        """Run `tail -n 100 -f` against the selected log path(s) until the
        user stops it with Ctrl+C."""
        server_config = self._get_server_config()
        filenames = self._log_filenames()
        log_paths = [
            posixpath.join(server_config["bench_path"], "logs", name) for name in filenames
        ]
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
