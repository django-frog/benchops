"""End-to-end deploys in the staged-overlay model: a local bench and a
"staging" bench on disk (the app cloned there, as `bench get-app` would),
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
from benchops.runner import LocalRunner
from benchops.status import StatusCommand

pytestmark = pytest.mark.skipif(platform.system() == "Windows", reason="remote bench is always POSIX")

APP = "myapp"
BUNDLE = "/assets/myapp/dist/js/myapp.bundle.AAA.js"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(path, content=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def flat(text):
    """Rich wraps long lines; compare output with whitespace collapsed."""
    return " ".join(text.split())


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
def tools_log(tmp_path, monkeypatch):
    """Stand-ins for `bench` and `yarn` on PATH that log each call as
    "<tool> <cwd> <args>"."""
    bin_dir, log = tmp_path / "bin", tmp_path / "tools.log"
    bin_dir.mkdir()
    for tool in ("bench", "yarn"):
        script = bin_dir / tool
        script.write_text(f'#!/bin/sh\necho "{tool} $PWD $*" >> "{log}"\n')
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    log.write_text("")
    return log


@pytest.fixture
def benches(tmp_path, monkeypatch, tools_log):
    local, remote = tmp_path / "local", tmp_path / "remote"
    app = make_local_bench(local)

    write(remote / "sites" / "common_site_config.json", "{}")
    write(
        remote / "sites" / "assets" / "assets.json",
        json.dumps({"desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.F.js"}),
    )
    (remote / "env" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, remote / "env" / "bin" / "python")
    (remote / "apps").mkdir()
    git(remote / "apps", "clone", "-q", str(app), APP)

    monkeypatch.chdir(local)
    return local, remote


def deploy(remote, config=None, site=None, **options):
    command = DeployCommand(server_alias="staging", app_name=APP, site=site, **{"yes": True, **options})
    command._get_server_config = lambda: {"bench_path": str(remote), **(config or {})}
    command._get_remote_runner = lambda server_config: LocalRunner()
    command.execute()


def status(path):
    return git(path, "status", "--porcelain", "--untracked-files=all").splitlines()


def record(remote):
    return json.loads((remote / "apps" / APP / ".git" / "benchops" / "deploy.json").read_text())


def lock_file(remote):
    return remote / "apps" / APP / ".git" / "benchops" / "lock.json"


# --------------------------------------------------------------------------- what ships


def test_ships_commits_and_staged_changes_only(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "committed.py", "c = 1\n")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "add committed.py")
    write(app / APP / "hooks.py", "app_name = 'myapp'\nstaged = True\n")
    git(app, "add", f"{APP}/hooks.py")
    write(app / APP / "hooks.py", "app_name = 'myapp'\nstaged = True\nunstaged = True\n")
    write(app / APP / "modules.txt", "Sales\nUnstaged\n")
    write(app / APP / "untracked.py", "u = 1\n")
    local_status_before = status(app)

    deploy(remote)

    assert git(rapp, "rev-parse", "HEAD") == git(app, "rev-parse", "HEAD")
    assert git(rapp, "symbolic-ref", "--short", "HEAD").strip() == "develop"
    assert (rapp / APP / "committed.py").read_text() == "c = 1\n"
    assert (rapp / APP / "hooks.py").read_text() == "app_name = 'myapp'\nstaged = True\n"
    assert (rapp / APP / "modules.txt").read_text() == "Sales\n"
    assert not (rapp / APP / "untracked.py").exists()
    assert status(rapp) == [f"M  {APP}/hooks.py"]
    assert record(remote)["staged"] == [f"{APP}/hooks.py"]
    # The developer's own index and working tree were not touched.
    assert status(app) == local_status_before
    assert not (rapp / ".git" / "benchops" / "lock.json").exists()
    assert not list((rapp / ".git" / "benchops").glob("package-*"))


def test_work_done_on_staging_is_left_alone_and_visible(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(rapp / APP / "sales" / "doctype" / "visit" / "visit.json", '{"edited": "on staging"}')
    write(rapp / APP / "sales" / "report" / "r" / "r.json", "{}")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    deploy(remote)

    assert (rapp / APP / "sales" / "doctype" / "visit" / "visit.json").read_text() == '{"edited": "on staging"}'
    assert (rapp / APP / "sales" / "report" / "r" / "r.json").is_file()
    assert sorted(status(rapp)) == sorted([
        f"A  {APP}/api.py",
        f" M {APP}/sales/doctype/visit/visit.json",
        f"?? {APP}/sales/report/r/r.json",
    ])


def test_staged_deletion_removes_the_file_on_staging(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    git(app, "rm", "-q", f"{APP}/sales/doctype/visit/visit.json")

    deploy(remote)

    assert not (rapp / APP / "sales" / "doctype" / "visit").exists()
    assert status(rapp) == [f"D  {APP}/sales/doctype/visit/visit.json"]


def test_previous_deploys_staged_files_stay_on_disk_as_unstaged(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "first deploy\n")
    git(app, "add", "-A")
    deploy(remote)
    git(app, "restore", "--staged", f"{APP}/hooks.py")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", f"{APP}/api.py")

    deploy(remote)

    assert (rapp / APP / "hooks.py").read_text() == "first deploy\n"
    assert sorted(status(rapp)) == sorted([f"A  {APP}/api.py", f" M {APP}/hooks.py"])


def test_two_developers_deploys_coexist(benches, tmp_path, monkeypatch):
    local, remote = benches
    app_a, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app_a / APP / "whatsapp.py", "by dev\n")
    git(app_a, "add", "-A")
    deploy(remote)

    bench_b = tmp_path / "ali"
    app_b = make_local_bench(bench_b, user="Ali", clone_from=app_a)
    write(app_b / APP / "service.py", "by ali\n")
    git(app_b, "add", "-A")
    monkeypatch.chdir(bench_b)
    deploy(remote)

    assert (rapp / APP / "whatsapp.py").read_text() == "by dev\n"
    assert (rapp / APP / "service.py").read_text() == "by ali\n"
    assert sorted(status(rapp)) == sorted([f"A  {APP}/service.py", f"?? {APP}/whatsapp.py"])
    assert record(remote)["deployer"].startswith("Ali@")


# --------------------------------------------------------------------------- overlaps


def test_overlap_asks_with_no_as_default_and_changes_nothing_on_no(benches, monkeypatch, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(rapp / APP / "hooks.py", "edited on staging\n")
    write(app / APP / "hooks.py", "mine\n")
    git(app, "add", "-A")
    head_before = git(rapp, "rev-parse", "HEAD")
    prompts = []

    def answer(message, default=None, **kwargs):
        prompts.append((message, default))
        return False

    monkeypatch.setattr(typer, "confirm", answer)

    with pytest.raises(typer.Exit):
        deploy(remote, yes=False)

    out = flat(capsys.readouterr().out)
    assert "OVERWRITE" in out and f"{APP}/hooks.py (modified on staging)" in out
    assert prompts == [("Overwrite these files on staging with your version?", False)]
    assert (rapp / APP / "hooks.py").read_text() == "edited on staging\n"
    assert git(rapp, "rev-parse", "HEAD") == head_before
    assert not lock_file(remote).exists()


def test_confirmed_overlap_overwrites_after_backing_up_staging(benches, monkeypatch, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(rapp / APP / "hooks.py", "edited on staging\n")
    write(rapp / APP / "new_on_staging.py", "staging's new file\n")
    write(app / APP / "hooks.py", "mine\n")
    write(app / APP / "new_on_staging.py", "mine too\n")
    git(app, "add", "-A")
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    deploy(remote, yes=False)

    assert (rapp / APP / "hooks.py").read_text() == "mine\n"
    assert (rapp / APP / "new_on_staging.py").read_text() == "mine too\n"
    backup = git(rapp, "for-each-ref", "--format=%(refname)", "refs/benchops/overwritten/").split()[0]
    assert git(rapp, "show", f"{backup}:{APP}/hooks.py") == "edited on staging\n"
    assert git(rapp, "show", f"{backup}:{APP}/new_on_staging.py") == "staging's new file\n"
    assert f"Backed up on the server as {backup}" in flat(capsys.readouterr().out)


def test_overlap_with_yes_needs_overwrite(benches, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(rapp / APP / "hooks.py", "edited on staging\n")
    write(app / APP / "hooks.py", "mine\n")
    git(app, "add", "-A")

    with pytest.raises(typer.Exit):
        deploy(remote)
    assert "add --overwrite" in flat(capsys.readouterr().out)
    assert (rapp / APP / "hooks.py").read_text() == "edited on staging\n"

    deploy(remote, overwrite=True)
    assert (rapp / APP / "hooks.py").read_text() == "mine\n"


def test_staged_deletion_of_a_file_edited_on_staging_is_an_overlap(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(rapp / APP / "sales" / "doctype" / "visit" / "visit.json", '{"edited": 1}')
    git(app, "rm", "-q", f"{APP}/sales/doctype/visit/visit.json")

    with pytest.raises(typer.Exit):
        deploy(remote)
    assert (rapp / APP / "sales" / "doctype" / "visit" / "visit.json").is_file()


def test_identical_change_on_staging_is_not_an_overlap(benches, monkeypatch):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(rapp / APP / "hooks.py", "same content\n")
    write(app / APP / "hooks.py", "same content\n")
    git(app, "add", "-A")
    prompts = []
    monkeypatch.setattr(typer, "confirm", lambda message, **k: prompts.append(message) or True)

    deploy(remote, yes=False)

    assert prompts == ["Proceed with deploy?"]
    assert status(rapp) == [f"M  {APP}/hooks.py"]


def test_desk_save_on_a_deployed_file_while_plan_is_shown_aborts(benches, monkeypatch):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "mine\n")
    git(app, "add", "-A")

    def desk_save_then_confirm(*args, **kwargs):
        write(rapp / APP / "hooks.py", "saved in Desk meanwhile\n")
        return True

    monkeypatch.setattr(typer, "confirm", desk_save_then_confirm)

    with pytest.raises(typer.Exit):
        deploy(remote, yes=False)
    assert (rapp / APP / "hooks.py").read_text() == "saved in Desk meanwhile\n"
    assert not lock_file(remote).exists()


def test_edit_to_an_unrelated_file_while_plan_is_shown_does_not_block(benches, monkeypatch):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "mine\n")
    git(app, "add", "-A")

    def unrelated_edit_then_confirm(*args, **kwargs):
        write(rapp / APP / "modules.txt", "Sales\nEdited\n")
        return True

    monkeypatch.setattr(typer, "confirm", unrelated_edit_then_confirm)

    deploy(remote, yes=False)
    assert (rapp / APP / "hooks.py").read_text() == "mine\n"
    assert (rapp / APP / "modules.txt").read_text() == "Sales\nEdited\n"


# --------------------------------------------------------------------------- history, branch, lock


def test_commit_made_on_staging_blocks_unless_forced(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(rapp / APP / "hotfix.py", "fix = 1\n")
    git(rapp, "add", "-A")
    git(rapp, "-c", "user.name=ops", "-c", "user.email=ops@x", "commit", "-q", "-m", "hotfix on staging")
    hotfix = git(rapp, "rev-parse", "HEAD").strip()
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    with pytest.raises(typer.Exit):
        deploy(remote)
    assert git(rapp, "rev-parse", "HEAD").strip() == hotfix

    deploy(remote, force=True)
    assert git(rapp, "rev-parse", "HEAD") == git(app, "rev-parse", "HEAD")
    backup = git(rapp, "for-each-ref", "--format=%(objectname)", "refs/benchops/previous-head/").split()
    assert hotfix in backup


def test_upstream_commit_pulled_on_staging_is_fine(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "feature.py", "f = 1\n")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "feature")
    git(rapp, "pull", "-q", "--ff-only")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    deploy(remote)

    assert (rapp / APP / "api.py").is_file()


def test_staging_switches_to_the_developers_branch(benches, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    git(rapp, "checkout", "-q", "-b", "someone-elses-branch")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    deploy(remote)

    assert git(rapp, "symbolic-ref", "--short", "HEAD").strip() == "develop"
    assert "Switching the server from branch 'someone-elses-branch' to 'develop'" in flat(capsys.readouterr().out)


def test_diverged_branch_on_staging_is_backed_up_before_moving(benches, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    git(rapp, "checkout", "-q", "-b", "parked")
    git(rapp, "branch", "-q", "-D", "develop")
    git(rapp, "checkout", "-q", "-b", "develop")
    write(rapp / APP / "only_on_staging.py", "")
    git(rapp, "add", "-A")
    git(rapp, "-c", "user.name=ops", "-c", "user.email=ops@x", "commit", "-q", "-m", "staging-only commit")
    diverged = git(rapp, "rev-parse", "HEAD").strip()
    git(rapp, "checkout", "-q", "parked")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    deploy(remote)

    assert git(rapp, "symbolic-ref", "--short", "HEAD").strip() == "develop"
    assert git(rapp, "rev-parse", "HEAD") == git(app, "rev-parse", "HEAD")
    backups = git(rapp, "for-each-ref", "--format=%(objectname)", "refs/benchops/branch-backup/").split()
    assert diverged in backups
    assert "is backed up under refs/benchops/branch-backup/" in flat(capsys.readouterr().out)


def test_detached_local_head_detaches_staging(benches, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    git(app, "checkout", "-q", "--detach")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    deploy(remote)

    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=rapp).returncode != 0
    assert "the server's HEAD will be detached too" in flat(capsys.readouterr().out)


def test_held_lock_blocks_until_broken(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    (rapp / ".git" / "benchops").mkdir(parents=True)
    lock_file(remote).write_text(json.dumps({"token": "other", "owner": "ali@laptop", "started": "earlier"}))
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    with pytest.raises(typer.Exit):
        deploy(remote)
    assert lock_file(remote).exists()
    assert not (rapp / APP / "api.py").exists()

    deploy(remote, break_lock=True)
    assert (rapp / APP / "api.py").is_file()
    assert not lock_file(remote).exists()


def test_lock_taken_over_mid_deploy_applies_nothing(benches, monkeypatch):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    def someone_breaks_the_lock(*args, **kwargs):
        lock_file(remote).write_text(json.dumps({"token": "other", "owner": "ali@laptop"}))
        return True

    monkeypatch.setattr(typer, "confirm", someone_breaks_the_lock)

    with pytest.raises(typer.Exit):
        deploy(remote, yes=False)
    assert not (rapp / APP / "api.py").exists()
    assert json.loads(lock_file(remote).read_text())["token"] == "other"


def test_server_without_git_is_refused(benches, capsys):
    local, remote = benches
    subprocess.run(["rm", "-rf", str(remote / "apps" / APP / ".git")], check=True)

    with pytest.raises(typer.Exit):
        deploy(remote)
    assert "not a git repository" in flat(capsys.readouterr().out)


def test_second_identical_deploy_is_a_no_op(benches, capsys):
    local, remote = benches
    app = local / "apps" / APP
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    deploy(remote)
    first = record(remote)

    deploy(remote)

    assert record(remote) == first
    assert "already up to date" in capsys.readouterr().out


def test_converts_the_old_snapshot_layout(benches, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    base = git(rapp, "rev-parse", "HEAD").strip()
    write(rapp / APP / "old_uncommitted.py", "from the old snapshot deploy\n")
    git(rapp, "add", "-A")
    git(rapp, "-c", "user.name=b", "-c", "user.email=b@x", "commit", "-q", "-m", "benchops snapshot: develop")
    snapshot = git(rapp, "rev-parse", "HEAD").strip()
    git(rapp, "checkout", "-q", "--detach")
    git(rapp, "branch", "-q", "-f", "develop", base)
    write(rapp / ".git" / "benchops" / "deploy.json", json.dumps({"snapshot": snapshot, "base": base}))

    deploy(remote)

    assert git(rapp, "rev-parse", "HEAD").strip() == base
    assert (rapp / APP / "old_uncommitted.py").is_file()
    assert status(rapp) == [f"?? {APP}/old_uncommitted.py"]
    assert "Converted the server from the old snapshot layout" in flat(capsys.readouterr().out)


# --------------------------------------------------------------------------- hooks and builds


def test_hooks_run_in_order_around_apply(benches, tmp_path):
    local, remote = benches
    app = local / "apps" / APP
    log = tmp_path / "hooks.log"
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    applied = f"test -f apps/{APP}/{APP}/api.py && echo applied || echo pending"
    config = {
        "pre_local_commands": [f'sh -c "echo pre-local:{{app}} >> {log}"'],
        "pre_remote_commands": [f'sh -c "echo pre-remote:$({applied}) >> {log}"'],
        "post_remote_commands": [f'sh -c "echo post-remote:{{site}}:$({applied}) >> {log}"'],
    }

    deploy(remote, config=config, site="s1.local")

    assert log.read_text().split() == ["pre-local:myapp", "pre-remote:pending", "post-remote:s1.local:applied"]


def test_failing_post_remote_hook_reports_failure_and_releases_lock(benches):
    local, remote = benches
    app = local / "apps" / APP
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")

    with pytest.raises(typer.Exit):
        deploy(remote, config={"post_remote_commands": ["false"]})
    assert (remote / "apps" / APP / APP / "api.py").is_file()
    assert not lock_file(remote).exists()


def test_build_runs_locally_and_cache_is_cleared_on_the_server(benches, tools_log):
    local, remote = benches
    app = local / "apps" / APP
    write(app / "package.json", '{"scripts": {"build": "cd frontend && yarn build"}}')
    git(app, "add", "-A")

    deploy(remote)

    assert tools_log.read_text().splitlines() == [
        f"yarn {app} install",
        f"bench {local} build --app {APP}",
        f"bench {remote} --site all clear-cache",
    ]
    assert (remote / "apps" / APP / APP / "public" / "dist" / "js" / "myapp.bundle.AAA.js").is_file()
    assets = json.loads((remote / "sites" / "assets" / "assets.json").read_text())
    assert assets == {"desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.F.js", "myapp.bundle.js": BUNDLE}


def test_skip_build_ships_the_existing_build(benches, tools_log):
    local, remote = benches

    deploy(remote, skip_build=True)

    assert tools_log.read_text().splitlines() == [f"bench {remote} --site all clear-cache"]
    assert (remote / "apps" / APP / APP / "public" / "dist" / "js" / "myapp.bundle.AAA.js").is_file()


def test_cache_is_not_cleared_when_build_outputs_are_unchanged(benches, tools_log):
    local, remote = benches
    app = local / "apps" / APP
    deploy(remote)
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    tools_log.write_text("")

    deploy(remote)

    assert "clear-cache" not in tools_log.read_text()


VITE_CONFIG = """
export default defineConfig({
	plugins: [frappeui({ buildConfig: { outDir: "../myapp/public/spa", indexHtmlPath: "../myapp/www/spa.html" } })],
	build: { outDir: "../myapp/public/spa", emptyOutDir: true },
})
"""


def spa_html(asset):
    return f'<script type="module" src="/assets/myapp/spa/assets/{asset}"></script>\n'


def build_spa(app, asset):
    """What `vite build` with emptyOutDir does: wipe the output, write new hashes and the HTML."""
    subprocess.run(["rm", "-rf", str(app / APP / "public" / "spa")], check=True)
    write(app / APP / "public" / "spa" / "assets" / asset, f"// {asset}")
    write(app / APP / "www" / "spa.html", spa_html(asset))


def test_spa_build_ships_even_when_committed_then_gitignored(benches, capsys):
    """Regression for the blank-page bug: a build committed before
    `public/spa/*` was gitignored leaves rebuilt hashes ignored by git."""
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / "frontend" / "vite.config.js", VITE_CONFIG)
    build_spa(app, "index-OLD.js")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "commit a build")
    write(app / ".gitignore", "myapp/public/dist/\nmyapp/public/spa/*\n")
    git(app, "commit", "-q", "-am", "ignore the build")
    git(rapp, "pull", "-q", "--ff-only")
    build_spa(app, "index-NEW.js")

    deploy(remote)

    out = flat(capsys.readouterr().out)
    assert (rapp / APP / "www" / "spa.html").read_text() == spa_html("index-NEW.js")
    assert sorted(p.name for p in (rapp / APP / "public" / "spa" / "assets").iterdir()) == ["index-NEW.js"]
    assert "myapp/public/spa is a build output but is tracked in git" in out


def test_unstaged_frontend_changes_ask_before_building(benches, tools_log, monkeypatch, capsys):
    local, remote = benches
    app = local / "apps" / APP
    write(app / "frontend" / "vite.config.js", "export default {}")
    write(app / "frontend" / "src" / "App.vue", "<template/>")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "frontend")
    write(app / "frontend" / "src" / "App.vue", "<template>unstaged</template>")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", f"{APP}/api.py")
    prompts = []

    def answer(message, default=None, **kwargs):
        prompts.append((message, default))
        return message == "Proceed with deploy?"

    monkeypatch.setattr(typer, "confirm", answer)

    deploy(remote, yes=False)

    out = flat(capsys.readouterr().out)
    assert prompts[0] == ("Build anyway, including these unstaged changes?", True)
    assert "frontend/src/App.vue" in out and "Not building" in out
    assert tools_log.read_text() == ""  # no yarn, no bench build, no cache clear
    assert (remote / "apps" / APP / APP / "api.py").is_file()
    assert not (remote / "apps" / APP / APP / "public" / "dist").exists()


def test_unstaged_frontend_changes_with_yes_build_anyway(benches, tools_log, capsys):
    local, remote = benches
    app = local / "apps" / APP
    write(app / APP / "public" / "js" / "form.js", "staged")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "js")
    write(app / APP / "public" / "js" / "form.js", "unstaged")

    deploy(remote)

    assert f"bench {local} build --app {APP}" in tools_log.read_text()
    assert "Building anyway (--yes)" in flat(capsys.readouterr().out)


def test_html_referencing_missing_assets_fails_before_touching_the_server(benches, capsys):
    local, remote = benches
    app = local / "apps" / APP
    write(app / "frontend" / "vite.config.js", VITE_CONFIG)
    build_spa(app, "index-NEW.js")
    (app / APP / "public" / "spa" / "assets" / "index-NEW.js").unlink()

    with pytest.raises(typer.Exit):
        deploy(remote)

    assert "myapp/www/spa.html → /assets/myapp/spa/assets/index-NEW.js" in flat(capsys.readouterr().out)
    assert not (remote / "apps" / APP / ".git" / "benchops").exists()


def test_pre_local_build_hook_is_flagged_as_duplicate(benches, tools_log, capsys):
    local, remote = benches

    deploy(remote, site="s1", config={"pre_local_commands": ["bench --site {site} build --app {app}"]})

    assert tools_log.read_text().splitlines()[0] == f"bench {local / 'apps'} --site s1 build --app {APP}"
    assert "BenchOps builds the app itself" in flat(capsys.readouterr().out)


# --------------------------------------------------------------------------- status


def test_status_shows_deploy_and_work_on_staging(benches, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "mine\n")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    deploy(remote)
    write(rapp / APP / "hooks.py", "changed on staging after the deploy\n")
    write(rapp / APP / "scratch.py", "")
    capsys.readouterr()

    command = StatusCommand(server_alias="staging", app_name=APP, files=True)
    command._get_server_config = lambda: {"bench_path": str(remote)}
    command._get_remote_runner = lambda server_config: LocalRunner()
    command.execute()

    out = flat(capsys.readouterr().out)
    assert "On develop @" in out
    assert "Last deploy Dev@" in out and "2 staged file(s)" in out
    assert f"Changes to be committed (staged) 2 A {APP}/api.py M {APP}/hooks.py" in out
    assert f"Changes not staged 1 M {APP}/hooks.py" in out
    assert f"Untracked files 1 {APP}/scratch.py" in out
    assert f"Deployed files changed since deploy 1 {APP}/hooks.py" in out
