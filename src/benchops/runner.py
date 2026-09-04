"""Execution engine: shared runner contract plus local subprocess and remote
SSH implementations.

Commands are plain shell strings rather than argv lists. Remote lifecycle
hooks rely on real shell semantics (&&, pipes, env vars), so the runner
interface standardizes on strings and lets each concrete implementation
decide how to execute them safely for its own transport.
"""

from __future__ import annotations

import json
import platform
import shlex
import shutil
import socket
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from types import TracebackType

import paramiko
from fabric import Connection

SSM_TUNNEL_READY_TIMEOUT_SECONDS = 15
SSM_TUNNEL_POLL_INTERVAL_SECONDS = 0.3
SSM_TUNNEL_TERMINATE_TIMEOUT_SECONDS = 5


class BenchOpsConnectionError(Exception):
    """Raised when a runner's transport cannot execute a command or transfer a
    file — a failed SSH connection, an OS-level failure launching a local
    process, or a failed SSM tunnel/subprocess."""


class Runner(ABC):
    """Common contract for anything that can execute commands and transfer
    files, whether that's a local subprocess, a direct SSH connection, or a
    connection brokered through an AWS SSM session."""

    @abstractmethod
    def run(self, command: str, cwd: str | None = None) -> None:
        """Execute a shell command, streaming output to the console.

        Raises BenchOpsConnectionError if the transport itself fails.
        """

    @abstractmethod
    def put(self, local_path: str, remote_path: str) -> None:
        """Transfer a local file to `remote_path`.

        Raises BenchOpsConnectionError if the transport itself fails.
        """

    def close(self) -> None:
        """Release any held resources. No-op by default for stateless runners."""

    def __enter__(self) -> "Runner":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class LocalRunner(Runner):
    """Runs commands locally, streaming output to the terminal in real-time."""

    def run(self, command: str, cwd: str | None = None) -> None:
        """Run a shell command locally, streaming stdout and stderr to the console.

        On POSIX the command is shlex-split and run without an intermediate
        shell (shell=False) — no shell operators, but no shell-injection
        surface either. On Windows, shlex's POSIX parsing rules strip
        backslashes from ordinary paths (e.g. "C:\\repo\\app" becomes
        "C:repoapp"), and there's no way to get cmd.exe builtins, %VAR%
        expansion, or "&&" chaining out of a bare argv list — so the command
        is handed to cmd.exe via shell=True instead. This is safe here
        because `command` always comes from the operator's own config.toml
        hooks, never from untrusted input.
        """
        is_windows = platform.system() == "Windows"
        try:
            proc = subprocess.Popen(
                command if is_windows else shlex.split(command),
                shell=is_windows,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd,
            )
        except OSError as exc:
            raise BenchOpsConnectionError(f"Failed to execute local command '{command}': {exc}") from exc

        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            print(line, end="")
        returncode = proc.wait()
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, command)

    def put(self, local_path: str, remote_path: str) -> None:
        """Copy a file locally. Kept for interface parity with RemoteRunner."""
        try:
            dest = Path(remote_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest)
        except OSError as exc:
            raise BenchOpsConnectionError(f"Failed to copy '{local_path}' to '{remote_path}': {exc}") from exc


def _require_ssm_binaries() -> None:
    """Fail fast, with an actionable message, if the SSM tunnel's external
    dependencies aren't resolvable on PATH — rather than letting a bare
    FileNotFoundError surface from deep inside subprocess.Popen (which is
    especially unhelpful on Windows, where a missing .exe/.cmd shim and a
    missing binary look identical from the caller's side).
    """
    missing = [name for name in ("aws", "session-manager-plugin") if shutil.which(name) is None]
    if missing:
        raise BenchOpsConnectionError(
            "Missing required tool(s) for SSM connections: " + ", ".join(missing) + ". "
            "Install the AWS CLI v2 and the Session Manager plugin, and ensure both are on PATH."
        )


