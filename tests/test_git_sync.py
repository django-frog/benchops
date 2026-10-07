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


def ledger(remote):
    return json.loads((remote / "apps" / APP / ".git" / "benchops" / "ledger.json").read_text())


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


def test_earlier_drafts_stay_staged_until_committed(benches):
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
    assert sorted(status(rapp)) == sorted([f"A  {APP}/api.py", f"M  {APP}/hooks.py"])
    assert sorted(ledger(remote)) == [f"{APP}/api.py", f"{APP}/hooks.py"]


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
    assert sorted(status(rapp)) == sorted([f"A  {APP}/service.py", f"A  {APP}/whatsapp.py"])
    assert record(remote)["deployer"].startswith("Ali@")
    owners = {path: entry["owner"] for path, entry in ledger(remote).items()}
    assert owners == {f"{APP}/whatsapp.py": "dev@example.com", f"{APP}/service.py": "ali@example.com"}


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


def run_status(remote, files=True):
    command = StatusCommand(server_alias="staging", app_name=APP, files=files)
    command._get_server_config = lambda: {"bench_path": str(remote)}
    command._get_remote_runner = lambda server_config: LocalRunner()
    command.execute()


def test_status_groups_drafts_by_label_and_developer(benches, tmp_path, monkeypatch, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "mine\n")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    deploy(remote, label="TASK-142")
    bench_b = tmp_path / "ali"
    app_b = make_local_bench(bench_b, user="Ali", clone_from=app)
    write(app_b / APP / "service.py", "by ali\n")
    git(app_b, "add", "-A")
    monkeypatch.chdir(bench_b)
    deploy(remote)
    monkeypatch.chdir(local)
    # Ali's draft is old; one of Dev's drafts was edited on staging; plus a hand edit.
    entries = ledger(remote)
    entries[f"{APP}/service.py"]["deployed_at"] = "2026-01-01T09:00:00+0000"
    write(rapp / ".git" / "benchops" / "ledger.json", json.dumps(entries))
    write(rapp / APP / "api.py", "edited on staging\n")
    write(rapp / APP / "scratch.py", "")
    capsys.readouterr()

    run_status(remote)

    out = flat(capsys.readouterr().out)
    assert "On develop @" in out
    assert "Last deploy Ali@" in out
    assert "Pending drafts (deployed, not committed yet):" in out
    assert f"TASK-142 Dev 2 file(s) today {APP}/api.py (edited on staging since) {APP}/hooks.py" in out
    assert f"(no label) Ali 1 file(s)" in out and "⚠ stale" in out
    assert "Drafts edited on staging since 1 file(s)" in out
    assert f"Hand edits on staging 2 file(s) {APP}/api.py {APP}/scratch.py" in out


def test_status_points_out_drafts_already_committed_locally(benches, capsys):
    local, remote = benches
    app = local / "apps" / APP
    write(app / APP / "hooks.py", "mine\n")
    git(app, "add", "-A")
    deploy(remote)
    git(app, "commit", "-q", "-m", "commit the draft")
    capsys.readouterr()

    run_status(remote)

    assert f"Committed on your machine 1 file(s) — run 'benchops sync' {APP}/hooks.py" in flat(capsys.readouterr().out)


# --------------------------------------------------------------------------- ledger: drafts, labels, sync


def as_ali(tmp_path, monkeypatch, app):
    bench_b = tmp_path / "ali"
    app_b = make_local_bench(bench_b, user="Ali", clone_from=app)
    monkeypatch.chdir(bench_b)
    return app_b


def test_replacing_your_own_draft_is_not_an_overlap(benches, monkeypatch, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "draft 1\n")
    git(app, "add", "-A")
    deploy(remote)
    write(app / APP / "hooks.py", "draft 2\n")
    git(app, "add", "-A")
    prompts = []
    monkeypatch.setattr(typer, "confirm", lambda message, **k: prompts.append(message) or True)
    capsys.readouterr()

    deploy(remote, yes=False)

    assert prompts == ["Proceed with deploy?"]
    assert (rapp / APP / "hooks.py").read_text() == "draft 2\n"
    assert "Your earlier drafts, replaced: 1" in flat(capsys.readouterr().out)


