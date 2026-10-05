"""End-to-end git-based deploys: a local bench and a "remote" bench on disk,
with LocalRunner standing in for SSH so the real remote agent runs against
real git repositories.
"""

import json
import os
import platform
import subprocess
import sys

import pytest
import typer

from benchops.deploy import DeployCommand
from benchops.gitlocal import LocalRepo
from benchops.runner import LocalRunner

pytestmark = pytest.mark.skipif(platform.system() == "Windows", reason="remote bench is always POSIX")

APP = "myapp"
BUNDLE = "/assets/myapp/dist/js/myapp.bundle.AAA.js"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(path, content=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def make_local_bench(root, user="Dev", clone_from=None):
    """A developer's bench: built assets plus the app as its own git repo,
    either fresh (one commit) or cloned from another developer's app."""
    app = root / "apps" / APP
    if clone_from is None:
        write(app / ".gitignore", "myapp/public/dist/\n")
        write(app / APP / "__init__.py")
        write(app / APP / "hooks.py", "app_name = 'myapp'\n")
        write(app / APP / "modules.txt", "Sales\n")
        write(app / APP / "sales" / "doctype" / "visit" / "visit.json", "{}")
        git(app, "init", "-q", "-b", "develop")
    else:
        app.parent.mkdir(parents=True)
        git(app.parent, "clone", "-q", str(clone_from), APP)
    git(app, "config", "user.name", user)
    git(app, "config", "user.email", f"{user.lower()}@example.com")
    if clone_from is None:
        git(app, "add", "-A")
        git(app, "commit", "-q", "-m", "initial")

    write(app / APP / "public" / "dist" / "js" / "myapp.bundle.AAA.js", "console.log(1)")
    write(root / "sites" / "assets" / "assets.json", json.dumps({"myapp.bundle.js": BUNDLE}))
    os.symlink(app / APP / "public", root / "sites" / "assets" / APP)
    return app


@pytest.fixture
def benches(tmp_path, monkeypatch):
    local, remote = tmp_path / "local", tmp_path / "remote"
    make_local_bench(local)

    # The remote mimics an app deployed by the old archive flow: same files,
    # no .git, plus a leftover and a file created on staging.
    write(remote / "sites" / "common_site_config.json", "{}")
    write(
        remote / "sites" / "assets" / "assets.json",
        json.dumps({"desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.F.js"}),
    )
    (remote / "env" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, remote / "env" / "bin" / "python")
    rapp = remote / "apps" / APP
    write(rapp / APP / "__init__.py")
    write(rapp / APP / "hooks.py", "app_name = 'myapp'\n")
    write(rapp / APP / "modules.txt", "Sales\n")
    write(rapp / APP / "sales" / "doctype" / "visit" / "visit.json", "{}")
    write(rapp / APP / "sales" / "doctype" / "old_thing" / "old_thing.json", "{}")

    monkeypatch.chdir(local)
    return local, remote


def deploy(remote, config=None, site=None, **options):
    command = DeployCommand(server_alias="staging", app_name=APP, site=site, **{"yes": True, **options})
    command._get_server_config = lambda: {"bench_path": str(remote), **(config or {})}
    command._get_remote_runner = lambda server_config: LocalRunner()
    command.execute()


def remote_status(remote):
    return git(remote / "apps" / APP, "status", "--porcelain", "--untracked-files=all")


def staging_owned(remote):
    return (remote / "apps" / APP / ".git" / "info" / "exclude").read_text()


def record(remote):
    return json.loads((remote / "apps" / APP / ".git" / "benchops" / "deploy.json").read_text())


def test_first_deploy_requires_adopt(benches):
    local, remote = benches

    with pytest.raises(typer.Exit):
        deploy(remote)

    assert not (remote / "apps" / APP / ".git").exists()


def test_adopt_then_deploy_lifecycle(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP

    deploy(remote, adopt=True)

    assert git(rapp, "rev-parse", "HEAD^{tree}") == git(app, "rev-parse", "HEAD^{tree}")
    assert remote_status(remote) == ""
    assert "/myapp/sales/doctype/old_thing/old_thing.json" in staging_owned(remote)
    assert (rapp / APP / "public" / "dist" / "js" / "myapp.bundle.AAA.js").is_file()
    assets = json.loads((remote / "sites" / "assets" / "assets.json").read_text())
    assert assets == {"desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.F.js", "myapp.bundle.js": BUNDLE}
    assert record(remote)["branch"] == "develop"
    assert not list((rapp / ".git" / "benchops").glob("package-*"))
    assert not (rapp / ".git" / "benchops" / "lock.json").exists()

    # Local: delete a committed file, leave an uncommitted edit and a new untracked file.
    git(app, "rm", "-q", f"{APP}/sales/doctype/visit/visit.json")
    git(app, "commit", "-q", "-m", "drop visit")
    write(app / APP / "hooks.py", "app_name = 'myapp'\nlocal = True\n")
    write(app / APP / "sales" / "doctype" / "note" / "note.json", "{}")
    # Staging: a Desk edit to a deployed file and a Desk-created file.
    write(rapp / APP / "hooks.py", "staging edit\n")
    write(rapp / APP / "sales" / "report" / "by_region" / "by_region.json", "{}")

    deploy(remote)

    assert not (rapp / APP / "sales" / "doctype" / "visit").exists()
    assert (rapp / APP / "hooks.py").read_text() == "app_name = 'myapp'\nlocal = True\n"
    assert (rapp / APP / "sales" / "doctype" / "note" / "note.json").is_file()
    assert (rapp / APP / "sales" / "report" / "by_region" / "by_region.json").is_file()
    assert "/myapp/sales/report/by_region/by_region.json" in staging_owned(remote)
    assert remote_status(remote) == ""
    backups = git(rapp, "for-each-ref", "--format=%(refname)", "refs/benchops/overwritten/")
    assert backups.strip()
    assert git(rapp, "show", f"{backups.split()[0]}:{APP}/hooks.py") == "staging edit\n"
    assert record(remote)["uncommitted"] == sorted([f"{APP}/hooks.py", f"{APP}/sales/doctype/note/note.json"])

    # The developer's own index and working tree were not touched.
    assert git(app, "status", "--porcelain", "--untracked-files=all") == (
        f" M {APP}/hooks.py\n?? {APP}/sales/doctype/note/note.json\n"
    )


def test_adopt_can_delete_leftovers(benches, monkeypatch):
    local, remote = benches
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "d")
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    deploy(remote, adopt=True, yes=False)

    assert not (remote / "apps" / APP / APP / "sales" / "doctype" / "old_thing").exists()
    assert "old_thing" not in staging_owned(remote)


def test_second_deploy_is_a_no_op(benches, capsys):
    local, remote = benches
    deploy(remote, adopt=True)
    first = record(remote)

    deploy(remote)

    assert record(remote) == first
    assert "already up to date" in capsys.readouterr().out


def test_server_ahead_of_local_history_blocks_unless_forced(benches):
    local, remote = benches
    app = local / "apps" / APP
    write(app / APP / "extra.py", "x = 1\n")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "extra")
    deploy(remote, adopt=True)
    git(app, "reset", "-q", "--hard", "HEAD~1")

    with pytest.raises(typer.Exit):
        deploy(remote)
    assert (remote / "apps" / APP / APP / "extra.py").is_file()

    deploy(remote, force=True)
    assert not (remote / "apps" / APP / APP / "extra.py").exists()


