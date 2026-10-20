"""Units behind bulk clone: remote parsing, provider permission mapping
(driven through a fake urlopen, so the real HTTP code runs), token handling
and project validation, plus the git/project CLI commands."""

import base64
import io
import json
import urllib.error

import pytest
from typer.testing import CliRunner

from benchops import cli
from benchops.auth import SERVICE_NAME
from benchops.clone import same_remote, summarize_git_error
from benchops.config import ConfigManager
from benchops.projects import explicit_specs, validate_project
from benchops.vcs import credentials
from benchops.vcs.credentials import GIT_SERVICE_NAME, Redactor, git_env, token_env_var
from benchops.vcs.providers import (
    Access,
    AuthError,
    GiteaProvider,
    GitHubProvider,
    GitLabProvider,
    Identity,
    gitlab_access,
    provider_kind_for,
)
from benchops.vcs.remotes import Remote, parse_remote

# --- remotes --------------------------------------------------------------


@pytest.mark.parametrize("url, host, path", [
    ("https://gitlab.akwad.qa/erp-team/akwad_core.git", "gitlab.akwad.qa", "erp-team/akwad_core"),
    ("https://GitHub.com/frappe/hrms", "github.com", "frappe/hrms"),
    ("git@github.com:frappe/hrms.git", "github.com", "frappe/hrms"),
    ("ssh://git@gitlab.akwad.qa:2222/erp/sub/app.git", "gitlab.akwad.qa", "erp/sub/app"),
    ("https://git.example.com:8443/team/app/", "git.example.com:8443", "team/app"),
    ("https://user:secret@github.com/o/r.git", "github.com", "o/r"),
    ("/srv/git/app.git", "file", "/srv/git/app"),
    ("file:///srv/git/app.git", "file", "/srv/git/app"),
])
def test_parse_remote(url, host, path):
    assert parse_remote(url) == Remote(host, path)


@pytest.mark.parametrize("url", ["", "ftp://host/x", "https://host/", "just-a-name"])
def test_parse_remote_rejects(url):
    with pytest.raises(ValueError):
        parse_remote(url)


def test_same_remote_ignores_protocol_and_case():
    assert same_remote(parse_remote("git@github.com:Frappe/HRMS.git"), parse_remote("https://github.com/frappe/hrms"))
    assert not same_remote(parse_remote("https://github.com/frappe/hrms"), parse_remote("https://github.com/fork/hrms"))


def test_git_error_summary_keeps_the_cause():
    stderr = ("Cloning into 'x'...\nfatal: '/x.git' does not appear to be a git repository\n"
              "fatal: Could not read from remote repository.\n\nPlease make sure you have the correct access rights\n")
    assert summarize_git_error(stderr).startswith("fatal: '/x.git' does not appear")


# --- providers ------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(json.dumps(data).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def api(monkeypatch):
    """Route urlopen to canned answers keyed by URL; records request headers."""
    routes, seen = {}, []

    def urlopen(request, timeout=None):
        seen.append(request)
        answer = routes.get(request.full_url)
        if answer is None:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "error", {}, None)
        return FakeResponse(*answer) if isinstance(answer, tuple) else FakeResponse(answer)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return routes, seen


def test_github_permissions_map_to_access(api):
    routes, seen = api
    base = "https://api.github.com/repos"
    routes[f"{base}/o/admin"] = {"full_name": "o/admin", "permissions": {"admin": True, "push": True, "pull": True}}
    routes[f"{base}/o/dev"] = {"full_name": "o/dev", "permissions": {"admin": False, "push": True, "pull": True}}
    routes[f"{base}/o/ro"] = {"full_name": "o/ro", "default_branch": "main", "permissions": {"push": False, "pull": True}}
    routes[f"{base}/o/public"] = {"full_name": "o/public"}
    provider = GitHubProvider("github.com", token="ghp_x")

    assert provider.repo("o/admin").access == Access.ADMIN
    assert provider.repo("o/dev").access == Access.WRITE
    ro = provider.repo("o/ro")
    assert ro.access == Access.READ and ro.repo.default_branch == "main"
    assert ro.repo.https_url == "https://github.com/o/ro.git"
    assert provider.repo("o/public").access == Access.READ
    assert provider.repo("o/hidden").access == Access.NONE  # 404: private repos are hidden
    assert seen[0].get_header("Authorization") == "Bearer ghp_x"


