"""Parsing git remote URLs into a (host, path) identity.

The same repository can be written as `https://host/group/app.git`,
`git@host:group/app.git` or `ssh://git@host:2222/group/app`. Comparing
remotes by their normalized identity is how bulk clone tells "this folder
already holds that repository" apart from "this folder holds something
else" without caring which protocol either side used.
"""

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


@dataclass(frozen=True)
class Remote:
    """A repository's location: `host` keeps a non-default port; local
    repositories use the host "file" and their absolute path."""

    host: str
    path: str

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)

    @property
    def is_local(self) -> bool:
        return self.host == "file"


def _strip(path: str) -> str:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path.rstrip("/")


def parse_remote(url: str) -> Remote:
    """Normalize any URL git accepts for a remote. Raises ValueError for
    something that is not a remote at all."""
    url = url.strip()
    if not url:
        raise ValueError("Empty repository URL.")

    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme == "file":
            return Remote("file", "/" + _strip(parts.path))
        if parts.scheme not in ("http", "https", "ssh", "git", "git+ssh", "ssh+git"):
            raise ValueError(f"Unsupported repository URL scheme '{parts.scheme}' in '{url}'.")
        if not parts.hostname:
            raise ValueError(f"No host in repository URL '{url}'.")
        host = parts.hostname.lower()
        default_port = {"http": 80, "https": 443, "git": 9418}.get(parts.scheme, 22)
        if parts.port and parts.port != default_port and parts.scheme in ("http", "https"):
            host = f"{host}:{parts.port}"
        path = _strip(parts.path)
        if not path:
            raise ValueError(f"No repository path in URL '{url}'.")
        return Remote(host, path)

    if url.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", url):
        return Remote("file", "/" + _strip(url.replace("\\", "/")))

    match = SCP_LIKE.match(url)
    if match:
        return Remote(match["host"].lower(), _strip(match["path"]))

    raise ValueError(f"'{url}' is not a repository URL (expected https://, ssh:// or git@host:path).")


def https_url(host: str, path: str) -> str:
    return f"https://{host}/{path}.git"


def ssh_url(host: str, path: str) -> str:
    # The scp-like form can't carry a port; hosts on a custom HTTPS port
    # almost never serve SSH on it, so the SSH form drops it.
    return f"git@{host.split(':', 1)[0]}:{path}.git"
