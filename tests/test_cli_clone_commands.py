"""The `git`, `project` and `clone` commands, and proof that adding them
left the existing commands, config and credentials alone."""

import sys

import pytest
from typer.testing import CliRunner

from benchops import auth, cli
from benchops.auth import SERVICE_NAME
from benchops.config import ConfigManager
from benchops.vcs import credentials
from benchops.vcs.credentials import GIT_SERVICE_NAME

EXISTING_COMMANDS = {"version", "init", "deploy", "sync", "status", "install", "uninstall", "logs", "execute"}
EXISTING_GROUPS = {"server", "auth"}


@pytest.fixture
def keyring_store(monkeypatch):
    """One fake keyring shared by server passwords and git tokens, exactly
    like the real one, so collisions between them would show."""
    store = {}

    def delete(service, name):
        if (service, name) not in store:
            raise auth.keyring.errors.PasswordDeleteError("missing")
        del store[(service, name)]

    for module in (auth.keyring, credentials.keyring):
        monkeypatch.setattr(module, "get_password", lambda service, name: store.get((service, name)))
        monkeypatch.setattr(module, "set_password", lambda service, name, value: store.__setitem__((service, name), value))
        monkeypatch.setattr(module, "delete_password", delete)
    return store


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr("benchops.cli.ConfigManager", lambda: ConfigManager(path))
    return path


def cells(text):
    """Table text with the box drawing removed and whitespace collapsed."""
    return " ".join(text.translate({ord(c): " " for c in "│┃┏┓┗┛┡┩└┘┳┻╇┴━─"}).split())


def run(*args, input=None):
    return CliRunner().invoke(cli.app, list(args), input=input)


# --- nothing existing changed -------------------------------------------------


def test_existing_commands_are_all_still_registered():
    commands = {c.name or c.callback.__name__ for c in cli.app.registered_commands}
    groups = {g.name for g in cli.app.registered_groups}
    assert EXISTING_COMMANDS <= commands and "clone" in commands
    assert EXISTING_GROUPS | {"git", "project"} <= groups


def test_server_remove_never_deletes_a_git_token_with_the_same_name(keyring_store, config_path):
    ConfigManager(config_path).add_server("gitlab.akwad.qa", "10.0.0.5", 22, "frappe", "/home/frappe/bench")
    keyring_store[(SERVICE_NAME, "gitlab.akwad.qa")] = "ssh-password"
    keyring_store[(GIT_SERVICE_NAME, "gitlab.akwad.qa")] = "glpat-token"

    assert run("server", "remove", "gitlab.akwad.qa").exit_code == 0

    assert keyring_store == {(GIT_SERVICE_NAME, "gitlab.akwad.qa"): "glpat-token"}


def test_git_logout_never_deletes_a_server_password_with_the_same_name(keyring_store, config_path):
    keyring_store[(SERVICE_NAME, "gitlab.akwad.qa")] = "ssh-password"
    keyring_store[(GIT_SERVICE_NAME, "gitlab.akwad.qa")] = "glpat-token"

    assert run("git", "logout", "gitlab.akwad.qa").exit_code == 0

    assert keyring_store == {(SERVICE_NAME, "gitlab.akwad.qa"): "ssh-password"}


def test_existing_config_keeps_its_servers_and_comments(config_path):
    config_path.write_text(
        "# my servers, hand-edited\n"
        "[servers]\n"
        'staging = { host = "1.2.3.4", port = 22, user = "frappe", bench_path = "/b" }  # keep me\n'
    )

    assert run("project", "add", "erp", "--host", "gitlab.akwad.qa", "--repo", "erp/core").exit_code == 0
    assert run("server", "list").exit_code == 0

    text = config_path.read_text()
    assert "# my servers, hand-edited" in text and "# keep me" in text
    config = ConfigManager(config_path)
    assert config.get_server("staging")["host"] == "1.2.3.4"
    assert config.get_server("staging")["connection_type"] == "ssh"
    assert config.get_project("erp")["repos"] == [{"path": "erp/core"}]


def test_config_without_new_tables_reads_as_empty(config_path):
    config_path.write_text('[servers]\nstaging = { host = "h", port = 22, user = "u", bench_path = "/b" }\n')
    config = ConfigManager(config_path)
    assert config.list_projects() == {} and config.list_git_hosts() == {}
    assert ConfigManager(config_path.parent / "missing.toml").list_projects() == {}


def test_update_notice_still_shows_but_never_for_the_credential_helper(keyring_store, config_path, monkeypatch):
    monkeypatch.setattr(cli, "update_notice", lambda: "benchops 9.9.9 is available")

    monkeypatch.setattr(sys, "argv", ["benchops", "project", "list"])
    assert "9.9.9" in run("project", "list").stderr

    monkeypatch.setattr(sys, "argv", ["benchops", "git", "credential", "get"])
    result = run("git", "credential", "get", input="protocol=https\nhost=x\n")
    assert "9.9.9" not in result.stderr and result.stdout == ""


# --- git ----------------------------------------------------------------------


