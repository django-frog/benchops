"""End-to-end bulk clones against real git repositories on disk: bare
"remote" repositories referenced by path, so the whole pipeline (preflight,
plan, parallel clone, lock, rename into place, summary, run record) runs
without a network."""

import json
import os
import socket
import subprocess

import pytest
import typer

from benchops import clone as clone_module
from benchops.clone import LOCK_NAME, TMP_PREFIX, CloneCommand
from benchops.config import ConfigManager
from benchops.vcs.providers import Access, GitLabProvider, RemoteRepo, RepoAccess


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def flat(text):
    return " ".join(text.split())


class NoTokens:
    def get(self, host):
        return None, None


def make_remote(root, name, branches=("develop",)):
    """A bare repository `<root>/<name>.git` with one commit per branch."""
    work = root / "work" / name
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", branches[0])
    git(work, "config", "user.email", "dev@example.com")
    git(work, "config", "user.name", "Dev")
    (work / "README.md").write_text(f"{name}\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    for branch in branches[1:]:
        git(work, "branch", branch)
    bare = root / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(root, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def push_commit(root, bare, message="more"):
    work = root / "pusher"
    if not work.exists():
        git(root, "clone", "-q", str(bare), str(work))
        git(work, "config", "user.email", "dev@example.com")
        git(work, "config", "user.name", "Dev")
    (work / f"{message}.txt").write_text(message)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    git(work, "push", "-q", "origin", "HEAD")


def write_manifest(path, repos, **settings):
    lines = [f'{key} = "{value}"' for key, value in settings.items()]
    for repo in repos:
        lines.append("[[repos]]")
        lines += [f'{key} = "{value}"' for key, value in repo.items()]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture(autouse=True)
def wide_console(monkeypatch):
    """Keep the plan table on one line per repository so reasons stay greppable."""
    monkeypatch.setattr(clone_module.console, "width", 250)


@pytest.fixture
def env(tmp_path):
    remotes = {name: make_remote(tmp_path, name, ("develop", "main")) for name in ("core", "hr", "crm")}
    return tmp_path, remotes


def command(tmp_path, manifest, **kwargs):
    kwargs.setdefault("yes", True)
    return CloneCommand(
        project=str(manifest),
        into=tmp_path / "apps",
        config=ConfigManager(tmp_path / "config.toml"),
        tokens=NoTokens(),
        runs_dir=tmp_path / "runs",
        **kwargs,
    )


def leftovers(apps):
    return [p.name for p in apps.iterdir() if p.name.startswith(TMP_PREFIX) or p.name == LOCK_NAME]


def test_clones_every_repository_in_parallel(env, capsys):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(b)} for b in remotes.values()], branch="develop")

    command(root, manifest, jobs=3).execute()

    apps = root / "apps"
    for name, bare in remotes.items():
        assert git(apps / name, "remote", "get-url", "origin").strip() == str(bare)
        assert git(apps / name, "branch", "--show-current").strip() == "develop"
    assert leftovers(apps) == []
    assert "3 cloned" in flat(capsys.readouterr().out)


def test_rerun_skips_existing_clones_and_update_fast_forwards(env, capsys):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}], branch="develop")
    command(root, manifest).execute()
    capsys.readouterr()

    command(root, manifest).execute()
    assert "already cloned" in flat(capsys.readouterr().out)

    push_commit(root, remotes["core"], "feature")
    command(root, manifest, update=True).execute()
    assert "fast-forwarded" in flat(capsys.readouterr().out)
    assert (root / "apps" / "core" / "feature.txt").exists()


def test_update_never_merges_over_local_changes(env, capsys):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}], branch="develop")
    command(root, manifest).execute()
    (root / "apps" / "core" / "README.md").write_text("my local edit\n")
    push_commit(root, remotes["core"], "upstream")

    command(root, manifest, update=True).execute()

    assert "not merged: local changes" in flat(capsys.readouterr().out)
    assert (root / "apps" / "core" / "README.md").read_text() == "my local edit\n"
    assert not (root / "apps" / "core" / "upstream.txt").exists()


