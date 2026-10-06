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
from benchops.build import outputs_digest
from benchops.sync import RemoteAgent, RemoteAgentError


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


def test_dirty_paths_lists_every_uncommitted_change(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "b.txt").write_text("b")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "b")
    git(repo, "mv", "a.txt", "renamed with space.txt")
    (repo / "b.txt").write_text("changed")
    (repo / "dir").mkdir()
    (repo / "dir" / "untracked.txt").write_text("u")

    dirty = remote_agent.dirty_paths(str(repo))

    assert dirty == {
        "a.txt": "D ",
        "renamed with space.txt": "A ",
        "b.txt": " M",
        "dir/untracked.txt": "??",
    }
    assert [remote_agent.describe(dirty[p]) for p in ("a.txt", "b.txt", "dir/untracked.txt")] == [
        "deleted on staging",
        "modified on staging",
        "new file on staging",
    ]


def test_ignore_block_hides_caches_and_build_outputs_and_drops_old_staging_only_entries(tmp_path):
    repo = init_repo(tmp_path / "repo", commit=False)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(
        "# user rule\n*.log\n"
        + "\n".join([remote_agent.IGNORE_BEGIN, "/myapp/public/dist/", "# staging-only:", "/myapp/report/r.json",
                     remote_agent.IGNORE_END]) + "\n"
    )
    outputs = [{"path": "myapp/public/my spa", "dir": True}, {"path": "myapp/www/[spa].html", "dir": False}]

    remote_agent.write_ignore_block(str(repo), outputs)
    remote_agent.write_ignore_block(str(repo), outputs)  # rewriting must not duplicate the block

    text = exclude.read_text()
    assert text.startswith("# user rule\n*.log\n")
    assert text.count(remote_agent.IGNORE_BEGIN) == 1
    for name in ("myapp/public/my spa/a.js", "myapp/www/[spa].html", "pkg/__pycache__/x.pyc", "myapp/report/r.json"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    # Build outputs and caches are hidden; the formerly hidden staging-only file is visible again.
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == "?? myapp/report/r.json\n"


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


def test_staged_state_without_git_identity_falls_back_to_os_user(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    git(repo, "config", "user.useConfigOnly", "true")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr("benchops.gitlocal.getpass.getuser", lambda: "osuser")

    local = LocalRepo.open(repo)
    state = local.staged_state(local.user_name() + "@host")

    assert git(repo, "log", "-1", "--format=%an <%ae>", state.commit).strip() == "osuser <osuser@localhost>"
    assert local.user_name() == "osuser"


def test_staged_state_splits_staged_unstaged_untracked_and_leaves_index_alone(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("staged")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("staged then edited")
    (repo / "b.txt").write_text("new, staged")
    git(repo, "add", "b.txt")
    (repo / "out" / "spa").mkdir(parents=True)
    (repo / "out" / "spa" / "x.js").write_text("build output")
    (repo / "notes.txt").write_text("untracked")
    before = git(repo, "status", "--porcelain")

    state = LocalRepo.open(repo).staged_state("dev@host", ["out/spa"])

    assert state.staged == [("M", "a.txt"), ("A", "b.txt")]
    assert state.unstaged == [("M", "a.txt")]
    assert state.untracked == ["notes.txt"]
    assert git(repo, "show", f"{state.commit}:a.txt") == "staged"
    assert git(repo, "rev-parse", f"{state.commit}^").strip() == state.base
    assert git(repo, "status", "--porcelain") == before


def test_staged_state_refuses_unresolved_conflicts(tmp_path):
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-q", "-b", "other")
    (repo / "a.txt").write_text("other")
    git(repo, "commit", "-qam", "other")
    git(repo, "checkout", "-q", "-")
    (repo / "a.txt").write_text("main")
    git(repo, "commit", "-qam", "main")
    subprocess.run(["git", "merge", "-q", "other"], cwd=repo, capture_output=True)

    with pytest.raises(GitError, match="unresolved merge conflicts"):
        LocalRepo.open(repo).staged_state("dev@host")


def test_staged_state_branch_and_detached_head(tmp_path):
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-q", "-b", "feature/x")
    assert LocalRepo.open(repo).staged_state("d").branch == "feature/x"

    git(repo, "checkout", "-q", "--detach")
    assert LocalRepo.open(repo).staged_state("d").branch is None


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


def test_outputs_digest_tracks_content_and_names(tmp_path):
    assert outputs_digest(tmp_path, ["missing"]) is None
    dist = tmp_path / "dist"
    (dist / "js").mkdir(parents=True)
    (dist / "js" / "a.js").write_text("1")
    (tmp_path / "spa.html").write_text("<html>")
    outputs = ["dist", "spa.html"]
    first = outputs_digest(tmp_path, outputs)

    (dist / "js" / "a.js").write_text("2")
    assert outputs_digest(tmp_path, outputs) != first
    (dist / "js" / "a.js").write_text("1")
    assert outputs_digest(tmp_path, outputs) == first
    (tmp_path / "spa.html").write_text("<html>new")
    assert outputs_digest(tmp_path, outputs) != first
    (tmp_path / "spa.html").write_text("<html>")
    (dist / "js" / "a.js").rename(dist / "js" / "b.js")
    assert outputs_digest(tmp_path, outputs) != first


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
        cli.app,
        ["deploy", "myapp", "staging", "--site", "s1", "-y", "--overwrite", "--force", "--break-lock", "--skip-build"],
    )

    assert result.exit_code == 0, result.output
    assert deploy_kwargs == {
        "server_alias": "staging",
        "app_name": "myapp",
        "site": "s1",
        "yes": True,
        "overwrite": True,
        "force": True,
        "break_lock": True,
        "skip_build": True,
        "executed": True,
    }


def test_cli_deploy_defaults_are_safe(deploy_kwargs):
    result = CliRunner().invoke(cli.app, ["deploy", "myapp", "staging"])

    assert result.exit_code == 0, result.output
    assert [deploy_kwargs[k] for k in ("yes", "overwrite", "force", "break_lock", "skip_build")] == [False] * 5


def test_cli_status_passes_arguments(monkeypatch):
    captured = {}

    class FakeStatus:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def execute(self):
            captured["executed"] = True

    monkeypatch.setattr(cli, "StatusCommand", FakeStatus)

    result = CliRunner().invoke(cli.app, ["status", "myapp", "staging", "--files"])

    assert result.exit_code == 0, result.output
    assert captured == {"server_alias": "staging", "app_name": "myapp", "files": True, "executed": True}
