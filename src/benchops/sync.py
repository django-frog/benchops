"""Sync engine: local compression, transfer, and remote extraction."""

import shlex
import tarfile
from pathlib import Path

from benchops.runner import Runner


def _tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = tarinfo.name.split("/")
    if any(part in (".git", "__pycache__", "node_modules") for part in parts):
        return None
    if tarinfo.name.endswith(".pyc"):
        return None
    return tarinfo


def create_tarball(app_path: str, output_path: str) -> str:
    """Create a .tar.gz archive of a local directory, excluding common junk."""
    src = Path(app_path)
    if not src.is_dir():
        raise FileNotFoundError(f"Application directory not found: {app_path}")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, mode="w:gz") as tar:
        tar.add(src, arcname=src.name, filter=_tar_filter)
    return str(out.resolve())


def transfer_tarball(runner: Runner, local_path: str, remote_dest_dir: str) -> str:
    """Transfer a local tarball to a remote directory."""
    runner.put(local_path, remote_dest_dir)
    return f"{remote_dest_dir.rstrip('/')}/{Path(local_path).name}"


def extract_and_cleanup(runner: Runner, remote_tar_path: str, remote_extract_dir: str) -> None:
    """Extract a remote tarball and remove it afterwards."""
    tar_path = shlex.quote(remote_tar_path)
    extract_dir = shlex.quote(remote_extract_dir)
    runner.run(f"tar -xzf {tar_path} -C {extract_dir}")
    runner.run(f"rm {tar_path}")
