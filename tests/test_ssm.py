"""SSM transport: credentials (aws-vault included), the port-forwarding
tunnel's lifecycle, and the error messages that tell people what to fix.

The tunnel tests run a fake `session-manager-plugin` as a real subprocess,
so readiness, early exit and teardown are exercised for real; only the AWS
API is faked.
"""

import json
import platform
import sys
import textwrap

import botocore.exceptions
import pytest

from benchops import awscreds, ssm
from benchops.awscreds import AwsCredentialsError, aws_session
from benchops.runner import BenchOpsConnectionError, RemoteRunner
from benchops.ssm import SsmError, SsmTunnel, explain_client_error

posix_only = pytest.mark.skipif(platform.system() == "Windows", reason="fake plugin is a shebang script")


# --- credentials ----------------------------------------------------------


class FakeBotoSession:
    made = []
    credentials = object()
    profile_region = None

    def __init__(self, profile_name=None, region_name=None, **keys):
        self.profile_name = profile_name
        self.keys = keys
        self.region_name = region_name or (FakeBotoSession.profile_region if profile_name else None)
        FakeBotoSession.made.append(self)

    def get_credentials(self):
        return FakeBotoSession.credentials


@pytest.fixture
def fake_boto(monkeypatch):
    FakeBotoSession.made = []
    FakeBotoSession.credentials = object()
    FakeBotoSession.profile_region = None
    monkeypatch.setattr("benchops.awscreds.boto3.Session", FakeBotoSession)
    monkeypatch.setattr("benchops.awscreds.shutil.which", lambda name: None)
    return FakeBotoSession


def test_profile_with_native_credentials_is_used_as_is(fake_boto):
    session = aws_session("dev", "ap-south-1")
    assert session.profile_name == "dev" and session.region_name == "ap-south-1"


def test_under_aws_vault_the_environment_wins_over_the_profile(fake_boto, monkeypatch):
    monkeypatch.setenv("AWS_VAULT", "dev")
    fake_boto.profile_region = "eu-west-1"
    session = aws_session("dev", None)
    assert session.profile_name is None  # boto3 then reads aws-vault's env credentials
    assert session.region_name == "eu-west-1"  # but the profile's region still applies


def test_aws_vault_profile_mismatch_is_refused(fake_boto, monkeypatch):
    monkeypatch.setenv("AWS_VAULT", "prod")
    with pytest.raises(AwsCredentialsError, match="aws-vault exec dev"):
        aws_session("dev", None)


def test_profile_without_credentials_falls_back_to_aws_vault_export(fake_boto, monkeypatch):
    fake_boto.credentials = None
    monkeypatch.setattr("benchops.awscreds.shutil.which", lambda name: "/usr/bin/aws-vault")
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps({"Version": 1, "AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST"})

    def run(argv, **kwargs):
        calls.append(argv)
        return Result()

    monkeypatch.setattr("benchops.awscreds.subprocess.run", run)
    session = aws_session("dev", "ap-south-1")
    assert calls == [["aws-vault", "export", "--format=json", "dev"]]
    assert session.keys == {"aws_access_key_id": "AK", "aws_secret_access_key": "SK", "aws_session_token": "ST"}
    assert session.region_name == "ap-south-1"


def test_failed_aws_vault_export_is_explained(fake_boto, monkeypatch):
    fake_boto.credentials = None
    monkeypatch.setattr("benchops.awscreds.shutil.which", lambda name: "/usr/bin/aws-vault")

    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr("benchops.awscreds.subprocess.run", lambda argv, **kwargs: Result())
    with pytest.raises(AwsCredentialsError, match="aws-vault could not provide credentials for profile 'dev'"):
        aws_session("dev", None)


def test_unknown_profile_never_reaches_aws_vault(monkeypatch):
    def missing(**kwargs):
        raise botocore.exceptions.ProfileNotFound(profile="nope")

    monkeypatch.setattr("benchops.awscreds.boto3.Session", missing)
    monkeypatch.setattr("benchops.awscreds.subprocess.run", lambda *a, **k: pytest.fail("aws-vault was called"))
    with pytest.raises(AwsCredentialsError, match="'nope' is not defined"):
        aws_session("nope", None)


