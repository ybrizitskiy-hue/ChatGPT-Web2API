from pathlib import Path


def _script() -> str:
    return Path("scripts/setup-opencode.ps1").read_text(encoding="utf-8")


def test_windows_installer_uses_braced_variable_before_colon():
    script = _script()
    assert "$LASTEXITCODE:" not in script
    assert "${LASTEXITCODE}:" in script


def test_windows_installer_restarts_only_its_managed_web2api_processes():
    script = _script()
    assert "function Stop-ManagedStack" in script
    assert "Get-CimInstance Win32_Process" in script
    assert "$fromVenv -and $isWeb2Api" in script
    assert 'Contains("chatgpt_web2api")' in script
    assert 'Contains("chatgpt-web2api")' in script
    assert "Stop-ManagedStack -VenvPath $venv" in script
    assert "Stop-Process -Id $process.ProcessId -Force" in script
    # Never kill arbitrary owners of the Web2API/bridge ports.
    assert "taskkill" not in script.lower()
    assert "Get-NetTCPConnection" not in script
