"""Unit tests for the building blocks of the git-based sync: the remote
agent's helpers, the local git wrapper's failure modes, the agent client,
post-deploy step suggestions, and CLI flag wiring.
"""

import json
import os
import platform
import subprocess

import pytest
from typer.testing import CliRunner

import benchops.cli as cli
from benchops import remote_agent
from benchops.deploy import required_steps
from benchops.gitlocal import GitError, LocalRepo
from benchops.runner import BenchOpsCommandError
from benchops.remote_agent import RESULT_MARKER
from benchops.sync import RemoteAgent, RemoteAgentError, dist_digest


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def init_repo(path, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Dev")
    git(path, "config", "user.email", "dev@example.com")
    if commit:
        (path / "a.txt").write_text("a")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "init")
    return path


# --------------------------------------------------------------------------- remote agent helpers


def test_safe_join_rejects_paths_outside_the_app(tmp_path):
    app = tmp_path / "apps" / "myapp"
    (app / "pkg").mkdir(parents=True)

    assert remote_agent.safe_join(str(app), "pkg/file.json") == str((app / "pkg" / "file.json").resolve())
    for bad in ("../other/file", "/etc/passwd", ".", "pkg/../../x"):
        with pytest.raises(remote_agent.AgentError):
            remote_agent.safe_join(str(app), bad)


@pytest.mark.skipif(platform.system() == "Windows", reason="symlinks")
def test_safe_join_rejects_symlinks_escaping_the_app(tmp_path):
    app = tmp_path / "apps" / "myapp"
    app.mkdir(parents=True)
    os.symlink(tmp_path, app / "escape")

    with pytest.raises(remote_agent.AgentError):
        remote_agent.safe_join(str(app), "escape/secret")


def test_tracked_changes_parses_renames_and_skips_untracked(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "b.txt").write_text("b")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "b")
    git(repo, "mv", "a.txt", "renamed with space.txt")
    (repo / "b.txt").write_text("changed")
    (repo / "untracked.txt").write_text("u")

    assert sorted(remote_agent.tracked_changes(str(repo))) == [["M", "b.txt"], ["R", "renamed with space.txt"]]


def test_staging_owned_round_trip_escapes_special_characters(tmp_path):
    repo = init_repo(tmp_path / "repo", commit=False)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# user rule\n*.log\n")
    owned = ["myapp/report/a b/[x].json", "myapp/#hash.json", "myapp/!bang.json"]

    remote_agent.write_staging_owned(str(repo), "myapp", owned)
    remote_agent.write_staging_owned(str(repo), "myapp", owned)  # rewriting must not duplicate the block

    text = exclude.read_text()
    assert text.startswith("# user rule\n*.log\n")
    assert text.count(remote_agent.EXCLUDE_BEGIN) == 1
    assert sorted(remote_agent.read_staging_owned(str(repo))) == sorted(owned)
    for name in owned:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


# --------------------------------------------------------------------------- local git wrapper


def test_open_rejects_a_plain_directory(tmp_path):
    with pytest.raises(GitError, match="not a git repository"):
        LocalRepo.open(tmp_path)


def test_open_rejects_an_app_nested_in_another_repo(tmp_path):
    repo = init_repo(tmp_path / "mono")
    (repo / "apps" / "myapp").mkdir(parents=True)

    with pytest.raises(GitError, match="root of its own repository"):
        LocalRepo.open(repo / "apps" / "myapp")


def test_open_rejects_a_repo_without_commits(tmp_path):
    repo = init_repo(tmp_path / "repo", commit=False)

    with pytest.raises(GitError, match="no commits"):
        LocalRepo.open(repo)


def test_open_requires_git_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr("benchops.gitlocal.shutil.which", lambda name: None)

    with pytest.raises(GitError, match="not installed"):
        LocalRepo.open(tmp_path)