def test_github_enterprise_api_url_and_token_metadata(api):
    routes, _ = api
    routes["https://ghe.corp/api/v3/user"] = (
        {"login": "mo"}, {"X-OAuth-Scopes": "read:org", "github-authentication-token-expiration": "2026-11-01 00:00:00 UTC"},
    )
    identity = GitHubProvider("ghe.corp", token="t").whoami()
    assert identity.username == "mo" and str(identity.expires) == "2026-11-01"
    assert any("repo" in w for w in identity.warnings)


def test_rejected_token_is_an_auth_error(api):
    routes, _ = api
    routes["https://gitlab.akwad.qa/api/v4/user"] = 401
    with pytest.raises(AuthError):
        GitLabProvider("gitlab.akwad.qa", token="bad").whoami()


@pytest.mark.parametrize("level, access", [(50, Access.ADMIN), (40, Access.ADMIN), (30, Access.WRITE),
                                           (20, Access.READ), (10, Access.NONE), (0, Access.NONE)])
def test_gitlab_roles(level, access):
    assert gitlab_access(level) == access


def test_gitlab_repo_uses_the_higher_of_project_and_group_role(api):
    routes, seen = api
    base = "https://gitlab.akwad.qa/api/v4/projects"
    routes[f"{base}/erp%2Fcore"] = {
        "path_with_namespace": "erp/core", "visibility": "private", "default_branch": "develop",
        "http_url_to_repo": "https://gitlab.akwad.qa/erp/core.git", "ssh_url_to_repo": "git@gitlab.akwad.qa:erp/core.git",
        "permissions": {"project_access": {"access_level": 20}, "group_access": {"access_level": 30}},
    }
    routes[f"{base}/erp%2Fguest"] = {
        "path_with_namespace": "erp/guest", "visibility": "private",
        "permissions": {"project_access": {"access_level": 10}, "group_access": None},
    }
    routes[f"{base}/erp%2Finternal"] = {
        "path_with_namespace": "erp/internal", "visibility": "internal", "permissions": {},
    }
    provider = GitLabProvider("gitlab.akwad.qa", token="glpat")

    core = provider.repo("erp/core")
    assert core.access == Access.WRITE and core.repo.default_branch == "develop"
    guest = provider.repo("erp/guest")
    assert guest.access == Access.NONE and "Guest" in guest.reason
    assert provider.repo("erp/internal").access == Access.READ
    assert seen[0].get_header("Private-token") == "glpat"


def test_group_discovery_follows_pagination(api):
    routes, _ = api
    base = "https://gitlab.akwad.qa/api/v4/groups/erp/projects?include_subgroups=true&archived=false&per_page=100"
    page = lambda n: {"path_with_namespace": f"erp/app{n}"}
    routes[base] = ([page(1), page(2)], {"Link": f'<{base}&page=2>; rel="next"'})
    routes[f"{base}&page=2"] = [page(3)]

    repos = GitLabProvider("gitlab.akwad.qa", token="t").list_group("erp")

    assert [r.path for r in repos] == ["erp/app1", "erp/app2", "erp/app3"]


def test_github_discovery_falls_back_from_org_to_user(api):
    routes, _ = api
    routes["https://api.github.com/users/mo/repos?per_page=100&type=owner"] = [
        {"full_name": "mo/a"}, {"full_name": "mo/old", "archived": True},
    ]
    assert [r.path for r in GitHubProvider("github.com", token="t").list_group("mo")] == ["mo/a"]


def test_gitea_uses_the_account_name_for_git(api):
    routes, _ = api
    routes["https://git.corp/api/v1/user"] = {"login": "mo"}
    provider = GiteaProvider("git.corp", token="t")
    provider.whoami()
    assert provider.git_username() == "mo"


def test_unreachable_api_is_a_provider_error_not_a_crash():
    # conftest disables urlopen for the whole test run
    with pytest.raises(Exception) as exc:
        GitLabProvider("gitlab.akwad.qa", token="t").whoami()
    assert "Could not reach" in str(exc.value)


def test_provider_detection():
    assert provider_kind_for("github.com") == "github"
    assert provider_kind_for("gitlab.akwad.qa") == "generic"
    assert provider_kind_for("gitlab.akwad.qa", "gitlab") == "gitlab"