def test_folder_with_a_different_repository_is_a_conflict_and_left_alone(env, capsys):
    root, remotes = env
    apps = root / "apps"
    apps.mkdir()
    git(apps, "clone", "-q", str(remotes["crm"]), "core")  # "core" holds crm
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}, {"url": str(remotes["hr"])}])

    with pytest.raises(typer.Exit) as exc:
        command(root, manifest).execute()

    assert exc.value.exit_code == 1
    out = flat(capsys.readouterr().out)
    assert "different repository" in out and "1 cloned" in out
    assert git(apps / "core", "remote", "get-url", "origin").strip() == str(remotes["crm"])
    assert (apps / "hr" / ".git").exists()


def test_non_empty_folder_conflicts_but_empty_folder_is_used(env, capsys):
    root, remotes = env
    apps = root / "apps"
    (apps / "core").mkdir(parents=True)
    (apps / "core" / "notes.txt").write_text("mine")
    (apps / "hr").mkdir()
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}, {"url": str(remotes["hr"])}])

    with pytest.raises(typer.Exit):
        command(root, manifest).execute()

    assert "not a git clone" in flat(capsys.readouterr().out)
    assert sorted(p.name for p in (apps / "core").iterdir()) == ["notes.txt"]
    assert (apps / "hr" / "README.md").exists()


def test_two_repositories_for_one_folder_are_both_conflicts(tmp_path, capsys):
    a = make_remote(tmp_path / "a", "utils")
    b = make_remote(tmp_path / "b", "utils")
    manifest = write_manifest(tmp_path / "p.toml", [{"url": str(a)}, {"url": str(b)}])

    with pytest.raises(typer.Exit):
        command(tmp_path, manifest).execute()

    assert "same folder as" in flat(capsys.readouterr().out)
    assert not (tmp_path / "apps" / "utils").exists()


def test_dir_setting_resolves_a_folder_clash(tmp_path):
    a = make_remote(tmp_path / "a", "utils")
    b = make_remote(tmp_path / "b", "utils")
    manifest = write_manifest(tmp_path / "p.toml", [{"url": str(a)}, {"url": str(b), "dir": "utils_b"}])

    command(tmp_path, manifest).execute()

    assert (tmp_path / "apps" / "utils").is_dir() and (tmp_path / "apps" / "utils_b").is_dir()


def test_missing_branch_and_missing_repository_are_reported_up_front(env, capsys):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [
        {"url": str(remotes["core"]), "branch": "version-15"},
        {"url": str(root / "remotes" / "ghost.git")},
        {"url": str(remotes["hr"])},
    ])

    with pytest.raises(typer.Exit):
        command(root, manifest).execute()

    out = flat(capsys.readouterr().out)
    assert "branch 'version-15' doesn't exist" in out
    assert "no access" in out
    assert not (root / "apps" / "core").exists() and not (root / "apps" / "ghost").exists()
    assert (root / "apps" / "hr").exists()


def test_dry_run_writes_nothing(env, capsys):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(b)} for b in remotes.values()])

    command(root, manifest, dry_run=True).execute()

    assert not (root / "apps").exists()
    assert "Dry run" in capsys.readouterr().out


def test_lock_held_by_a_running_clone_blocks_a_second_run(env, capsys):
    root, remotes = env
    apps = root / "apps"
    apps.mkdir()
    (apps / LOCK_NAME).write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started": "now"}))
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])

    with pytest.raises(typer.Exit):
        command(root, manifest).execute()

    assert "Another 'benchops clone'" in flat(capsys.readouterr().out)
    assert not (apps / "core").exists()


@pytest.mark.skipif(os.name == "nt", reason="stale-lock detection is POSIX only")
def test_stale_lock_from_a_dead_process_is_taken_over(env):
    root, remotes = env
    apps = root / "apps"
    apps.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    (apps / LOCK_NAME).write_text(json.dumps({"pid": dead.pid, "host": socket.gethostname()}))
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])

    command(root, manifest).execute()

    assert (apps / "core" / ".git").exists() and not (apps / LOCK_NAME).exists()


