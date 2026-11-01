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


def test_ssm_server_add_lists_instances_to_pick_from(config, monkeypatch):
    monkeypatch.setattr("benchops.cli.session_for", lambda server: object())
    monkeypatch.setattr(
        "benchops.cli.list_managed_instances",
        lambda session: [
            ManagedInstance("i-111", "develop.example.com", "Online", "RHEL 9.7"),
            ManagedInstance("i-222", "old", "ConnectionLost", "Ubuntu 22.04"),
        ],
    )
    result = invoke(
        "server", "add", "--alias", "dev", "--connection-type", "ssm", "--user", "frappe",
        "--bench-path", "/home/frappe/bench", "--aws-region", "ap-south-1", input="1\n",
    )
    assert result.exit_code == 0, result.output
    assert "develop.example.com" in result.output and "auth setup-keys dev" in result.output
    server = config.get_server("dev")
    assert server["instance_id"] == "i-111" and server["host"] == "i-111" and server["port"] == 22
    assert server["aws_region"] == "ap-south-1"
    assert "Server host" not in result.output  # no meaningless host prompt for ssm


def test_ssm_server_add_falls_back_to_typing_the_id(config, monkeypatch):
    def denied(server):
        raise SsmError("Your AWS identity is not allowed to call ssm:DescribeInstanceInformation.")

    monkeypatch.setattr("benchops.cli.session_for", denied)
    result = invoke(
        "server", "add", "--alias", "dev", "--connection-type", "ssm", "--user", "frappe",
        "--bench-path", "/b", input="i-0abc\n",
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
