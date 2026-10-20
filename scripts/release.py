#!/usr/bin/env python3
"""Cut a semver release: bump version, update CHANGELOG, commit, tag, push.

Usage (from anywhere in the repo):
    scripts/release.py                 # infer bump from conventional commits
    scripts/release.py minor           # force major | minor | patch
    scripts/release.py --dry-run       # show next version + notes, change nothing
    scripts/release.py --no-push       # commit + tag locally only

Bump inference over commits since the last `v*` tag: `type!:` or a
`BREAKING CHANGE` footer → major; any `feat` → minor; otherwise patch.
The first release (no tag yet) is INITIAL_VERSION.

Release notes: hand-written text under `## [Unreleased]` in CHANGELOG.md wins;
otherwise notes are generated from commit subjects. Pushing the tag triggers
.github/workflows/release.yml (GHCR image + GitHub Release).
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_PY = ROOT / "backend" / "app" / "__init__.py"
PACKAGE_JSON = ROOT / "frontend" / "package.json"
PACKAGE_LOCK = ROOT / "frontend" / "package-lock.json"
INITIAL_VERSION = "1.0.0"
BRANCH = "main"

UNRELEASED = "## [Unreleased]"
UNRELEASED_HEADING = re.compile(r"^## \[Unreleased\][ \t]*$", re.MULTILINE)
CONVENTIONAL = re.compile(r"^(?P<type>\w+)(?:\([^)]*\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
SECTIONS = {"breaking": "Breaking", "feat": "Features", "fix": "Fixes", "other": "Other"}


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def die(msg: str) -> None:
    sys.exit(f"release: {msg}")


def check_preconditions() -> None:
    if git("status", "--porcelain"):
        die("working tree is not clean; commit or stash first")
    if git("rev-parse", "--abbrev-ref", "HEAD") != BRANCH:
        die(f"not on {BRANCH}")
    git("fetch", "--quiet", "origin", BRANCH)
    if int(git("rev-list", "--count", f"HEAD..origin/{BRANCH}")):
        die(f"behind origin/{BRANCH}; pull first")


def last_tag() -> str | None:
    tag = git("describe", "--tags", "--abbrev=0", "--match", "v*", check=False)
    return tag or None


def commits_since(tag: str | None) -> list[tuple[str, str]]:
    """(subject, body) pairs, oldest first, excluding release commits."""
    rev = f"{tag}..HEAD" if tag else "HEAD"
    raw = git("log", "--reverse", "--format=%s%x1f%b%x1e", rev)
    out = []
    for entry in raw.split("\x1e"):
        if not entry.strip():
            continue
        subject, _, body = entry.strip().partition("\x1f")
        if subject.startswith("chore(release)"):
            continue
        out.append((subject.strip(), body.strip()))
    return out


def classify(subject: str, body: str) -> tuple[str, str]:
    """(section key, description) for one commit."""
    m = CONVENTIONAL.match(subject)
    if m is None:
        return "other", subject
    desc = m["desc"].strip()
    desc = desc[0].upper() + desc[1:]
    if m["bang"] or "BREAKING CHANGE" in body:
        return "breaking", desc
    if m["type"] in ("feat", "fix"):
        return m["type"], desc
    return "other", desc


def infer_bump(commits: list[tuple[str, str]]) -> str:
    kinds = {classify(s, b)[0] for s, b in commits}
    if "breaking" in kinds:
        return "major"
    return "minor" if "feat" in kinds else "patch"


def bump(version: str, level: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def generate_notes(commits: list[tuple[str, str]]) -> str:
    grouped: dict[str, list[str]] = {k: [] for k in SECTIONS}
    for subject, body in commits:
        key, desc = classify(subject, body)
        grouped[key].append(desc)
    blocks = [f"### {SECTIONS[k]}\n" + "\n".join(f"- {d}" for d in items)
              for k, items in grouped.items() if items]
    return "\n\n".join(blocks)


def split_changelog(text: str) -> tuple[str, str, str]:
    """(head up to and including the Unreleased heading, unreleased body, rest)."""
    heading = UNRELEASED_HEADING.search(text)
    if heading is None:
        die(f"CHANGELOG.md has no '{UNRELEASED}' heading")
    head, tail = text[:heading.start()], text[heading.end():]
    m = re.search(r"^## \[", tail, flags=re.MULTILINE)
    body, rest = (tail[:m.start()], tail[m.start():]) if m else (tail, "")
    return head + UNRELEASED, body.strip(), rest


def release_notes(commits: list[tuple[str, str]]) -> str:
    _, unreleased, _ = split_changelog(CHANGELOG.read_text())
    return unreleased or generate_notes(commits)


def write_changelog(version: str, notes: str) -> None:
    head, _, rest = split_changelog(CHANGELOG.read_text())
    section = f"## [{version}] - {date.today().isoformat()}\n\n{notes}\n"
    text = f"{head}\n\n{section}" + (f"\n{rest}" if rest else "")
    CHANGELOG.write_text(text.rstrip() + "\n")


def current_version() -> str:
    m = re.search(r'__version__ = "([^"]+)"', VERSION_PY.read_text())
    if m is None:
        die(f"no __version__ in {VERSION_PY}")
    return m[1]


def write_version(version: str) -> None:
    VERSION_PY.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"',
                                 VERSION_PY.read_text()))
    pkg = json.loads(PACKAGE_JSON.read_text())
    pkg["version"] = version
    PACKAGE_JSON.write_text(json.dumps(pkg, indent=2) + "\n")
    if PACKAGE_LOCK.exists():
        lock = json.loads(PACKAGE_LOCK.read_text())
        lock["version"] = version
        if "" in lock.get("packages", {}):
            lock["packages"][""]["version"] = version
        PACKAGE_LOCK.write_text(json.dumps(lock, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("level", nargs="?", choices=["major", "minor", "patch"],
                        help="force the bump level instead of inferring it")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the next version and notes; change nothing")
    parser.add_argument("--no-push", action="store_true", help="commit and tag locally only")
    args = parser.parse_args()

    if not args.dry_run:
        check_preconditions()
    tag = last_tag()
    commits = commits_since(tag)
    if not commits:
        print(f"release: nothing to release since {tag}")
        return
    if tag is None:
        version, level = INITIAL_VERSION, "initial"
    else:
        level = args.level or infer_bump(commits)
        version = bump(tag.removeprefix("v"), level)
    notes = release_notes(commits)

    print(f"{tag or '(no tag)'} → v{version} ({level}, {len(commits)} commits; "
          f"code was {current_version()})\n")
    print(notes)
    if args.dry_run:
        return

    write_changelog(version, notes)
    write_version(version)
    git("add", str(CHANGELOG), str(VERSION_PY), str(PACKAGE_JSON), str(PACKAGE_LOCK))
    git("commit", "--quiet", "-m", f"chore(release): v{version}")
    git("tag", "-a", f"v{version}", "-m", f"v{version}")
    print(f"\nrelease: committed and tagged v{version}")
    if args.no_push:
        print(f"release: not pushed; run `git push --atomic origin {BRANCH} v{version}`")
        return
    git("push", "--atomic", "--quiet", "origin", BRANCH, f"v{version}")
    print(f"release: pushed {BRANCH} and v{version}")


if __name__ == "__main__":
    main()