def test_replacing_another_developers_draft_names_them(benches, tmp_path, monkeypatch, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    app_b = as_ali(tmp_path, monkeypatch, app)
    write(app_b / APP / "hooks.py", "ali's draft\n")
    git(app_b, "add", "-A")
    deploy(remote, label="TASK-150")
    monkeypatch.chdir(local)
    write(app / APP / "hooks.py", "mine\n")
    git(app, "add", "-A")
    capsys.readouterr()

    with pytest.raises(typer.Exit):
        deploy(remote)
    out = flat(capsys.readouterr().out)
    assert f"will be replaced by your version: 1 {APP}/hooks.py (Ali's deployed draft [TASK-150])" in out
    assert "Taking over" not in out  # listed once, as an overlap
    assert (rapp / APP / "hooks.py").read_text() == "ali's draft\n"

    deploy(remote, overwrite=True)
    assert (rapp / APP / "hooks.py").read_text() == "mine\n"
    assert ledger(remote)[f"{APP}/hooks.py"]["owner"] == "dev@example.com"
    assert ledger(remote)[f"{APP}/hooks.py"]["label"] == "TASK-150"  # the task label carries over


def test_own_draft_edited_on_staging_is_a_hand_edit_again(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "draft 1\n")
    git(app, "add", "-A")
    deploy(remote)
    write(rapp / APP / "hooks.py", "edited in Desk after the deploy\n")
    write(app / APP / "hooks.py", "draft 2\n")
    git(app, "add", "-A")

    with pytest.raises(typer.Exit):  # an overlap: --yes without --overwrite
        deploy(remote)
    assert (rapp / APP / "hooks.py").read_text() == "edited in Desk after the deploy\n"


def test_labels_are_kept_on_redeploy_and_can_be_changed(benches):
    local, remote = benches
    app = local / "apps" / APP
    write(app / APP / "hooks.py", "v1\n")
    git(app, "add", "-A")
    deploy(remote, label="TASK-1")
    write(app / APP / "hooks.py", "v2\n")
    git(app, "add", "-A")

    deploy(remote)
    assert ledger(remote)[f"{APP}/hooks.py"]["label"] == "TASK-1"

    deploy(remote, label="TASK-2")
    assert ledger(remote)[f"{APP}/hooks.py"]["label"] == "TASK-2"
    assert record(remote)["label"] == "TASK-2"


def test_deploying_exactly_someone_elses_draft_takes_it_over(benches, tmp_path, monkeypatch, capsys):
    local, remote = benches
    app = local / "apps" / APP
    app_b = as_ali(tmp_path, monkeypatch, app)
    write(app_b / APP / "service.py", "shared\n")
    git(app_b, "add", "-A")
    deploy(remote, label="TASK-7")
    monkeypatch.chdir(local)
    write(app / APP / "service.py", "shared\n")
    git(app, "add", "-A")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    capsys.readouterr()

    deploy(remote)

    assert f"Taking over drafts deployed by others: 1 {APP}/service.py (Ali's deployed draft [TASK-7])" in flat(
        capsys.readouterr().out
    )
    assert ledger(remote)[f"{APP}/service.py"]["owner"] == "dev@example.com"
    assert ledger(remote)[f"{APP}/service.py"]["label"] == "TASK-7"


def test_sync_marks_committed_drafts_clean(benches, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "approved\n")
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    deploy(remote, label="TASK-142")
    git(app, "commit", "-q", "-m", "TASK-142")
    capsys.readouterr()

    deploy(remote, sync=True)

    out = flat(capsys.readouterr().out)
    assert "Drafts now committed (become clean on staging): 2" in out
    assert "2 draft(s) marked committed" in out
    assert git(rapp, "rev-parse", "HEAD") == git(app, "rev-parse", "HEAD")
    assert status(rapp) == []
    assert ledger(remote) == {}
    assert record(remote)["mode"] == "sync"


def test_sync_replaces_your_own_draft_with_the_committed_version(benches, monkeypatch):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "draft reviewed by business\n")
    git(app, "add", "-A")
    deploy(remote)
    write(app / APP / "hooks.py", "draft reviewed by business, tidied up\n")
    git(app, "commit", "-qam", "tidy and commit")
    prompts = []
    monkeypatch.setattr(typer, "confirm", lambda message, **k: prompts.append(message) or True)

    deploy(remote, sync=True, yes=False)

    assert prompts == ["Proceed with sync?"]
    assert (rapp / APP / "hooks.py").read_text() == "draft reviewed by business, tidied up\n"
    assert status(rapp) == []
    assert ledger(remote) == {}


