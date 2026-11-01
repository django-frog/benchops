"""The SSM shell runner, end to end against a real interactive bash in a
real pseudo-terminal.

The fake session-manager-plugin below does what the real one does for a
shell session — relays our stdin into a terminal and the terminal back to
our stdout — so the runner meets the real obstacles: echo, prompts, \\r\\n
translation, terminal line limits and job control. Only the AWS API is
faked.
"""

import _thread
import getpass
import os
import platform
import sys
import threading

import pytest

from benchops.runner import BenchOpsCommandError, BenchOpsConnectionError
from benchops.ssm_shell import SsmShellRunner

pytestmark = pytest.mark.skipif(platform.system() != "Linux", reason="needs a pty and GNU coreutils")

FAKE_PLUGIN = """\
import os, pty, select, sys
print("Starting session with SessionId: fake-session", flush=True)
pid, fd = pty.fork()
if pid == 0:
    os.environ["PS1"] = "[fake prompt]$ "
    os.execvp("bash", ["bash", "--norc", "--noprofile", "-i"])
stdin, out = sys.stdin.buffer.fileno(), sys.stdout.buffer.fileno()
reading = True
while True:
    ready, _, _ = select.select([fd] + ([stdin] if reading else []), [], [])
    if fd in ready:
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        os.write(out, data)
    if stdin in ready:
        data = os.read(stdin, 65536)
        if not data:
            reading = False
            os.write(fd, b"exit\\n")
        else:
            os.write(fd, data)
"""


class FakeClient:
    def __init__(self):
        self.started = []
        self.terminated = []
        self.meta = type("Meta", (), {"region_name": "ap-south-1", "endpoint_url": "https://ssm.example"})

    def start_session(self, **request):
        self.started.append(request)
        return {"SessionId": "fake-session", "TokenValue": "t", "StreamUrl": "wss://example"}

    def terminate_session(self, SessionId):
        self.terminated.append(SessionId)


class FakeSession:
    region_name = "ap-south-1"

    def __init__(self):
        self.ssm = FakeClient()

    def client(self, service):
        return self.ssm


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    script = tmp_path / "session-manager-plugin"
    script.write_text(f"#!{sys.executable}\n" + FAKE_PLUGIN)
    script.chmod(0o755)
    monkeypatch.setattr("benchops.ssm.shutil.which", lambda name: str(script))


@pytest.fixture
def runner(plugin):
    session = FakeSession()
    runner = SsmShellRunner(session, "i-abc", getpass.getuser()).open()
    yield runner
    runner.close()


def test_connects_with_the_standard_shell_document_and_knows_who_it_is(runner):
    assert runner._client.started == [{"Target": "i-abc"}]  # no DocumentName: the account's shell document
    assert runner.session_user == getpass.getuser()
    assert runner.hostname == platform.node()


def test_capture_returns_stdout_exactly(runner):
    assert runner.capture("printf 'a\\nb\\n'; printf 'ünïcode\\n'") == "a\nb\nünïcode\n"
    assert runner.capture("true") == ""


def test_capture_keeps_stdout_stderr_and_exit_code_apart(runner):
    with pytest.raises(BenchOpsCommandError) as exc_info:
        runner.capture("echo out; echo err >&2; exit 3")
    assert (exc_info.value.exit_code, exc_info.value.stdout, exc_info.value.stderr) == (3, "out\n", "err\n")


def test_commands_run_in_cwd_with_real_shell_semantics(runner, tmp_path):
    (tmp_path / "file.txt").write_text("x")
    assert runner.capture("ls | grep file && echo $((6*7))", cwd=str(tmp_path)) == "file.txt\n42\n"


def test_output_that_looks_like_a_marker_is_not_mistaken_for_one(runner):
    fake = "BENCHOPS_E_0123456789abcdef:0:\nBENCHOPS_R_x\n"
    assert runner.capture(f"printf '%s' '{fake}'") == fake


def test_long_commands_pass_the_terminal_line_limit(runner):
    payload = "x" * 200_000
    assert runner.capture(f"printf '%s' '{payload}' | wc -c").strip() == "200000"


def test_run_streams_output_and_raises_on_failure(runner, capsys):
    runner.run("echo first; echo second >&2")
    assert capsys.readouterr().out == "first\nsecond\n"
    with pytest.raises(BenchOpsCommandError) as exc_info:
        runner.run("exit 4")
    assert exc_info.value.exit_code == 4
    assert runner.capture("echo still-usable") == "still-usable\n"


def test_put_transfers_binary_files_exactly(runner, tmp_path):
    data = bytes(range(256)) * 2000 + os.urandom(100_000)
    local, remote = tmp_path / "local.bin", tmp_path / "remote.bin"
    local.write_bytes(data)
    runner.put(str(local), str(remote))
    assert remote.read_bytes() == data


def test_put_to_an_unwritable_path_is_a_connection_error(runner, tmp_path):
    local = tmp_path / "f"
    local.write_text("x")
    with pytest.raises(BenchOpsConnectionError, match="Failed to write"):
        runner.put(str(local), str(tmp_path / "missing-dir" / "f"))


def test_ctrl_c_stops_an_interactive_command_and_keeps_the_session(runner):
    threading.Timer(1.0, _thread.interrupt_main).start()
    runner.run("echo started; sleep 30", interactive=True)  # returns instead of raising
    assert runner.capture("echo after") == "after\n"


def test_ctrl_c_during_a_normal_command_stops_it_and_propagates(runner):
    threading.Timer(1.0, _thread.interrupt_main).start()
    with pytest.raises(KeyboardInterrupt):
        runner.run("sleep 30")
    assert runner.capture("echo after") == "after\n"


def test_a_user_the_session_cannot_become_is_explained(plugin):
    with pytest.raises(BenchOpsConnectionError, match="Run As"):
        SsmShellRunner(FakeSession(), "i-abc", "no-such-benchops-user").open()


def test_close_ends_the_plugin_and_the_ssm_session(plugin):
    session = FakeSession()
    runner = SsmShellRunner(session, "i-abc", getpass.getuser()).open()
    runner.close()
    assert not runner.is_alive()
    assert session.ssm.terminated == ["fake-session"]
    with pytest.raises(BenchOpsConnectionError):
        runner.capture("echo too late")
