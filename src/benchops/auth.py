"""Authentication and credential management for benchops."""

import keyring
import keyring.errors

SERVICE_NAME = "benchops"


class KeyringUnavailableError(Exception):
    """Raised when the system keyring backend cannot be reached (e.g. no backend
    is configured, as is common on headless RHEL hosts)."""


class AuthManager:
    """Securely stores and retrieves server passwords via the system keyring."""

    def set_password(self, server_alias: str, password: str) -> None:
        """Store the password for a server in the system credential store."""
        try:
            keyring.set_password(SERVICE_NAME, server_alias, password)
        except keyring.errors.KeyringError as exc:
            raise KeyringUnavailableError(f"Could not save credential: {exc}") from exc

    def get_password(self, server_alias: str) -> str | None:
        """Return the stored password for a server, or None if not set."""
        try:
            return keyring.get_password(SERVICE_NAME, server_alias)
        except keyring.errors.KeyringError as exc:
            raise KeyringUnavailableError(f"Could not read credential: {exc}") from exc

    def delete_password(self, server_alias: str) -> None:
        """Delete the stored password for a server if one exists."""
        try:
            keyring.delete_password(SERVICE_NAME, server_alias)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as exc:
            raise KeyringUnavailableError(f"Could not delete credential: {exc}") from exc
