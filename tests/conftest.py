import pytest


@pytest.fixture(autouse=True)
def no_update_check(monkeypatch, tmp_path):
    """Tests never reach PyPI or touch the real ~/.benchops cache."""

    def network_disabled(*args, **kwargs):
        raise OSError("network access is disabled in tests")

    monkeypatch.setenv("BENCHOPS_NO_UPDATE_CHECK", "1")
    # Running the suite under `aws-vault exec` must not change AWS behaviour.
    monkeypatch.delenv("AWS_VAULT", raising=False)
    monkeypatch.setattr("benchops.version.CACHE_PATH", tmp_path / "update-check.json")
    # Nor the record of which Claude Code permission rules benchops added.
    monkeypatch.setattr("benchops.claude_permissions.STATE_PATH", tmp_path / "claude-permissions.json")
    # Never read or write the developer's real Claude Code config (~/.claude).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setattr("benchops.version.urllib.request.urlopen", network_disabled)
