from pathlib import Path


def test_windows_installer_uses_braced_variable_before_colon():
    script = Path("scripts/setup-opencode.ps1").read_text(encoding="utf-8")
    assert "$LASTEXITCODE:" not in script
    assert "${LASTEXITCODE}:" in script
