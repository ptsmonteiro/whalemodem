from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_release.py"


def run(*args: str | Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(str(arg) for arg in args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )


def test_release_pushes_commit_and_tag_to_origin(tmp_path: Path) -> None:
    remote = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copyfile(SCRIPT, checkout / "scripts" / "make_release.py")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "release-test"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )

    run("git", "init", "--bare", remote, cwd=tmp_path)
    run("git", "init", "-b", "main", cwd=checkout)
    run("git", "config", "user.name", "Release Test", cwd=checkout)
    run("git", "config", "user.email", "release@example.invalid", cwd=checkout)
    run("git", "add", ".", cwd=checkout)
    run("git", "commit", "-m", "Initial", cwd=checkout)
    run("git", "remote", "add", "origin", remote, cwd=checkout)
    run("git", "push", "-u", "origin", "main", cwd=checkout)

    result = run(sys.executable, "scripts/make_release.py", "1.1.0", cwd=checkout)

    local_head = run("git", "rev-parse", "HEAD", cwd=checkout).stdout.strip()
    remote_branch = run(
        "git", "--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path
    ).stdout.strip()
    remote_tag_commit = run(
        "git", "--git-dir", remote, "rev-parse", "refs/tags/v1.1.0^{}", cwd=tmp_path
    ).stdout.strip()
    assert remote_branch == local_head
    assert remote_tag_commit == local_head
    assert "Created and pushed release commit and tag v1.1.0." in result.stdout
