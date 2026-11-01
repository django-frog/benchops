"""Merging benchops' permission rules into Claude Code's settings.json."""

import json
import os
import platform
import re

import pytest
from typer.testing import CliRunner

from benchops import claude_permissions as perms
from benchops import cli, skill

ALL_RULES = [r for kind in perms.KINDS for r in perms.RULES[kind]]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home / "settings.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_creates_settings_with_every_rule(settings):
    change = perms.install(settings)

    data = read(settings)
    assert {kind: data["permissions"][kind] for kind in perms.KINDS} == perms.RULES
    assert change.present == len(ALL_RULES) and change.backup is None
    assert perms.missing(settings) == []


def test_keeps_everything_already_there_and_backs_it_up(settings):
    settings.parent.mkdir(parents=True)
    original = {
        "model": "opus",
        "env": {"NOTE": "café ⚠"},
        "permissions": {"allow": ["Bash(npm test *)"], "deny": ["Read(./.env)"], "defaultMode": "default"},
    }
    settings.write_text(json.dumps(original), encoding="utf-8")
    if platform.system() != "Windows":
        os.chmod(settings, 0o600)

    change = perms.install(settings)

    data = read(settings)
    assert data["model"] == "opus" and data["env"] == {"NOTE": "café ⚠"}
    assert data["permissions"]["defaultMode"] == "default"
    assert data["permissions"]["allow"] == ["Bash(npm test *)", *perms.RULES["allow"]]
    assert data["permissions"]["deny"] == ["Read(./.env)", *perms.RULES["deny"]]
    assert read(change.backup) == original
    if platform.system() != "Windows":
        assert settings.stat().st_mode & 0o777 == 0o600


def test_second_install_changes_nothing(settings):
    perms.install(settings)
    before = settings.read_bytes()

    change = perms.install(settings)

    assert not change.changed and change.backup is None
    assert settings.read_bytes() == before


def test_uninstall_removes_only_the_rules_benchops_added(settings):
    settings.parent.mkdir(parents=True)
    mine = perms.RULES["deny"][0]
    settings.write_text(json.dumps({"permissions": {"deny": [mine], "allow": ["Bash(ls *)"]}}), encoding="utf-8")
    perms.install(settings)

    change = perms.uninstall(settings)

    assert read(settings) == {"permissions": {"deny": [mine], "allow": ["Bash(ls *)"]}}
    assert mine not in change.removed["deny"]


def test_uninstall_leaves_no_empty_permissions_behind(settings):
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    perms.install(settings)

    perms.uninstall(settings)

    assert read(settings) == {"model": "opus"}
    assert not perms.STATE_PATH.exists() or str(settings) not in json.loads(perms.STATE_PATH.read_text())


def test_a_rule_dropped_in_a_new_version_is_removed_on_upgrade(settings, monkeypatch):
    current = perms.RULES
    old = {kind: [*rules] for kind, rules in current.items()}
    old["ask"].append("Bash(benchops old-command *)")
    monkeypatch.setattr(perms, "RULES", old)
    perms.install(settings)
    monkeypatch.setattr(perms, "RULES", current)  # not undo(): that would also lift the conftest isolation

    change = perms.install(settings)

    assert change.removed["ask"] == ["Bash(benchops old-command *)"]
    assert "Bash(benchops old-command *)" not in read(settings)["permissions"]["ask"]


def test_a_rule_the_developer_deleted_is_added_back(settings):
    perms.install(settings)
    data = read(settings)
    data["permissions"]["ask"].remove("Bash(benchops execute *)")
    settings.write_text(json.dumps(data), encoding="utf-8")
    assert perms.missing(settings) == ["Bash(benchops execute *)"]

    change = perms.install(settings)

    assert change.added["ask"] == ["Bash(benchops execute *)"]


@pytest.mark.parametrize(
    "content, problem",
    [
        ('{"model": "opus",}', "isn't valid JSON"),
        ("[1, 2]", "doesn't hold a JSON object"),
        ('{"permissions": {"allow": "Bash(ls)"}}', "isn't in the expected shape"),
    ],
)
def test_unreadable_settings_are_left_untouched(settings, content, problem):
    settings.parent.mkdir(parents=True)
    settings.write_text(content, encoding="utf-8")

    with pytest.raises(perms.SettingsError, match=problem):
        perms.install(settings)

    assert settings.read_text(encoding="utf-8") == content
    assert perms.missing(settings) is (None if "JSON" in problem else perms.missing(settings))


