"""AWS sessions that work the way people actually hold AWS credentials:
the default credential chain, a named profile, or aws-vault.

aws-vault keeps credentials out of ~/.aws entirely, which breaks the usual
"just pass the profile name" approach in two ways:

* Under `aws-vault exec <profile> -- benchops ...`, credentials arrive as
  environment variables. Passing `profile_name` to boto3 makes it skip the
  environment and read the profile from ~/.aws/config instead, which has no
  credentials — so a configured `aws_profile` must give way to the
  environment here.
* Outside aws-vault, the profile alone cannot produce credentials. When
  aws-vault is installed, `aws-vault export` fetches them (prompting for MFA
  or the keychain passphrase on the terminal, as `aws-vault exec` would).
"""

import json
import os
import shutil
import subprocess

import boto3
import botocore.exceptions

AWS_VAULT_ENV = "AWS_VAULT"
AWS_VAULT_EXPORT_TIMEOUT_SECONDS = 300


class AwsCredentialsError(Exception):
    """Raised when no usable AWS credentials can be found for a profile."""


def aws_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    """Return a boto3 session with resolvable credentials for `profile`
    (or the default chain when None)."""
    vault_profile = os.environ.get(AWS_VAULT_ENV)
    if vault_profile:
        if profile and profile != vault_profile:
            raise AwsCredentialsError(
                f"Running under 'aws-vault exec {vault_profile}', but this server uses AWS profile "
                f"'{profile}'. Run under 'aws-vault exec {profile}' instead."
            )
        return boto3.Session(region_name=region or _profile_region(profile))

    if not profile:
        return boto3.Session(region_name=region)

    native_error: Exception | None = None
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        if session.get_credentials() is not None:
            return session
    except botocore.exceptions.ProfileNotFound as exc:
        raise AwsCredentialsError(f"AWS profile '{profile}' is not defined in ~/.aws/config.") from exc
    except (botocore.exceptions.BotoCoreError, ValueError) as exc:
        native_error = exc

    if shutil.which("aws-vault"):
        return _aws_vault_session(profile, region)

    detail = f": {native_error}" if native_error else "."
    raise AwsCredentialsError(f"AWS profile '{profile}' has no usable credentials{detail}")


def _profile_region(profile: str | None) -> str | None:
    """The region set on a profile in ~/.aws/config, without touching its
    credentials (which may not be resolvable outside aws-vault)."""
    if not profile:
        return None
    try:
        return boto3.Session(profile_name=profile).region_name
    except botocore.exceptions.BotoCoreError:
        return None


def _aws_vault_session(profile: str, region: str | None) -> boto3.Session:
    """Fetch temporary credentials for `profile` from aws-vault. stdin and
    stderr stay attached to the terminal so aws-vault can prompt."""
    try:
        result = subprocess.run(
            ["aws-vault", "export", "--format=json", profile],
            stdout=subprocess.PIPE,
            text=True,
            timeout=AWS_VAULT_EXPORT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AwsCredentialsError(f"Could not run aws-vault for profile '{profile}': {exc}") from exc
    if result.returncode != 0:
        raise AwsCredentialsError(
            f"aws-vault could not provide credentials for profile '{profile}' "
            f"(exit code {result.returncode}). Check 'aws-vault list'."
        )

    try:
        creds = json.loads(result.stdout)
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds.get("SessionToken"),
            region_name=region or _profile_region(profile),
        )
    except (ValueError, KeyError) as exc:
        raise AwsCredentialsError(f"Unexpected output from 'aws-vault export' for '{profile}': {exc}") from exc
