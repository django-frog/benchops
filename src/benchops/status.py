"""`benchops status`: what a server runs for an app, without logging in.

Read-only — it never takes the deploy lock or changes anything. It shows
the server's branch and commit, the last BenchOps deploy, the server's
`git status` (the last deploy's staged files plus any work done directly
there), and which deployed files were changed on the server since.
"""

import typer
from rich.console import Console

from benchops.base import BaseCommand
from benchops.runner import BenchOpsConnectionError
from benchops.sync import RemoteAgent, RemoteAgentError

console = Console()


class StatusCommand(BaseCommand):
    """Prints the server-side state of one app."""

    def __init__(self, server_alias: str, app_name: str, files: bool = False) -> None:
        super().__init__(server_alias=server_alias, app_name=app_name)
        self.files = files

    def _section(self, title: str, items: list, fmt=str) -> None:
        console.print(f"  {title:<38}{len(items)}")
        if self.files:
            for item in items:
                console.print(f"      {fmt(item)}")

    def execute(self) -> None:
        server_config = self._get_server_config()
        try:
            with self._get_remote_runner(server_config) as runner:
                state = RemoteAgent(runner, server_config["bench_path"]).call("status", app=self.app_name)
        except RemoteAgentError as exc:
            message = (
                f"apps/{self.app_name} on the server is not a git repository."
                if exc.error == "not_repo"
                else exc.error
            )
            console.print(f"[red]Error: {message}[/red]")
            raise typer.Exit(1)
        except BenchOpsConnectionError as exc:
            console.print(f"[red]Failed to reach '{self.server_alias}': {exc}[/red]")
            raise typer.Exit(1)

        record, lock = state.get("record") or {}, state.get("lock")
        console.print(f"[bold]{self.server_alias} / {self.app_name}[/bold]")
        console.print(
            f"  {'On':<38}{state.get('branch') or '(detached)'} @ {(state.get('head') or '')[:10]}  {state.get('subject') or ''}"
        )
        if record:
            staged = record.get("staged")
            what = f", {len(staged)} staged file(s)" if staged is not None else ""
            console.print(
                f"  {'Last deploy':<38}{record.get('deployer')} at {record.get('deployed_at')} "
                f"({record.get('branch') or '(detached)'} @ {str(record.get('base'))[:10]}{what})"
            )
        else:
            console.print(f"  {'Last deploy':<38}none recorded")
        if lock:
            console.print(f"  [yellow]{'Deploy lock':<38}held by {lock.get('owner')} since {lock.get('started')}[/yellow]")

        self._section("Changes to be committed (staged)", state["staged"], lambda c: f"{c[0]}  {c[1]}")
        self._section("Changes not staged", state["unstaged"], lambda c: f"{c[0]}  {c[1]}")
        self._section("Untracked files", state["untracked"])
        if state["drifted"]:
            console.print(f"  [yellow]{'Deployed files changed since deploy':<38}{len(state['drifted'])}[/yellow]")
            if self.files:
                for path in state["drifted"]:
                    console.print(f"      {path}")
        if not self.files and (state["staged"] or state["unstaged"] or state["untracked"]):
            console.print("  [dim](add --files to list them)[/dim]")