# --- credentials ----------------------------------------------------------


def test_git_env_scopes_the_token_to_its_host_and_keeps_existing_config():
    env = git_env("gitlab.akwad.qa", "oauth2", "glpat-secret",
                  base={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false"})

    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["core.autocrlf"] == "false"
    header = pairs["http.https://gitlab.akwad.qa/.extraHeader"]
    assert base64.b64decode(header.split()[-1]).decode() == "oauth2:glpat-secret"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not any("glpat-secret" in value for value in env.values())


def test_git_env_without_token_sends_no_header():
    env = git_env(base={})
    assert not any("extraHeader" in env.get(f"GIT_CONFIG_KEY_{i}", "") for i in range(int(env["GIT_CONFIG_COUNT"])))


def test_redactor_masks_raw_and_basic_forms():
    redact = Redactor()
    redact.add("glpat-secret", "oauth2")
    basic = base64.b64encode(b"oauth2:glpat-secret").decode()
    assert redact(f"bad token glpat-secret / {basic}") == "bad token *** / ***"


def test_git_tokens_never_share_keyring_entries_with_server_passwords():
    assert GIT_SERVICE_NAME != SERVICE_NAME


def test_env_token_wins_over_keyring(monkeypatch):
    monkeypatch.setenv(token_env_var("gitlab.akwad.qa"), "from-env")
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *a: pytest.fail("keyring was read"))
    assert credentials.TokenStore().get("gitlab.akwad.qa") == ("from-env", "env")


# --- projects and config --------------------------------------------------


def test_project_validation_errors():
    with pytest.raises(ValueError, match="unknown setting"):
        validate_project({"host": "h", "repos": [{"path": "a"}], "hots": "x"})
    with pytest.raises(ValueError, match="exactly one"):
        validate_project({"host": "h", "repos": [{"path": "a", "url": "https://h/a"}]})
    with pytest.raises(ValueError, match="needs the project's 'host'"):
        validate_project({"repos": [{"path": "a/b"}]})
    with pytest.raises(ValueError, match="safe folder"):
        validate_project({"host": "h", "repos": [{"path": "a/b", "dir": "../evil"}]})
    with pytest.raises(ValueError, match="list some repos"):
        validate_project({"host": "h"})
    with pytest.raises(ValueError, match="contains credentials"):
        validate_project({"repos": [{"url": "https://oauth2:glpat-x@gitlab.akwad.qa/erp/core.git"}]})
    with pytest.raises(ValueError, match="access level"):
        validate_project({"host": "h", "repos": [{"path": "a/b", "min_access": "owner"}]})


def test_explicit_specs_mix_hosts():
    project = validate_project({
        "host": "gitlab.akwad.qa", "branch": "develop",
        "repos": [{"path": "erp/core"}, {"url": "https://github.com/frappe/hrms", "branch": "version-15", "dir": "hr"}],
    })
    core, hrms = explicit_specs(project)
    assert (core.host, core.path, core.dir, core.branch) == ("gitlab.akwad.qa", "erp/core", "core", "develop")
    assert (hrms.host, hrms.path, hrms.dir, hrms.branch) == ("github.com", "frappe/hrms", "hr", "version-15")


def test_projects_hosts_and_servers_with_one_name_coexist(tmp_path):
    config = ConfigManager(tmp_path / "config.toml")
    config.add_server("erp", "1.2.3.4", 22, "frappe", "/home/frappe/bench")
    config.set_project("erp", validate_project({"host": "gitlab.akwad.qa", "repos": [{"path": "erp/core"}]}))
    config.set_git_host("erp", "gitlab")

    assert config.get_server("erp")["host"] == "1.2.3.4"
    assert config.get_project("erp")["repos"] == [{"path": "erp/core"}]
    assert config.get_git_host("erp")["provider"] == "gitlab"
    config.remove_project("erp")
    assert config.get_server("erp") is not None and config.get_git_host("erp") is not None


# --- CLI ------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    store = {}
    monkeypatch.setattr(credentials.keyring, "get_password", lambda service, host: store.get((service, host)))
    monkeypatch.setattr(credentials.keyring, "set_password", lambda service, host, token: store.__setitem__((service, host), token))
    monkeypatch.setattr("benchops.cli.ConfigManager", lambda: ConfigManager(tmp_path / "config.toml"))
    return store


def test_project_add_refuses_to_overwrite_without_replace(cli_env):
    runner = CliRunner()
    args = ["project", "add", "erp", "--host", "gitlab.akwad.qa", "--repo", "erp/core,erp/hr"]
    assert runner.invoke(cli.app, args).exit_code == 0
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1 and "already exists" in result.output
    assert runner.invoke(cli.app, [*args, "--replace"]).exit_code == 0


def test_git_login_verifies_then_stores_the_token(cli_env, monkeypatch):
    monkeypatch.setattr(GitLabProvider, "whoami", lambda self: Identity("mo"))
    result = CliRunner().invoke(
        cli.app, ["git", "login", "https://gitlab.akwad.qa/erp/x.git", "--provider", "gitlab", "--token-stdin"],
        input="glpat-secret\n",
    )
    assert result.exit_code == 0, result.output
    assert cli_env[(GIT_SERVICE_NAME, "gitlab.akwad.qa")] == "glpat-secret"
    assert "glpat-secret" not in result.output


def test_git_login_saves_nothing_when_the_token_is_rejected(cli_env, monkeypatch):
    def rejected(self):
        raise AuthError("401")

    monkeypatch.setattr(GitLabProvider, "whoami", rejected)
    result = CliRunner().invoke(cli.app, ["git", "login", "gitlab.akwad.qa", "--provider", "gitlab", "--token-stdin"],
                                input="bad\n")
    assert result.exit_code == 1 and cli_env == {}


def test_credential_helper_answers_only_for_known_https_hosts(cli_env):
    cli_env[(GIT_SERVICE_NAME, "gitlab.akwad.qa")] = "glpat-secret"
    runner = CliRunner()

    result = runner.invoke(cli.app, ["git", "credential", "get"], input="protocol=https\nhost=gitlab.akwad.qa\n\n")
    assert result.output == "username=oauth2\npassword=glpat-secret\n"
    assert runner.invoke(cli.app, ["git", "credential", "get"], input="protocol=https\nhost=other.com\n").output == ""
    assert runner.invoke(cli.app, ["git", "credential", "store"], input="protocol=https\nhost=gitlab.akwad.qa\n").output == ""


def test_project_add_treats_repos_without_host_as_urls(cli_env, tmp_path):
    result = CliRunner().invoke(cli.app, ["project", "add", "local", "--repo", "/srv/git/core.git",
                                          "--repo", "git@github.com:frappe/hrms.git"])
    assert result.exit_code == 0, result.output
    repos = ConfigManager(tmp_path / "config.toml").get_project("local")["repos"]
    assert repos == [{"url": "/srv/git/core.git"}, {"url": "git@github.com:frappe/hrms.git"}]


def test_gitea_permissions(api):
    routes, seen = api
    base = "https://git.corp/api/v1/repos"
    routes[f"{base}/o/admin"] = {"full_name": "o/admin", "private": True, "permissions": {"admin": True}}
    routes[f"{base}/o/dev"] = {"full_name": "o/dev", "private": True, "permissions": {"push": True, "pull": True}}
    routes[f"{base}/o/locked"] = {"full_name": "o/locked", "private": True, "permissions": {}}
    routes[f"{base}/o/open"] = {"full_name": "o/open", "private": False, "permissions": {}}
    provider = GiteaProvider("git.corp", token="t")

    assert provider.repo("o/admin").access == Access.ADMIN
    assert provider.repo("o/dev").access == Access.WRITE
    assert provider.repo("o/locked").access == Access.NONE
    assert provider.repo("o/open").access == Access.READ
    assert seen[0].get_header("Authorization") == "token t"


def test_server_errors_are_provider_errors_not_no_access(api):
    routes, _ = api
    routes["https://api.github.com/repos/o/r"] = 500
    with pytest.raises(Exception) as exc:
        GitHubProvider("github.com", token="t").repo("o/r")
    assert "HTTP 500" in str(exc.value)


def test_generic_hosts_cannot_discover_groups():
    from benchops.vcs.providers import Provider, ProviderError

    with pytest.raises(ProviderError, match="generic git host"):
        Provider("git.corp").list_group("team")