def test_profile_without_credentials_and_without_aws_vault(fake_boto):
    fake_boto.credentials = None
    with pytest.raises(AwsCredentialsError, match="no usable credentials"):
        aws_session("dev", None)


# --- error messages -------------------------------------------------------


def client_error(code, message="nope"):
    return botocore.exceptions.ClientError({"Error": {"Code": code, "Message": message}}, "StartSession")


def test_access_denied_names_the_permission_and_the_document():
    text = explain_client_error(client_error("AccessDeniedException"), "i-abc", "ssm:StartSession")
    assert "ssm:StartSession" in text and "instance/i-abc" in text and "AWS-StartPortForwardingSession" in text


@pytest.mark.parametrize(
    "error, expected",
    [
        (client_error("TargetNotConnected"), "not connected to SSM"),
        (client_error("InvalidInstanceId"), "not managed by SSM"),
        (client_error("ExpiredTokenException"), "expired"),
        (botocore.exceptions.NoCredentialsError(), "No AWS credentials"),
        (botocore.exceptions.NoRegionError(), "aws_region"),
    ],
)
def test_common_failures_are_explained(error, expected):
    assert expected in explain_client_error(error, "i-abc", "ssm:StartSession")


def test_missing_plugin_says_how_to_install(monkeypatch):
    monkeypatch.setattr("benchops.ssm.shutil.which", lambda name: None)
    with pytest.raises(SsmError, match="session-manager-plugin.*install"):
        ssm.require_plugin()


# --- tunnel ---------------------------------------------------------------


class FakeSsmClient:
    def __init__(self, error=None):
        self.error = error
        self.started = []
        self.terminated = []
        self.meta = type("Meta", (), {"region_name": "ap-south-1", "endpoint_url": "https://ssm.ap-south-1.amazonaws.com"})

    def start_session(self, **request):
        if self.error:
            raise self.error
        self.started.append(request)
        return {"SessionId": "mhamdan-0abc", "TokenValue": "token", "StreamUrl": "wss://example"}

    def terminate_session(self, SessionId):
        self.terminated.append(SessionId)


class FakeSsmSession:
    region_name = "ap-south-1"

    def __init__(self, client):
        self._client = client

    def client(self, service):
        assert service == "ssm"
        return self._client


def fake_plugin(tmp_path, monkeypatch, body):
    """Install an executable stand-in for session-manager-plugin that
    records its argv and then runs `body` (Python)."""
    script = tmp_path / "session-manager-plugin"
    argv_file = tmp_path / "argv.json"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"json.dump(sys.argv[1:], open({str(argv_file)!r}, 'w'))\n"
        + textwrap.dedent(body)
    )
    script.chmod(0o755)
    monkeypatch.setattr("benchops.ssm.shutil.which", lambda name: str(script))
    return argv_file


@posix_only
def test_tunnel_starts_the_plugin_with_the_session_and_terminates_it(tmp_path, monkeypatch):
    argv_file = fake_plugin(
        tmp_path, monkeypatch, "print('Port opened'); print('Waiting for connections...', flush=True); time.sleep(60)\n"
    )
    client = FakeSsmClient()
    tunnel = SsmTunnel(FakeSsmSession(client), "i-abc", 22).open()
    try:
        assert tunnel.is_alive()
        request = client.started[0]
        assert request["Target"] == "i-abc" and request["DocumentName"] == "AWS-StartPortForwardingSession"
        assert request["Parameters"] == {"portNumber": ["22"], "localPortNumber": [str(tunnel.local_port)]}

        argv = json.loads(argv_file.read_text())
        assert json.loads(argv[0])["SessionId"] == "mhamdan-0abc"
        assert argv[1:4] == ["ap-south-1", "StartSession", ""]
        assert json.loads(argv[4]) == request
        assert argv[5] == "https://ssm.ap-south-1.amazonaws.com"
    finally:
        tunnel.close()

    assert not tunnel.is_alive()
    assert client.terminated == ["mhamdan-0abc"]  # the SSM session itself is ended, not left to time out
    tunnel.close()  # idempotent
    assert client.terminated == ["mhamdan-0abc"]