def test_held_lock_blocks_until_broken(benches):
    local, remote = benches
    deploy(remote, adopt=True)
    lock = remote / "apps" / APP / ".git" / "benchops" / "lock.json"
    lock.write_text(json.dumps({"token": "someone-else", "owner": "ali@laptop", "started": "earlier"}))
    write(local / "apps" / APP / APP / "new.py")

    with pytest.raises(typer.Exit):
        deploy(remote)
    assert lock.exists()
    assert not (remote / "apps" / APP / APP / "new.py").exists()

    deploy(remote, break_lock=True)
    assert (remote / "apps" / APP / APP / "new.py").is_file()
    assert not lock.exists()


def test_local_file_at_staging_only_path_wins(benches):
    local, remote = benches
    deploy(remote, adopt=True)
    write(local / "apps" / APP / APP / "sales" / "doctype" / "old_thing" / "old_thing.json", '{"local": 1}')

    deploy(remote)

    rfile = remote / "apps" / APP / APP / "sales" / "doctype" / "old_thing" / "old_thing.json"
    assert rfile.read_text() == '{"local": 1}'
    assert "old_thing" not in staging_owned(remote)
    assert remote_status(remote) == ""


def test_snapshot_ignores_caches_and_build_output(benches, monkeypatch):
    local, _ = benches
    app = local / "apps" / APP
    # Neither the app's nor the developer's ignore rules may be what keeps these out.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    write(app / ".gitignore", "")
    git(app, "commit", "-q", "-am", "no ignore rules")
    write(app / APP / "__pycache__" / "hooks.cpython-312.pyc", "x")
    write(app / APP / "public" / "dist" / "js" / "new.js", "x")
    write(app / APP / "api.py", "def f(): pass\n")

    snap = LocalRepo.open(app).snapshot(APP, "dev@laptop")

    files = git(app, "ls-tree", "-r", "--name-only", snap.commit).split()
    assert f"{APP}/api.py" in files
    assert not [f for f in files if "__pycache__" in f or "/dist/" in f]
    assert snap.uncommitted == [("A", f"{APP}/api.py")]
    assert git(app, "rev-parse", f"{snap.commit}^") == git(app, "rev-parse", "HEAD")