@pytest.mark.skipif(platform.system() == "Windows", reason="symlinks need extra privileges on Windows")
def test_symlinked_settings_stay_a_symlink(settings, tmp_path):
    dotfiles = tmp_path / "dotfiles" / "claude-settings.json"
    dotfiles.parent.mkdir()
    dotfiles.write_text('{"model": "opus"}', encoding="utf-8")
    settings.parent.mkdir(parents=True)
    os.symlink(dotfiles, settings)

    perms.install(settings)

    assert settings.is_symlink()
    assert read(dotfiles)["model"] == "opus" and read(dotfiles)["permissions"]["deny"] == perms.RULES["deny"]


# --------------------------------------------------------------------------- the rules themselves


def classify(command):
    """Claude Code's documented order: deny, then ask, then allow; '*'
    matches any text. A command matching nothing prompts."""
    for kind in perms.KINDS:
        for rule in perms.RULES[kind]:
            pattern = re.escape(rule[len("Bash("):-1]).replace(r"\*", ".*")
            if re.fullmatch(pattern, command):
                return kind
    return "prompt"


@pytest.mark.parametrize(
    "command, expected",
    [
        ("benchops status basma dev-bench-02 --json < /dev/null", "allow"),
        ("benchops deploy basma dev-bench-02 --site s --label T-1 --plan-only --json < /dev/null", "allow"),
        ("benchops sync basma dev-bench-02 --site s --plan-only --json < /dev/null", "allow"),
        ("benchops logs dev-bench-02 --type web.error.log --lines 200 < /dev/null", "allow"),
        ("benchops server list", "allow"),
        ("benchops server check dev-bench-02", "allow"),
        ("benchops --version", "allow"),
        ("benchops deploy basma dev-bench-02 --site s --label T-1 --yes --skip-build --json < /dev/null", "ask"),
        ("benchops deploy basma dev-bench-02 -y", "ask"),
        ("benchops sync basma dev-bench-02 --site s --yes --json < /dev/null", "ask"),
        ("benchops sync basma dev-bench-02 -y --json", "ask"),
        ("benchops deploy basma dev-bench-02 --yes --overwrite", "ask"),
        ("benchops execute dev-bench-02 frappe.clear_cache --site s", "ask"),
        ("benchops server add --alias x", "ask"),
        ("benchops server add-hook dev-bench-02 post-remote 'bench migrate'", "ask"),
        ("benchops skill uninstall", "ask"),
        ("benchops deploy basma dev-bench-02 --yes --force", "deny"),
        ("benchops sync basma dev-bench-02 --break-lock", "deny"),
        ("benchops restore prod-backups --site s --server x --yes", "deny"),
        ("benchops logs dev-bench-02", "prompt"),  # follows forever: never pre-approved
    ],
)
def test_rules_classify_the_commands_the_skill_runs(command, expected):
    assert classify(command) == expected


def test_skill_commands_start_with_benchops_so_allow_rules_match():
    """Allow rules don't match past a leading VAR=value, so the skill must
    not tell Claude to prefix benchops commands with one."""
    prefixed = re.findall(r"[A-Z_]+=\S+ benchops ", skill.bundled_skill())
    assert prefixed == ["VAR=1 benchops "]  # only the "don't do this" example


# --------------------------------------------------------------------------- CLI


def test_cli_install_adds_rules_and_status_reports_them(settings, monkeypatch):
    monkeypatch.setattr(cli, "installed_version", lambda: "0.23.0")

    result = CliRunner().invoke(cli.app, ["skill", "install"])

    assert result.exit_code == 0, result.output
    assert f"{len(ALL_RULES)} added" in result.output
    assert perms.missing(settings) == []
    status = CliRunner().invoke(cli.app, ["skill", "status"])
    assert f"all {len(ALL_RULES)}" in " ".join(status.output.split())


def test_cli_install_without_permissions_leaves_settings_alone(settings):
    result = CliRunner().invoke(cli.app, ["skill", "install", "--no-permissions"])

    assert result.exit_code == 0, result.output
    assert not settings.exists()
    assert skill.skill_path().is_file()


def test_cli_install_with_broken_settings_installs_the_skill_and_prints_the_rules(settings):
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["skill", "install"])

    assert result.exit_code == 1
    assert skill.skill_path().is_file()
    assert settings.read_text(encoding="utf-8") == "{not json"
    assert "Permission rules not added" in result.output and "Bash(benchops * --force*)" in result.output


def test_cli_uninstall_removes_skill_and_rules(settings):
    CliRunner().invoke(cli.app, ["skill", "install"])

    result = CliRunner().invoke(cli.app, ["skill", "uninstall"])

    assert result.exit_code == 0, result.output
    assert f"Removed {len(ALL_RULES)} permission rule(s)" in " ".join(result.output.split())
    assert not skill.skill_path().exists()
    assert "permissions" not in read(settings)
