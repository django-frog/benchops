"""OS dispatch for benchops.security.secure_file(): chmod on POSIX
(Linux/macOS), icacls on Windows — since NTFS has no chmod-bit equivalent
and os.chmod() there silently no-ops instead of raising.
"""

from unittest.mock import MagicMock, patch

import pytest

from benchops.runner import BenchOpsConnectionError
from benchops.security import secure_file


def test_linux_uses_chmod(monkeypatch, tmp_path):
    monkeypatch.setattr("benchops.security.platform.system", lambda: "Linux")
    target = tmp_path / "config.toml"
    target.write_text("")

    with patch.object(type(target), "chmod") as mock_chmod:
        secure_file(target, 0o600)

    mock_chmod.assert_called_once_with(0o600)


def test_macos_uses_chmod(monkeypatch, tmp_path):
    monkeypatch.setattr("benchops.security.platform.system", lambda: "Darwin")
    target = tmp_path / "benchops_ed25519"
    target.write_text("")

    with patch.object(type(target), "chmod") as mock_chmod:
        secure_file(target, 0o600)

    mock_chmod.assert_called_once_with(0o600)


def test_windows_uses_icacls_with_current_user_full_control(monkeypatch, tmp_path):
    monkeypatch.setattr("benchops.security.platform.system", lambda: "Windows")
    monkeypatch.setenv("USERNAME", "devuser")
    monkeypatch.setattr(
        "benchops.security.shutil.which", lambda name: r"C:\Windows\System32\icacls.exe"
    )
    target = tmp_path / "config.toml"

    mock_result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("benchops.security.subprocess.run", return_value=mock_result) as mock_run, patch.object(
        type(target), "chmod"
    ) as mock_chmod:
        secure_file(target, 0o600)

    mock_chmod.assert_not_called()
    called_argv = mock_run.call_args[0][0]
    assert called_argv[0] == r"C:\Windows\System32\icacls.exe"
    assert str(target) in called_argv
    assert "/inheritance:r" in called_argv
    assert "devuser:F" in called_argv


def test_windows_missing_username_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("benchops.security.platform.system", lambda: "Windows")
    monkeypatch.delenv("USERNAME", raising=False)

    with pytest.raises(BenchOpsConnectionError, match="USERNAME"):
        secure_file(tmp_path / "config.toml", 0o600)


def test_windows_missing_icacls_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("benchops.security.platform.system", lambda: "Windows")
    monkeypatch.setenv("USERNAME", "devuser")
    monkeypatch.setattr("benchops.security.shutil.which", lambda name: None)

    with pytest.raises(BenchOpsConnectionError, match="icacls"):
        secure_file(tmp_path / "config.toml", 0o600)


def test_windows_icacls_nonzero_exit_raises_with_stderr_detail(monkeypatch, tmp_path):
    monkeypatch.setattr("benchops.security.platform.system", lambda: "Windows")
    monkeypatch.setenv("USERNAME", "devuser")
    monkeypatch.setattr("benchops.security.shutil.which", lambda name: "icacls")
    mock_result = MagicMock(returncode=1, stdout="", stderr="Access is denied.")

    with patch("benchops.security.subprocess.run", return_value=mock_result):
        with pytest.raises(BenchOpsConnectionError, match="Access is denied"):
            secure_file(tmp_path / "config.toml", 0o600)


def test_windows_icacls_launch_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("benchops.security.platform.system", lambda: "Windows")
    monkeypatch.setenv("USERNAME", "devuser")
    monkeypatch.setattr("benchops.security.shutil.which", lambda name: "icacls")

    with patch("benchops.security.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(BenchOpsConnectionError, match="icacls"):
            secure_file(tmp_path / "config.toml", 0o600)
