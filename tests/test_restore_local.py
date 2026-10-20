"""`benchops restore --local`: a restore into the bench on this machine,
against a real bench folder layout, a fake S3 and a recording LocalRunner."""

import gzip
import json
import shlex
import subprocess

import pytest
from typer.testing import CliRunner

from benchops import cli, restore
from benchops.backups import BackupSourceError, group_backups
from benchops.config import ConfigManager

STAMP = "20261019_020000"
DUMP = gzip.compress(b"CREATE TABLE tabUser (name varchar(140));\n" * 50)
CONTENT = {"database.sql.gz": DUMP, "files.tar": b"p" * 300, "private-files.tar": b"q" * 500}


def key(part):
    return f"{STAMP}/{STAMP}-erp_akwad_qa-{part}"


class FakeS3:
    content = CONTENT
    written = None  # override what a download writes, per part
    error = None

    def __init__(self, source):
        self.source = source

    def list_sets(self, site=None):
        return group_backups([(key(part), len(data)) for part, data in self.content.items()], site)

    def download(self, s3_key, path, progress=None):
        if FakeS3.error:
            raise FakeS3.error
        part = s3_key.split("-erp_akwad_qa-", 1)[1]
        data = (FakeS3.written or {}).get(part, self.content[part])
        with open(path, "wb") as f:
            f.write(data)
        if progress:
            progress(len(data))