def test_staging_edit_while_plan_is_shown_aborts_apply(benches, monkeypatch):
    local, remote = benches
    deploy(remote, adopt=True)
    write(local / "apps" / APP / APP / "api.py", "x = 1\n")
    hooks = remote / "apps" / APP / APP / "hooks.py"

    def desk_save_then_confirm(*args, **kwargs):
        hooks.write_text("saved in Desk meanwhile\n")
        return True

    monkeypatch.setattr(typer, "confirm", desk_save_then_confirm)

    with pytest.raises(typer.Exit):
        deploy(remote, yes=False)

    assert hooks.read_text() == "saved in Desk meanwhile\n"
    assert not (remote / "apps" / APP / APP / "api.py").exists()
    assert not (remote / "apps" / APP / ".git" / "benchops" / "lock.json").exists()


def test_staging_files_in_removed_folder_are_reported(benches, capsys):
    local, remote = benches
    app = local / "apps" / APP
    write(app / APP / "legacy" / "doctype" / "a" / "a.json", "{}")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "legacy module")
    deploy(remote, adopt=True)
    write(remote / "apps" / APP / APP / "legacy" / "doctype" / "b" / "b.json", "{}")
    git(app, "rm", "-r", "-q", f"{APP}/legacy")
    git(app, "commit", "-q", "-m", "drop legacy")
    capsys.readouterr()

    deploy(remote)

    out = capsys.readouterr().out
    assert "left inside folders this deploy removes: 1" in out
    assert (remote / "apps" / APP / APP / "legacy" / "doctype" / "b" / "b.json").is_file()
    assert not (remote / "apps" / APP / APP / "legacy" / "doctype" / "a").exists()


def flat(text):
    """Rich wraps long lines; compare output with whitespace collapsed."""
    return " ".join(text.split())


def test_hooks_run_in_order_around_apply(benches, tmp_path):
    local, remote = benches
    log = tmp_path / "hooks.log"
    deploy(remote, adopt=True)
    write(local / "apps" / APP / APP / "api.py", "x = 1\n")
    applied = f"test -f apps/{APP}/{APP}/api.py && echo applied || echo pending"
    config = {
        "pre_local_commands": [
            f'sh -c "echo pre-local >> {log}"',
            # A pre-local hook that writes app files (e.g. export-fixtures) is part of the snapshot.
            f'sh -c "mkdir -p {APP}/{APP}/fixtures && echo [] > {APP}/{APP}/fixtures/custom_field.json"',
        ],
        "pre_remote_commands": [f'sh -c "echo pre-remote:$({applied}) >> {log}"'],
        "post_remote_commands": [f'sh -c "echo post-remote:{{site}}:$({applied}) >> {log}"'],
    }

    deploy(remote, config=config, site="s1.local")

    assert log.read_text().split() == ["pre-local", "pre-remote:pending", "post-remote:s1.local:applied"]
    assert (remote / "apps" / APP / APP / "fixtures" / "custom_field.json").is_file()


def test_failing_post_remote_hook_reports_failure_and_releases_lock(benches):
    local, remote = benches
    deploy(remote, adopt=True)
    write(local / "apps" / APP / APP / "api.py", "x = 1\n")

    with pytest.raises(typer.Exit):
        deploy(remote, config={"post_remote_commands": ["false"]})

    assert (remote / "apps" / APP / APP / "api.py").is_file()
    assert not (remote / "apps" / APP / ".git" / "benchops" / "lock.json").exists()


