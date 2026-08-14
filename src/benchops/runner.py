"""Execution engine: local subprocess and remote SSH command runners."""

import subprocess

import paramiko
from fabric import Connection


class RemoteConnectionError(Exception):
    """Raised when an SSH connection to a remote host cannot be established."""


class LocalRunner:
    """Runs commands locally, streaming output to the terminal in real-time."""

    def run(self, command: list[str], cwd: str | None = None) -> None:
        """Run a command locally, streaming stdout and stderr to the console."""
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
        )
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            print(line, end="")
        returncode = proc.wait()
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, command)


class RemoteRunner:
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
        try:
            if cwd:
                with self.connection.cd(cwd):
                    self.connection.run(command, hide=False)
            else:
                self.connection.run(command, hide=False)
        except (paramiko.ssh_exception.SSHException, OSError) as exc:
            raise RemoteConnectionError(
                f"Failed to connect to {self.user}@{self.host}:{self.port}: {exc}"
            ) from exc

    def close(self) -> None:
        """Close the SSH connection."""
        self.connection.close()
