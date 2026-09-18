import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _posix_shell():
    shell = shutil.which("sh")
    if shell:
        return shell
    git_shell = Path(r"C:\Program Files\Git\bin\sh.exe")
    return str(git_shell) if git_shell.exists() else None


def test_unix_platform_detection_and_urls():
    shell = _posix_shell()
    if shell is None:
        pytest.skip("POSIX shell unavailable")
    script = f'''. "{ROOT / "install.sh"}"
test "$(platform_tag Linux x86_64)" = linux-x86_64
test "$(platform_tag Linux aarch64)" = linux-aarch64
test "$(platform_tag Linux armv7l)" = linux-armv7
test "$(platform_tag Darwin x86_64)" = macos-x86_64
test "$(platform_tag Darwin arm64)" = macos-arm64
test "$(asset_name linux-x86_64)" = whale-linux-x86_64.tar.gz
test "$(asset_name macos-arm64)" = whale-macos-arm64.zip
test "$(release_url owner/repo v1.2.3 whale-linux-x86_64.tar.gz)" = \
  https://github.com/owner/repo/releases/download/v1.2.3/whale-linux-x86_64.tar.gz
! platform_tag FreeBSD x86_64
'''
    env = {**os.environ, "WHALE_INSTALLER_TEST": "1"}
    subprocess.run([shell, "-n", str(ROOT / "install.sh")], check=True)
    subprocess.run([shell, "-c", script], check=True, env=env)


def test_release_workflow_publishes_stable_assets_and_checksums():
    workflow = (ROOT / ".github/workflows/standalone-builds.yml").read_text()
    for name in (
        "whale-linux-x86_64.tar.gz", "whale-linux-aarch64.tar.gz",
        "whale-linux-armv7.tar.gz", "whale-macos-x86_64.zip",
        "whale-macos-arm64.zip", "whale-windows-x86_64.zip",
    ):
        assert name in workflow or "whale-${{ matrix.tag }}" in workflow
    assert "sha256sum whale-* > SHA256SUMS" in workflow


def test_windows_platform_detection_and_url():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    command = f'''. '{ROOT / "install.ps1"}'
Get-WhalePlatformTag 'Microsoft Windows' X64
$Repository='owner/repo'
Get-WhaleReleaseUrl 'v1.2.3' 'whale-windows-x86_64.zip'
try {{ Get-WhalePlatformTag 'Microsoft Windows' Arm64 }} catch {{ 'unsupported' }}
'''
    env = {**os.environ, "WHALE_INSTALLER_TEST": "1"}
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", command], check=True,
        env=env, capture_output=True, text=True,
    )
    assert result.stdout.splitlines() == [
        "windows-x86_64",
        "https://github.com/owner/repo/releases/download/v1.2.3/whale-windows-x86_64.zip",
        "unsupported",
    ]