def _find_free_local_port() -> int:
    """Ask the OS for an ephemeral, currently-unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _build_ssm_port_forward_command(
    instance_id: str,
    remote_port: int,
    local_port: int,
    aws_profile: str | None,
    aws_region: str | None,
) -> list[str]:
    """Build the argv for an `aws ssm start-session` port-forwarding session
    from a local port to `remote_port` on the target instance itself.

    Uses AWS-StartPortForwardingSession, not the *ToRemoteHost variant: the
    latter forwards through the target instance to a *different* host/service
    (e.g. an RDS endpoint reached via a bastion) and needs a "host" parameter
    that doesn't apply here — we're forwarding to sshd on the same instance
    named by --target, which is exactly what the plain document is for.
    """
    parts = [
        "aws", "ssm", "start-session",
        "--target", instance_id,
        "--document-name", "AWS-StartPortForwardingSession",
        "--parameters", json.dumps({
            "portNumber": [str(remote_port)],
            "localPortNumber": [str(local_port)],
        }),
    ]
    if aws_profile:
        parts += ["--profile", aws_profile]
    if aws_region:
        parts += ["--region", aws_region]
    return parts


def _wait_for_tunnel(process: subprocess.Popen, port: int) -> None:
    """Block until the local end of the port-forwarding tunnel is accepting
    connections, or raise with the AWS CLI's own diagnostic if it exits
    early (missing IAM permission, offline SSM agent, bad instance ID, ...).
    """
    deadline = time.monotonic() + SSM_TUNNEL_READY_TIMEOUT_SECONDS
    last_error: OSError | None = None

    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            stderr = (process.stderr.read() if process.stderr else "") or ""
            raise BenchOpsConnectionError(
                f"The SSM port-forwarding session exited early (code {exit_code}): {stderr.strip()}"
            )

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(SSM_TUNNEL_POLL_INTERVAL_SECONDS)

    raise BenchOpsConnectionError(
        f"Timed out waiting for the SSM tunnel on 127.0.0.1:{port} to become ready: {last_error}"
    )


def _terminate_process(process: subprocess.Popen) -> None:
    """Cross-platform process teardown: Popen.terminate()/.kill() dispatch to
    SIGTERM/TerminateProcess correctly per OS without any extra code here.
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=SSM_TUNNEL_TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=SSM_TUNNEL_TERMINATE_TIMEOUT_SECONDS)


class RemoteRunner(Runner):
    """Runs commands on a remote host over SSH, streaming output in real-time.

    Supports two transports: a direct TCP connection (connection_type="ssh")
    or an SSH session carried over a local TCP port that AWS SSM forwards to
    the target instance (connection_type="ssm", built via `via_ssm`). Both
    end up as an ordinary Fabric Connection over real sockets, so `run`/
    `put`/`close` behave identically regardless of which one was used —
    and, notably, this avoids paramiko.ProxyCommand, whose reliance on
    select() over a subprocess pipe (rather than a socket) does not work on
    Windows at all.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str | None = None,
        key_path: str | None = None,
        display_target: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.display_target = display_target or f"{host}:{port}"
        self._tunnel_process: subprocess.Popen | None = None

        connect_kwargs: dict = {}
        if password:
            connect_kwargs["password"] = password
        if key_path:
            connect_kwargs["key_filename"] = key_path

        self.connection = Connection(
            host=host,
            port=port,
            user=user,
            connect_kwargs=connect_kwargs,
        )

    @classmethod
    def via_ssm(
        cls,
        instance_id: str,
        user: str,
        port: int = 22,
        password: str | None = None,
        key_path: str | None = None,
        aws_profile: str | None = None,
        aws_region: str | None = None,
    ) -> "RemoteRunner":
        """Build a RemoteRunner whose SSH traffic rides a local TCP port that
        `aws ssm start-session` forwards to `port` on the target instance."""
        _require_ssm_binaries()

        local_port = _find_free_local_port()
        argv = _build_ssm_port_forward_command(instance_id, port, local_port, aws_profile, aws_region)

        try:
            tunnel_process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise BenchOpsConnectionError(
                f"Could not launch the AWS CLI for the SSM tunnel to '{instance_id}': {exc}"
            ) from exc

        try:
            _wait_for_tunnel(tunnel_process, local_port)
        except BenchOpsConnectionError:
            _terminate_process(tunnel_process)
            raise

        runner = cls(
            host="127.0.0.1",
            port=local_port,
            user=user,
            password=password,
            key_path=key_path,
            display_target=f"{instance_id} (via SSM tunnel on 127.0.0.1:{local_port})",
        )
        runner._tunnel_process = tunnel_process
        return runner

    def _connection_error(self, action: str, exc: Exception) -> BenchOpsConnectionError:
        detail = ""
        if self._tunnel_process is not None and self._tunnel_process.poll() is not None:
            stderr = (self._tunnel_process.stderr.read() if self._tunnel_process.stderr else "") or ""
            if stderr.strip():
                detail = f" AWS CLI reported: {stderr.strip()}"
        return BenchOpsConnectionError(
            f"Failed to {action} on {self.user}@{self.display_target}: {exc}.{detail}"
        )

    def run(self, command: str, cwd: str | None = None) -> None:
        """Run a command on the remote host, streaming output to the terminal."""
        full_command = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        try:
            self.connection.run(full_command, hide=False)
        except (paramiko.ssh_exception.SSHException, OSError) as exc:
            raise self._connection_error("connect", exc) from exc

    def put(self, local_path: str, remote_path: str) -> None:
        """Transfer a local file to the remote host over SFTP."""
        try:
            self.connection.put(local_path, remote_path)
        except (paramiko.ssh_exception.SSHException, OSError) as exc:
            raise self._connection_error("transfer a file", exc) from exc

    def close(self) -> None:
        """Close the SSH connection and, if tunneled, terminate the SSM session."""
        self.connection.close()
        if self._tunnel_process is not None:
            _terminate_process(self._tunnel_process)
