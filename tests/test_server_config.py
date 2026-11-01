"""`server add`: updating a server keeps what it already had, and SSM
servers are set up by picking an instance rather than typing a host."""

import pytest
from typer.testing import CliRunner

from benchops import cli
from benchops.config import ConfigManager
from benchops.ssm import ManagedInstance, SsmError


@pytest.fixture
def config(tmp_path, monkeypatch):
    manager = ConfigManager(tmp_path / "config.toml")
    monkeypatch.setattr("benchops.cli.ConfigManager", lambda: manager)
    return manager


class FakeAwsSession:
    region_name = "ap-south-1"

    def client(self, service):
        assert service == "sts"
        return self

    def get_caller_identity(self):
        return {"Arn": "arn:aws:iam::123456789012:user/dev"}


def invoke(*args, input=None):
    return CliRunner().invoke(cli.app, list(args), input=input, env={"COLUMNS": "250"})


def test_updating_a_server_keeps_its_hooks_and_key(config):
    config.add_server("dev", "1.2.3.4", 22, "frappe", "/b", private_key_path="/k")
    config.add_hook("dev", "post-remote", "bench restart")
    config.add_hook("dev", "post-restore", "bench --site {site} migrate")

    config.add_server("dev", "1.2.3.4", 22, "frappe", "/b", connection_type="ssm", instance_id="i-abc")

    server = config.get_server("dev")
    assert server["connection_type"] == "ssm" and server["instance_id"] == "i-abc"
    assert server["private_key_path"] == "/k"
    assert server["post_remote_commands"] == ["bench restart"]
    assert server["post_restore_commands"] == ["bench --site {site} migrate"]


def test_explicit_values_still_replace_existing_ones(config):
    config.add_server("dev", "h", 22, "u", "/b", private_key_path="/old", post_remote_commands=["a"])
    config.add_server("dev", "h", 22, "u", "/b", private_key_path="/new", post_remote_commands=[])
    server = config.get_server("dev")
    assert server["private_key_path"] == "/new" and server["post_remote_commands"] == []


def test_ssm_server_add_asks_for_the_profile_then_lists_instances(config, monkeypatch):
    used = []
    monkeypatch.setattr("benchops.cli.available_profiles", lambda: ["Akwad", "dev-test"])
    monkeypatch.setattr("benchops.cli.session_for", lambda server: used.append(server) or FakeAwsSession())
    monkeypatch.setattr(
        "benchops.cli.list_managed_instances",
        lambda session: [
            ManagedInstance("i-111", "develop.example.com", "Online", "RHEL 9.7"),
            ManagedInstance("i-222", "old", "ConnectionLost", "Ubuntu 22.04"),
        ],
    )
    result = invoke(
        "server", "add", "--alias", "dev", "--connection-type", "ssm", "--user", "frappe",
        "--bench-path", "/home/frappe/bench", "--aws-region", "ap-south-1", input="1\n1\n",
    )
    assert result.exit_code == 0, result.output
    assert used == [{"aws_profile": "Akwad", "aws_region": "ap-south-1"}]
    assert "arn:aws:iam::123456789012:user/dev" in result.output  # whose instances these are
    assert "develop.example.com" in result.output
    assert "server check dev" in result.output and "setup-keys" not in result.output  # ssm needs no key
    server = config.get_server("dev")
    assert server["instance_id"] == "i-111" and server["host"] == "i-111" and server["port"] == 22
    assert server["aws_region"] == "ap-south-1" and server["aws_profile"] == "Akwad"
    assert "Server host" not in result.output  # no meaningless host prompt for ssm


def test_ssm_server_add_falls_back_to_typing_the_id(config, monkeypatch):
    def denied(server):
        raise SsmError("Your AWS identity is not allowed to call ssm:DescribeInstanceInformation.")

    monkeypatch.setattr("benchops.cli.session_for", denied)
    result = invoke(
        "server", "add", "--alias", "dev", "--connection-type", "ssm", "--user", "frappe",
        "--bench-path", "/b", "--aws-profile", "dev-test", input="i-0abc\n",
    )
    assert result.exit_code == 0, result.output
    assert "Could not list" in result.output
    assert config.get_server("dev")["instance_id"] == "i-0abc"


def test_ssh_server_add_still_asks_for_host_and_port(config):
    result = invoke(
        "server", "add", "--alias", "web", "--connection-type", "ssh", "--user", "frappe",
        "--bench-path", "/b", input="10.0.0.5\n2222\n",
    )
    assert result.exit_code == 0, result.output
    server = config.get_server("web")
    assert server["host"] == "10.0.0.5" and server["port"] == 2222 and "instance_id" not in server


def test_under_aws_vault_the_profile_is_not_asked(config, monkeypatch):
    monkeypatch.setenv("AWS_VAULT", "Akwad")
    monkeypatch.setattr("benchops.cli.available_profiles", lambda: pytest.fail("profiles listed"))
    monkeypatch.setattr("benchops.cli.session_for", lambda server: FakeAwsSession())
    monkeypatch.setattr(
        "benchops.cli.list_managed_instances", lambda session: [ManagedInstance("i-1", "a", "Online", "RHEL")]
    )
    result = invoke(
        "server", "add", "--alias", "dev", "--connection-type", "ssm", "--user", "u", "--bench-path", "/b", input="1\n",
    )
    assert result.exit_code == 0, result.output
    assert config.get_server("dev")["aws_profile"] == "Akwad"


def test_ssm_ssh_server_add_points_to_setup_keys(config):
    result = invoke(
        "server", "add", "--alias", "dev", "--connection-type", "ssm-ssh", "--user", "u", "--bench-path", "/b",
        "--instance-id", "i-1",
    )
    assert result.exit_code == 0, result.output
    assert "auth setup-keys dev" in result.output
    assert config.get_server("dev")["connection_type"] == "ssm-ssh"


def test_connection_type_prompt_shows_plain_values(config):
    result = invoke("server", "add", "--alias", "web", input="\nu\n/b\nhost\n22\n")
    assert "[ssh]" in result.output and "ConnectionType" not in result.output
