"""Execution engine: shared runner contract plus local subprocess and remote
SSH implementations.

Commands are plain shell strings rather than argv lists. Remote lifecycle
hooks rely on real shell semantics (&&, pipes, env vars), so the runner
interface standardizes on strings and lets each concrete implementation
decide how to execute them safely for its own transport.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from types import TracebackType

import paramiko
from fabric import Connection


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
        """Run a shell command locally, streaming stdout and stderr to the console."""
        try:
            proc = subprocess.Popen(
                shlex.split(command),
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


def _build_ssm_proxy_command(
    instance_id: str,
    port: int,
    aws_profile: str | None,
    aws_region: str | None,
) -> str:
    """Build the `aws ssm start-session` command line used as an SSH ProxyCommand.

    paramiko.ProxyCommand shlex-splits this string itself (it does not go
    through a shell), so it is built with shlex.join for a safe, exact
    round-trip rather than hand-rolled string interpolation.
    """
    parts = [
        "aws", "ssm", "start-session",
        "--target", instance_id,
        "--document-name", "AWS-StartSSHSession",
        "--parameters", f"portNumber={port}",
    ]
    if aws_profile:
        parts += ["--profile", aws_profile]
    if aws_region:
        parts += ["--region", aws_region]
    return shlex.join(parts)


def _proxy_failure_detail(sock: object | None) -> str:
    """Best-effort extraction of the AWS CLI's stderr when an SSM-tunneled
    connection fails, so IAM/agent-offline errors surface instead of a bare
    'Error reading SSH protocol banner'.
    """
    process = getattr(sock, "process", None)
    if process is None or process.poll() is None:
        return ""
    try:
        stderr_output = process.stderr.read()
    except Exception:
        return ""
    text = stderr_output.decode(errors="replace").strip() if stderr_output else ""
    return f" AWS CLI reported: {text}" if text else ""


class RemoteRunner(Runner):
    """Runs commands on a remote host over SSH, streaming output in real-time.

    Supports two transports: a direct TCP connection (connection_type="ssh")
    or an SSH session tunneled through an AWS SSM port-forwarding session
    (connection_type="ssm", built via `via_ssm`). Both go through the same
    Fabric Connection underneath, so `run`/`put`/`close` behave identically
    regardless of which one was used.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str | None = None,
        key_path: str | None = None,
        proxy_command: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self._proxy_sock: paramiko.ProxyCommand | None = None

        connect_kwargs: dict = {}
        if password:
            connect_kwargs["password"] = password
        if key_path:
            connect_kwargs["key_filename"] = key_path

        if proxy_command:
            try:
                self._proxy_sock = paramiko.ProxyCommand(proxy_command)
            except FileNotFoundError as exc:
                raise BenchOpsConnectionError(
                    "Could not launch the AWS CLI for the SSM tunnel. Ensure the AWS "
                    "CLI v2 and the Session Manager plugin are installed and on PATH."
                ) from exc
            connect_kwargs["sock"] = self._proxy_sock

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
        """Build a RemoteRunner whose SSH traffic is tunneled through an AWS
        SSM session instead of a direct TCP connection to the host."""
        proxy_command = _build_ssm_proxy_command(instance_id, port, aws_profile, aws_region)
        return cls(
            host=instance_id,
            port=port,
            user=user,
            password=password,
            key_path=key_path,
            proxy_command=proxy_command,
        )

    def _connection_error(self, action: str, exc: Exception) -> BenchOpsConnectionError:
        detail = _proxy_failure_detail(self._proxy_sock)
        return BenchOpsConnectionError(
            f"Failed to {action} on {self.user}@{self.host}:{self.port}: {exc}.{detail}"
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
        """Close the SSH connection (and, if tunneled, the underlying SSM session)."""
        self.connection.close()
