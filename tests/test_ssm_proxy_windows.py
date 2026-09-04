"""Pre-flight checks for the SSM tunnel: missing `aws` / `session-manager-
plugin` binaries must fail fast with an actionable BenchOpsConnectionError,
before any subprocess is spawned — this is the check that matters most on
Windows, where a missing .exe/.cmd shim and a genuinely missing tool look
identical from subprocess's point of view.
"""

from unittest.mock import MagicMock, patch

import pytest

from benchops.runner import BenchOpsConnectionError, RemoteRunner


def test_missing_aws_binary_raises_before_spawning_anything(monkeypatch):
    monkeypatch.setattr(
        "benchops.runner.shutil.which",
        lambda name: None if name == "aws" else "/usr/local/bin/session-manager-plugin",
    )
    with patch("benchops.runner.subprocess.Popen") as mock_popen:
        with pytest.raises(BenchOpsConnectionError, match="aws"):
            RemoteRunner.via_ssm(instance_id="i-0123456789abcdef0", user="ec2-user")
    mock_popen.assert_not_called()


def test_missing_session_manager_plugin_raises_before_spawning_anything(monkeypatch):
    monkeypatch.setattr(
        "benchops.runner.shutil.which",
        lambda name: "/usr/local/bin/aws" if name == "aws" else None,
    )
    with patch("benchops.runner.subprocess.Popen") as mock_popen:
        with pytest.raises(BenchOpsConnectionError, match="session-manager-plugin"):
            RemoteRunner.via_ssm(instance_id="i-0123456789abcdef0", user="ec2-user")
    mock_popen.assert_not_called()


def test_both_missing_are_both_named_in_the_error(monkeypatch):
    monkeypatch.setattr("benchops.runner.shutil.which", lambda name: None)

    with pytest.raises(BenchOpsConnectionError) as exc_info:
        RemoteRunner.via_ssm(instance_id="i-0123456789abcdef0", user="ec2-user")

    assert "aws" in str(exc_info.value)
    assert "session-manager-plugin" in str(exc_info.value)


def test_binaries_present_proceeds_to_spawn_the_tunnel(monkeypatch):
    monkeypatch.setattr("benchops.runner.shutil.which", lambda name: f"/usr/bin/{name}")

    with patch("benchops.runner._find_free_local_port", return_value=54321), patch(
        "benchops.runner.subprocess.Popen"
    ) as mock_popen, patch("benchops.runner._wait_for_tunnel"):
        mock_popen.return_value = MagicMock()
        RemoteRunner.via_ssm(instance_id="i-abc", user="ec2-user", aws_region="me-south-1")

    mock_popen.assert_called_once()
    argv = mock_popen.call_args[0][0]
    assert argv[0] == "aws"
    assert "--target" in argv and "i-abc" in argv
    assert "--region" in argv and "me-south-1" in argv
    assert "AWS-StartPortForwardingSession" in argv