def test_declining_the_plan_changes_nothing(benches, monkeypatch):
    local, remote = benches
    deploy(remote, adopt=True)
    before = record(remote)
    write(local / "apps" / APP / APP / "api.py", "x = 1\n")
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)

    with pytest.raises(typer.Exit):
        deploy(remote, yes=False)

    rapp = remote / "apps" / APP
    assert not (rapp / APP / "api.py").exists()
    assert record(remote) == before
    assert remote_status(remote) == ""
    assert not (rapp / ".git" / "benchops" / "lock.json").exists()
    assert not list((rapp / ".git" / "benchops").glob("package-*"))
    assert not (rapp / ".git" / "benchops" / "incoming").exists()


def test_staging_edits_are_reverted_when_local_is_unchanged(benches):
    local, remote = benches
    deploy(remote, adopt=True)
    before = record(remote)
    hooks = remote / "apps" / APP / APP / "hooks.py"
    hooks.write_text("edited in Desk\n")

    deploy(remote)

    assert hooks.read_text() == "app_name = 'myapp'\n"
    assert remote_status(remote) == ""
    assert record(remote)["snapshot"] == before["snapshot"]
    backups = git(remote / "apps" / APP, "for-each-ref", "--format=%(refname)", "refs/benchops/overwritten/")
    assert backups.strip()


def test_asset_only_change_replaces_dist_and_manifest(benches, capsys):
    local, remote = benches
    write(local / "apps" / APP / APP / "public" / "dist" / "js" / "myapp.removed.bundle.R.js", "")
    write(
        local / "sites" / "assets" / "assets.json",
        json.dumps({"myapp.bundle.js": BUNDLE, "myapp.removed.bundle.js": "/assets/myapp/dist/js/myapp.removed.bundle.R.js"}),
    )
    deploy(remote, adopt=True)
    assert "myapp.removed.bundle.js" in json.loads((remote / "sites" / "assets" / "assets.json").read_text())
    before = record(remote)
    (local / "apps" / APP / APP / "public" / "dist" / "js" / "myapp.removed.bundle.R.js").unlink()
    dist = local / "apps" / APP / APP / "public" / "dist" / "js"
    (dist / "myapp.bundle.AAA.js").unlink()
    write(dist / "myapp.bundle.BBB.js", "console.log(2)")
    new_bundle = "/assets/myapp/dist/js/myapp.bundle.BBB.js"
    write(local / "sites" / "assets" / "assets.json", json.dumps({"myapp.bundle.js": new_bundle}))
    capsys.readouterr()

    deploy(remote)

    rdist = remote / "apps" / APP / APP / "public" / "dist" / "js"
    assert sorted(p.name for p in rdist.iterdir()) == ["myapp.bundle.BBB.js"]
    assets = json.loads((remote / "sites" / "assets" / "assets.json").read_text())
    assert assets["myapp.bundle.js"] == new_bundle
    assert assets["desk.bundle.js"] == "/assets/frappe/dist/js/desk.bundle.F.js"
    assert record(remote)["snapshot"] == before["snapshot"]
    assert record(remote)["dist_digest"] != before["dist_digest"]
    assert "myapp.removed.bundle.js" not in assets
    assert "Built assets: shipped" in capsys.readouterr().out


def test_head_moved_on_server_blocks_unless_forced(benches):
    local, remote = benches
    rapp = remote / "apps" / APP
    deploy(remote, adopt=True)
    git(rapp, "-c", "user.name=ops", "-c", "user.email=ops@x", "commit", "-q", "--allow-empty", "-m", "hotfix")
    write(local / "apps" / APP / APP / "api.py", "x = 1\n")

    with pytest.raises(typer.Exit):
        deploy(remote)
    assert not (rapp / APP / "api.py").exists()

    deploy(remote, force=True)
    assert (rapp / APP / "api.py").is_file()
    assert git(rapp, "log", "-1", "--format=%s").startswith("benchops snapshot")


def test_server_repo_without_record_requires_adopt(benches, capsys):
    local, remote = benches
    git(remote / "apps" / APP, "init", "-q")

    with pytest.raises(typer.Exit):
        deploy(remote)

    assert "--adopt" in flat(capsys.readouterr().out)
    assert not (remote / "apps" / APP / ".git" / "benchops" / "deploy.json").exists()


