"""`benchops logs`: following the logs, and printing the last lines (--lines)."""

import platform

import pytest
import typer
from typer.testing import CliRunner

from benchops import cli
from benchops.logs import LogsCommand, LogType
from benchops.runner import BenchOpsCommandError, LocalRunner


class FakeRunner:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, command, cwd=None, interactive=False):
        self.calls.append((command, interactive))
        if self.fail:
            raise BenchOpsCommandError(1)


def logs_command(runner, bench="/home/akwad/bench", **kwargs):
    command = LogsCommand(server_alias="staging", **kwargs)
    command._get_server_config = lambda: {"bench_path": bench}
    command._get_remote_runner = lambda server_config: runner
    return command


def test_default_follows_all_three_logs_interactively():
    runner = FakeRunner()

    logs_command(runner).execute()

    expected = (
        "tail -n 100 -f /home/akwad/bench/logs/frappe.log /home/akwad/bench/logs/web.error.log "
        "/home/akwad/bench/logs/worker.error.log"
    )
    assert runner.calls == [(expected, True)]


def test_lines_prints_the_last_lines_once_and_does_not_follow():
    runner = FakeRunner()

    logs_command(runner, log_type=LogType.web_error, lines=50).execute()

    assert runner.calls == [("tail -n 50 -- /home/akwad/bench/logs/web.error.log", False)]


def test_lines_quotes_paths():
    runner = FakeRunner()

    logs_command(runner, bench="/srv/my bench", lines=5).execute()

    assert "'/srv/my bench/logs/frappe.log'" in runner.calls[0][0]


def test_lines_with_an_unreadable_log_exits_1(capsys):
    with pytest.raises(typer.Exit) as exc:
        logs_command(FakeRunner(fail=True), lines=10).execute()

    assert exc.value.exit_code == 1
    assert "could not be read" in capsys.readouterr().out


@pytest.mark.skipif(platform.system() == "Windows", reason="the bench is always POSIX")
def test_lines_against_real_files(tmp_path, capfd):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "web.error.log").write_text("".join(f"line {i}\n" for i in range(1, 21)))

    logs_command(LocalRunner(), bench=str(tmp_path), log_type=LogType.web_error, lines=3).execute()

    assert capfd.readouterr().out.splitlines()[-3:] == ["line 18", "line 19", "line 20"]


@pytest.mark.parametrize(
    "args, expected",
    [
        (["logs", "staging"], {"log_type": None, "lines": None}),
        (["logs", "staging", "--lines", "40", "--type", "frappe.log"], {"log_type": LogType.frappe, "lines": 40}),
        (["logs", "staging", "-n", "5"], {"log_type": None, "lines": 5}),
    ],
)
def test_cli_logs_passes_options(monkeypatch, args, expected):
    captured = {}

    class FakeLogs:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def execute(self):
            pass

    monkeypatch.setattr(cli, "LogsCommand", FakeLogs)

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert captured == {"server_alias": "staging", **expected}


def test_cli_lines_must_be_positive():
    assert CliRunner().invoke(cli.app, ["logs", "staging", "--lines", "0"]).exit_code == 2
