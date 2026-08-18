#!/usr/bin/env python3
"""Regenerate the top-level NOTICE file from requirements.txt.

The Docker image published from this repository bundles the third-party packages
pinned in requirements.txt. Distributing that image redistributes those packages,
which carries attribution obligations (Apache-2.0 section 4(d), and the "retain the
copyright notice" clause in MIT/BSD). NOTICE is how we carry them.

Run this after any change to requirements.txt and commit the result:

    python scripts/generate_notice.py

Package metadata is read from PyPI rather than from an installed environment, so
the output depends only on the pinned versions and is reproducible on any machine.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
NOTICE = REPO_ROOT / "NOTICE"

PINNED = re.compile(r"^([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)")
GIT_DEP = re.compile(r"^([A-Za-z0-9._-]+)\s*@\s*git\+(\S+)")

# Longest license string kept verbatim before falling back to the classifiers.
MAX_LICENSE_LENGTH = 60

# A few releases publish no license metadata to PyPI at all. These were verified
# against the upstream repository by hand; extend this map if the script reports
# another package needing manual verification.
LICENSE_OVERRIDES = {
    # LICENSE is Apache 2.0.
    "google-crc32c": ("Apache-2.0", "https://github.com/googleapis/python-crc32c"),
    # LICENSE reads "licensed under the terms of the MIT license" behind a preamble,
    # which is why GitHub reports the repository as NOASSERTION.
    "mypy-extensions": ("MIT", "https://github.com/python/mypy_extensions"),
}


def parse_requirements(
    path: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (pinned PyPI packages, git-sourced packages)."""
    pypi: list[tuple[str, str]] = []
    git: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        if match := PINNED.match(line):
            pypi.append((match.group(1), match.group(2)))
        elif match := GIT_DEP.match(line):
            git.append((match.group(1), match.group(2)))
    return pypi, git


def fetch_license(package: str, version: str) -> tuple[str, str, str]:
    """Return (package, license, project URL) for a pinned PyPI release."""
    if override := LICENSE_OVERRIDES.get(package):
        licence, project_url = override
        return package, licence, project_url

    url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            info = json.load(response)["info"]
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
        print(
            f"  WARNING: could not read metadata for {package}=={version}: {exc}",
            file=sys.stderr,
        )
        return package, "UNKNOWN - verify manually", ""

    classifiers = [c for c in info.get("classifiers", []) if c.startswith("License ::")]
    raw = info.get("license_expression") or info.get("license") or ""
    licence = summarise_license(raw, classifiers)

    project_url = info.get("home_page") or info.get("project_url") or ""
    return package, licence, project_url


def summarise_license(raw: str, classifiers: list[str]) -> str:
    """Reduce PyPI's license field to a single short line.

    Some projects publish a bare SPDX identifier, others paste their entire license
    text (headers, copyright lines and all) into the same field. Take the first
    meaningful line and collapse its whitespace so one package always renders as one
    line, then fall back to the classifiers when that line is still unusably long.
    """
    first_line = ""
    for line in raw.splitlines():
        if collapsed := " ".join(line.split()):
            first_line = collapsed
            break

    if first_line and len(first_line) <= MAX_LICENSE_LENGTH:
        return first_line

    if from_classifiers := " | ".join(c.split("::")[-1].strip() for c in classifiers):
        return from_classifiers

    if first_line:
        return first_line[: MAX_LICENSE_LENGTH - 3].rstrip() + "..."

    return "UNKNOWN - verify manually"


def render(
    pypi_licences: list[tuple[str, str, str]], git_deps: list[tuple[str, str]]
) -> str:
    width = max((len(name) for name, _, _ in pypi_licences), default=0)
    lines = [
        "Monte Carlo Hermes Agent",
        "Copyright 2025 Monte Carlo Data, Inc. (now known as Monte Carlo AI, Inc.)",
        "",
        "This product bundles the third-party Python packages listed below. They are",
        "installed into the published container image from the versions pinned in",
        "requirements.txt, and remain under their own licenses and copyrights.",
        "",
        "Regenerate with: python scripts/generate_notice.py",
        "",
        "Scope: this file covers the Python dependencies bundled into the image. It does",
        "not cover operating system packages inherited from the base image, which carry",
        "their own licenses and are documented by that image.",
        "",
        "=" * 78,
        "Monte Carlo components (first-party, not third-party dependencies)",
        "=" * 78,
        "",
    ]
    for name, source in sorted(git_deps):
        lines.append(f"{name} - {source}")
    lines += [
        "",
        "=" * 78,
        f"Third-party Python packages ({len(pypi_licences)})",
        "=" * 78,
        "",
    ]
    for name, licence, project_url in sorted(
        pypi_licences, key=lambda item: item[0].lower()
    ):
        entry = f"{name.ljust(width)}  {licence}"
        if project_url:
            entry += f"  <{project_url}>"
        lines.append(entry)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    pypi, git_deps = parse_requirements(REQUIREMENTS)
    print(f"Reading {len(pypi)} pinned packages and {len(git_deps)} git dependencies")

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        licences = list(executor.map(lambda item: fetch_license(*item), pypi))

    NOTICE.write_text(render(licences, git_deps))
    unknown = [name for name, licence, _ in licences if licence.startswith("UNKNOWN")]
    print(f"Wrote {NOTICE.relative_to(REPO_ROOT)} covering {len(licences)} packages")
    if unknown:
        print(f"  {len(unknown)} need manual verification: {', '.join(unknown)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
