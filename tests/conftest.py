import pytest


@pytest.fixture(autouse=True)
def no_update_check(monkeypatch, tmp_path):
    """Tests never reach PyPI or touch the real ~/.benchops cache."""

    def network_disabled(*args, **kwargs):
        raise OSError("network access is disabled in tests")

    monkeypatch.setenv("BENCHOPS_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr("benchops.version.CACHE_PATH", tmp_path / "update-check.json")
    monkeypatch.setattr("benchops.version.urllib.request.urlopen", network_disabled)
