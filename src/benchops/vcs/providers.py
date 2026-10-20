"""Git hosting providers: who the token belongs to and what it may read.

Each provider maps its own permission model onto one `Access` scale, so the
clone pipeline never has to know whether it is talking to GitHub, a
self-hosted GitLab or a Gitea instance. The git host stays the only source
of truth for permissions: BenchOps reports what the host says and never
grants or assumes access on its own.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum

from benchops.vcs.remotes import https_url, ssh_url

HTTP_TIMEOUT_SECONDS = 20
MAX_PAGES = 50


class Access(IntEnum):
    NONE = 0
    READ = 1
    WRITE = 2
    ADMIN = 3

    @classmethod
    def parse(cls, value: str) -> "Access":
        try:
            return cls[value.strip().upper()]
        except KeyError:
            raise ValueError(f"Unknown access level '{value}' (expected read, write or admin).") from None

    @property
    def label(self) -> str:
        return self.name.lower()


class ProviderError(Exception):
    """The provider's API could not answer (unreachable, bad response)."""


class AuthError(ProviderError):
    """The provider rejected the token itself."""


class NotFound(ProviderError):
    """The API answered 404 (also how hosts hide private resources)."""


@dataclass(frozen=True)
class RemoteRepo:
    path: str
    https_url: str
    ssh_url: str
    default_branch: str | None = None
    private: bool | None = None

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass
class RepoAccess:
    access: Access
    repo: RemoteRepo | None = None
    reason: str = ""


@dataclass
class Identity:
    username: str | None
    name: str | None = None
    scopes: list[str] | None = None
    expires: date | None = None
    warnings: list[str] = field(default_factory=list)


def _next_link(header: str | None) -> str | None:
    for part in (header or "").split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if match:
            return match.group(1)
    return None


