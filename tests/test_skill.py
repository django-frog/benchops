"""`benchops skill`: installing, updating and removing the Claude Code skill."""

import os
import platform

import pytest
from typer.testing import CliRunner

from benchops import cli, skill


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def installed(claude_home):
    return claude_home / "skills" / "benchops" / "SKILL.md"


def test_skill_goes_to_claude_config_dir_or_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    assert skill.skill_path() == tmp_path / "custom" / "skills" / "benchops" / "SKILL.md"

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(skill.Path, "home", classmethod(lambda cls: tmp_path / "user"))
    assert skill.skill_path() == tmp_path / "user" / ".claude" / "skills" / "benchops" / "SKILL.md"


def test_bundled_skill_is_the_repo_copy():
    text = skill.bundled_skill()
    assert text.startswith("---\nname: benchops\n")
    assert "--plan-only" in text and "--lines" in text


def test_install_writes_utf8_with_lf_and_a_version_marker(claude_home):
    path, replaced = skill.install("0.22.0")

    assert path == installed(claude_home) and replaced is None
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")  # non-ASCII (e.g. the warning sign) survives on every OS
    assert text.startswith(skill.bundled_skill().rstrip("\n"))
    assert "<!-- installed by benchops 0.22.0 sha256=" in text
    found = skill.inspect_installed()
    assert (found.version, found.modified, found.symlink) == ("0.22.0", False, False)
    assert not [p for p in path.parent.iterdir() if p.name != "SKILL.md"]  # no temp files left


def test_reinstall_updates_an_older_copy(claude_home):
    skill.install("0.21.0")

    _, replaced = skill.install("0.22.0")

    assert replaced == "0.21.0"
    assert skill.inspect_installed().version == "0.22.0"


def test_a_copy_saved_with_windows_line_endings_is_still_recognised(claude_home):
    path, _ = skill.install("0.22.0")
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    found = skill.inspect_installed()
    assert (found.version, found.modified) == ("0.22.0", False)
    skill.install("0.23.0")  # no --force needed


def test_edited_copy_is_kept_unless_forced(claude_home):
    path, _ = skill.install("0.21.0")
    path.write_text(path.read_text(encoding="utf-8").replace("Mental model", "My notes"), encoding="utf-8")

    assert skill.inspect_installed().modified is True
    with pytest.raises(skill.SkillError, match="was edited after benchops 0.21.0 installed it"):
        skill.install("0.22.0")
    assert "My notes" in path.read_text(encoding="utf-8")
    with pytest.raises(skill.SkillError, match="--force"):
        skill.uninstall()

    skill.install("0.22.0", force=True)
    assert "My notes" not in path.read_text(encoding="utf-8")


def test_hand_made_copy_is_kept_unless_forced(claude_home):
    path = installed(claude_home)
    path.parent.mkdir(parents=True)
    path.write_text("my own skill\n", encoding="utf-8")

    with pytest.raises(skill.SkillError, match="wasn't installed by benchops"):
        skill.install("0.22.0")
    assert path.read_text(encoding="utf-8") == "my own skill\n"

    _, replaced = skill.install("0.22.0", force=True)
    assert replaced == "an unmanaged copy"


@pytest.mark.skipif(platform.system() == "Windows", reason="symlinks need extra privileges on Windows")
@pytest.mark.parametrize("linked", ["file", "directory"])
def test_symlinked_skill_is_replaced_without_touching_its_source(claude_home, tmp_path, linked):
    source_dir = tmp_path / "checkout" / "skills" / "benchops"
    source_dir.mkdir(parents=True)
    (source_dir / "SKILL.md").write_text("source of truth\n", encoding="utf-8")
    target = installed(claude_home)
    target.parent.parent.mkdir(parents=True)
    if linked == "file":
        target.parent.mkdir()
        os.symlink(source_dir / "SKILL.md", target)
    else:
        os.symlink(source_dir, target.parent, target_is_directory=True)

    with pytest.raises(skill.SkillError, match="symbolic link"):
        skill.install("0.22.0")

    skill.install("0.22.0", force=True)

    assert (source_dir / "SKILL.md").read_text(encoding="utf-8") == "source of truth\n"
    assert not target.is_symlink() and not target.parent.is_symlink()
    assert skill.inspect_installed().version == "0.22.0"


def test_uninstall_removes_the_file_and_its_empty_folder(claude_home):
    path, _ = skill.install("0.22.0")

    assert skill.uninstall() == path
    assert not path.exists() and not path.parent.exists()
    assert skill.uninstall() is None


def test_uninstall_keeps_other_files_in_the_folder(claude_home):
    path, _ = skill.install("0.22.0")
    (path.parent / "notes.md").write_text("mine", encoding="utf-8")

    skill.uninstall()

    assert not path.exists() and (path.parent / "notes.md").exists()


def test_stale_notice_only_for_a_managed_copy_from_another_version(claude_home):
    assert skill.stale_notice("0.22.0") is None  # not installed
    skill.install("0.22.0")
    assert skill.stale_notice("0.22.0") is None
    skill.install("0.21.0")
    assert skill.stale_notice("0.22.0") == (
        "The benchops Claude skill is from 0.21.0 (you have 0.22.0); run 'benchops skill install'."
    )
    installed(claude_home).write_text("hand-made\n", encoding="utf-8")
    assert skill.stale_notice("0.22.0") is None


# --------------------------------------------------------------------------- CLI


def test_cli_install_prints_where_and_the_targets_table(claude_home, monkeypatch):
    monkeypatch.setattr(cli, "installed_version", lambda: "0.22.0")

    result = CliRunner().invoke(cli.app, ["skill", "install"])

    assert result.exit_code == 0, result.output
    assert "Installed the Claude skill for benchops 0.22.0" in result.output
    assert "## BenchOps targets" in result.output and "| App" in result.output
    assert installed(claude_home).is_file()

    result = CliRunner().invoke(cli.app, ["skill", "status"])
    assert "Up to date (benchops 0.22.0)" in result.output


def test_cli_install_refusal_exits_1(claude_home):
    path = installed(claude_home)
    path.parent.mkdir(parents=True)
    path.write_text("hand-made\n", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["skill", "install"])

    assert result.exit_code == 1
    assert "--force" in " ".join(result.output.split())


def test_cli_uninstall(claude_home):
    CliRunner().invoke(cli.app, ["skill", "install"])

    result = CliRunner().invoke(cli.app, ["skill", "uninstall"])

    assert result.exit_code == 0 and "Removed the Claude skill" in result.output
    assert not installed(claude_home).exists()


def test_other_commands_point_out_a_stale_skill(claude_home, monkeypatch):
    skill.install("0.21.0")
    monkeypatch.setattr(cli, "installed_version", lambda: "0.22.0")

    class NoServers:
        def list_servers(self):
            return {}

    monkeypatch.setattr(cli, "ConfigManager", NoServers)

    result = CliRunner().invoke(cli.app, ["server", "list"])

    assert result.exit_code == 0
    assert "skill is from 0.21.0" in " ".join(result.stderr.split())
    assert "skill is from" not in result.stdout


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX file modes")
def test_upgrade_fixes_the_0600_mode_of_a_0_22_0_copy(claude_home):
    path, _ = skill.install("0.22.0")
    os.chmod(path, 0o600)

    skill.install("0.23.0")

    umask = os.umask(0)
    os.umask(umask)
    assert path.stat().st_mode & 0o777 == 0o666 & ~umask
