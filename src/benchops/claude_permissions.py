"""Permission rules for Claude Code, merged into the user's settings.json.

`benchops skill install` adds these rules to Claude Code's user settings so
that, whatever the skill says, Claude Code itself:

  * never runs the dangerous flags (deny: blocked in every mode),
  * asks the developer before anything changes a server (ask: prompts even
    in auto and bypass modes; in `claude -p` such calls are denied),
  * runs read-only commands without prompting (allow).

Bash rules match the command text, so they stop mistakes, not someone
working around them on purpose (`bash -c ...`); see the Claude Code
permissions docs.

The merge only ever adds rules and removes rules it added itself: which ones
those are is recorded in ~/.benchops/claude-permissions.json, per settings
file. Rules a developer wrote are never changed or removed, even when they
are identical to one of ours.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from benchops.config import CONFIG_PATH

KINDS = ("deny", "ask", "allow")

RULES: dict[str, list[str]] = {
    "deny": [
        "Bash(benchops * --force*)",
        "Bash(benchops * --break-lock*)",
        "Bash(benchops restore *)",
    ],
    "ask": [
        "Bash(benchops deploy * --yes*)",
        "Bash(benchops deploy * -y*)",
        "Bash(benchops sync * --yes*)",
        "Bash(benchops sync * -y*)",
        "Bash(benchops * --overwrite*)",
        "Bash(benchops execute *)",
        "Bash(benchops install *)",
        "Bash(benchops uninstall *)",
        "Bash(benchops server add*)",
        "Bash(benchops server remove *)",
        "Bash(benchops server set-auth *)",
        "Bash(benchops server clear-hooks *)",
        "Bash(benchops server edit-hooks *)",
        "Bash(benchops local *)",
        "Bash(benchops auth *)",
        "Bash(benchops backup source *)",
        "Bash(benchops skill install*)",
        "Bash(benchops skill uninstall*)",
    ],
    "allow": [
        "Bash(benchops status *)",
        "Bash(benchops deploy * --plan-only*)",
        "Bash(benchops sync * --plan-only*)",
        "Bash(benchops logs * --lines*)",
        "Bash(benchops server list*)",
        "Bash(benchops server check *)",
        "Bash(benchops backup list *)",
        "Bash(benchops version*)",
        "Bash(benchops --version*)",
        "Bash(benchops skill status*)",
    ],
}

STATE_PATH = CONFIG_PATH.parent / "claude-permissions.json"
BACKUP_SUFFIX = ".benchops-backup"


class SettingsError(Exception):
    """settings.json can't be read or updated safely; it was left untouched."""


@dataclass
class Change:
    path: Path
    added: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)
    present: int = 0  # our rules now in the file
    backup: Path | None = None

    @property
    def changed(self) -> bool:
        return any(self.added.values()) or any(self.removed.values())


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"{path} isn't valid JSON ({exc}); it was left unchanged.") from exc
    if not isinstance(settings, dict):
        raise SettingsError(f"{path} doesn't hold a JSON object; it was left unchanged.")
    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict) or any(
        not isinstance(permissions.get(kind, []), list) for kind in KINDS
    ):
        raise SettingsError(f"'permissions' in {path} isn't in the expected shape; it was left unchanged.")
    return settings


def _load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(state: dict) -> None:
    atomic_write(STATE_PATH, json.dumps(state, indent=2) + "\n")


def _default_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def atomic_write(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings atomically, keeping an existing file's mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else _default_mode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_settings(path: Path, settings: dict, change: Change) -> None:
    # A settings.json symlinked from a dotfiles repo stays a symlink: write
    # through it rather than replacing the link with a plain file.
    path = path.resolve() if path.is_symlink() else path
    if path.exists():
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        shutil.copy2(path, backup)
        change.backup = backup
    atomic_write(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


def _apply(path: Path, wanted: dict[str, list[str]]) -> Change:
    """Make our rules in `path` exactly `wanted`: add what's missing, remove
    what we added earlier and no longer want, never touch anyone else's."""
    settings = _load_settings(path)
    state = _load_state()
    key = str(path)
    owned = {kind: list(state.get(key, {}).get(kind, [])) for kind in KINDS}
    permissions = settings.get("permissions", {})
    change = Change(path)
    now_owned: dict[str, list[str]] = {}

    for kind in KINDS:
        rules = list(permissions.get(kind, []))
        drop = [r for r in owned[kind] if r not in wanted.get(kind, []) and r in rules]
        rules = [r for r in rules if r not in drop]
        add = [r for r in wanted.get(kind, []) if r not in rules]
        rules += add
        change.added[kind], change.removed[kind] = add, drop
        # Ours: what we just added, plus what we added before and still want.
        now_owned[kind] = [r for r in wanted.get(kind, []) if r in add or r in owned[kind]]
        change.present += sum(1 for r in wanted.get(kind, []) if r in rules)
        if rules:
            permissions[kind] = rules
        else:
            permissions.pop(kind, None)

    if change.changed:
        if permissions:
            settings["permissions"] = permissions
        else:
            settings.pop("permissions", None)
        _write_settings(path, settings, change)

    if any(now_owned.values()):
        state[key] = now_owned
    else:
        state.pop(key, None)
    if state != _load_state():
        _save_state(state)
    return change


def install(settings_path: Path) -> Change:
    return _apply(settings_path, RULES)


def uninstall(settings_path: Path) -> Change:
    return _apply(settings_path, {})


def missing(settings_path: Path) -> list[str] | None:
    """Our rules that aren't in the settings file (None: it can't be read)."""
    try:
        permissions = _load_settings(settings_path).get("permissions", {})
    except SettingsError:
        return None
    return [r for kind in KINDS for r in RULES[kind] if r not in permissions.get(kind, [])]


def snippet() -> str:
    """The rules as a settings.json fragment, for pasting by hand."""
    return json.dumps({"permissions": RULES}, indent=2)
