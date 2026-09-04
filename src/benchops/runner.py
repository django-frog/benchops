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
    process, or (in the future) a failed SSM tunnel/subprocess."""


class Runner(ABC):
    """Common contract for anything that can execute commands and transfer
    files, whether that's a local subprocess, a direct SSH connection, or
    (later) a connection brokered through an AWS SSM session."""

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


class RemoteRunner(Runner):
    """Runs commands on a remote host over SSH, streaming output in real-time."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str | None = None,
        key_path: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
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

    def run(self, command: str, cwd: str | None = None) -> None:
        """Run a command on the remote host, streaming output to the terminal."""
        full_command = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        try:
            self.connection.run(full_command, hide=False)
        except (paramiko.ssh_exception.SSHException, OSError) as exc:
            raise BenchOpsConnectionError(
                f"Failed to connect to {self.user}@{self.host}:{self.port}: {exc}"
            ) from exc

    def put(self, local_path: str, remote_path: str) -> None:
        """Transfer a local file to the remote host over SFTP."""
        try:
            self.connection.put(local_path, remote_path)
        except (paramiko.ssh_exception.SSHException, OSError) as exc:
            raise BenchOpsConnectionError(
                f"Failed to transfer '{local_path}' to {self.user}@{self.host}:{self.port}: {exc}"
            ) from exc

    def close(self) -> None:
        """Close the SSH connection."""
        self.connection.close()