def test_folder_appearing_during_the_clone_is_never_overwritten(env, monkeypatch, capsys):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])
    cmd = command(root, manifest)
    real_git = cmd._git

    def racing_git(args, **kwargs):
        out = real_git(args, **kwargs)
        if args[0] == "clone":
            (root / "apps" / "core").mkdir()
            (root / "apps" / "core" / "someone-elses.txt").write_text("x")
        return out

    monkeypatch.setattr(cmd, "_git", racing_git)
    with pytest.raises(typer.Exit):
        cmd.execute()

    assert "appeared while cloning" in flat(capsys.readouterr().out)
    assert [p.name for p in (root / "apps" / "core").iterdir()] == ["someone-elses.txt"]
    assert leftovers(root / "apps") == []


def test_retry_failed_redoes_only_what_failed(env, capsys):
    root, remotes = env
    apps = root / "apps"
    (apps / "hr").mkdir(parents=True)
    (apps / "hr" / "junk").write_text("x")
    manifest = write_manifest(root / "p.toml", [{"url": str(b)} for b in remotes.values()])
    with pytest.raises(typer.Exit):
        command(root, manifest).execute()
    (apps / "hr" / "junk").unlink()
    capsys.readouterr()

    command(root, manifest, retry_failed=True).execute()

    out = flat(capsys.readouterr().out)
    assert "1 cloned" in out and "core" not in out
    assert (apps / "hr" / ".git").exists()


def test_only_and_skip_filters(env):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(b)} for b in remotes.values()])

    command(root, manifest, only=["c*"], skip=["crm"]).execute()

    assert sorted(p.name for p in (root / "apps").iterdir()) == ["core"]


def test_only_pattern_matching_nothing_is_an_error(env):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])
    with pytest.raises(typer.Exit):
        command(root, manifest, only=["corr"]).execute()


def test_shallow_clone(env):
    root, remotes = env
    push_commit(root, remotes["core"], "second")
    manifest = write_manifest(root / "p.toml", [{"url": f"file://{remotes['core']}"}], branch="develop")

    command(root, manifest, depth=1).execute()

    assert git(root / "apps" / "core", "rev-list", "--count", "HEAD").strip() == "1"


# --- roles: what the git host says the token may do -----------------------

def api_project(root, repos, monkeypatch, access_for):
    """A project on an API host whose permission answers come from `access_for`."""
    def fake_repo(self, path):
        access, url = access_for[path]
        repo = RemoteRepo(path, https_url=url, ssh_url=url) if url else None
        return RepoAccess(access, repo, "" if access else "no membership")

    monkeypatch.setattr(GitLabProvider, "repo", fake_repo)
    return write_manifest(root / "p.toml", repos, host="gitlab.example.test", provider="gitlab")


def test_role_gates_cloning(env, monkeypatch, capsys):
    root, remotes = env
    manifest = api_project(root, [
        {"path": "erp/core"},
        {"path": "erp/hr", "min_access": "write"},
        {"path": "erp/secret"},
    ], monkeypatch, {
        "erp/core": (Access.WRITE, str(remotes["core"])),
        "erp/hr": (Access.READ, str(remotes["hr"])),
        "erp/secret": (Access.NONE, None),
    })

    command(root, manifest).execute()

    out = flat(capsys.readouterr().out)
    assert (root / "apps" / "core").exists()
    assert not (root / "apps" / "hr").exists() and "needs write access, you have read" in out
    assert not (root / "apps" / "secret").exists() and "no access" in out


def test_api_access_but_git_refusal_points_at_token_scope(env, monkeypatch, capsys):
    root, _ = env
    manifest = api_project(root, [{"path": "erp/core"}], monkeypatch, {
        "erp/core": (Access.READ, str(root / "remotes" / "missing.git")),
    })

    command(root, manifest).execute()

    assert "git refused the token" in flat(capsys.readouterr().out)


# --- interruption, retries and failures ------------------------------------

def test_ctrl_c_stops_cleanly_and_retry_failed_resumes(env, monkeypatch, capsys):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(b)} for b in remotes.values()])
    real_as_completed = clone_module.as_completed

    def interrupted_after_first(futures):
        for future in real_as_completed(futures):
            yield future
            raise KeyboardInterrupt

    monkeypatch.setattr(clone_module, "as_completed", interrupted_after_first)
    with pytest.raises(typer.Exit) as exc:
        command(root, manifest, jobs=1).execute()

    apps = root / "apps"
    assert exc.value.exit_code == 130
    assert leftovers(apps) == []  # no temp folders, lock released
    assert "cancelled" in flat(capsys.readouterr().out)

    monkeypatch.setattr(clone_module, "as_completed", real_as_completed)
    command(root, manifest, retry_failed=True).execute()
    assert sorted(p.name for p in apps.iterdir()) == ["core", "crm", "hr"]


