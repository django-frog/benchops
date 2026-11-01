"""A Runner over an AWS Session Manager shell session — no sshd, no SSH keys,
no port forwarding. It needs only the IAM permission behind a plain
`aws ssm start-session --target <instance>`: the session uses the account's
standard shell document, so it runs as whatever user the Session Manager
preferences say (RunAs) and starts the way they say (shell profile).

A shell session is a terminal, not a pipe: it echoes input, rewrites \\n as
\\r\\n, and the shell prints prompts and escape codes around everything. So
the runner:

* quiets the shell first (no echo, no prompts, no line editing);
* frames every command between markers that the remote shell assembles
  with printf, so the echo of the typed command can never match them;
* moves payloads — the command itself, uploaded files, captured output —
  as base64 in short lines, which a terminal passes through untouched and
  which keeps every typed line far below the terminal's line limit.

Commands run as the server's configured `user`: directly when the session
already is that user, otherwise through `sudo -n -u <user>`.
"""

from __future__ import annotations

import base64
import codecs
import gzip
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time

import boto3

from benchops.runner import BenchOpsCommandError, BenchOpsConnectionError, Runner
from benchops.ssm import PLUGIN, PLUGIN_ISOLATION, SsmError, end_session, session_for, start_session

READY_TIMEOUT_SECONDS = 45
READY_PROBE_INTERVAL_SECONDS = 1.5
STEP_TIMEOUT_SECONDS = 30

# Run before anything else: no echo, no \n -> \r\n, no line editing (whose
# own echo and prompts would mix into the output), no history, and a SIGINT
# trap so Ctrl+C stops the running command but not the helper around it.
_QUIET = (
    "stty -echo -onlcr 2>/dev/null; set +o emacs 2>/dev/null; set +o vi 2>/dev/null; "
    "PS1=; PS2=; PROMPT_COMMAND=; unset HISTFILE; trap : INT"
)

# Defined once per session. Each helper receives a nonce and the length of
# the base64 payload that follows its command line on the terminal.
#   R_<n>  payload received, command starting (Ctrl+C is safe from here on)
#   X_<n>  payload could not be received
#   E_<n>  done, with the exit code
_HELPERS = r"""
__bo_recv() {
    if head -c "$1" | base64 -d | gunzip > "$2"; then printf '%s_%s\n' BENCHOPS_R "$3"; return 0; fi
    printf '%s_%s\n' BENCHOPS_X "$3"; return 1
}
__bo_exec() {
    if [ -n "$__BO_AS" ]; then sudo -n -H -u "$__BO_AS" -- bash -lc "$(cat "$1")"; else bash "$1"; fi
}
__bo_run() {
    __bo_f=$(mktemp) || return
    if __bo_recv "$2" "$__bo_f" "$1"; then
        if [ "$3" = 1 ]; then __bo_exec "$__bo_f" 2>&1; else __bo_exec "$__bo_f" 2>&1 </dev/null; fi
        __bo_rc=$?
        printf '\n%s_%s:%s:\n' BENCHOPS_E "$1" "$__bo_rc"
    fi
    rm -f "$__bo_f"
}
__bo_capture() {
    __bo_f=$(mktemp) || return; __bo_o=$(mktemp); __bo_e=$(mktemp)
    if __bo_recv "$2" "$__bo_f" "$1"; then
        __bo_exec "$__bo_f" >"$__bo_o" 2>"$__bo_e" </dev/null; __bo_rc=$?
        printf '%s_%s:' BENCHOPS_S "$1"; base64 -w0 "$__bo_o"; printf ':'; base64 -w0 "$__bo_e"
        printf ':%s:%s_%s\n' "$__bo_rc" BENCHOPS_E "$1"
    fi
    rm -f "$__bo_f" "$__bo_o" "$__bo_e"
}
__bo_put() {
    __bo_t=$(mktemp) || return
    if __bo_recv "$2" "$__bo_t" "$1"; then
        if [ -n "$__BO_AS" ]; then
            sudo -n -H -u "$__BO_AS" -- sh -c 'cat > "$1"' _ "$3" < "$__bo_t"
        else
            cat "$__bo_t" > "$3"
        fi
        printf '%s_%s:%s:\n' BENCHOPS_E "$1" "$?"
    fi
    rm -f "$__bo_t"
}
"""


