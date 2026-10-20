"""Git host tokens: where they live and how they reach git without leaking.

Tokens are kept in the system keyring under their own service name, so a
git host can never collide with a server alias that happens to share its
name. They reach git as an HTTP header passed through `GIT_CONFIG_*`
environment variables: never in argv (visible in `ps`), never in the remote
URL (written to .git/config and echoed in errors), never on disk.
"""

import base64
import os
import re
import shutil
import sys

import keyring
import keyring.errors

from benchops.auth import KeyringUnavailableError

GIT_SERVICE_NAME = "benchops-git"

# Abort a transfer that stalls below 1 KB/s for a minute, so a dead
# connection fails (and is retried) instead of hanging a worker forever.
LOW_SPEED_LIMIT = "1000"
LOW_SPEED_TIME = "60"


def token_env_var(host: str) -> str:
    """BENCHOPS_GIT_TOKEN_<HOST>, e.g. BENCHOPS_GIT_TOKEN_GITLAB_AKWAD_QA."""
    return "BENCHOPS_GIT_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", host).upper()


class TokenStore:
    """Per-host tokens in the system keyring, with an environment override
    for CI and headless hosts that have no keyring backend."""

    def get(self, host: str) -> tuple[str | None, str | None]:
        """Return (token, source), where source is 'env' or 'keyring'."""
        env_token = os.environ.get(token_env_var(host))
        if env_token:
            return env_token.strip(), "env"
        try:
            token = keyring.get_password(GIT_SERVICE_NAME, host)
        except keyring.errors.KeyringError as exc:
            raise KeyringUnavailableError(f"Could not read the token for {host}: {exc}") from exc
        return (token, "keyring") if token else (None, None)

    def set(self, host: str, token: str) -> None:
        try:
            keyring.set_password(GIT_SERVICE_NAME, host, token)
        except keyring.errors.KeyringError as exc:
            raise KeyringUnavailableError(f"Could not save the token for {host}: {exc}") from exc

    def delete(self, host: str) -> None:
        try:
            keyring.delete_password(GIT_SERVICE_NAME, host)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as exc:
            raise KeyringUnavailableError(f"Could not delete the token for {host}: {exc}") from exc


def basic_auth_value(username: str, token: str) -> str:
    return base64.b64encode(f"{username}:{token}".encode()).decode()


def git_env(host: str | None = None, username: str | None = None, token: str | None = None,
            base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for one git process.

    Never prompts (parallel workers would fight over the terminal), aborts
    stalled transfers, and, when a token is given, sends it only to `host`:
    the header is scoped to that URL prefix, so a redirect or submodule
    pointing elsewhere never receives it. Config the caller already passes
    through GIT_CONFIG_* is kept.
    """
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GCM_INTERACTIVE", "never")
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"

    settings = [("http.lowSpeedLimit", LOW_SPEED_LIMIT), ("http.lowSpeedTime", LOW_SPEED_TIME)]
    if host and token:
        header = f"Authorization: Basic {basic_auth_value(username or 'oauth2', token)}"
        settings.append((f"http.https://{host}/.extraHeader", header))

    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    for key, value in settings:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


class Redactor:
    """Masks tokens (raw and in their Basic-auth form) in text shown to the user."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, token: str | None, username: str | None = None) -> None:
        if not token:
            return
        self._secrets.add(token)
        if username:
            self._secrets.add(basic_auth_value(username, token))

    def __call__(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text


def credential_helper_command() -> str:
    """The `credential.helper` value that calls back into this BenchOps."""
    executable = shutil.which("benchops")
    if executable:
        return f"!'{executable}' git credential"
    return f"!'{sys.executable}' -m benchops.cli git credential"


def parse_credential_request(text: str) -> dict[str, str]:
    request = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            request[key.strip()] = value.strip()
    return request