class RecordingRunner:
    """Stands in for LocalRunner; records (argv, cwd) and fails on request."""
    runs = []
    fail_on = None

    def run(self, command, cwd=None, interactive=False):
        argv = shlex.split(command)
        RecordingRunner.runs.append((argv, cwd))
        if RecordingRunner.fail_on and RecordingRunner.fail_on in command:
            raise subprocess.CalledProcessError(3, command)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    bench = tmp_path / "frappe-bench"
    (bench / "apps" / "frappe").mkdir(parents=True)
    (bench / "sites" / "erp.akwad.qa").mkdir(parents=True)
    (bench / "sites" / "common_site_config.json").write_text(json.dumps({"root_password": "local-root"}))

    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("benchops.cli.ConfigManager", lambda: ConfigManager(config_path))
    monkeypatch.setattr("benchops.base.ConfigManager", lambda: ConfigManager(config_path))
    ConfigManager(config_path).set_backup_source("prod", "akwad-backups")

    monkeypatch.setattr(restore, "S3Backups", FakeS3)
    monkeypatch.setattr(restore, "LocalRunner", RecordingRunner)
    monkeypatch.setattr(restore.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.delenv(restore.ROOT_PASSWORD_ENV, raising=False)
    monkeypatch.setattr(
        "benchops.base.BaseCommand._get_remote_runner",
        lambda self, cfg: pytest.fail("a local restore must never connect to a server"),
    )
    FakeS3.content, FakeS3.written, FakeS3.error = CONTENT, None, None
    RecordingRunner.runs, RecordingRunner.fail_on = [], None
    monkeypatch.chdir(bench)
    return bench


def invoke(*args, input=None):
    return CliRunner().invoke(cli.app, ["restore", "prod", "--local", *args], input=input, env={"COLUMNS": "400"})


def workdirs(bench):
    return list(bench.glob(".benchops-restore.*"))


def test_local_replace_downloads_into_the_bench_and_restores(bench, monkeypatch):
    seen = {}

    def run(self, command, cwd=None, interactive=False):
        argv = shlex.split(command)
        RecordingRunner.runs.append((argv, cwd))
        if argv[3:4] == ["restore"]:  # the downloads exist while bench restores them
            seen.update({arg: open(arg, "rb").read() for arg in argv if ".benchops-restore." in arg})

    monkeypatch.setattr(RecordingRunner, "run", run)
    result = invoke("--site", "erp.akwad.qa", input="erp.akwad.qa\n")

    assert result.exit_code == 0, result.output
    (backup, backup_cwd), (bench_restore, restore_cwd) = RecordingRunner.runs
    assert backup == ["bench", "--site", "erp.akwad.qa", "backup", "--with-files"]
    assert backup_cwd == restore_cwd == str(bench)
    assert bench_restore[:4] == ["bench", "--site", "erp.akwad.qa", "restore"]
    assert "--mariadb-root-password" not in bench_restore  # bench reads it from common_site_config
    assert sorted(seen.values(), key=len) == sorted(CONTENT.values(), key=len)
    assert all(str(bench / ".benchops-restore.") in path for path in seen)
    assert workdirs(bench) == []
    assert "Restored erp.akwad.qa on your local bench" in result.output



def test_bench_defaults_to_the_current_directory_and_can_be_given(bench, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "is not a Frappe bench" in " ".join(result.output.split())

    result = invoke("--site", "erp.akwad.qa", "--yes", "--bench", str(bench))
    assert result.exit_code == 0, result.output
    assert {cwd for _, cwd in RecordingRunner.runs} == {str(bench)}


def test_missing_bench_command_is_reported(bench, monkeypatch):
    monkeypatch.setattr(restore.shutil, "which", lambda name: None)
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "'bench' command is not on PATH" in result.output
    assert RecordingRunner.runs == []


def test_new_local_site_asks_for_the_root_password_when_bench_has_none(bench):
    (bench / "sites" / "common_site_config.json").write_text("{}")
    result = invoke("--site", "uat.akwad.qa", "--from-site", "erp.akwad.qa", "--new-site", "--no-files",
                    input="s3cret-root\ny\n")
    assert result.exit_code == 0, result.output
    (new_site, _), (bench_restore, _) = RecordingRunner.runs
    assert new_site[:3] == ["bench", "new-site", "uat.akwad.qa"]
    assert new_site[-2:] == bench_restore[-2:] == ["--mariadb-root-password", "s3cret-root"]
    assert "--with-public-files" not in bench_restore
    assert "s3cret-root" not in result.output


def test_site_config_root_password_counts_too(bench):
    (bench / "sites" / "common_site_config.json").write_text("{}")
    (bench / "sites" / "erp.akwad.qa" / "site_config.json").write_text(json.dumps({"root_password": "x"}))
    assert invoke("--site", "erp.akwad.qa", "--yes").exit_code == 0


def test_corrupt_dump_restores_nothing_and_cleans_up(bench):
    FakeS3.content = {**CONTENT, "database.sql.gz": b"\x1f\x8b" + b"not really gzip" * 10}
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "is corrupt" in result.output
    assert RecordingRunner.runs == [] and workdirs(bench) == []


def test_truncated_download_restores_nothing(bench):
    FakeS3.written = {"files.tar": b"p" * 10}
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "files.tar is incomplete (10 of 300 bytes)" in result.output
    assert RecordingRunner.runs == [] and workdirs(bench) == []


def test_s3_failure_during_download_is_reported_and_cleaned_up(bench):
    FakeS3.error = BackupSourceError("Failed to reach S3: connection reset")
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "connection reset" in result.output
    assert workdirs(bench) == []


def test_failed_local_restore_hides_secrets_and_names_the_safety_backup(bench):
    RecordingRunner.fail_on = " restore "
    result = invoke("--site", "erp.akwad.qa", "--yes", "--admin-password", "adm1n")
    output = " ".join(result.output.split())
    assert result.exit_code == 1 and "Restoring erp.akwad.qa failed (exit code 3)" in output
    assert f"{bench}/sites/erp.akwad.qa/private/backups" in output
    assert "adm1n" not in output and workdirs(bench) == []


def test_local_hooks_run_after_the_restore_and_server_hooks_do_not(bench, tmp_path):
    config = ConfigManager(tmp_path / "config.toml")
    config.add_server("staging", "10.0.0.1", 22, "frappe", "/bench")
    config.set_hooks("staging", "post-restore", ["bench --site {site} server-only"])
    assert cli_run("local", "add-hook", "post-restore", "bench --site {site} migrate").exit_code == 0

    result = invoke("--site", "erp.akwad.qa", "--yes")

    assert result.exit_code == 0, result.output
    assert "1 post-restore hook(s)" in result.output
    assert RecordingRunner.runs[-1] == (["bench", "--site", "erp.akwad.qa", "migrate"], str(bench))
    assert not any("server-only" in argv for argv, _ in RecordingRunner.runs)


def test_failing_local_hook_says_the_restore_succeeded(bench, tmp_path):
    ConfigManager(tmp_path / "config.toml").add_local_hook("post-restore", "bench --site {site} migrate")
    RecordingRunner.fail_on = "migrate"
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1
    assert "erp.akwad.qa was restored, but a post-restore hook failed" in " ".join(result.output.split())


def test_hook_that_cannot_start_is_reported(bench, tmp_path, monkeypatch):
    ConfigManager(tmp_path / "config.toml").add_local_hook("post-restore", "no-such-tool")

    def run(self, command, cwd=None, interactive=False):
        if command == "no-such-tool":
            raise restore.BenchOpsConnectionError(f"Failed to execute local command '{command}'")

    monkeypatch.setattr(RecordingRunner, "run", run)
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "could not be started" in " ".join(result.output.split())


# --- choosing the target ----------------------------------------------------------


def cli_run(*args, input=None):
    return CliRunner().invoke(cli.app, list(args), input=input, env={"COLUMNS": "200"})


@pytest.mark.parametrize("args", [
    ["--site", "erp.akwad.qa"],
    ["--site", "erp.akwad.qa", "--local", "--server", "staging"],
])
def test_exactly_one_of_server_or_local(bench, args):
    result = cli_run("restore", "prod", *args)
    assert result.exit_code == 1 and "Pass either --server <alias> or --local" in result.output


def test_bench_requires_local(bench):
    result = cli_run("restore", "prod", "--server", "staging", "--bench", str(bench), "--site", "erp.akwad.qa")
    assert result.exit_code == 1 and "--bench only applies with --local" in result.output


def test_the_0_18_syntax_explains_the_new_one(bench):
    result = cli_run("restore", "staging", "prod", "--site", "erp.akwad.qa")
    assert result.exit_code == 1
    assert "benchops restore prod --server staging (or --local)" in " ".join(result.output.split())


# --- local hooks ----------------------------------------------------------------------


def test_local_hook_commands_and_config(bench, tmp_path):
    config = ConfigManager(tmp_path / "config.toml")
    config.add_server("local", "10.0.0.1", 22, "frappe", "/bench")  # a server may still be called "local"
    assert "none" in cli_run("local", "list-hooks").output

    assert cli_run("local", "add-hook", "post-restore", "bench --site {site} migrate").exit_code == 0
    assert cli_run("local", "add-hook", "post-restore", "bench --site {site} disable-scheduler").exit_code == 0
    assert config.get_local_hooks("post-restore") == [
        "bench --site {site} migrate", "bench --site {site} disable-scheduler"
    ]
    assert "bench --site {site} disable-scheduler" in cli_run("local", "list-hooks").output
    assert config.get_hooks("local", "post-restore") == []  # the server named "local" is untouched

    assert cli_run("local", "clear-hooks", "post-restore").exit_code == 0
    assert config.get_local_hooks("post-restore") == []
    assert cli_run("local", "add-hook", "pre-remote", "x").exit_code != 0  # only post-restore exists locally


def test_local_edit_hooks_saves_and_cancels(bench, tmp_path, monkeypatch):
    config = ConfigManager(tmp_path / "config.toml")
    monkeypatch.setattr(cli, "prompt", lambda *a, **k: "bench --site {site} migrate\n# a comment\n\n")
    assert cli_run("local", "edit-hooks", "post-restore").exit_code == 0
    assert config.get_local_hooks("post-restore") == ["bench --site {site} migrate"]

    def cancel(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "prompt", cancel)
    result = cli_run("local", "edit-hooks", "post-restore")
    assert result.exit_code == 0 and "No changes were made" in result.output
    assert config.get_local_hooks("post-restore") == ["bench --site {site} migrate"]


def test_server_edit_hooks_still_works_after_the_editor_refactor(bench, tmp_path, monkeypatch):
    config = ConfigManager(tmp_path / "config.toml")
    config.add_server("staging", "10.0.0.1", 22, "frappe", "/bench")
    monkeypatch.setattr(cli, "prompt", lambda *a, **k: "bench --site {site} migrate\n")
    assert cli_run("server", "edit-hooks", "staging", "post-remote").exit_code == 0
    assert config.get_hooks("staging", "post-remote") == ["bench --site {site} migrate"]
    assert cli_run("server", "edit-hooks", "nope", "post-remote").exit_code == 1
