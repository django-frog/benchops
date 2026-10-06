"""Installed version, latest release on PyPI, and the bundled changelog.

The update check is best-effort: one request to PyPI at most once a day
(cached in ~/.benchops/update-check.json), a short timeout, and any failure
is silent — it must never slow down or break a command. Set
BENCHOPS_NO_UPDATE_CHECK=1 to turn the passive notice off.
"""

import json
import os
import re
import time
import urllib.request
from importlib import metadata, resources
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

PACKAGE = "benchops"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CACHE_PATH = Path.home() / ".benchops" / "update-check.json"
CHECK_INTERVAL_SECONDS = 24 * 3600
CHECK_TIMEOUT_SECONDS = 2
DISABLE_ENV = "BENCHOPS_NO_UPDATE_CHECK"
UPGRADE_HINT = "uv tool upgrade benchops  (or: pip install -U benchops)"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")
_HEADING_RE = re.compile(r"^## \[([^\]]+)\]")


def installed_version() -> str:
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return "0+unknown"


def version_key(version: str) -> tuple:
    """Sort key: numeric parts, and a final release above its pre-releases
    (1.0.0rc1 < 1.0.0). Unparseable versions sort lowest."""
    match = _VERSION_RE.match(version.strip())
    if not match:
        return (-1,)
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor), int(patch), 0 if suffix else 1)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def fetch_latest(timeout: float = CHECK_TIMEOUT_SECONDS) -> str | None:
    """The latest release on PyPI, or None if it can't be determined."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as response:
            return json.load(response)["info"]["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def latest_version(force: bool = False, cache_path: Path | None = None) -> str | None:
    """The latest release, from the daily cache unless it is stale or `force`."""
    cache_path = cache_path or CACHE_PATH
    try:
        cached = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cached = {}
    if not force and time.time() - cached.get("checked_at", 0) < CHECK_INTERVAL_SECONDS:
        return cached.get("latest")

    latest = fetch_latest()
    # Record the attempt even when it failed, so an offline machine doesn't
    # retry (and wait for the timeout) on every command.
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"checked_at": time.time(), "latest": latest or cached.get("latest")}))
    except OSError:
        pass
    return latest or cached.get("latest")


def update_notice() -> str | None:
    """A one-line notice when a newer version is available, else None."""
    if os.environ.get(DISABLE_ENV):
        return None
    current, latest = installed_version(), latest_version()
    if latest and is_newer(latest, current):
        return f"benchops {latest} is available (you have {current}). Upgrade: {UPGRADE_HINT}"
    return None


def _changelog_text() -> str | None:
    try:
        return resources.files(PACKAGE).joinpath("CHANGELOG.md").read_text()
    except (FileNotFoundError, OSError):
        pass
    # Running from a source checkout, where the changelog sits at the repo root.
    source_copy = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    return source_copy.read_text() if source_copy.is_file() else None


def release_notes() -> list[tuple[str, str]]:
    """[(version, markdown body), ...] in changelog order (newest first),
    skipping sections with no notes."""
    text = _changelog_text()
    if not text:
        return []
    notes: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            notes.append((heading.group(1), []))
        elif notes:
            notes[-1][1].append(line)
    # An empty section (e.g. "Unreleased" right after a release) has nothing to show.
    return [(version, "\n".join(body).strip()) for version, body in notes if "\n".join(body).strip()]


def render_version_report(console: Console, show_all: bool = False) -> None:
    """`benchops version`: installed vs latest, and the release notes."""
    current = installed_version()
    console.print(f"[bold]benchops {current}[/bold]")

    latest = latest_version(force=True)
    if latest is None:
        console.print("[dim]Could not check PyPI for a newer version.[/dim]")
    elif is_newer(latest, current):
        console.print(f"[yellow]A newer version is available: {latest}.[/yellow]")
        console.print(f"[yellow]Upgrade: {UPGRADE_HINT}[/yellow]")
    else:
        console.print("[green]You are on the latest version.[/green]")

    notes = release_notes()
    if not show_all:
        notes = [(version, body) for version, body in notes if version == current]
        if not notes:
            console.print(f"[dim]No release notes are bundled for {current}.[/dim]")
            return
    for version, body in notes:
        console.print()
        console.print(Markdown(f"## {version}\n\n{body}"))
    if not show_all:
        console.print("\n[dim]Run 'benchops version --all' for every release.[/dim]")