class Provider:
    """Base class: HTTP plumbing shared by the API-backed providers."""

    kind = "generic"
    default_username = "oauth2"
    has_api = False

    def __init__(self, host: str, token: str | None = None, api_url: str | None = None,
                 username: str | None = None) -> None:
        self.host = host
        self.token = token
        self.api_url = (api_url or self.default_api_url()).rstrip("/")
        self.username = username

    def default_api_url(self) -> str:
        return f"https://{self.host}"

    def auth_headers(self) -> dict[str, str]:
        return {}

    def git_username(self) -> str:
        """The HTTP Basic username git sends alongside the token."""
        return self.username or self.default_username

    def _request(self, url: str) -> tuple[object, dict[str, str]]:
        if not url.startswith(("http://", "https://")):
            url = f"{self.api_url}/{url.lstrip('/')}"
        from benchops.version import installed_version

        headers = {"Accept": "application/json", "User-Agent": f"benchops/{installed_version()}"}
        headers.update(self.auth_headers())
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                body = response.read()
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthError(f"{self.host} rejected the token (HTTP 401).") from None
            raise _HTTPStatus(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(f"Could not reach {self.api_url}: {reason}") from None
        try:
            return (json.loads(body) if body else None), response_headers
        except ValueError:
            raise ProviderError(f"{self.api_url} did not answer with JSON; is the API URL right?") from None

    def _get(self, path: str) -> tuple[object, dict[str, str]]:
        try:
            return self._request(path)
        except _HTTPStatus as exc:
            error = NotFound if exc.code == 404 else ProviderError
            raise error(f"{self.host} answered HTTP {exc.code} for {path}.") from None

    def _get_optional(self, path: str) -> tuple[object, dict[str, str]] | None:
        """GET that maps 403/404 to None: hosts answer 404 for private
        repositories the caller may not see, so both mean 'no access'."""
        try:
            return self._request(path)
        except _HTTPStatus as exc:
            if exc.code in (403, 404):
                return None
            raise ProviderError(f"{self.host} answered HTTP {exc.code} for {path}.") from None

    def _paginate(self, path: str) -> list:
        items, url, pages = [], path, 0
        while url and pages < MAX_PAGES:
            data, headers = self._get(url)
            if not isinstance(data, list):
                raise ProviderError(f"Unexpected response from {self.host} for {path}.")
            items.extend(data)
            url, pages = _next_link(headers.get("link")), pages + 1
        return items

    def _repo(self, path: str, data: dict, default_branch_key: str = "default_branch") -> RemoteRepo:
        return RemoteRepo(
            path=path,
            https_url=https_url(self.host, path),
            ssh_url=ssh_url(self.host, path),
            default_branch=data.get(default_branch_key),
            private=data.get("private"),
        )

    def whoami(self) -> Identity:
        return Identity(username=self.username)

    def repo(self, path: str) -> RepoAccess:
        """Access to one repository. Generic hosts have no API; the clone
        pipeline confirms access with `git ls-remote` instead."""
        return RepoAccess(Access.READ, RemoteRepo(path, https_url(self.host, path), ssh_url(self.host, path)),
                          "no API: checked with git")

    def list_group(self, group: str) -> list[RemoteRepo]:
        raise ProviderError(
            f"'{self.host}' is a generic git host; listing a group needs a GitHub, GitLab or Gitea API. "
            "List the repositories in the project instead."
        )


class _HTTPStatus(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class GitHubProvider(Provider):
    kind = "github"
    default_username = "x-access-token"
    has_api = True

    def default_api_url(self) -> str:
        return "https://api.github.com" if self.host == "github.com" else f"https://{self.host}/api/v3"

    def auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def whoami(self) -> Identity:
        data, headers = self._get("/user")
        scopes = headers.get("x-oauth-scopes")
        identity = Identity(
            username=data.get("login"),
            name=data.get("name"),
            # Fine-grained tokens don't send the header; their permissions
            # show up per repository instead.
            scopes=[s.strip() for s in scopes.split(",") if s.strip()] if scopes is not None else None,
        )
        expiry = headers.get("github-authentication-token-expiration")
        if expiry:
            try:
                identity.expires = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
        if identity.scopes is not None and not {"repo", "public_repo"} & set(identity.scopes):
            identity.warnings.append("The token has no 'repo' scope, so private repositories won't be visible.")
        return identity

    def repo(self, path: str) -> RepoAccess:
        found = self._get_optional(f"/repos/{path}")
        if found is None:
            return RepoAccess(Access.NONE, reason="not found, or private and this token can't see it")
        data, _ = found
        repo = self._repo(data.get("full_name") or path, data)
        permissions = data.get("permissions")
        if not permissions:
            # Anonymous requests get no permissions block; seeing it at all means it's public.
            return RepoAccess(Access.READ, repo, "public")
        if permissions.get("admin") or permissions.get("maintain"):
            return RepoAccess(Access.ADMIN, repo)
        if permissions.get("push"):
            return RepoAccess(Access.WRITE, repo)
        if permissions.get("pull"):
            return RepoAccess(Access.READ, repo)
        return RepoAccess(Access.NONE, repo, "the token has no read permission")

    def list_group(self, group: str) -> list[RemoteRepo]:
        quoted = urllib.parse.quote(group, safe="")
        try:
            items = self._paginate(f"/orgs/{quoted}/repos?per_page=100&type=all")
        except NotFound:
            items = self._paginate(f"/users/{quoted}/repos?per_page=100&type=owner")
        return [self._repo(item["full_name"], item) for item in items if not item.get("archived")]


def gitlab_access(level: int) -> Access:
    """Owner (50) and Maintainer (40) administer, Developer (30) pushes,
    Reporter (20) reads; Guest (10) can't read code in a private project."""
    if level >= 40:
        return Access.ADMIN
    if level >= 30:
        return Access.WRITE
    if level >= 20:
        return Access.READ
    return Access.NONE


class GitLabProvider(Provider):
    kind = "gitlab"
    default_username = "oauth2"
    has_api = True

    def default_api_url(self) -> str:
        return f"https://{self.host}/api/v4"

    def auth_headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token} if self.token else {}

    def whoami(self) -> Identity:
        data, _ = self._get("/user")
        identity = Identity(username=data.get("username"), name=data.get("name"))
        # Only personal/project/group access tokens can describe themselves;
        # OAuth tokens get a 404 here, which is fine.
        token_info = self._get_optional("/personal_access_tokens/self")
        if token_info:
            info, _ = token_info
            identity.scopes = info.get("scopes")
            if info.get("expires_at"):
                try:
                    identity.expires = date.fromisoformat(info["expires_at"][:10])
                except ValueError:
                    pass
            if identity.scopes is not None and not {"api", "read_api", "read_repository"} & set(identity.scopes):
                identity.warnings.append("The token has no read_repository/read_api scope.")
            elif identity.scopes is not None and not {"api", "read_repository", "write_repository"} & set(identity.scopes):
                identity.warnings.append("The token can use the API but not git (add the read_repository scope).")
        return identity

    def _gitlab_repo(self, data: dict) -> RemoteRepo:
        return RemoteRepo(
            path=data["path_with_namespace"],
            https_url=data.get("http_url_to_repo") or https_url(self.host, data["path_with_namespace"]),
            ssh_url=data.get("ssh_url_to_repo") or ssh_url(self.host, data["path_with_namespace"]),
            default_branch=data.get("default_branch"),
            private=data.get("visibility") == "private" if data.get("visibility") else None,
        )

    def repo(self, path: str) -> RepoAccess:
        found = self._get_optional(f"/projects/{urllib.parse.quote(path, safe='')}")
        if found is None:
            return RepoAccess(Access.NONE, reason="not found, or private and this token can't see it")
        data, _ = found
        repo = self._gitlab_repo(data)
        permissions = data.get("permissions") or {}
        levels = [
            (permissions.get(key) or {}).get("access_level") or 0
            for key in ("project_access", "group_access")
        ]
        level = max(levels)
        if gitlab_access(level) > Access.NONE:
            return RepoAccess(gitlab_access(level), repo)
        if data.get("visibility") in ("public", "internal"):
            return RepoAccess(Access.READ, repo, data["visibility"])
        if level == 10:
            return RepoAccess(Access.NONE, repo, "the Guest role can't read code in a private project")
        return RepoAccess(Access.NONE, repo, "no membership")

    def list_group(self, group: str) -> list[RemoteRepo]:
        quoted = urllib.parse.quote(group, safe="")
        try:
            items = self._paginate(
                f"/groups/{quoted}/projects?include_subgroups=true&archived=false&per_page=100"
            )
        except NotFound:
            items = self._paginate(f"/users/{quoted}/projects?archived=false&per_page=100")
        return [self._gitlab_repo(item) for item in items]


class GiteaProvider(Provider):
    kind = "gitea"
    has_api = True

    def default_api_url(self) -> str:
        return f"https://{self.host}/api/v1"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"} if self.token else {}

    def git_username(self) -> str:
        # Gitea pairs a token with the account it belongs to.
        return self.username or "git"

    def whoami(self) -> Identity:
        data, _ = self._get("/user")
        if not self.username:
            self.username = data.get("login")
        return Identity(username=data.get("login"), name=data.get("full_name"))

    def repo(self, path: str) -> RepoAccess:
        found = self._get_optional(f"/repos/{path}")
        if found is None:
            return RepoAccess(Access.NONE, reason="not found, or private and this token can't see it")
        data, _ = found
        repo = self._repo(data.get("full_name") or path, data)
        permissions = data.get("permissions") or {}
        if permissions.get("admin"):
            return RepoAccess(Access.ADMIN, repo)
        if permissions.get("push"):
            return RepoAccess(Access.WRITE, repo)
        if permissions.get("pull") or not data.get("private"):
            return RepoAccess(Access.READ, repo)
        return RepoAccess(Access.NONE, repo, "the token has no read permission")

    def list_group(self, group: str) -> list[RemoteRepo]:
        quoted = urllib.parse.quote(group, safe="")
        try:
            items = self._paginate(f"/orgs/{quoted}/repos?limit=50")
        except NotFound:
            items = self._paginate(f"/users/{quoted}/repos?limit=50")
        return [self._repo(item["full_name"], item) for item in items if not item.get("archived")]


PROVIDERS: dict[str, type[Provider]] = {
    cls.kind: cls for cls in (GitHubProvider, GitLabProvider, GiteaProvider, Provider)
}
KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "gitea", "gitea.com": "gitea"}


def provider_kind_for(host: str, configured: str | None = None) -> str:
    kind = configured or KNOWN_HOSTS.get(host, "generic")
    if kind not in PROVIDERS:
        raise ValueError(f"Unknown provider '{kind}' (expected one of: {', '.join(PROVIDERS)}).")
    return kind


def make_provider(kind: str, host: str, token: str | None = None, api_url: str | None = None,
                  username: str | None = None) -> Provider:
    return PROVIDERS[provider_kind_for(host, kind)](host, token=token, api_url=api_url, username=username)