def test_transient_network_errors_are_retried(env, monkeypatch, capsys):
    root, remotes = env
    monkeypatch.setattr(clone_module, "RETRY_BACKOFF_SECONDS", 0)
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])
    cmd = command(root, manifest)
    real_git, attempts = cmd._git, []

    def flaky_git(args, **kwargs):
        if args[0] == "clone":
            attempts.append(1)
            if len(attempts) < 3:
                raise clone_module.GitFailed("fatal: unable to access: Could not resolve host: gitlab")
        return real_git(args, **kwargs)

    monkeypatch.setattr(cmd, "_git", flaky_git)
    cmd.execute()

    assert len(attempts) == 3 and (root / "apps" / "core" / ".git").exists()


def test_persistent_errors_fail_after_the_retries(env, monkeypatch, capsys):
    root, remotes = env
    monkeypatch.setattr(clone_module, "RETRY_BACKOFF_SECONDS", 0)
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])
    cmd = command(root, manifest)
    attempts = []

    def always_down(args, **kwargs):
        if args[0] == "clone":
            attempts.append(1)
            raise clone_module.GitFailed("fatal: early EOF")
        return real_git(args, **kwargs)

    real_git = cmd._git
    monkeypatch.setattr(cmd, "_git", always_down)
    with pytest.raises(typer.Exit) as exc:
        cmd.execute()

    assert exc.value.exit_code == 1 and len(attempts) == clone_module.RETRIES + 1
    assert leftovers(root / "apps") == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as a normal user")
def test_read_only_target_directory_is_a_clean_error(env, capsys):
    root, remotes = env
    apps = root / "apps"
    apps.mkdir()
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])
    apps.chmod(0o555)
    try:
        with pytest.raises(typer.Exit) as exc:
            command(root, manifest).execute()
    finally:
        apps.chmod(0o755)

    assert exc.value.exit_code == 1
    assert "Can't write into" in flat(capsys.readouterr().out)


def test_permission_denied_while_cloning_is_a_failure_not_no_access(env, monkeypatch, capsys):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])
    cmd = command(root, manifest)
    real_git = cmd._git

    def disk_denied(args, **kwargs):
        if args[0] == "clone":
            raise clone_module.GitFailed("fatal: could not create work tree dir 'x': Permission denied")
        return real_git(args, **kwargs)

    monkeypatch.setattr(cmd, "_git", disk_denied)
    with pytest.raises(typer.Exit) as exc:
        cmd.execute()

    assert exc.value.exit_code == 1
    assert "1 failed" in flat(capsys.readouterr().out)


def test_tokens_in_git_output_are_masked(env, capsys):
    root, _ = env
    secret = "glpat-SuperSecret123"
    manifest = write_manifest(root / "p.toml", [{"url": str(root / "remotes" / f"{secret}.git"), "dir": "x"}])
    cmd = command(root, manifest)
    cmd.redact.add(secret)

    cmd.execute()

    out = capsys.readouterr().out
    assert secret not in out and "***" in out


def test_depth_and_partial_cannot_be_combined(env):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])
    with pytest.raises(typer.Exit):
        command(root, manifest, depth=1, partial=True).execute()
    assert not (root / "apps").exists()


def test_clone_flags(env):
    root, remotes = env
    manifest = write_manifest(root / "p.toml", [{"url": str(remotes["core"])}])
    item = clone_module.PlanItem(clone_module.explicit_specs(clone_module.load_manifest(manifest))[0],
                                 root / "x", clone_module.Action.CLONE)

    assert "--filter=blob:none" in command(root, manifest, partial=True)._clone_args(item)
    shallow = command(root, manifest, depth=1)._clone_args(item)
    assert shallow[-3:] == ["--depth", "1", "--single-branch"]
    assert command(root, manifest, branch="main")._clone_args(item)[-2:] == ["--branch", "main"]