@posix_only
def test_plugin_exiting_early_reports_its_output_and_ends_the_session(tmp_path, monkeypatch):
    fake_plugin(tmp_path, monkeypatch, "print('websocket: bad handshake', flush=True); sys.exit(1)\n")
    client = FakeSsmClient()
    with pytest.raises(SsmError, match="exited early.*bad handshake"):
        SsmTunnel(FakeSsmSession(client), "i-abc").open()
    assert client.terminated == ["mhamdan-0abc"]


@posix_only
def test_plugin_that_never_gets_ready_times_out(tmp_path, monkeypatch):
    fake_plugin(tmp_path, monkeypatch, "print('Starting session', flush=True); time.sleep(60)\n")
    monkeypatch.setattr("benchops.ssm.TUNNEL_READY_TIMEOUT_SECONDS", 0.5)
    client = FakeSsmClient()
    with pytest.raises(SsmError, match="Timed out.*Starting session"):
        SsmTunnel(FakeSsmSession(client), "i-abc").open()
    assert client.terminated == ["mhamdan-0abc"]


def test_start_session_denied_never_starts_the_plugin(monkeypatch):
    monkeypatch.setattr("benchops.ssm.shutil.which", lambda name: "/usr/bin/session-manager-plugin")
    monkeypatch.setattr("benchops.ssm.subprocess.Popen", lambda *a, **k: pytest.fail("plugin started"))
    client = FakeSsmClient(error=client_error("AccessDeniedException"))
    with pytest.raises(SsmError, match="not allowed to call ssm:StartSession"):
        SsmTunnel(FakeSsmSession(client), "i-abc").open()


def test_via_ssm_surfaces_tunnel_failures_as_connection_errors(monkeypatch):
    monkeypatch.setattr("benchops.ssm.shutil.which", lambda name: None)
    monkeypatch.setattr("benchops.runner.session_for", lambda config: FakeSsmSession(FakeSsmClient()))
    with pytest.raises(BenchOpsConnectionError, match="session-manager-plugin"):
        RemoteRunner.via_ssm({"instance_id": "i-abc", "user": "frappe", "port": 22})


@posix_only
def test_via_ssm_points_ssh_at_the_tunnel_and_close_tears_it_down(tmp_path, monkeypatch):
    fake_plugin(tmp_path, monkeypatch, "print('Waiting for connections...', flush=True); time.sleep(60)\n")
    client = FakeSsmClient()
    monkeypatch.setattr("benchops.runner.session_for", lambda config: FakeSsmSession(client))

    runner = RemoteRunner.via_ssm({"instance_id": "i-abc", "user": "frappe", "port": 2222}, key_path="/k")
    assert runner.connection.host == "127.0.0.1"
    assert runner.connection.port == runner._tunnel.local_port
    assert client.started[0]["Parameters"]["portNumber"] == ["2222"]
    runner.close()
    assert not runner._tunnel.is_alive() and client.terminated == ["mhamdan-0abc"]


def test_key_install_script_is_posix_sh_and_uses_the_users_own_group():
    from benchops.auth import _build_authorized_keys_script

    script = _build_authorized_keys_script("frappe", "ssh-ed25519 AAAA benchops_ed25519")
    assert "pipefail" not in script  # /bin/sh is dash on Debian/Ubuntu
    assert 'id -gn "$TARGET_USER"' in script and '-g "$TARGET_USER"' not in script


def test_aws_vault_module_never_runs_when_unneeded(fake_boto, monkeypatch):
    monkeypatch.setattr(awscreds.subprocess, "run", lambda *a, **k: pytest.fail("aws-vault was called"))
    aws_session(None, None)
    aws_session("dev", None)
