"""`benchops restore`: the remote steps it runs, in order, against a fake
server and a fake S3 — and that secrets never reach the terminal."""

import shlex

import pytest
from invoke.exceptions import UnexpectedExit
from typer.testing import CliRunner

from benchops import cli, restore
from benchops.backups import group_backups
from benchops.config import ConfigManager
from benchops.runner import BenchOpsCommandError, BenchOpsConnectionError
from benchops.restore import build_curl_config, build_restore_command

BENCH = "/home/frappe/bench"
WORKDIR = f"{BENCH}/.benchops-restore.abc123"
STAMP = "20261019_020000"
SIZES = {"database.sql.gz": 1000, "files.tar": 2000, "private-files.tar": 3000}


class FakeS3:
    def __init__(self, source):
        self.source = source

    def list_sets(self, site=None):
        objects = [(f"{STAMP}/{STAMP}-erp_akwad_qa-{part}", size) for part, size in SIZES.items()]
        return group_backups(objects, site)

    def presign(self, key, expires=3600):
        return f"https://bucket.s3.amazonaws.com/{key}?X-Amz-Signature=secret"


class FakeResult:
    def __init__(self, exited):
        self.exited = exited
        self.command = "the command"
        self.stdout = self.stderr = ""
        self.pty = False
        self.hide = ()
        self.env = {}
        self.encoding = "utf-8"


