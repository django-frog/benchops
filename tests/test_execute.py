"""`execute` takes a dotted Python method, and says so before connecting
when given a bench command instead."""

import pytest
from typer.testing import CliRunner

from benchops import cli
from benchops.execute import method_problem


@pytest.mark.parametrize("method", ["frappe.get_installed_apps", "frappe.db.get_value", "my_app.api.do_it"])
def test_dotted_paths_are_accepted(method):
    assert method_problem(method) is None


@pytest.mark.parametrize("method", ["list-apps", "frappe", "frappe.", "frappe get_installed_apps", "1x.y"])
def test_anything_else_is_rejected(method):
    assert "not a Python method path" in method_problem(method)


def test_a_known_bench_command_gets_its_equivalent():
    assert method_problem("list-apps").endswith("use: frappe.get_installed_apps")


def test_a_bench_command_fails_before_connecting(monkeypatch):
    monkeypatch.setattr("benchops.execute.ExecuteCommand._get_server_config", lambda self: pytest.fail("connected"))
    result = CliRunner().invoke(
        cli.app, ["execute", "prod", "list-apps", "--site", "s"], env={"COLUMNS": "250"}
    )
    assert result.exit_code == 1
    assert "frappe.get_installed_apps" in result.output
