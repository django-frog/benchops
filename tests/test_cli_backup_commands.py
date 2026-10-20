"""The `backup` commands and the backup-source config, and proof that adding
them left the existing commands, servers, hooks and projects alone."""

import pytest
from typer.testing import CliRunner

from benchops import cli
from benchops.backups import BackupSourceError, group_backups
from benchops.config import ConfigManager

STAMPS = ["20261019_020000", "20261018_020000", "20261017_020000"]


def cells(text):
    return " ".join(text.translate({ord(c): " " for c in "│┃┏┓┗┛┡┩└┘┳┻╇┴━─"}).split())


def run(*args, input=None):
    return CliRunner().invoke(cli.app, list(args), input=input, env={"COLUMNS": "250"})


class FakeS3:
    instances = []
    error = None

    def __init__(self, source):
        if FakeS3.error:
            raise FakeS3.error
        self.source = source
        FakeS3.instances.append(self)

    def list_sets(self, site=None):
        objects = [(f"{s}/{s}-erp_akwad_qa-database.sql.gz", 2048) for s in STAMPS]
        objects += [(f"{STAMPS[0]}/{STAMPS[0]}-erp_akwad_qa-files.tar", 4096)]
        objects += [(f"{STAMPS[0]}/{STAMPS[0]}-hr_akwad_qa-files.tar", 1)]  # no database
        return group_backups(objects, site)


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr("benchops.cli.ConfigManager", lambda: ConfigManager(path))
    monkeypatch.setattr("benchops.cli.S3Backups", FakeS3)
    FakeS3.instances, FakeS3.error = [], None
    return ConfigManager(path)


# --- nothing existing changed -------------------------------------------------


def test_existing_commands_and_hook_phases_are_still_there():
    commands = {c.name or c.callback.__name__ for c in cli.app.registered_commands}
    groups = {g.name for g in cli.app.registered_groups}
    assert {"version", "init", "deploy", "sync", "status", "install", "uninstall", "logs", "execute",
            "clone", "restore"} <= commands
    assert {"server", "auth", "git", "project", "backup"} <= groups
    assert [p.value for p in cli.HookPhase] == [
        "pre-local", "pre-remote", "post-remote", "install-remote", "uninstall-remote", "post-restore"
    ]


def test_backup_sources_live_apart_from_servers_and_projects(config):
    config.add_server("prod", "10.0.0.1", 22, "frappe", "/bench", post_remote_commands=["bench migrate"])
    config.set_project("prod", {"host": "github.com", "repos": [{"path": "akwad/erp"}]})
    config.set_backup_source("prod", "akwad-backups")

    assert config.get_server("prod")["post_remote_commands"] == ["bench migrate"]
    assert config.get_project("prod")["repos"] == [{"path": "akwad/erp"}]
    config.remove_backup_source("prod")
    assert config.get_server("prod") and config.get_project("prod")
    assert config.list_backup_sources() == {}


def test_post_restore_hooks_use_the_existing_hook_commands(config):
    config.add_server("staging", "10.0.0.1", 22, "frappe", "/bench")
    result = run("server", "add-hook", "staging", "post-restore", "bench --site {site} migrate")
    assert result.exit_code == 0, result.output
    assert config.get_hooks("staging", "post-restore") == ["bench --site {site} migrate"]
    assert "Post-Restore" in run("server", "list").output
    assert run("server", "clear-hooks", "staging", "post-restore").exit_code == 0
    assert config.get_hooks("staging", "post-restore") == []


# --- config -------------------------------------------------------------------


def test_set_backup_source_strips_and_omits_empty_values(config):
    config.set_backup_source(" prod ", " akwad-backups ", prefix="/backups/prod/", aws_profile="ro")
    assert config.get_backup_source("prod") == {"bucket": "akwad-backups", "prefix": "backups/prod", "aws_profile": "ro"}
    config.set_backup_source("prod", "other", prefix="/")
    assert config.get_backup_source("prod") == {"bucket": "other"}


def test_reading_sources_without_a_config_file(tmp_path):
    assert ConfigManager(tmp_path / "missing.toml").list_backup_sources() == {}
    with pytest.raises(ValueError, match="not found"):
        ConfigManager(tmp_path / "missing.toml").remove_backup_source("prod")


# --- backup source add / list / remove ------------------------------------------


def test_source_add_checks_the_bucket_then_saves(config):
    result = run("backup", "source", "add", "prod", "--bucket", "akwad-backups", "--prefix", "prod",
                 "--aws-profile", "ro", "--aws-region", "me-south-1")
    assert result.exit_code == 0, result.output
    assert "4 backup(s) of 2 site(s) found" in result.output
    assert FakeS3.instances[0].source["bucket"] == "akwad-backups"
    assert config.get_backup_source("prod") == {
        "bucket": "akwad-backups", "prefix": "prod", "aws_profile": "ro", "aws_region": "me-south-1"
    }


def test_source_add_saves_nothing_when_the_bucket_cant_be_read(config):
    FakeS3.error = BackupSourceError("Bucket 'nope' does not exist.")
    result = run("backup", "source", "add", "prod", "--bucket", "nope")
    assert result.exit_code == 1 and "does not exist" in result.output
    assert config.list_backup_sources() == {}


def test_source_list_and_remove(config):
    assert "No backup sources yet" in run("backup", "source", "list").output
    config.set_backup_source("prod", "akwad-backups", prefix="prod", endpoint_url="https://minio.local")
    out = cells(run("backup", "source", "list").output)
    assert "prod s3://akwad-backups/prod default https://minio.local" in out
    assert run("backup", "source", "remove", "prod").exit_code == 0
    result = run("backup", "source", "remove", "prod")
    assert result.exit_code == 1 and "not found" in result.output


# --- backup list ----------------------------------------------------------------


def test_backup_list_newest_first_with_sizes_and_missing_databases(config):
    config.set_backup_source("prod", "akwad-backups")
    out = cells(run("backup", "list", "prod").output)
    assert "20261019_020000 2026-10-19 02:00 hr_akwad_qa missing 1 B —" in out
    assert "20261019_020000 2026-10-19 02:00 erp_akwad_qa 2.0 KB 4.0 KB —" in out
    assert out.index("20261019_020000") < out.index("20261018_020000") < out.index("20261017_020000")


def test_backup_list_filters_by_site_and_limits(config):
    config.set_backup_source("prod", "akwad-backups")
    out = cells(run("backup", "list", "prod", "--site", "erp.akwad.qa", "-n", "2").output)
    assert "(2 of 3)" in out and "20261017_020000" not in out and "hr_akwad_qa" not in out
    assert "20261017_020000" in cells(run("backup", "list", "prod", "--site", "erp.akwad.qa", "-n", "0").output)


def test_backup_list_errors(config):
    result = run("backup", "list", "nope")
    assert result.exit_code == 1 and "'nope' not found" in result.output
    config.set_backup_source("prod", "akwad-backups")
    assert "No Frappe backups found for 'other.qa'" in run("backup", "list", "prod", "--site", "other.qa").output
    FakeS3.error = BackupSourceError("Access to bucket was denied")
    result = run("backup", "list", "prod")
    assert result.exit_code == 1 and "denied" in result.output
