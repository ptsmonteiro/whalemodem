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
GITHUB_REPOSITORY_RE = re.compile(
    r"(?:github\.com[/:])(?P<owner>[^/]+)/(?P<repo>[^/#]+?)(?:\.git)?$",
    re.IGNORECASE,
)


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


def project_version() -> str:
    """Return the version declared by the working tree."""
    match = PROJECT_VERSION_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    if match is None:
        fail("could not find project.version in pyproject.toml")
    return match.group(1)


def github_repository() -> str | None:
    """Return the GitHub owner/repository for origin, if it has one."""
    origin = git("remote", "get-url", "origin", capture=True)
    match = GITHUB_REPOSITORY_RE.search(origin)
    return f"{match.group('owner')}/{match.group('repo')}" if match else None


def github_release_tags() -> list[str]:
    """Return release tags from origin, newest version first."""
    output = git("ls-remote", "--tags", "--refs", "origin", capture=True)
    tags = [
        line.rsplit("/", 1)[-1]
        for line in output.splitlines()
        if "\trefs/tags/v" in line
    ]
    return sorted(tags, key=lambda tag: version_key(tag.removeprefix("v")), reverse=True)


def version_key(version: str) -> tuple[int, ...]:
    """Compare the numeric release component of supported release versions."""
    match = re.match(r"^(\d+(?:\.\d+)*)", version)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def show_releases() -> None:
    """Print GitHub release tags and note a newer local tree version."""
    git("rev-parse", "--is-inside-work-tree", capture=True)
    repository = github_repository()
    if repository is None:
        fail("origin is not a GitHub repository; cannot list GitHub releases")
    releases = github_release_tags()
    local_version = project_version()

    print(f"Releases ({repository}):")
    if releases:
        latest_version = releases[0].removeprefix("v")
        local_is_newer = version_key(local_version) > version_key(latest_version)
        if local_is_newer:
            print(f"  v{local_version}  (latest, local tree; newer than GitHub)")
        for index, release in enumerate(releases):
            label = "latest" if index == 0 and not local_is_newer else "previous"
            print(f"  {release}  ({label}, GitHub)")
    else:
        print("  no GitHub release tags")
        print(f"  v{local_version}  (latest, local tree)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Update project.version, commit it, create a vVERSION tag, and push "
            "the branch and tag to origin."
        )
    )
    parser.add_argument(
        "version", nargs="?", help="PEP 440 release version, without a leading v"
    )
    args = parser.parse_args()
    if args.version is None:
        show_releases()
        return
    version = args.version
    tag = f"v{version}"

    if not VERSION_RE.fullmatch(version):
        fail("version must be PEP 440-like (for example 1.2.3 or 1.2.3rc1), without 'v'")
    git("rev-parse", "--is-inside-work-tree", capture=True)
    if git("status", "--porcelain", capture=True):
        fail("working tree is not clean; commit, stash, or discard changes first")
    branch = git("branch", "--show-current", capture=True)
    if not branch:
        fail("HEAD is detached; check out the release branch first")

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
    git("push", "--atomic", "origin", branch, tag)

    print(f"Created and pushed release commit and tag {tag}.")


if __name__ == "__main__":
    main()
