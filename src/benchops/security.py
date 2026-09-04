"""Cross-platform helpers for locking down sensitive local files.

POSIX filesystems use permission bits (chmod); NTFS uses ACLs. There is no
chmod equivalent on Windows — os.chmod() there only toggles the read-only
attribute and never raises, silently providing none of the access
restriction the POSIX call assumes. secure_file() gives both platforms a
real, correctly-failing lockdown instead of a Windows no-op.
"""

import os
import platform
import shutil
import subprocess
from pathlib import Path

from benchops.runner import BenchOpsConnectionError


def secure_file(path: Path, mode: int) -> None:
    """Restrict a file or directory to the current user only.

    POSIX: chmod to `mode`. Windows: strip inherited ACEs and grant Full
    Control to the current user only, via icacls.
    """
    if platform.system() == "Windows":
        _secure_windows(path)
    else:
        path.chmod(mode)


def _secure_windows(path: Path) -> None:
    username = os.environ.get("USERNAME")
    if not username:
        raise BenchOpsConnectionError(
            f"Could not determine the current Windows user to secure '{path}' "
            "(the USERNAME environment variable is not set)."
        )

    icacls = shutil.which("icacls")
    if icacls is None:
        raise BenchOpsConnectionError(
            f"Could not secure '{path}': the 'icacls' utility was not found on PATH."
        )

    try:
        result = subprocess.run(
            [icacls, str(path), "/inheritance:r", "/grant:r", f"{username}:F"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise BenchOpsConnectionError(f"Failed to run icacls on '{path}': {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BenchOpsConnectionError(f"icacls failed to secure '{path}': {detail}")