def test_credential_helper_is_scoped_to_the_host_and_resets_inherited_helpers(env, monkeypatch):
    root, remotes = env
    target = root / "apps" / "core"
    target.parent.mkdir()
    git(target.parent, "clone", "-q", str(remotes["core"]), "core")
    manifest = write_manifest(root / "p.toml", [{"path": "erp/core"}], host="gitlab.example.test")
    cmd = command(root, manifest, persist_credentials=True)
    monkeypatch.setattr(clone_module, "credential_helper_command", lambda: "!benchops git credential")
    spec = clone_module.explicit_specs(clone_module.load_manifest(manifest))[0]
    item = clone_module.PlanItem(spec, target, clone_module.Action.CLONE, url="https://gitlab.example.test/erp/core.git")
    cmd.hosts["gitlab.example.test"] = clone_module.HostAuth(GitLabProvider("gitlab.example.test"), token="t")

    assert cmd._wants_helper(item)
    cmd._install_helper(item)

    helpers = git(target, "config", "--get-all", "credential.https://gitlab.example.test.helper").splitlines()
    assert helpers == ["", "!benchops git credential"]
    item.url = "git@gitlab.example.test:erp/core.git"
    assert not cmd._wants_helper(item)  # ssh clones use keys, never the helper


# --- tokens and discovery -------------------------------------------------

class OneToken:
    def __init__(self, host, token):
        self.host, self.token = host, token

    def get(self, host):
        return (self.token, "keyring") if host == self.host else (None, None)


def test_rejected_token_stops_the_run_before_anything_is_written(env, monkeypatch, capsys):
    root, _ = env

    def rejected(self):
        raise clone_module.AuthError("401")

    monkeypatch.setattr(GitLabProvider, "whoami", rejected)
    monkeypatch.setattr(GitLabProvider, "repo", lambda self, path: pytest.fail("checked repos with a bad token"))
    manifest = write_manifest(root / "p.toml", [{"path": "erp/core"}], host="gitlab.example.test", provider="gitlab")
    cmd = command(root, manifest)
    cmd.tokens = OneToken("gitlab.example.test", "glpat-expired")

    with pytest.raises(typer.Exit) as exc:
        cmd.execute()

    out = flat(capsys.readouterr().out)
    assert exc.value.exit_code == 1 and "rejected the token" in out and "glpat-expired" not in out
    assert not (root / "apps").exists()


def test_missing_keyring_falls_back_to_anonymous(env, capsys):
    root, remotes = env

    class NoKeyring:
        def get(self, host):
            raise clone_module.KeyringUnavailableError("no backend")

    manifest = write_manifest(root / "p.toml", [{"url": "https://github.com/frappe/hrms"}])
    cmd = command(root, manifest, dry_run=True)
    cmd.tokens = NoKeyring()
    cmd._load_project = lambda: ("p", clone_module.validate_project({"repos": [{"url": str(remotes["core"])}]}))
    cmd.project = {}
    cmd._host("github.com")

    out = flat(capsys.readouterr().out)
    assert "BENCHOPS_GIT_TOKEN_GITHUB_COM" in out and cmd.hosts["github.com"].token is None


def test_group_discovery_merges_with_explicit_entries(env, monkeypatch, capsys):
    root, remotes = env
    discovered = [RemoteRepo(f"erp/{name}", https_url=str(bare), ssh_url=str(bare)) for name, bare in remotes.items()]
    discovered.append(RemoteRepo("erp/legacy", https_url="x", ssh_url="x"))
    by_path = {repo.path: repo for repo in discovered}
    monkeypatch.setattr(GitLabProvider, "list_group", lambda self, group: discovered)
    monkeypatch.setattr(GitLabProvider, "repo", lambda self, path: RepoAccess(Access.WRITE, by_path[path]))
    manifest = root / "p.toml"
    manifest.write_text(
        'host = "gitlab.example.test"\nprovider = "gitlab"\ngroup = "erp"\nmatch = "[chc]*"\n'
        '[[repos]]\npath = "erp/hr"\nbranch = "main"\n'
    )

    command(root, manifest).execute()

    apps = root / "apps"
    assert sorted(p.name for p in apps.iterdir()) == ["core", "crm", "hr"]  # "legacy" filtered by match
    assert git(apps / "hr", "branch", "--show-current").strip() == "main"  # explicit entry wins
    assert git(apps / "core", "branch", "--show-current").strip() == "develop"
