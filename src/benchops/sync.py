"""Sync engine: deploy package building and the client for the remote agent.

A deploy package is a single .tar.gz uploaded per deploy:

    meta.json          what the package carries (bundle ref, ...)
    snapshot.bundle    git objects the remote is missing (optional)
    dist/              the app's locally built public/dist (optional)
    manifests.json     the app's entries from sites/assets/assets*.json
"""

import hashlib
import json
import shlex
import tarfile
from pathlib import Path

from benchops.remote_agent import RESULT_MARKER
from benchops.runner import BenchOpsCommandError, Runner

AGENT_SOURCE_PATH = Path(__file__).with_name("remote_agent.py")


class RemoteAgentError(Exception):
    """An expected failure reported by the remote agent (lock held, ...)."""

    def __init__(self, error: str, data: dict | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.data = data or {}


class RemoteAgent:
    """Runs remote_agent.py commands on the server under the bench's Python."""

    def __init__(self, runner: Runner, bench_path: str) -> None:
        self.runner = runner
        self.bench_path = bench_path
        self._source = AGENT_SOURCE_PATH.read_text()

    def call(self, command: str, **args) -> dict:
        cmd = (
            f"./env/bin/python -c {shlex.quote(self._source)} "
            f"{command} {shlex.quote(json.dumps(args))}"
        )
        try:
            output = self.runner.capture(cmd, cwd=self.bench_path)
        except BenchOpsCommandError as exc:
            detail = (exc.stderr or exc.stdout).strip()
            raise RemoteAgentError(f"remote {command} crashed: {detail}") from exc

        for line in reversed(output.splitlines()):
            if line.startswith(RESULT_MARKER):
                result = json.loads(line[len(RESULT_MARKER):])
                break
        else:
            raise RemoteAgentError(f"remote {command} returned no result: {output.strip()}")

        if not result.pop("ok"):
            raise RemoteAgentError(result.pop("error"), result)
        return result


def dist_digest(dist_dir: Path) -> str | None:
    """Content hash of a build output directory, or None if it doesn't exist."""
    if not dist_dir.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in dist_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(dist_dir).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _neutral_owner(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    """Don't carry the developer's uid/username to the server."""
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = ""
    return tarinfo


def build_package(
    output_path: Path,
    meta: dict,
    bundle_path: Path | None = None,
    dist_dir: Path | None = None,
    manifests: dict | None = None,
) -> Path:
    """Write the deploy package and return its path."""
    staging = output_path.parent
    (staging / "meta.json").write_text(json.dumps(meta))
    if manifests is not None:
        (staging / "manifests.json").write_text(json.dumps(manifests))

    with tarfile.open(output_path, mode="w:gz") as tar:
        tar.add(staging / "meta.json", arcname="meta.json", filter=_neutral_owner)
        if bundle_path is not None:
            tar.add(bundle_path, arcname="snapshot.bundle", filter=_neutral_owner)
        if dist_dir is not None:
            tar.add(dist_dir, arcname="dist", filter=_neutral_owner)
            tar.add(staging / "manifests.json", arcname="manifests.json", filter=_neutral_owner)
    return output_path