@pytest.mark.parametrize("value, host", [
    ("GitLab.akwad.qa", "gitlab.akwad.qa"),
    ("https://gitlab.akwad.qa/erp/x.git", "gitlab.akwad.qa"),
    ("git@github.com:o/r.git", "github.com"),
    ("https://token@git.corp:8443/", "git.corp:8443"),
])
def test_host_normalization(value, host):
    assert cli._normalize_host(value) == host


def test_git_hosts_lists_where_each_token_comes_from(keyring_store, config_path, monkeypatch):
    config = ConfigManager(config_path)
    config.set_git_host("gitlab.akwad.qa", "gitlab")
    config.set_git_host("git.corp", "gitea", api_url="https://git.corp/api/v1")
    keyring_store[(GIT_SERVICE_NAME, "gitlab.akwad.qa")] = "t"
    monkeypatch.setenv("BENCHOPS_GIT_TOKEN_GIT_CORP", "t2")

    out = cells(run("git", "hosts").stdout)

    assert "gitlab.akwad.qa gitlab default keyring" in out
    assert "git.corp gitea https://git.corp/api/v1 env" in out


def test_git_whoami_without_a_token_says_how_to_add_one(keyring_store, config_path):
    result = run("git", "whoami", "gitlab.akwad.qa")
    assert result.exit_code == 1 and "benchops git login gitlab.akwad.qa" in result.stdout


def test_git_login_rejects_an_empty_token(keyring_store, config_path):
    result = run("git", "login", "gitlab.akwad.qa", "--provider", "gitlab", "--token-stdin", input="\n")
    assert result.exit_code == 1 and keyring_store == {}


def test_git_login_on_a_generic_host_stores_without_an_api_check(keyring_store, config_path):
    result = run("git", "login", "git.corp", "--provider", "generic", "--username", "x-token-auth",
                 "--token-stdin", input="tok\n")

    assert result.exit_code == 0, result.stdout
    assert keyring_store[(GIT_SERVICE_NAME, "git.corp")] == "tok"
    assert ConfigManager(config_path).get_git_host("git.corp") == {"provider": "generic", "username": "x-token-auth"}


def test_credential_helper_uses_the_stored_username(keyring_store, config_path):
    ConfigManager(config_path).set_git_host("git.corp", "generic", username="x-token-auth")
    keyring_store[(GIT_SERVICE_NAME, "git.corp")] = "tok"

    result = run("git", "credential", "get", input="protocol=https\nhost=git.corp\n")

    assert result.stdout == "username=x-token-auth\npassword=tok\n"


def test_credential_helper_never_answers_over_plain_http(keyring_store, config_path):
    keyring_store[(GIT_SERVICE_NAME, "git.corp")] = "tok"
    assert run("git", "credential", "get", input="protocol=http\nhost=git.corp\n").stdout == ""


# --- project ------------------------------------------------------------------


def test_project_lifecycle(config_path, tmp_path):
    manifest = tmp_path / "erp.toml"
    manifest.write_text('host = "gitlab.akwad.qa"\ngroup = "erp"\nmatch = "akwad_*"\n'
                        '[[repos]]\npath = "erp/core"\nmin_access = "write"\n')

    assert run("project", "add", "erp", "--manifest", str(manifest)).exit_code == 0
    listing = cells(run("project", "list").stdout)
    assert "erp gitlab.akwad.qa 1 erp (akwad_*)" in listing
    shown = run("project", "show", "erp").stdout
    assert "erp/core" in shown and "min_access=write" in shown and "--dry-run" in shown

    assert run("project", "remove", "erp").exit_code == 0
    assert run("project", "show", "erp").exit_code == 1
    assert run("project", "remove", "erp").exit_code == 1


def test_project_add_reports_validation_errors(config_path, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('host = "h"\n[[repos]]\npath = "a/b"\ndir = "../escape"\n')

    result = run("project", "add", "bad", "--manifest", str(bad))

    assert result.exit_code == 1 and "safe folder name" in result.stdout
    assert ConfigManager(config_path).get_project("bad") is None


def test_project_add_needs_some_repositories(config_path):
    result = run("project", "add", "empty", "--host", "gitlab.akwad.qa")
    assert result.exit_code == 1 and "list some repos" in result.stdout


# --- clone wiring ---------------------------------------------------------------


def test_clone_passes_options_through(monkeypatch):
    captured = {}

    class Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def execute(self):
            pass

    monkeypatch.setattr(cli, "CloneCommand", Recorder)
    result = run("clone", "erp", "--only", "core,hr", "--only", "crm", "--skip", "legacy_*",
                 "-j", "6", "--protocol", "ssh", "--partial", "--dry-run", "-y")

    assert result.exit_code == 0, result.stdout
    assert captured["only"] == ["core", "hr", "crm"] and captured["skip"] == ["legacy_*"]
    assert captured["jobs"] == 6 and captured["protocol"] == "ssh"
    assert captured["partial"] and captured["dry_run"] and captured["yes"]


def test_clone_unknown_project_is_a_clear_error(config_path, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("benchops.clone.ConfigManager", lambda: ConfigManager(config_path))
    result = run("clone", "nope")
    assert result.exit_code == 1 and "No project named 'nope'" in result.stdout


def test_clone_rejects_too_many_jobs():
    assert run("clone", "erp", "-j", "100").exit_code != 0
