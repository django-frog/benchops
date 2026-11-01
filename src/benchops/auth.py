"""Authentication and credential management for benchops."""

import shlex
import time
from pathlib import Path

import botocore.exceptions
import keyring
import keyring.errors
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from benchops.runner import BenchOpsConnectionError
from benchops.security import secure_file
from benchops.ssm import SsmError, explain_client_error, session_for

SERVICE_NAME = "benchops"

KEYS_DIR = Path.home() / ".benchops" / "keys"
DEFAULT_KEY_NAME = "benchops_ed25519"

# SSM enforces a minimum of 30 seconds for RunCommand.
SSM_COMMAND_TIMEOUT_SECONDS = 30
SSM_POLL_INTERVAL_SECONDS = 2
# How long to wait, beyond the command's own timeout, for SSM to report a
# terminal status before giving up client-side.
SSM_POLL_GRACE_SECONDS = 15


class KeyringUnavailableError(Exception):
    """Raised when the system keyring backend cannot be reached (e.g. no backend
    is configured, as is common on headless RHEL hosts)."""


class AuthManager:
    """Securely stores and retrieves server credentials, and bootstraps SSH
    trust onto remote hosts over SSM."""

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

    def generate_keypair(self, key_name: str = DEFAULT_KEY_NAME) -> tuple[Path, Path]:
        """Generate a BenchOps-managed ed25519 keypair under ~/.benchops/keys/.

        Idempotent: if a keypair with this name already exists, it is reused
        rather than regenerated, so re-running `setup-keys` for another
        server doesn't invalidate trust already established on other hosts.
        """
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        secure_file(KEYS_DIR, 0o700)

        private_path = KEYS_DIR / key_name
        public_path = KEYS_DIR / f"{key_name}.pub"

        if private_path.exists() and public_path.exists():
            return private_path, public_path

        private_key = Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )

        private_path.write_bytes(private_bytes)
        secure_file(private_path, 0o600)
        public_path.write_bytes(public_bytes + f" {key_name}\n".encode())
        public_path.chmod(0o644)

        return private_path, public_path

    def provision_public_key(self, server_config: dict, public_key: str) -> None:
        """Append `public_key` to the server user's authorized_keys on its
        EC2 instance via an SSM RunCommand (AWS-RunShellScript).

        Raises BenchOpsConnectionError for anything that prevents trust from
        being established: missing IAM permissions, an offline/unregistered
        SSM agent, or a failing remote script.
        """
        instance_id = server_config["instance_id"]
        try:
            ssm = session_for(server_config).client("ssm")
        except SsmError as exc:
            raise BenchOpsConnectionError(str(exc)) from exc
        except botocore.exceptions.BotoCoreError as exc:
            raise BenchOpsConnectionError(explain_client_error(exc, instance_id, "ssm:SendCommand")) from exc

        script = _build_authorized_keys_script(server_config["user"], public_key)

        try:
            response = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Comment="benchops auth setup-keys",
                Parameters={"commands": script.splitlines()},
                TimeoutSeconds=SSM_COMMAND_TIMEOUT_SECONDS,
            )
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as exc:
            raise BenchOpsConnectionError(explain_client_error(exc, instance_id, "ssm:SendCommand")) from exc

        command_id = response["Command"]["CommandId"]
        self._await_command_success(ssm, command_id, instance_id)

    def _await_command_success(self, ssm, command_id: str, instance_id: str) -> None:
        """Poll an SSM command invocation until it reaches a terminal status."""
        deadline = time.monotonic() + SSM_COMMAND_TIMEOUT_SECONDS + SSM_POLL_GRACE_SECONDS
        last_status = "Pending"

        while time.monotonic() < deadline:
            try:
                invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            except botocore.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "InvocationDoesNotExist":
                    time.sleep(SSM_POLL_INTERVAL_SECONDS)
                    continue
                raise BenchOpsConnectionError(
                    explain_client_error(exc, instance_id, "ssm:GetCommandInvocation")
                ) from exc

            last_status = invocation["Status"]
            if last_status == "Success":
                return
            if last_status in ("Failed", "Cancelled", "TimedOut"):
                stderr = invocation.get("StandardErrorContent", "").strip()
                raise BenchOpsConnectionError(
                    f"SSM command {last_status} on '{instance_id}'"
                    + (f": {stderr}" if stderr else ".")
                )
            time.sleep(SSM_POLL_INTERVAL_SECONDS)

        raise BenchOpsConnectionError(
            f"Timed out waiting for SSM command '{command_id}' on '{instance_id}' "
            f"(last status: {last_status})."
        )


def _build_authorized_keys_script(remote_user: str, public_key: str) -> str:
    """Build the remote shell script that installs a public key with strict,
    correctly-owned permissions. Runs as root (the SSM agent's default user),
    so paths are resolved via getent rather than relying on tilde expansion.
    AWS-RunShellScript runs it with /bin/sh, which is dash on Debian/Ubuntu,
    so it sticks to POSIX sh (no pipefail).
    """
    user = shlex.quote(remote_user)
    key_line = shlex.quote(public_key.strip())
    return f"""
set -eu
TARGET_USER={user}
HOME_DIR=$(getent passwd "$TARGET_USER" | cut -d: -f6)
if [ -z "$HOME_DIR" ]; then
    echo "User $TARGET_USER not found on this host" >&2
    exit 1
fi
SSH_DIR="$HOME_DIR/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"
TARGET_GROUP=$(id -gn "$TARGET_USER")
install -d -m 700 -o "$TARGET_USER" -g "$TARGET_GROUP" "$SSH_DIR"
touch "$AUTH_KEYS"
chown "$TARGET_USER":"$TARGET_GROUP" "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"
grep -qxF {key_line} "$AUTH_KEYS" || echo {key_line} >> "$AUTH_KEYS"
command -v restorecon >/dev/null 2>&1 && restorecon -R "$SSH_DIR" || true
""".strip()
