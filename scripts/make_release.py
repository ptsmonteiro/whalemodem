#!/usr/bin/env python3
"""Create a release commit and annotated tag from any supported platform."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
# A deliberately small, conventional PEP 440 subset for releases.  In
# particular, use ``1.2.3rc1`` rather than the ambiguous ``1.2.3-rc1``.
VERSION_RE = re.compile(
    r"^\d+(?:\.\d+){1,2}(?:(?:a|b|rc)\d+|\.post\d+|\.dev\d+)?"
    r"(?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?$",
    re.IGNORECASE,
)
PROJECT_VERSION_RE = re.compile(r'(?m)^version\s*=\s*"([^"]*)"\s*$')


def git(*args: str, capture: bool = False) -> str:
    """Run git at the repository root, failing with git's own output."""
    result = subprocess.run(
        ("git", *args), cwd=ROOT, text=True, capture_output=capture,
    )
    if result.returncode:
        if capture:
            sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout.strip() if capture else ""


def fail(message: str) -> None:
    raise SystemExit(f"error: {message}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update project.version, commit it, and create a vVERSION tag."
    )
    parser.add_argument("version", help="PEP 440 release version, without a leading v")
    parser.add_argument(
        "--push", action="store_true",
        help="push the release commit and its tag to origin after creating them",
    )
    args = parser.parse_args()
    version = args.version
    tag = f"v{version}"

    if not VERSION_RE.fullmatch(version):
        fail("version must be PEP 440-like (for example 1.2.3 or 1.2.3rc1), without 'v'")
    git("rev-parse", "--is-inside-work-tree", capture=True)
    if git("status", "--porcelain", capture=True):
        fail("working tree is not clean; commit, stash, or discard changes first")

    tag_exists = subprocess.run(
        ("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"), cwd=ROOT,
    )
    if tag_exists.returncode == 0:
        fail(f"tag {tag} already exists")
    if tag_exists.returncode != 1:
        raise SystemExit(tag_exists.returncode)

    source = PYPROJECT.read_text(encoding="utf-8")
    match = PROJECT_VERSION_RE.search(source)
    if match is None:
        fail("could not find project.version in pyproject.toml")
    if match.group(1) == version:
        fail(f"project.version is already {version}")
    updated = source[:match.start(1)] + version + source[match.end(1):]
    PYPROJECT.write_text(updated, encoding="utf-8", newline="")

    git("add", "pyproject.toml")
    git("commit", "-m", f"Release {tag}")
    git("tag", "-a", tag, "-m", f"Release {tag}")

    if args.push:
        branch = git("branch", "--show-current", capture=True)
        if not branch:
            fail("release was created locally, but HEAD is detached; push it manually")
        git("push", "origin", branch)
        git("push", "origin", tag)

    print(f"Created release commit and tag {tag}.")
    if not args.push:
        print(f"To publish it: git push origin HEAD {tag}")


if __name__ == "__main__":
    main()
