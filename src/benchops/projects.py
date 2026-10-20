"""Clone projects: a named group of repositories cloned together.

A project lists repositories explicitly, discovers them from a GitHub org /
GitLab group / Gitea org, or both (explicit entries override discovered
ones). The same schema is used in ~/.benchops/config.toml under
[projects.<name>] and in a standalone manifest file a team can commit:

    host = "gitlab.akwad.qa"
    protocol = "https"          # or "ssh"
    branch = "develop"          # default branch for every repo (optional)
    group = "erp-team"          # discover repos from this group (optional)
    match = "akwad_*"           # only discovered repos matching this glob

    [[repos]]
    path = "erp-team/akwad_core"

    [[repos]]
    url = "https://github.com/frappe/hrms"
    branch = "version-15"
    dir = "hrms"                # folder name (default: the repo name)
    min_access = "write"        # skip it for users who can only read

Tokens never go in a project, which is what makes a manifest safe to share.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from benchops.vcs.providers import PROVIDERS, Access
from benchops.vcs.remotes import parse_remote

PROJECT_KEYS = {"host", "provider", "protocol", "branch", "group", "match", "repos"}
REPO_KEYS = {"path", "url", "branch", "dir", "min_access"}
PROTOCOLS = ("https", "ssh")
SAFE_DIR = re.compile(r"^[A-Za-z0-9._-]+$")
CREDENTIALS_IN_URL = re.compile(r"^[a-z+]+://[^/@]*:[^/@]*@", re.IGNORECASE)


@dataclass(frozen=True)
class RepoSpec:
    """One repository to clone, before any network check."""

    host: str
    path: str
    dir: str
    branch: str | None = None
    url: str | None = None
    min_access: Access = Access.READ
    explicit: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return self.host, self.path.lower()

    @property
    def label(self) -> str:
        return self.path if self.host == "file" else f"{self.host}/{self.path}"


def validate_dir_name(name: str) -> str:
    if not SAFE_DIR.match(name) or name in (".", ".."):
        raise ValueError(f"'{name}' is not a safe folder name (use letters, digits, '.', '_' and '-').")
    return name


def validate_project(data: dict, source: str = "project") -> dict:
    """Check a project's shape and normalize it. Raises ValueError naming
    the first problem, so a typo in a manifest never half-clones."""
    if not isinstance(data, dict):
        raise ValueError(f"{source}: expected a table of settings.")
    unknown = set(data) - PROJECT_KEYS
    if unknown:
        raise ValueError(f"{source}: unknown setting(s) {', '.join(sorted(unknown))}.")

    project = {key: data[key] for key in PROJECT_KEYS - {"repos"} if data.get(key) not in (None, "")}
    if "host" in project:
        project["host"] = str(project["host"]).strip().lower()
    protocol = project.setdefault("protocol", "https")
    if protocol not in PROTOCOLS:
        raise ValueError(f"{source}: protocol must be 'https' or 'ssh', not '{protocol}'.")
    if "provider" in project and project["provider"] not in PROVIDERS:
        raise ValueError(f"{source}: unknown provider '{project['provider']}' (expected {', '.join(PROVIDERS)}).")
    if "group" in project and "host" not in project:
        raise ValueError(f"{source}: 'group' needs a 'host' to discover repositories on.")
    if "match" in project and "group" not in project:
        raise ValueError(f"{source}: 'match' filters discovered repositories, so it needs a 'group'.")

    repos = []
    for index, repo in enumerate(data.get("repos") or [], start=1):
        where = f"{source}: repos[{index}]"
        if not isinstance(repo, dict):
            raise ValueError(f"{where}: expected a table.")
        unknown = set(repo) - REPO_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}.")
        if bool(repo.get("path")) == bool(repo.get("url")):
            raise ValueError(f"{where}: set exactly one of 'path' or 'url'.")
        if repo.get("path") and "host" not in project:
            raise ValueError(f"{where}: 'path' needs the project's 'host' (or use a full 'url').")
        if repo.get("url"):
            parse_remote(repo["url"])
            if CREDENTIALS_IN_URL.match(repo["url"]):
                raise ValueError(
                    f"{where}: the URL contains credentials. Remove them and run 'benchops git login <host>'; "
                    "a token in a URL ends up in .git/config and in shared manifests."
                )
        if repo.get("dir"):
            validate_dir_name(repo["dir"])
        if repo.get("min_access"):
            Access.parse(repo["min_access"])
        repos.append({key: value for key, value in repo.items() if value not in (None, "")})

    if not repos and "group" not in project:
        raise ValueError(f"{source}: list some repos or set a 'group' to discover them from.")
    project["repos"] = repos
    return project


def load_manifest(path: Path) -> dict:
    try:
        data = tomlkit.parse(path.read_text()).unwrap()
    except OSError as exc:
        raise ValueError(f"Could not read manifest '{path}': {exc}") from exc
    except tomlkit.exceptions.TOMLKitError as exc:
        raise ValueError(f"'{path}' is not valid TOML: {exc}") from exc
    return validate_project(data, source=str(path))


def explicit_specs(project: dict) -> list[RepoSpec]:
    """The repositories a project lists by hand."""
    specs = []
    for repo in project["repos"]:
        if repo.get("url"):
            remote = parse_remote(repo["url"])
            host, path, url = remote.host, remote.path, repo["url"]
        else:
            host, path, url = project["host"], repo["path"].strip("/").removesuffix(".git"), None
        specs.append(RepoSpec(
            host=host,
            path=path,
            dir=repo.get("dir") or path.rsplit("/", 1)[-1],
            branch=repo.get("branch") or project.get("branch"),
            url=url,
            min_access=Access.parse(repo["min_access"]) if repo.get("min_access") else Access.READ,
        ))
    return specs
