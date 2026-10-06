"""Sync engine: deploy package building and the client for the remote agent.

A deploy package is a single .tar.gz uploaded per deploy:

    meta.json          what the package carries (bundle ref, build outputs)
    snapshot.bundle    git objects the remote is missing (optional)
    build/<path>       each shipped build output, by path in the app
    manifests.json     the app's entries from sites/assets/assets*.json
"""

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


def _neutral_owner(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    """Don't carry the developer's uid/username to the server."""
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = ""
    return tarinfo


def build_package(
    output_path: Path,
    meta: dict,
    bundle_path: Path | None = None,
    app_dir: Path | None = None,
    build_outputs: list[str] | None = None,
    manifests: dict | None = None,
) -> Path:
    """Write the deploy package and return its path. Build outputs are stored
    under build/<path relative to the app> and listed in meta.json."""
    staging = output_path.parent
    meta = dict(meta, build_outputs=list(build_outputs or []))
    (staging / "meta.json").write_text(json.dumps(meta))
    (staging / "manifests.json").write_text(json.dumps(manifests or {}))

    with tarfile.open(output_path, mode="w:gz") as tar:
        tar.add(staging / "meta.json", arcname="meta.json", filter=_neutral_owner)
        tar.add(staging / "manifests.json", arcname="manifests.json", filter=_neutral_owner)
        if bundle_path is not None:
            tar.add(bundle_path, arcname="snapshot.bundle", filter=_neutral_owner)
        for rel in meta["build_outputs"]:
            tar.add(app_dir / rel, arcname=f"build/{rel}", filter=_neutral_owner)
    return output_path