def _encode(data: bytes) -> bytes:
    """gzip, then base64 in 76-character lines, as `base64 -d | gunzip`
    reads it. Session Manager carries input slowly (~0.2 MB/s), so
    compressing first pays for itself on scripts and most files."""
    return base64.encodebytes(gzip.compress(data, compresslevel=6))


class SsmShellRunner(Runner):
    """Runs commands and transfers files over one Session Manager shell
    session, which stays open for the runner's lifetime."""

    def __init__(self, session: boto3.Session, instance_id: str, user: str) -> None:
        self.session = session
        self.instance_id = instance_id
        self.user = user
        self.session_user: str | None = None
        self.hostname: str | None = None
        self.display_target = f"{instance_id} (via SSM)"
        self.session_id: str | None = None
        self._client = None
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._eof = False
        # Per-command Ctrl+C state; see _on_interrupt.
        self._in_command = False
        self._running = False
        self._interrupted = False
        self._interrupt_sent = False

    @classmethod
    def connect(cls, server_config: dict) -> "SsmShellRunner":
        """Open a shell session to the server's instance, ready for commands."""
        try:
            return cls(session_for(server_config), server_config["instance_id"], server_config["user"]).open()
        except SsmError as exc:
            raise BenchOpsConnectionError(str(exc)) from exc

    # --- session lifecycle ------------------------------------------------

    def open(self) -> "SsmShellRunner":
        # No DocumentName: the account's standard shell document, with its
        # RunAs and shell-profile preferences.
        self._client, response, argv = start_session(self.session, {"Target": self.instance_id})
        self.session_id = response["SessionId"]
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                **PLUGIN_ISOLATION,
            )
        except OSError as exc:
            self.close()
            raise SsmError(f"Could not launch {PLUGIN}: {exc}") from exc

        self._reader = threading.Thread(target=self._read_loop, name="benchops-ssm-reader", daemon=True)
        self._reader.start()
        try:
            self._bootstrap()
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        """Stop the plugin and terminate the SSM session. Safe to call twice."""
        end_session(self._client, self.session_id, self._process)
        self.session_id = None
        if self._reader is not None:
            self._reader.join(timeout=2)

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None and not self._eof

    def _bootstrap(self) -> None:
        # The session's shell profile (e.g. `cd ~ && exec bash -l`) may still
        # be starting, so the quieting probe is repeated until one answers.
        nonce = secrets.token_hex(6)
        ready = re.compile(rb"BENCHOPS_READY_" + nonce.encode())
        probe = f"{_QUIET}; printf '%s_%s\\n' BENCHOPS_READY {nonce}\n".encode()
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while True:
            self._send(probe)
            try:
                self._wait(ready, READY_PROBE_INTERVAL_SECONDS)
                break
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise BenchOpsConnectionError(
                        f"The SSM shell on '{self.instance_id}' did not respond within {READY_TIMEOUT_SECONDS}s. "
                        f"Session output: {self._tail() or 'none'}"
                    ) from None

        nonce = secrets.token_hex(6)
        helpers = base64.encodebytes(_HELPERS.encode())
        self._send(
            (
                f'__bo_h=$(mktemp) && head -c {len(helpers)} | base64 -d > "$__bo_h" && . "$__bo_h"; '
                f'rm -f "$__bo_h"; {_QUIET}; '
                f"printf '%s_%s:%s:%s:\\n' BENCHOPS_HELLO {nonce} \"$(id -un)\" \"$(uname -n)\"\n"
            ).encode()
            + helpers
        )
        hello = self._wait(re.compile(rb"BENCHOPS_HELLO_" + nonce.encode() + rb":([^:\r\n]*):([^:\r\n]*):"), STEP_TIMEOUT_SECONDS)
        self.session_user = hello.group(1).decode()
        self.hostname = hello.group(2).decode()

        if self.session_user != self.user:
            nonce = secrets.token_hex(6)
            self._send(
                f"__BO_AS={shlex.quote(self.user)}; sudo -n -u \"$__BO_AS\" true </dev/null >/dev/null 2>&1; "
                f"printf '%s_%s:%s:\\n' BENCHOPS_AS {nonce} $?\n".encode()
            )
            sudo = self._wait(re.compile(rb"BENCHOPS_AS_" + nonce.encode() + rb":(\d+):"), STEP_TIMEOUT_SECONDS)
            if sudo.group(1) != b"0":
                raise BenchOpsConnectionError(
                    f"SSM sessions on '{self.instance_id}' run as '{self.session_user}', which cannot "
                    f"'sudo -n -u {self.user}'. Either set Session Manager preferences → Run As to '{self.user}', "
                    f"or let '{self.session_user}' sudo to '{self.user}' without a password."
                )

    # --- terminal I/O -----------------------------------------------------

    def _read_loop(self) -> None:
        stream = self._process.stdout
        while True:
            try:
                chunk = stream.read(65536)
            except (OSError, ValueError):
                chunk = b""
            with self._cond:
                if not chunk:
                    self._eof = True
                    self._cond.notify_all()
                    return
                self._buffer += chunk
                self._cond.notify_all()

    def _send(self, data: bytes) -> None:
        if not self.is_alive():
            raise BenchOpsConnectionError(self._dead_message())
        view = memoryview(data)
        try:
            while view:
                written = self._process.stdin.write(view)
                view = view[written:]
            self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise BenchOpsConnectionError(self._dead_message()) from exc

    def _tail(self) -> str:
        with self._cond:
            text = bytes(self._buffer[-600:]).decode("utf-8", "replace")
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r", "", text).strip()

    def _dead_message(self) -> str:
        return f"The SSM session to '{self.instance_id}' ended unexpectedly. Session output: {self._tail() or 'none'}"

    def _on_interrupt(self) -> None:
        """Ctrl+C: forward it to the remote command once it is running (never
        while its payload is still being received, which a stray SIGINT would
        cut short); a second Ctrl+C after that gives up waiting. Outside a
        command (connecting), Ctrl+C aborts right away."""
        if not self._in_command or (self._running and self._interrupt_sent):
            raise KeyboardInterrupt
        self._interrupted = True
        if self._running:
            self._interrupt_sent = True
            self._send(b"\x03")

    def _wait(self, pattern: re.Pattern, timeout: float | None = None) -> re.Match:
        """Wait for `pattern` in the session output; consume everything up to
        the end of the match."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                with self._cond:
                    # Search a snapshot: a match on the bytearray itself would
                    # change under us once the consumed part is deleted.
                    match = pattern.search(bytes(self._buffer))
                    if match:
                        del self._buffer[: match.end()]
                        return match
                    if self._eof:
                        raise BenchOpsConnectionError(self._dead_message())
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError
                    self._cond.wait(0.5 if remaining is None else min(remaining, 0.5))
            except KeyboardInterrupt:
                self._on_interrupt()

    def _start(self, helper: str, payload: bytes, *args: str) -> str:
        """Send one helper invocation with its payload, and wait until the
        remote side has received it. Returns the command's nonce."""
        nonce = secrets.token_hex(8)
        self._running = self._interrupted = self._interrupt_sent = False
        self._in_command = True
        line = " ".join([helper, nonce, str(len(payload)), *(shlex.quote(a) for a in args)]) + "\n"
        self._send(line.encode() + payload)
        received = self._wait(re.compile(rb"BENCHOPS_([RX])_" + nonce.encode() + rb"\n"))
        if received.group(1) == b"X":
            raise BenchOpsConnectionError(f"The SSM shell on '{self.instance_id}' could not receive the command.")
        self._running = True
        if self._interrupted:
            self._interrupt_sent = True
            self._send(b"\x03")
        return nonce

    def _finish(self, interactive: bool = False) -> None:
        self._in_command = self._running = False
        if self._interrupted and not interactive:
            raise KeyboardInterrupt

    def _abandon(self) -> None:
        """A command ended in an error: the terminal is no longer in a known
        state, so no further command may be sent on this session."""
        self._in_command = self._running = False
        end_session(self._client, self.session_id, self._process)
        self.session_id = None

    @staticmethod
    def _script(command: str, cwd: str | None) -> bytes:
        script = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        return _encode((script + "\n").encode())

    # --- Runner -----------------------------------------------------------

    def run(self, command: str, cwd: str | None = None, interactive: bool = False) -> None:
        """Run a command, streaming its output (stdout and stderr together,
        as in a terminal). With `interactive=True` the command keeps the
        terminal as its stdin, Ctrl+C stops it, and a non-zero exit is the
        expected ending rather than an error."""
        try:
            exit_code = self._run(command, cwd, interactive)
        except BaseException:
            if self._in_command:
                self._abandon()
            raise
        if exit_code != 0 and not interactive:
            raise BenchOpsCommandError(exit_code)

    def _run(self, command: str, cwd: str | None, interactive: bool) -> int:
        nonce = self._start("__bo_run", self._script(command, cwd), "1" if interactive else "0")
        marker = b"\nBENCHOPS_E_" + nonce.encode() + b":"
        end = re.compile(re.escape(marker) + rb"(\d+):\n")
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        exit_code = None
        while exit_code is None:
            try:
                with self._cond:
                    match = end.search(bytes(self._buffer))
                    if match:
                        output = bytes(self._buffer[: match.start()])
                        del self._buffer[: match.end()]
                        exit_code = int(match.group(1))
                    else:
                        output = bytes(self._buffer[: self._safe_prefix(marker)])
                        del self._buffer[: len(output)]
                        if not output:
                            if self._eof:
                                raise BenchOpsConnectionError(self._dead_message())
                            self._cond.wait(0.5)
                if output or exit_code is not None:
                    sys.stdout.write(decoder.decode(output, final=exit_code is not None))
                    sys.stdout.flush()
            except KeyboardInterrupt:
                self._on_interrupt()

        self._finish(interactive)
        return exit_code

    def _safe_prefix(self, marker: bytes) -> int:
        """How much of the buffer can be shown without cutting into a marker
        that may still be arriving (call with the lock held)."""
        found = self._buffer.find(marker)
        if found != -1:
            return found
        for size in range(min(len(marker) - 1, len(self._buffer)), 0, -1):
            if self._buffer.endswith(marker[:size]):
                return len(self._buffer) - size
        return len(self._buffer)

    def capture(self, command: str, cwd: str | None = None) -> str:
        try:
            nonce = self._start("__bo_capture", self._script(command, cwd))
            result = self._wait(
                re.compile(
                    rb"BENCHOPS_S_" + nonce.encode() + rb":([A-Za-z0-9+/=]*):([A-Za-z0-9+/=]*):(\d+):BENCHOPS_E_" + nonce.encode()
                )
            )
            self._finish()
        except BaseException:
            if self._in_command:
                self._abandon()
            raise
        stdout = base64.b64decode(result.group(1)).decode("utf-8", "replace")
        stderr = base64.b64decode(result.group(2)).decode("utf-8", "replace")
        exit_code = int(result.group(3))
        if exit_code != 0:
            raise BenchOpsCommandError(exit_code, stdout, stderr)
        return stdout

    def put(self, local_path: str, remote_path: str) -> None:
        try:
            with open(local_path, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise BenchOpsConnectionError(f"Could not read '{local_path}': {exc}") from exc
        try:
            nonce = self._start("__bo_put", _encode(data), remote_path)
            done = self._wait(re.compile(rb"BENCHOPS_E_" + nonce.encode() + rb":(\d+):"))
            self._finish()
        except BaseException:
            if self._in_command:
                self._abandon()
            raise
        if done.group(1) != b"0":
            raise BenchOpsConnectionError(
                f"Failed to write '{remote_path}' on {self.user}@{self.display_target} (exit code {done.group(1).decode()})."
            )