class FakeRunner:
    def __init__(self, site_exists=True, root_password=True, fail_on=None, sizes=None, curl=True,
                 free_kb=99999999, workdir=WORKDIR, cleanup_fails=False, common_config=None, capture_error=None):
        self.common_config = common_config
        self.capture_error = capture_error
        self.site_exists = site_exists
        self.root_password = root_password
        self.fail_on = fail_on
        self.sizes = SIZES if sizes is None else sizes
        self.curl = curl
        self.free_kb = free_kb
        self.workdir = workdir
        self.cleanup_fails = cleanup_fails
        self.runs, self.captures, self.uploads = [], [], {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def capture(self, command, cwd=None):
        self.captures.append(command)
        if self.capture_error and command.startswith(self.capture_error[0]):
            raise self.capture_error[1]
        if command.startswith("test -d"):
            return f"exists={int(self.site_exists)}\ncurl={int(self.curl)}\nfree_kb={self.free_kb}\n"
        if "common_site_config.json" in command and self.common_config is not None:
            return self.common_config
        if "common_site_config.json" in command:
            return '{"root_password": "fromconfig"}' if self.root_password else "{}"
        if command.startswith("cat "):
            return ""
        if command.startswith("mktemp"):
            return self.workdir + "\n"
        if command.startswith("rm -rf") and self.cleanup_fails:
            raise BenchOpsConnectionError("connection lost")
        if command.startswith("stat"):
            # Like stat: one size per requested file that exists.
            names = [path.rsplit("-erp_akwad_qa-", 1)[-1] for path in shlex.split(command)[3:]]
            return "\n".join(str(self.sizes[name]) for name in names if name in self.sizes)
        return ""

    def run(self, command, cwd=None, interactive=False):
        self.runs.append(command)
        if self.fail_on and self.fail_on in command:
            raise UnexpectedExit(FakeResult(2))

    def put(self, local_path, remote_path):
        with open(local_path) as f:
            self.uploads[remote_path] = f.read()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    config = ConfigManager(path)
    config.add_server("staging", "10.0.0.1", 22, "frappe", BENCH)
    config.set_backup_source("prod", "akwad-backups")
    monkeypatch.setattr("benchops.cli.ConfigManager", lambda: ConfigManager(path))
    monkeypatch.setattr("benchops.base.ConfigManager", lambda: ConfigManager(path))
    monkeypatch.setattr(restore, "S3Backups", FakeS3)
    monkeypatch.delenv(restore.ROOT_PASSWORD_ENV, raising=False)

    def use(runner):
        monkeypatch.setattr("benchops.base.BaseCommand._get_remote_runner", lambda self, cfg: runner)
        return runner

    return config, use


def invoke(*args, input=None):
    return CliRunner().invoke(cli.app, ["restore", "staging", "prod", *args], input=input)


def test_replace_downloads_backs_up_restores_and_cleans_up(setup):
    config, use = setup
    runner = use(FakeRunner())
    config.set_hooks("staging", "post-restore", ["bench --site {site} migrate"])

    result = invoke("--site", "erp.akwad.qa", input="erp.akwad.qa\n")

    assert result.exit_code == 0, result.output
    curl, gzip, backup, bench_restore, hook = runner.runs
    assert "--config" in curl and "rm -f" in curl
    assert gzip.startswith("gzip -t")
    assert backup == "bench --site erp.akwad.qa backup --with-files"
    assert shlex.split(bench_restore) == [
        "bench", "--site", "erp.akwad.qa", "restore", f"{WORKDIR}/{STAMP}-erp_akwad_qa-database.sql.gz",
        "--with-public-files", f"{WORKDIR}/{STAMP}-erp_akwad_qa-files.tar",
        "--with-private-files", f"{WORKDIR}/{STAMP}-erp_akwad_qa-private-files.tar",
    ]
    assert hook == "bench --site erp.akwad.qa migrate"
    assert runner.captures[-1] == f"rm -rf {WORKDIR}"
    # The signed URLs travel in a file, never on a command line or the terminal.
    assert "X-Amz-Signature" in runner.uploads[f"{WORKDIR}/curl.conf"]
    assert not any("X-Amz-Signature" in c for c in runner.runs + runner.captures)
    assert "X-Amz-Signature" not in result.output


def test_replace_requires_typing_the_site_name(setup):
    _, use = setup
    runner = use(FakeRunner())
    result = invoke("--site", "erp.akwad.qa", input="erp\n")
    assert result.exit_code == 1
    assert "didn't match" in result.output
    assert runner.runs == [] and not any(c.startswith("mktemp") for c in runner.captures)


def test_missing_site_points_to_new_site(setup):
    _, use = setup
    use(FakeRunner(site_exists=False))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "--new-site" in result.output


def test_new_site_from_another_sites_backup_prompts_for_root_password(setup):
    _, use = setup
    runner = use(FakeRunner(site_exists=False, root_password=False))

    result = invoke("--site", "uat.akwad.qa", "--from-site", "erp.akwad.qa", "--new-site", "--no-files",
                    input="s3cret-root\ny\n")

    assert result.exit_code == 0, result.output
    curl, gzip, new_site, bench_restore = runner.runs
    assert shlex.split(new_site)[:3] == ["bench", "new-site", "uat.akwad.qa"]
    assert shlex.split(new_site)[-2:] == ["--mariadb-root-password", "s3cret-root"]
    assert shlex.split(bench_restore) == [
        "bench", "--site", "uat.akwad.qa", "restore", f"{WORKDIR}/{STAMP}-erp_akwad_qa-database.sql.gz",
        "--mariadb-root-password", "s3cret-root",
    ]
    assert "s3cret-root" not in result.output
    assert "Log in as Administrator with the password from the backup" in result.output


def test_yes_without_a_root_password_stops_before_changing_anything(setup):
    _, use = setup
    runner = use(FakeRunner(site_exists=False, root_password=False))
    result = invoke("--site", "uat.akwad.qa", "--from-site", "erp.akwad.qa", "--new-site", "--yes")
    assert result.exit_code == 1 and restore.ROOT_PASSWORD_ENV in result.output
    assert runner.runs == []


def test_incomplete_download_restores_nothing_and_cleans_up(setup):
    _, use = setup
    runner = use(FakeRunner(sizes={**SIZES, "files.tar": 10}))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "incomplete" in result.output
    assert len(runner.runs) == 1  # only the download
    assert runner.captures[-1] == f"rm -rf {WORKDIR}"


def test_failed_restore_hides_the_command_and_names_the_safety_backup(setup, monkeypatch):
    _, use = setup
    monkeypatch.setenv(restore.ROOT_PASSWORD_ENV, "s3cret-root")
    runner = use(FakeRunner(root_password=False, fail_on=" restore "))
    result = invoke("--site", "erp.akwad.qa", "--yes", "--admin-password", "adm1n")
    assert result.exit_code == 1
    assert "exit code 2" in result.output and "private/backups" in result.output
    assert "s3cret-root" not in result.output and "adm1n" not in result.output
    assert runner.captures[-1] == f"rm -rf {WORKDIR}"


def test_no_backup_of_the_site_names_what_was_searched(setup):
    _, use = setup
    use(FakeRunner())
    result = invoke("--site", "uat.akwad.qa", "--yes")
    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "uat_akwad_qa" in output and "benchops backup list prod --site uat.akwad.qa" in output


def test_invalid_site_name_is_rejected_before_connecting(setup):
    _, use = setup
    runner = use(FakeRunner())
    result = invoke("--site", "../etc", "--yes")
    assert result.exit_code == 1 and "not a valid site name" in result.output
    assert runner.captures == []


def test_curl_config_escapes_quotes_and_backslashes():
    assert build_curl_config([('https://x/a"b', "/tmp/a\\b")]) == 'url = "https://x/a\\"b"\noutput = "/tmp/a\\\\b"\n'


def test_restore_command_quotes_every_argument():
    command = build_restore_command("site.qa", "/w/db file.sql.gz", root_password="p'w")
    assert shlex.split(command) == ["bench", "--site", "site.qa", "restore", "/w/db file.sql.gz",
                                    "--mariadb-root-password", "p'w"]


# --- the remaining branches ---------------------------------------------------


def test_no_files_skips_archives_and_backs_up_only_the_database(setup):
    _, use = setup
    runner = use(FakeRunner(sizes={"database.sql.gz": 1000}))
    result = invoke("--site", "erp.akwad.qa", "--no-files", "--yes")
    assert result.exit_code == 0, result.output
    _, _, backup, bench_restore = runner.runs
    assert backup == "bench --site erp.akwad.qa backup"
    assert "--with-public-files" not in bench_restore and "--with-private-files" not in bench_restore
    assert "files.tar" not in runner.uploads[f"{WORKDIR}/curl.conf"]


def test_skip_safety_backup_and_admin_password(setup):
    _, use = setup
    runner = use(FakeRunner())
    result = invoke("--site", "erp.akwad.qa", "--skip-safety-backup", "--admin-password", "adm1n", "--yes")
    assert result.exit_code == 0, result.output
    assert not any(" backup" in run for run in runner.runs)
    assert shlex.split(runner.runs[-1])[-2:] == ["--admin-password", "adm1n"]
    assert "adm1n" not in result.output


def test_new_site_on_an_existing_site_is_refused(setup):
    _, use = setup
    runner = use(FakeRunner(site_exists=True))
    result = invoke("--site", "erp.akwad.qa", "--new-site", "--yes")
    assert result.exit_code == 1 and "already exists" in result.output
    assert runner.runs == []


def test_new_site_can_be_declined(setup):
    _, use = setup
    runner = use(FakeRunner(site_exists=False))
    result = invoke("--site", "erp.akwad.qa", "--new-site", input="n\n")
    assert result.exit_code == 1 and "Cancelled" in result.output
    assert runner.runs == []


def test_missing_curl_is_reported_before_anything_changes(setup):
    _, use = setup
    runner = use(FakeRunner(curl=False))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "curl is not installed" in result.output
    assert runner.runs == []


def test_low_disk_space_and_missing_archives_are_warned_about(setup, monkeypatch):
    _, use = setup

    class OnlyDatabase(FakeS3):
        def list_sets(self, site=None):
            return group_backups([(f"{STAMP}/{STAMP}-erp_akwad_qa-database.sql.gz", 10 * 1024**3)], site)

    monkeypatch.setattr(restore, "S3Backups", OnlyDatabase)
    use(FakeRunner(free_kb=1024))
    result = invoke("--site", "erp.akwad.qa", input="no\n")
    output = " ".join(result.output.split())
    assert "no public files archive" in output and "no private files archive" in output
    assert "Only 1.0 MB free" in output


def test_at_picks_an_older_backup(setup, monkeypatch):
    _, use = setup

    class TwoBackups(FakeS3):
        def list_sets(self, site=None):
            objects = [(f"{stamp}/{stamp}-erp_akwad_qa-database.sql.gz", 1000)
                       for stamp in ("20261019_020000", "20261018_020000")]
            return group_backups(objects, site)

    monkeypatch.setattr(restore, "S3Backups", TwoBackups)
    runner = use(FakeRunner(sizes={"database.sql.gz": 1000}))
    result = invoke("--site", "erp.akwad.qa", "--at", "20261018", "--yes")
    assert result.exit_code == 0, result.output
    assert "20261018_020000-erp_akwad_qa-database.sql.gz" in runner.runs[-1]


def test_failing_hook_says_the_restore_itself_succeeded(setup):
    config, use = setup
    config.set_hooks("staging", "post-restore", ["bench --site {site} migrate"])
    use(FakeRunner(fail_on="migrate"))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    output = " ".join(result.output.split())
    assert result.exit_code == 1 and "erp.akwad.qa was restored, but a post-restore hook failed" in output


def test_failed_new_site_restore_suggests_dropping_it(setup):
    _, use = setup
    use(FakeRunner(site_exists=False, fail_on=" restore "))
    result = invoke("--site", "erp.akwad.qa", "--new-site", "--yes")
    assert result.exit_code == 1 and "bench drop-site erp.akwad.qa" in " ".join(result.output.split())


def test_missing_downloads_are_detected(setup):
    _, use = setup
    runner = use(FakeRunner(sizes={"database.sql.gz": 1000}))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "Expected 3 downloaded file(s), found 1" in result.output
    assert len(runner.runs) == 1


def test_unexpected_mktemp_output_is_never_removed(setup):
    _, use = setup
    runner = use(FakeRunner(workdir="/"))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "Unexpected temp dir" in result.output
    assert not any(c.startswith("rm -rf") for c in runner.captures) and runner.runs == []


def test_cleanup_failure_is_a_warning_not_a_crash(setup):
    _, use = setup
    use(FakeRunner(cleanup_fails=True))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 0, result.output
    assert "delete it by hand" in " ".join(result.output.split())


def test_s3_failure_while_signing_is_reported(setup, monkeypatch):
    _, use = setup

    class Expired(FakeS3):
        def presign(self, key, expires=3600):
            raise restore.BackupSourceError("Failed to reach S3: token expired")

    monkeypatch.setattr(restore, "S3Backups", Expired)
    runner = use(FakeRunner())
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and "token expired" in result.output
    assert runner.captures[-1] == f"rm -rf {WORKDIR}"


def test_unknown_source_and_unknown_server(setup):
    _, use = setup
    use(FakeRunner())
    result = CliRunner().invoke(cli.app, ["restore", "staging", "nope", "--site", "erp.akwad.qa"])
    assert result.exit_code == 1 and "Backup source 'nope' not found" in result.output
    result = CliRunner().invoke(cli.app, ["restore", "prod-server", "prod", "--site", "erp.akwad.qa"])
    assert result.exit_code == 1 and "Server 'prod-server' not found" in result.output


@pytest.mark.parametrize("common_config", ["not json", "[1, 2]"])
def test_unreadable_site_config_means_asking_for_the_root_password(setup, common_config):
    _, use = setup
    use(FakeRunner(common_config=common_config))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and restore.ROOT_PASSWORD_ENV in result.output


def test_failed_restore_without_a_safety_backup_has_no_hint(setup):
    _, use = setup
    use(FakeRunner(fail_on=" restore "))
    result = invoke("--site", "erp.akwad.qa", "--skip-safety-backup", "--yes")
    assert result.exit_code == 1 and "private/backups" not in result.output


@pytest.mark.parametrize("prefix, error, shown", [
    ("test -d", BenchOpsConnectionError("connection reset"), "connection reset"),
    ("stat", BenchOpsCommandError(1, "", "stat: cannot stat"), "stat: cannot stat"),
])
def test_transport_and_command_failures_are_reported(setup, prefix, error, shown):
    _, use = setup
    use(FakeRunner(capture_error=(prefix, error)))
    result = invoke("--site", "erp.akwad.qa", "--yes")
    assert result.exit_code == 1 and shown in result.output
