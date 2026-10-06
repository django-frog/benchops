"""Version reporting: installed vs latest on PyPI (cached daily, failures
silent), the bundled changelog, and the CLI surface (--version, `version`,
and the passive update notice on stderr)."""

import json
import time

import pytest
from typer.testing import CliRunner

import benchops.cli as cli
from benchops import version


@pytest.mark.parametrize(
    "candidate, current, newer",
    [
        ("0.15.0", "0.14.0", True),
        ("0.14.1", "0.14.0", True),
        ("1.0.0", "0.99.9", True),
        ("0.14.0", "0.14.0", False),
        ("0.13.9", "0.14.0", False),
        ("1.0.0", "1.0.0rc1", True),
        ("1.0.0rc1", "0.14.0", True),
        ("garbage", "0.14.0", False),
    ],
)
def test_is_newer(candidate, current, newer):
    assert version.is_newer(candidate, current) is newer


def test_latest_version_is_cached_for_a_day(monkeypatch, tmp_path):
    cache = tmp_path / "check.json"
    calls = []
    monkeypatch.setattr(version, "fetch_latest", lambda timeout=None: calls.append(1) or "0.15.0")

    assert version.latest_version(cache_path=cache) == "0.15.0"
    assert version.latest_version(cache_path=cache) == "0.15.0"
    assert len(calls) == 1

    cache.write_text(json.dumps({"checked_at": time.time() - 2 * 24 * 3600, "latest": "0.15.0"}))
    version.latest_version(cache_path=cache)
    assert len(calls) == 2

    version.latest_version(force=True, cache_path=cache)
    assert len(calls) == 3


def test_offline_check_falls_back_to_cache_and_does_not_retry_every_run(monkeypatch, tmp_path):
    cache = tmp_path / "check.json"
    cache.write_text(json.dumps({"checked_at": 0, "latest": "0.15.0"}))
    calls = []
    monkeypatch.setattr(version, "fetch_latest", lambda timeout=None: calls.append(1) or None)

    assert version.latest_version(cache_path=cache) == "0.15.0"
    assert version.latest_version(cache_path=cache) == "0.15.0"
    assert len(calls) == 1


def test_fetch_latest_swallows_network_errors():
    # The network is disabled for every test (conftest.py), so this is the offline path.
    assert version.fetch_latest() is None


def test_update_notice(monkeypatch):
    monkeypatch.setattr(version, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(version, "latest_version", lambda: "0.15.0")

    assert version.update_notice() is None  # disabled in tests via BENCHOPS_NO_UPDATE_CHECK

    monkeypatch.delenv(version.DISABLE_ENV)
    assert "benchops 0.15.0 is available (you have 0.14.0)" in version.update_notice()

    monkeypatch.setattr(version, "latest_version", lambda: "0.14.0")
    assert version.update_notice() is None


def test_bundled_changelog_covers_the_installed_version():
    notes = dict(version.release_notes())

    assert version.installed_version() in notes
    assert all(body for body in notes.values())


def test_empty_changelog_sections_are_skipped(monkeypatch):
    monkeypatch.setattr(
        version, "_changelog_text", lambda: "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n- Done.\n"
    )

    assert version.release_notes() == [("1.0.0", "- Done.")]


def test_cli_version_flag():
    result = CliRunner().invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"benchops {version.installed_version()}"


def test_cli_version_command_reports_newer_release(monkeypatch):
    monkeypatch.setattr(version, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(version, "latest_version", lambda force=False: "0.15.0")

    result = CliRunner().invoke(cli.app, ["version"])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "benchops 0.14.0" in out
    assert "A newer version is available: 0.15.0" in out
    assert "uv tool upgrade benchops" in out
    assert "0.14.0" in out and "Overlap warning" in out


def test_cli_version_command_when_up_to_date_and_all(monkeypatch):
    monkeypatch.setattr(version, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(version, "latest_version", lambda force=False: "0.14.0")

    result = CliRunner().invoke(cli.app, ["version", "--all"])

    out = " ".join(result.output.split())
    assert "You are on the latest version." in out
    assert "0.6.0" in out and "0.13.0" in out


def test_passive_notice_goes_to_stderr_before_commands(monkeypatch):
    monkeypatch.delenv(version.DISABLE_ENV)
    monkeypatch.setattr(cli, "update_notice", lambda: "benchops 0.15.0 is available (you have 0.14.0).")

    result = CliRunner().invoke(cli.app, ["server", "list"])

    assert "benchops 0.15.0 is available" in result.stderr
    assert "0.15.0" not in result.stdout


def test_no_passive_notice_on_the_version_command(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "update_notice", lambda: calls.append(1))
    monkeypatch.setattr(version, "latest_version", lambda force=False: None)

    CliRunner().invoke(cli.app, ["version"])

    assert calls == []
