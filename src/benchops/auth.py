"""Authentication and credential management for benchops."""

import keyring

SERVICE_NAME = "benchops"


class AuthManager:
    """Securely stores and retrieves server passwords via the system keyring."""

    def set_password(self, server_alias: str, password: str) -> None:
        """Store the password for a server in the system credential store."""
        keyring.set_password(SERVICE_NAME, server_alias, password)

    def get_password(self, server_alias: str) -> str | None:
        """Return the stored password for a server, or None if not set."""
        return keyring.get_password(SERVICE_NAME, server_alias)

    def delete_password(self, server_alias: str) -> None:
        """Delete the stored password for a server if one exists."""
        try:
            keyring.delete_password(SERVICE_NAME, server_alias)
        except keyring.errors.PasswordDeleteError:
            pass
