"""`benchops status`: what a server runs for an app, without logging in.

Read-only — it never takes the deploy lock or changes anything. It shows
the server's branch and commit, the last deploy, the pending drafts grouped
by label and developer (flagging stale ones and ones edited on the server
since), drafts your own machine has already committed (so a `benchops sync`
would clear them), and hand edits made directly on the server.
"""

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from benchops.base import BaseCommand
from benchops.gitlocal import GitError, LocalRepo
from benchops.runner import BenchOpsConnectionError
from benchops.sync import RemoteAgent, RemoteAgentError

console = Console()

STALE_AFTER_DAYS = 7
WIDTH = 38


def age_in_days(stamp: str | None, now: datetime | None = None) -> int | None:
    try:
        deployed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None
    return ((now or datetime.now(timezone.utc)) - deployed).days


def describe_age(days: int | None) -> str:
    if days is None:
        return "unknown age"
    if days == 0:
        return "today"
    return "1 day ago" if days == 1 else f"{days} days ago"


class StatusCommand(BaseCommand):
    """Prints the server-side state of one app."""

    def __init__(self, server_alias: str, app_name: str, files: bool = False) -> None:
        super().__init__(server_alias=server_alias, app_name=app_name)
        self.files = files

    def _local_blobs(self) -> dict[str, str] | None:
        """Files in this machine's HEAD, if the app is checked out here."""
        for candidate in (Path.cwd() / "apps" / self.app_name, Path.cwd() / self.app_name):
            if candidate.is_dir():
                try:
                    return LocalRepo.open(candidate).blobs("HEAD")
                except GitError:
                    return None
        return None

    def _line(self, title: str, value: str, style: str | None = None) -> None:
        text = f"  {title:<{WIDTH}}{value}"
        console.print(f"[{style}]{text}[/{style}]" if style else text)

    def _paths(self, paths) -> None:
        if self.files:
            for path in paths:
                console.print(f"      {path}")

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
        self._line("On", f"{state.get('branch') or '(detached)'} @ {(state.get('head') or '')[:10]}  {state.get('subject') or ''}")
        if record:
            staged = record.get("staged")
            what = f", {len(staged)} staged file(s)" if staged is not None else ""
            label = f" [{record['label']}]" if record.get("label") else ""
            self._line(
                "Last deploy",
                f"{record.get('deployer')} at {record.get('deployed_at')} "
                f"({record.get('mode', 'deploy')}{label}, {record.get('branch') or '(detached)'} @ "
                f"{str(record.get('base'))[:10]}{what})",
            )
        else:
            self._line("Last deploy", "none recorded")
        if lock:
            self._line("Deploy lock", f"held by {lock.get('owner')} since {lock.get('started')}", "yellow")

        drafts = state.get("drafts", [])
        console.print()
        if drafts:
            console.print("  [bold]Pending drafts (deployed, not committed yet):[/bold]")
            groups = defaultdict(list)
            for draft in drafts:
                groups[(draft.get("label") or "(no label)", draft.get("name") or draft.get("owner"))].append(draft)
            for (label, name), items in sorted(groups.items()):
                days = max((age_in_days(d.get("deployed_at")) or 0) for d in items)
                stale = days >= STALE_AFTER_DAYS
                summary = f"{name:<20} {len(items)} file(s)   {describe_age(days)}" + ("   ⚠ stale" if stale else "")
                self._line(f"  {label}", summary, "yellow" if stale else None)
                self._paths(d["path"] + ("   (edited on staging since)" if d["edited"] else "") for d in items)
        else:
            self._line("Pending drafts", "none")

        local = self._local_blobs()
        if local is not None:
            committed = [d["path"] for d in drafts if local.get(d["path"]) == d["blob"]]
            if committed:
                self._line("Committed on your machine", f"{len(committed)} file(s) — run 'benchops sync'", "green")
                self._paths(committed)

        edited = [d["path"] for d in drafts if d["edited"]]
        if edited:
            self._line("Drafts edited on staging since", f"{len(edited)} file(s)", "yellow")
        hand_edits = state.get("hand_edits", [])
        self._line("Hand edits on staging", f"{len(hand_edits)} file(s)")
        self._paths(hand_edits)

        self._line(
            "Server git status",
            f"{len(state['staged'])} staged, {len(state['unstaged'])} not staged, {len(state['untracked'])} untracked",
            "dim",
        )
        if not self.files and (drafts or hand_edits):
            console.print("  [dim](add --files to list the files)[/dim]")
