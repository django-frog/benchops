"""AWS Systems Manager Session Manager: port-forwarding tunnels and the
diagnostics around them.

A tunnel is opened the same way `aws ssm start-session` does it — the
StartSession API call, then `session-manager-plugin` carrying the stream —
but without the AWS CLI in the middle. Owning both halves means the session
can always be terminated explicitly on close (killing the CLI wrapper leaves
the plugin orphaned and the session open until the idle timeout), credentials
come from the same chain as every other AWS call in benchops (aws-vault
included), and AWS errors arrive as structured codes that can be explained.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass

import boto3
import botocore.exceptions

from benchops.awscreds import AwsCredentialsError, aws_session

PLUGIN = "session-manager-plugin"
PORT_FORWARD_DOCUMENT = "AWS-StartPortForwardingSession"
READY_MARKER = "Waiting for connections"
TUNNEL_READY_TIMEOUT_SECONDS = 20
TUNNEL_POLL_INTERVAL_SECONDS = 0.2
TUNNEL_TERMINATE_TIMEOUT_SECONDS = 5

PLUGIN_INSTALL_URL = (
    "https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"
)


class SsmError(Exception):
    """Raised when an SSM operation fails; the message says how to fix it."""


def require_plugin() -> str:
    """Return the path to session-manager-plugin, or raise with install help."""
    path = shutil.which(PLUGIN)
    if path is None:
        raise SsmError(f"'{PLUGIN}' is not installed or not on PATH. Install it: {PLUGIN_INSTALL_URL}")
    return path


def session_for(server_config: dict) -> boto3.Session:
    """A boto3 session for a server's aws_profile/aws_region."""
    try:
        return aws_session(server_config.get("aws_profile"), server_config.get("aws_region"))
    except AwsCredentialsError as exc:
        raise SsmError(str(exc)) from exc


def explain_client_error(exc: Exception, instance_id: str, action: str, document: str | None = None) -> str:
    """Turn an AWS error into a message that names the actual fix.
    `document` is the SSM document a StartSession call used, if any."""
    if isinstance(exc, botocore.exceptions.NoCredentialsError):
        return (
            "No AWS credentials found. Set aws_profile on the server, run under "
            "'aws-vault exec <profile> --', or configure the AWS CLI."
        )
    if isinstance(exc, botocore.exceptions.NoRegionError):
        return "No AWS region configured. Set aws_region on the server ('benchops server add --aws-region ...')."
    if not isinstance(exc, botocore.exceptions.ClientError):
        return f"Could not reach AWS: {exc}"

    error = exc.response.get("Error", {})
    code = error.get("Code", "")
    message = error.get("Message", str(exc))
    if code in ("AccessDeniedException", "AccessDenied"):
        if action == "ssm:DescribeInstanceInformation":
            resources = '"*" (it cannot be limited to instances)'
        else:
            resources = f"arn:aws:ec2:*:*:instance/{instance_id}"
        if action == "ssm:StartSession":
            if document:
                resources += f" and arn:aws:ssm:*::document/{document}"
            resources += " (plus ssm:TerminateSession)"
        return f"Your AWS identity is not allowed to call {action}. {message}\nAsk an AWS admin to allow {action} on {resources}."
    if code == "TargetNotConnected":
        return (
            f"Instance '{instance_id}' is not connected to SSM: it is stopped, its SSM agent is offline, or its "
            "IAM role lacks AmazonSSMManagedInstanceCore. Check Systems Manager → Fleet Manager."
        )
    if code in ("InvalidInstanceId", "InvalidTarget"):
        return (
            f"Instance '{instance_id}' is not managed by SSM in this account/region "
            "(check instance_id and aws_region)."
        )
    if code in ("ExpiredTokenException", "ExpiredToken", "UnrecognizedClientException", "InvalidClientTokenId"):
        return f"Your AWS credentials are expired or invalid ({code}). Refresh them (e.g. re-run aws-vault) and retry."
    return f"AWS rejected {action} ({code}): {message}"


@dataclass
class ManagedInstance:
    instance_id: str
    name: str
    ping_status: str
    platform: str


def list_managed_instances(session: boto3.Session, instance_ids: list[str] | None = None) -> list[ManagedInstance]:
    """Instances registered with SSM in the session's region, online first."""
    ssm = session.client("ssm")
    kwargs = {"Filters": [{"Key": "InstanceIds", "Values": instance_ids}]} if instance_ids else {}
    instances = []
    try:
        for page in ssm.get_paginator("describe_instance_information").paginate(**kwargs):
            for info in page["InstanceInformationList"]:
                instances.append(
                    ManagedInstance(
                        instance_id=info["InstanceId"],
                        name=info.get("ComputerName") or "",
                        ping_status=info.get("PingStatus", "Unknown"),
                        platform=" ".join(filter(None, (info.get("PlatformName"), info.get("PlatformVersion")))),
                    )
                )
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as exc:
        raise SsmError(
            explain_client_error(exc, (instance_ids or ["*"])[0], "ssm:DescribeInstanceInformation")
        ) from exc
    return sorted(instances, key=lambda i: (i.ping_status != "Online", i.name, i.instance_id))


