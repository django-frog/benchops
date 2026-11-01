"""`benchops skill`: install the Claude Code skill that ships with BenchOps.

The skill (skills/benchops/SKILL.md in the repo, bundled into the wheel)
teaches Claude Code how to drive benchops safely. It describes this
version's exact flags and JSON, so it is installed from the installed
package — never fetched separately — and a stale copy is pointed out.

The installed file ends with a marker recording the benchops version and a
hash of the content, so BenchOps can tell its own untouched copy (safe to
replace or remove) from one someone edited or made by hand (left alone
unless --force).
"""

import hashlib
import os
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from benchops.claude_permissions import atomic_write

SKILL_NAME = "benchops"
PACKAGE = "benchops"
_MARKER_RE = re.compile(r"\n<!-- installed by benchops (\S+) sha256=([0-9a-f]{64}) -->\n?\Z")


class SkillError(Exception):
    """The skill can't be installed or removed as asked; nothing was changed."""


def claude_dir() -> Path:
    """Claude Code's configuration directory: $CLAUDE_CONFIG_DIR, else
    ~/.claude (%USERPROFILE%\\.claude on Windows)."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def settings_path() -> Path:
    """Claude Code's user settings file."""
    return claude_dir() / "settings.json"


def skill_path() -> Path:
    return claude_dir() / "skills" / SKILL_NAME / "SKILL.md"


def bundled_skill() -> str:
    """The SKILL.md shipped with this version of benchops."""
    try:
        return resources.files(PACKAGE).joinpath("skills", SKILL_NAME, "SKILL.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        pass
    # Running from a source checkout, where the skill sits at the repo root.
    source_copy = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME / "SKILL.md"
    if source_copy.is_file():
        return source_copy.read_text(encoding="utf-8")
    raise SkillError("This benchops installation doesn't include the Claude skill; reinstall benchops.")


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def stamp(body: str, version: str) -> str:
    body = body.rstrip("\n") + "\n"
    return f"{body}\n<!-- installed by benchops {version} sha256={_digest(body)} -->\n"


@dataclass
class Installed:
    path: Path
    version: str | None  # None: not installed by benchops (no marker)
    modified: bool  # edited since benchops installed it
    symlink: bool


def inspect_installed(path: Path | None = None) -> Installed | None:
    """What is at the skill's location, or None if nothing is."""
    path = path or skill_path()
    symlink = path.is_symlink() or path.parent.is_symlink()
    if not path.exists():
        return Installed(path, None, False, True) if symlink else None
    text = path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    match = _MARKER_RE.search(text)
    if not match:
        return Installed(path, None, False, symlink)
    body = text[: match.start()]
    return Installed(path, match.group(1), _digest(body) != match.group(2), symlink)


def _refusal(found: Installed) -> str | None:
    """Why replacing or removing `found` needs --force, if it does."""
    if found.symlink:
        return f"{found.path} is a symbolic link (or inside one), probably to a checkout of the skill."
    if found.version is None:
        return f"{found.path} wasn't installed by benchops (it may be a hand-made copy)."
    if found.modified:
        return f"{found.path} was edited after benchops {found.version} installed it."
    return None


def _remove_link(found: Installed) -> None:
    """Remove a symlinked skill without touching what it points at."""
    for candidate in (found.path, found.path.parent):
        if candidate.is_symlink():
            candidate.unlink()
            return


def install(version: str, force: bool = False) -> tuple[Path, str | None]:
    """Install (or update) the bundled skill. Returns the path and the
    version it replaced, if any."""
    path = skill_path()
    found = inspect_installed(path)
    replaced = None
    if found:
        reason = _refusal(found)
        if reason and not force:
            raise SkillError(f"{reason} Re-run with --force to replace it.")
        if found.symlink:
            _remove_link(found)
        replaced = found.version or "an unmanaged copy"
    atomic_write(path, stamp(bundled_skill(), version))
    return path, replaced


def uninstall(force: bool = False) -> Path | None:
    """Remove the installed skill. Returns its path, or None if there was none."""
    path = skill_path()
    found = inspect_installed(path)
    if not found:
        return None
    reason = _refusal(found)
    if reason and not force:
        raise SkillError(f"{reason} Re-run with --force to remove it.")
    if found.symlink:
        _remove_link(found)
    else:
        path.unlink()
        try:
            path.parent.rmdir()  # only if nothing else was put there
        except OSError:
            pass
    return path


def stale_notice(version: str) -> str | None:
    """A one-line notice when the installed skill is from another benchops
    version. Never raises: it runs before every command."""
    try:
        found = inspect_installed()
    except OSError:
        return None
    if not found or found.version is None or found.version == version:
        return None
    return f"The benchops Claude skill is from {found.version} (you have {version}); run 'benchops skill install'."


TARGETS_SNIPPET = """\
## BenchOps targets
| App   | Server       | Site            | Default |
|-------|--------------|-----------------|---------|
| basma | dev-bench-02 | basma.akwad.qa  | yes     |
"""