def test_sync_ships_no_staged_files_and_keeps_other_drafts(benches, tmp_path, monkeypatch):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "a.py", "a\n")
    write(app / APP / "b.py", "b\n")
    git(app, "add", "-A")
    deploy(remote)
    git(app, "commit", "-q", "-m", "a only", "--", f"{APP}/a.py")
    write(app / APP / "c.py", "staged, not committed\n")
    git(app, "add", f"{APP}/c.py")

    deploy(remote, sync=True)

    assert not (rapp / APP / "c.py").exists()
    assert status(rapp) == [f"A  {APP}/b.py"]
    assert sorted(ledger(remote)) == [f"{APP}/b.py"]


def test_sync_that_overwrites_someone_elses_draft_asks_first(benches, tmp_path, monkeypatch):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    app_b = as_ali(tmp_path, monkeypatch, app)
    write(app_b / APP / "hooks.py", "ali's draft\n")
    git(app_b, "add", "-A")
    deploy(remote)
    monkeypatch.chdir(local)
    write(app / APP / "hooks.py", "committed by dev\n")
    git(app, "commit", "-qam", "dev's change")

    with pytest.raises(typer.Exit):
        deploy(remote, sync=True)
    assert (rapp / APP / "hooks.py").read_text() == "ali's draft\n"


def test_sync_without_new_commits_is_a_no_op(benches, capsys):
    local, remote = benches
    app = local / "apps" / APP
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "api")
    deploy(remote, sync=True)
    capsys.readouterr()

    deploy(remote, sync=True)

    assert "already up to date" in capsys.readouterr().out


def test_staged_deletion_draft_is_absorbed_when_committed(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    git(app, "rm", "-q", f"{APP}/sales/doctype/visit/visit.json")
    deploy(remote)
    assert f"{APP}/sales/doctype/visit/visit.json" in ledger(remote)
    git(app, "commit", "-q", "-m", "drop visit")

    deploy(remote, sync=True)

    assert status(rapp) == []
    assert ledger(remote) == {}


def test_sync_only_builds_when_commits_touch_frontend_source(benches, tools_log):
    local, remote = benches
    app = local / "apps" / APP
    deploy(remote, skip_build=True)
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "backend only")
    tools_log.write_text("")

    deploy(remote, sync=True)
    assert "build --app" not in tools_log.read_text()

    write(app / APP / "public" / "js" / "form.js", "frontend")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "frontend change")

    deploy(remote, sync=True)
    assert f"bench {local} build --app {APP}" in tools_log.read_text()


def test_ledger_is_bootstrapped_from_a_0_14_deploy_record(benches, capsys):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "deployed by 0.14\n")
    git(app, "add", "-A")
    deploy(remote)
    (rapp / ".git" / "benchops" / "ledger.json").unlink()  # what a 0.14 server looks like
    git(app, "commit", "-q", "-m", "commit it")

    deploy(remote, sync=True)

    assert "Drafts now committed (become clean on staging): 1" in flat(capsys.readouterr().out)
    assert status(rapp) == []


def test_draft_is_absorbed_when_its_commit_reached_staging_another_way(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "approved\n")
    git(app, "add", "-A")
    deploy(remote)
    git(app, "commit", "-q", "-m", "approved")
    # Someone on staging moved HEAD to the commit themselves (e.g. git pull), files untouched.
    git(rapp, "fetch", "-q", "origin")
    git(rapp, "reset", "-q", "--soft", git(app, "rev-parse", "HEAD").strip())

    deploy(remote, sync=True)

    assert status(rapp) == []
    assert ledger(remote) == {}


def test_drafts_unstaged_on_the_server_are_staged_again(benches):
    local, remote = benches
    app, rapp = local / "apps" / APP, remote / "apps" / APP
    write(app / APP / "hooks.py", "draft\n")
    git(app, "add", "-A")
    deploy(remote)
    git(rapp, "reset", "-q")  # e.g. a 0.14 deploy, or someone on the server, unstaged it
    assert status(rapp) == [f" M {APP}/hooks.py"]
    git(app, "restore", "--staged", f"{APP}/hooks.py")  # this deploy doesn't include the draft
    write(app / APP / "api.py", "x = 1\n")
    git(app, "add", f"{APP}/api.py")

    deploy(remote)

    assert sorted(status(rapp)) == sorted([f"A  {APP}/api.py", f"M  {APP}/hooks.py"])