# Popen options that keep the plugin out of the terminal's process group:
# Ctrl+C is meant for the remote command (logs -f), not for the session
# carrying it, which benchops ends itself.
PLUGIN_ISOLATION = (
    {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    if platform.system() == "Windows"
    else {"start_new_session": True}
)


def start_session(session: boto3.Session, request: dict) -> tuple[object, dict, list[str]]:
    """Call StartSession and build the session-manager-plugin argv that
    carries it — what `aws ssm start-session` does, minus the AWS CLI.
    Returns (ssm client, response, argv)."""
    plugin = require_plugin()
    try:
        client = session.client("ssm")
        response = client.start_session(**request)
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as exc:
        raise SsmError(
            explain_client_error(exc, request["Target"], "ssm:StartSession", request.get("DocumentName"))
        ) from exc
    argv = [
        plugin,
        json.dumps(response, default=str),
        client.meta.region_name,
        "StartSession",
        "",  # profile: not needed, the session token in the response is already authorized
        json.dumps(request),
        client.meta.endpoint_url,
    ]
    return client, response, argv


def end_session(client, session_id: str | None, process: subprocess.Popen | None) -> None:
    """Stop the plugin and terminate the SSM session; both best effort."""
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=TUNNEL_TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=TUNNEL_TERMINATE_TIMEOUT_SECONDS)
    if session_id and client is not None:
        try:
            client.terminate_session(SessionId=session_id)
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError):
            pass  # it ends by itself at the idle timeout


def find_free_local_port() -> int:
    """Ask the OS for an ephemeral, currently-unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class SsmTunnel:
    """A Session Manager port-forwarding session from 127.0.0.1:<local_port>
    to `remote_port` on the instance itself. Use as a context manager, or
    call close() — which terminates the plugin *and* the SSM session."""

    def __init__(self, session: boto3.Session, instance_id: str, remote_port: int = 22) -> None:
        self.session = session
        self.instance_id = instance_id
        self.remote_port = remote_port
        self.local_port: int | None = None
        self.session_id: str | None = None
        self._client = None
        self._process: subprocess.Popen | None = None
        self._log_path: str | None = None

    def open(self) -> "SsmTunnel":
        self.local_port = find_free_local_port()
        request = {
            "Target": self.instance_id,
            "DocumentName": PORT_FORWARD_DOCUMENT,
            "Parameters": {
                "portNumber": [str(self.remote_port)],
                "localPortNumber": [str(self.local_port)],
            },
        }
        self._client, response, argv = start_session(self.session, request)
        self.session_id = response["SessionId"]

        # The plugin's chatter goes to a file rather than a pipe nobody reads,
        # so a long session can never block on a full pipe buffer; it writes
        # in append mode and is read through a separate handle, so the two
        # never share a file offset.
        fd, self._log_path = tempfile.mkstemp(prefix="benchops-ssm-", suffix=".log")
        os.close(fd)
        try:
            with open(self._log_path, "ab") as log:
                self._process = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **PLUGIN_ISOLATION
                )
        except OSError as exc:
            self.close()
            raise SsmError(f"Could not launch {PLUGIN}: {exc}") from exc

        try:
            self._wait_until_ready()
        except SsmError:
            self.close()
            raise
        return self

    def output(self) -> str:
        """Everything the plugin has printed so far."""
        if self._log_path is None:
            return ""
        try:
            with open(self._log_path, encoding="utf-8", errors="replace") as log:
                return log.read().strip()
        except OSError:
            return ""

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _wait_until_ready(self) -> None:
        # Readiness comes from the plugin's own "Waiting for connections"
        # line rather than a probe connection: on agents without multiplexing
        # support, the first TCP connection to close ends the whole session.
        deadline = time.monotonic() + TUNNEL_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if READY_MARKER in self.output():
                return
            if not self.is_alive():
                raise SsmError(
                    f"{PLUGIN} exited early (code {self._process.returncode}): {self.output() or 'no output'}"
                )
            time.sleep(TUNNEL_POLL_INTERVAL_SECONDS)
        raise SsmError(
            f"Timed out after {TUNNEL_READY_TIMEOUT_SECONDS}s waiting for the SSM tunnel to "
            f"'{self.instance_id}': {self.output() or 'no output from ' + PLUGIN}"
        )

    def close(self) -> None:
        """Stop the plugin and terminate the SSM session. Safe to call twice."""
        end_session(self._client, self.session_id, self._process)
        self.session_id = None
        if self._log_path is not None:
            try:
                os.remove(self._log_path)
            except OSError:
                pass
            self._log_path = None

    def __enter__(self) -> "SsmTunnel":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()
