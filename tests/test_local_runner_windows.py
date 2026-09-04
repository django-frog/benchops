"""Windows-specific behavior of LocalRunner.run(): commands must reach
subprocess.Popen as a raw string with shell=True on Windows, and as a
shlex-split argv with shell=False everywhere else.

The bug this guards against: shlex.split() always applies POSIX escaping
rules, regardless of host OS. An unquoted Windows path like
"C:\\repo\\app" has its backslashes silently stripped by shlex.split()
(backslash is an escape character in POSIX mode), corrupting the path with
no error raised.
"""

from unittest.mock import MagicMock, patch

import pytest

from benchops.runner import LocalRunner


def _mock_popen(returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout.readline.return_value = ""  # iter(readline, "") stops immediately
    proc.wait.return_value = returncode
    return proc


def test_windows_receives_the_raw_command_string(monkeypatch):
    monkeypatch.setattr("benchops.runner.platform.system", lambda: "Windows")
    command = r"xcopy C:\repo\app D:\deploy\app /E /I"

    with patch("benchops.runner.subprocess.Popen", return_value=_mock_popen()) as mock_popen:
        LocalRunner().run(command)

    args, kwargs = mock_popen.call_args
    assert args[0] == command, "Windows must receive the unmodified string, not a shlex-split argv"
    assert kwargs["shell"] is True


def test_windows_never_calls_shlex_split(monkeypatch):
    monkeypatch.setattr("benchops.runner.platform.system", lambda: "Windows")

    with patch("benchops.runner.shlex.split") as mock_split, patch(
        "benchops.runner.subprocess.Popen", return_value=_mock_popen()
    ):
        LocalRunner().run(r"echo C:\Users\dev\Desktop")

    mock_split.assert_not_called()


def test_posix_still_uses_split_argv_without_a_shell(monkeypatch):
    monkeypatch.setattr("benchops.runner.platform.system", lambda: "Linux")

    with patch("benchops.runner.subprocess.Popen", return_value=_mock_popen()) as mock_popen:
        LocalRunner().run("git status --short")

    args, kwargs = mock_popen.call_args
    assert args[0] == ["git", "status", "--short"]
    assert kwargs["shell"] is False


def test_macos_also_uses_split_argv_without_a_shell(monkeypatch):
    monkeypatch.setattr("benchops.runner.platform.system", lambda: "Darwin")

    with patch("benchops.runner.subprocess.Popen", return_value=_mock_popen()) as mock_popen:
        LocalRunner().run("bench build")

    args, kwargs = mock_popen.call_args
    assert args[0] == ["bench", "build"]
    assert kwargs["shell"] is False


def test_nonzero_exit_raises_called_process_error(monkeypatch):
    monkeypatch.setattr("benchops.runner.platform.system", lambda: "Linux")

    with patch("benchops.runner.subprocess.Popen", return_value=_mock_popen(returncode=1)):
        with pytest.raises(__import__("subprocess").CalledProcessError):
            LocalRunner().run("false")


def test_missing_binary_raises_benchops_connection_error(monkeypatch):
    from benchops.runner import BenchOpsConnectionError

    monkeypatch.setattr("benchops.runner.platform.system", lambda: "Linux")
    with patch("benchops.runner.subprocess.Popen", side_effect=FileNotFoundError("no such file")):
        with pytest.raises(BenchOpsConnectionError):
            LocalRunner().run("this-binary-does-not-exist")
