"""Execution engine: shared runner contract plus local subprocess and remote
SSH implementations.

Commands are plain shell strings rather than argv lists. Remote lifecycle
hooks rely on real shell semantics (&&, pipes, env vars), so the runner
interface standardizes on strings and lets each concrete implementation
decide how to execute them safely for its own transport.
"""

from __future__ import annotations

import platform
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from types import TracebackType

import paramiko
from fabric import Connection

from benchops.ssm import SsmError, SsmTunnel, session_for

# Keeps long, silent remote commands (bench migrate) from being cut off by
# NAT/firewall idle timeouts or the SSM session's idle timeout.
SSH_KEEPALIVE_SECONDS = 30


class BenchOpsConnectionError(Exception):
    """Raised when a runner's transport cannot execute a command or transfer a
    file — a failed SSH connection, an OS-level failure launching a local
    process, or a failed SSM tunnel/subprocess."""


class BenchOpsCommandError(Exception):
    """Raised by Runner.capture() when the command itself ran (the transport
    was fine) but exited non-zero. Carries the captured output so callers
    can decide how to surface it, rather than only getting a return code.
    """

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"Command exited with code {exit_code}")


class Runner(ABC):
    """Common contract for anything that can execute commands and transfer
    files, whether that's a local subprocess, a direct SSH connection, or a
    connection brokered through an AWS SSM session."""

    @abstractmethod
    def run(self, command: str, cwd: str | None = None, interactive: bool = False) -> None:
        """Execute a shell command, streaming output to the console.

        `interactive=True` is for long-running, foreground commands that
        don't exit on their own (e.g. `tail -f`) and that a local Ctrl+C
        should be able to stop. For RemoteRunner this allocates a remote
        pty so Ctrl+C is delivered as a real SIGINT on the far end, and
        tolerates a non-zero exit (the expected result of being
        interrupted) instead of raising for it.

        Raises BenchOpsConnectionError if the transport itself fails.
        """

    @abstractmethod
    def capture(self, command: str, cwd: str | None = None) -> str:
        """Execute a command and return its captured stdout, without
        streaming it live — for callers that need to parse or post-process
        the output rather than mirror it to the terminal.

        Raises BenchOpsConnectionError if the transport itself fails, or
        BenchOpsCommandError (with stdout/stderr attached) if the command
        ran but exited non-zero.
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

    def run(self, command: str, cwd: str | None = None, interactive: bool = False) -> None:
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

        `interactive` is accepted for interface parity with RemoteRunner but
        is a no-op here: a local child process already receives Ctrl+C
        (SIGINT) directly from the terminal's process group, with no relay
        needed.
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

    def capture(self, command: str, cwd: str | None = None) -> str:
        """Run a shell command locally and return its captured stdout."""
        is_windows = platform.system() == "Windows"
        try:
            result = subprocess.run(
                command if is_windows else shlex.split(command),
                shell=is_windows,
                capture_output=True,
                text=True,
                cwd=cwd,
            )
        except OSError as exc:
            raise BenchOpsConnectionError(f"Failed to execute local command '{command}': {exc}") from exc

        if result.returncode != 0:
            raise BenchOpsCommandError(result.returncode, result.stdout, result.stderr)
        return result.stdout

    def put(self, local_path: str, remote_path: str) -> None:
        """Copy a file locally. Kept for interface parity with RemoteRunner."""
        try:
            dest = Path(remote_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest)
        except OSError as exc:
            raise BenchOpsConnectionError(f"Failed to copy '{local_path}' to '{remote_path}': {exc}") from exc


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
        self._tunnel: SsmTunnel | None = None

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
        server_config: dict,
        password: str | None = None,
        key_path: str | None = None,
    ) -> "RemoteRunner":
        """Build a RemoteRunner whose SSH traffic rides a Session Manager
        port-forwarding tunnel to the server's sshd port on its instance."""
        instance_id = server_config["instance_id"]
        try:
            tunnel = SsmTunnel(session_for(server_config), instance_id, int(server_config.get("port", 22))).open()
        except SsmError as exc:
            raise BenchOpsConnectionError(str(exc)) from exc

        runner = cls(
            host="127.0.0.1",
            port=tunnel.local_port,
            user=server_config["user"],
            password=password,
            key_path=key_path,
            display_target=f"{instance_id} (via SSM)",
        )
        runner._tunnel = tunnel
        return runner

    def _connection_error(self, action: str, exc: Exception) -> BenchOpsConnectionError:
        detail = ""
        if self._tunnel is not None and not self._tunnel.is_alive():
            detail = f" The SSM tunnel closed: {self._tunnel.output() or 'no output'}"
        return BenchOpsConnectionError(
            f"Failed to {action} on {self.user}@{self.display_target}: {exc}.{detail}"
        )

    def _ensure_open(self) -> None:
        """Connect on first use and turn on SSH keepalives."""
        if not self.connection.is_connected:
            self.connection.open()
            self.connection.transport.set_keepalive(SSH_KEEPALIVE_SECONDS)

    def run(self, command: str, cwd: str | None = None, interactive: bool = False) -> None:
        """Run a command on the remote host, streaming output to the terminal.

        With `interactive=True`, a remote pty is allocated. Invoke's runner
        deliberately does not let a local Ctrl+C stop a plain (non-pty)
        blocking run() — it just forwards the interrupt byte to the remote
        process's stdin and keeps waiting, which never actually stops a
        command like `tail -f` that doesn't read stdin. With a real remote
        pty, that same forwarded byte is interpreted by the remote tty line
        discipline as SIGINT, which does stop it — so Ctrl+C works as
        expected. `warn=True` goes with it: being interrupted is the
        expected/successful outcome here, not a failure to raise on.
        """
        full_command = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        try:
            self._ensure_open()
            self.connection.run(full_command, hide=False, pty=interactive, warn=interactive)
        except (paramiko.ssh_exception.SSHException, OSError) as exc:
            raise self._connection_error("connect", exc) from exc

    def capture(self, command: str, cwd: str | None = None) -> str:
        """Run a command on the remote host and return its captured stdout,
        without echoing it to the local terminal."""
        full_command = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        try:
            self._ensure_open()
            result = self.connection.run(full_command, hide=True, warn=True)
        except (paramiko.ssh_exception.SSHException, OSError) as exc:
            raise self._connection_error("connect", exc) from exc

        if result.exited != 0:
            raise BenchOpsCommandError(result.exited, result.stdout, result.stderr)
        return result.stdout

    def put(self, local_path: str, remote_path: str) -> None:
        """Transfer a local file to the remote host over SFTP."""
        try:
            self._ensure_open()
            self.connection.put(local_path, remote_path)
        except (paramiko.ssh_exception.SSHException, OSError) as exc:
            raise self._connection_error("transfer a file", exc) from exc

    def close(self) -> None:
        """Close the SSH connection and, if tunneled, terminate the SSM session."""
        self.connection.close()
        if self._tunnel is not None:
            self._tunnel.close()