def test_snapshot_without_git_identity_falls_back_to_os_user(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    git(repo, "config", "user.useConfigOnly", "true")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr("benchops.gitlocal.getpass.getuser", lambda: "osuser")
    (repo / "new.txt").write_text("n")

    local = LocalRepo.open(repo)
    snap = local.snapshot("myapp", local.user_name() + "@host")

    assert git(repo, "log", "-1", "--format=%an <%ae>", snap.commit).strip() == "osuser <osuser@localhost>"
    assert local.user_name() == "osuser"


def test_snapshot_branch_and_detached_head(tmp_path):
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-q", "-b", "feature/x")
    assert LocalRepo.open(repo).snapshot("myapp", "d").branch == "feature/x"

    git(repo, "checkout", "-q", "--detach")
    assert LocalRepo.open(repo).snapshot("myapp", "d").branch == "(detached)"


# --------------------------------------------------------------------------- agent client


class FakeRunner:
    def __init__(self, output=None, error=None):
        self.output, self.error, self.commands = output, error, []

    def capture(self, command, cwd=None):
        self.commands.append((command, cwd))
        if self.error:
            raise self.error
        return self.output


def test_agent_call_returns_result_and_runs_in_bench():
    runner = FakeRunner(output="noise\n" + RESULT_MARKER + json.dumps({"ok": True, "head": "abc"}) + "\n")

    assert RemoteAgent(runner, "/home/frappe/bench").call("inspect", app="myapp") == {"head": "abc"}
    command, cwd = runner.commands[0]
    assert cwd == "/home/frappe/bench"
    assert command.startswith("./env/bin/python -c ")


def test_agent_call_raises_reported_errors_with_data():
    runner = FakeRunner(output=RESULT_MARKER + json.dumps({"ok": False, "error": "locked", "lock": {"owner": "ali"}}))

    with pytest.raises(RemoteAgentError) as info:
        RemoteAgent(runner, "/b").call("inspect")
    assert info.value.error == "locked"
    assert info.value.data == {"lock": {"owner": "ali"}}


def test_agent_call_reports_crashes_and_missing_results():
    crashed = FakeRunner(error=BenchOpsCommandError(1, "", "Traceback: boom"))
    with pytest.raises(RemoteAgentError, match="crashed: Traceback: boom"):
        RemoteAgent(crashed, "/b").call("apply")

    silent = FakeRunner(output="python: not found\n")
    with pytest.raises(RemoteAgentError, match="returned no result"):
        RemoteAgent(silent, "/b").call("apply")


def test_dist_digest_tracks_content_and_names(tmp_path):
    assert dist_digest(tmp_path / "missing") is None
    dist = tmp_path / "dist"
    (dist / "js").mkdir(parents=True)
    (dist / "js" / "a.js").write_text("1")
    first = dist_digest(dist)

    (dist / "js" / "a.js").write_text("2")
    assert dist_digest(dist) != first
    (dist / "js" / "a.js").write_text("1")
    assert dist_digest(dist) == first
    (dist / "js" / "a.js").rename(dist / "js" / "b.js")
    assert dist_digest(dist) != first


# --------------------------------------------------------------------------- deploy helpers and CLI


@pytest.mark.parametrize(
    "changes, expected",
    [
        (["myapp/public/js/form.js"], []),
        (["myapp/public/dist/x.json"], []),
        (["myapp/api.py"], ["bench restart"]),
        (["myapp/sales/doctype/visit/visit.json"], ["bench migrate"]),
        (["myapp/patches.txt"], ["bench migrate"]),
        (["myapp/hooks.py"], ["bench migrate", "bench restart"]),
        (["pyproject.toml"], ["bench setup requirements --python"]),
    ],
)
def test_required_steps(changes, expected):
    assert required_steps("myapp", changes) == expected


@pytest.fixture
def deploy_kwargs(monkeypatch):
    captured = {}

    class FakeDeploy:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def execute(self):
            captured["executed"] = True

    monkeypatch.setattr(cli, "DeployCommand", FakeDeploy)
    return captured


def test_cli_deploy_passes_flags(deploy_kwargs):
    result = CliRunner().invoke(
        cli.app, ["deploy", "myapp", "staging", "--site", "s1", "--adopt", "-y", "--force", "--break-lock"]
    )

    assert result.exit_code == 0, result.output
    assert deploy_kwargs == {
        "server_alias": "staging",
        "app_name": "myapp",
        "site": "s1",
        "adopt": True,
        "yes": True,
        "force": True,
        "break_lock": True,
        "executed": True,
    }


def test_cli_deploy_defaults_are_safe(deploy_kwargs):
    result = CliRunner().invoke(cli.app, ["deploy", "myapp", "staging"])

    assert result.exit_code == 0, result.output
    assert [deploy_kwargs[k] for k in ("adopt", "yes", "force", "break_lock")] == [False] * 4