def test_adopt_over_existing_git_checkout(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    # As installed by `bench get-app`: a clone of the app, plus an untracked leftover.
    subprocess.run(["rm", "-rf", str(rapp)], check=True)
    git(rapp.parent, "clone", "-q", str(app), APP)
    write(rapp / APP / "leftover.py", "")
    git(app, "rm", "-q", f"{APP}/sales/doctype/visit/visit.json")
    git(app, "commit", "-q", "-m", "drop visit")

    deploy(remote, adopt=True)

    assert not (rapp / APP / "sales" / "doctype" / "visit").exists()
    assert (rapp / APP / "leftover.py").is_file()
    assert "/myapp/leftover.py" in staging_owned(remote)
    assert git(rapp, "rev-parse", "HEAD^{tree}") == git(app, "rev-parse", "HEAD^{tree}")
    assert remote_status(remote) == ""


def test_adopt_choose_per_file(benches, monkeypatch):
    local, remote = benches
    write(remote / "apps" / APP / APP / "sales" / "report" / "r" / "r.json", "{}")
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "c")
    monkeypatch.setattr(typer, "confirm", lambda message, **k: not message.endswith("old_thing.json?"))

    deploy(remote, adopt=True, yes=False)

    rapp = remote / "apps" / APP
    assert not (rapp / APP / "sales" / "doctype" / "old_thing").exists()
    assert (rapp / APP / "sales" / "report" / "r" / "r.json").is_file()
    assert "/myapp/sales/report/r/r.json" in staging_owned(remote)


def test_two_developers_sharing_a_server(benches, tmp_path, monkeypatch, capsys):
    local, remote = benches
    app_a = local / "apps" / APP
    write(app_a / APP / "wip.py", "work in progress\n")
    deploy(remote, adopt=True)

    # Ali clones Dev's committed history (not the uncommitted wip.py) and deploys.
    bench_b = tmp_path / "ali"
    app_b = make_local_bench(bench_b, user="Ali", clone_from=app_a)
    monkeypatch.chdir(bench_b)
    capsys.readouterr()
    deploy(remote)
    assert "replaces 1 uncommitted file(s) deployed by Dev@" in flat(capsys.readouterr().out)
    assert not (remote / "apps" / APP / APP / "wip.py").exists()

    # Ali commits work Dev doesn't have yet; Dev must not be able to roll it back.
    write(app_b / APP / "ali.py", "x = 1\n")
    git(app_b, "add", "-A")
    git(app_b, "commit", "-q", "-m", "ali's feature")
    deploy(remote)

    monkeypatch.chdir(local)
    capsys.readouterr()
    with pytest.raises(typer.Exit):
        deploy(remote)
    assert "not in your branch's history" in flat(capsys.readouterr().out)
    assert (remote / "apps" / APP / APP / "ali.py").is_file()

    git(app_a, "pull", "-q", "--no-rebase", str(app_b), "develop")
    deploy(remote)
    assert (remote / "apps" / APP / APP / "ali.py").is_file()
    assert (remote / "apps" / APP / APP / "wip.py").is_file()


def test_plan_warnings(benches, capsys):
    local, remote = benches
    write(local / "apps" / "frappe" / "frappe" / "__init__.py", '__version__ = "15.1.0"\n')
    write(remote / "apps" / "frappe" / "frappe" / "__init__.py", '__version__ = "15.2.0"\n')
    deploy(remote, adopt=True)
    app = local / "apps" / APP
    git(app, "checkout", "-q", "-b", "feature/x")
    write(app / APP / "api.py", "x = 1\n")
    write(remote / "apps" / APP / APP / "modules.txt", "Sales\nStaging Module\n")
    capsys.readouterr()

    deploy(remote)

    out = flat(capsys.readouterr().out)
    assert "Switching the server from 'develop'" in out
    assert "to 'feature/x'" in out
    assert "modules.txt was edited on the server" in out
    assert "Frappe versions differ (local 15.1.0, server 15.2.0)" in out
    assert "is not pushed to any remote yet" in out


def test_lock_taken_over_mid_deploy_applies_nothing(benches, monkeypatch):
    local, remote = benches
    deploy(remote, adopt=True)
    write(local / "apps" / APP / APP / "api.py", "x = 1\n")
    lock = remote / "apps" / APP / ".git" / "benchops" / "lock.json"

    def someone_breaks_the_lock(*args, **kwargs):
        lock.write_text(json.dumps({"token": "other", "owner": "ali@laptop"}))
        return True

    monkeypatch.setattr(typer, "confirm", someone_breaks_the_lock)

    with pytest.raises(typer.Exit):
        deploy(remote, yes=False)

    assert not (remote / "apps" / APP / APP / "api.py").exists()
    assert json.loads(lock.read_text())["token"] == "other"
