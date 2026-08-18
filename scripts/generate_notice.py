#!/usr/bin/env python3
"""Regenerate the top-level NOTICE file, or verify it is up to date.

The Docker image published from this repository bundles third-party Python
packages. Distributing that image redistributes them, which carries attribution
obligations (Apache-2.0 section 4(d), and the "retain the copyright notice" clause
in MIT/BSD). NOTICE is how we carry them.

Two sources are covered, because both end up in the published image:

  * every package pinned in requirements.txt
  * the packages the Dockerfile installs directly into the venv, currently pip and
    setuptools, whose versions are parsed from the Dockerfile so this stays correct
    as those pins move

Run after any change to requirements.txt or to the Dockerfile's pip install pins:

    python scripts/generate_notice.py

CI runs the same script with --check, which regenerates in memory and fails if the
committed NOTICE differs.

Package metadata is read from PyPI rather than from an installed environment, so
the output depends only on the pinned versions and is reproducible on any machine.
A metadata fetch that fails aborts the run before writing, so a transient outage
cannot silently degrade a good NOTICE into one full of unknowns.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
DOCKERFILE = REPO_ROOT / "Dockerfile"
NOTICE = REPO_ROOT / "NOTICE"

PINNED = re.compile(r"^([A-Za-z0-9._-]+)(?:\[[^\]]+\])?\s*==\s*([^\s;]+)")
GIT_DEP = re.compile(r"^([A-Za-z0-9._-]+)\s*@\s*git\+(\S+)")
# The Dockerfile line that installs packages into the venv outside requirements.txt.
DOCKERFILE_PIN = re.compile(r"\bpip install\b([^\n]*)")
DOCKERFILE_PKG = re.compile(r"([A-Za-z0-9._-]+)==([0-9][^\s]*)")

# Longest license string kept verbatim before falling back to the classifiers.
MAX_LICENSE_LENGTH = 60

# Raw license values that name no actual license. They pass the token check below
# but tell a reviewer nothing, so the classifiers are preferred instead.
UNINFORMATIVE_LICENSES = frozenset(
    {
        "dual license",
        "other",
        "other/proprietary license",
        "see license",
        "see license file",
        "see licence file",
        "unknown",
    }
)

# A license field whose first line contains none of these is most likely prose or a
# copyright line rather than a license name, so the classifiers are preferred.
LICENSE_TOKENS = (
    "license",
    "licence",
    "agreement",
    "mit",
    "bsd",
    "apache",
    "gpl",
    "mpl",
    "isc",
    "zlib",
    "psf",
    "python",
    "unlicense",
    "public domain",
    "proprietary",
    "cc0",
    "epl",
    "cddl",
    "artistic",
    "boost",
)

# project_urls keys that point at the project itself, best first. PyPI's own
# info["project_url"] is the PyPI page, not upstream, so it is only a last resort.
UPSTREAM_URL_KEYS = (
    "Homepage",
    "Home",
    "Source",
    "Source Code",
    "Repository",
    "Code",
    "GitHub",
    "Documentation",
)

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


class MetadataError(RuntimeError):
    """Raised when a package's metadata could not be retrieved."""


def parse_requirements(
    path: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (pinned PyPI packages, git-sourced packages) from a requirements file."""
    pypi: list[tuple[str, str]] = []
    git: list[tuple[str, str]] = []
    for number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        if match := PINNED.match(line):
            pypi.append((match.group(1), match.group(2)))
        elif match := GIT_DEP.match(line):
            git.append((match.group(1), match.group(2)))
        else:
            # Never skip a requirement silently: a missed line is missing attribution.
            print(
                f"  WARNING: {path.name}:{number} not recognised as a requirement: {line}",
                file=sys.stderr,
            )
    return pypi, git


def parse_dockerfile_pins(path: Path) -> list[tuple[str, str]]:
    """Return packages the Dockerfile installs into the venv outside requirements.txt.

    These reach the published image but appear in no requirements file, so they would
    otherwise be missing from NOTICE entirely.
    """
    packages: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        if install := DOCKERFILE_PIN.search(line):
            packages.extend(DOCKERFILE_PKG.findall(install.group(1)))
    return packages


def looks_like_license(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in LICENSE_TOKENS)


def summarise_license(raw: str, expression: str, classifiers: list[str]) -> str:
    """Reduce PyPI's license metadata to a single short line.

    Preference order is the SPDX expression, then the raw license field when it looks
    like a license name and is short enough, then the classifiers. Some projects paste
    their entire license text into the raw field, so it is only trusted when it passes
    both checks.
    """
    if expression := expression.strip():
        return expression

    first_line = ""
    for line in raw.splitlines():
        if collapsed := " ".join(line.split()):
            first_line = collapsed
            break

    usable = (
        first_line
        and len(first_line) <= MAX_LICENSE_LENGTH
        and looks_like_license(first_line)
        and first_line.lower().strip(" .") not in UNINFORMATIVE_LICENSES
    )
    if usable:
        return first_line

    from_classifiers = " | ".join(c.split("::")[-1].strip() for c in classifiers)
    if from_classifiers:
        return from_classifiers

    if first_line:
        return first_line[: MAX_LICENSE_LENGTH - 3].rstrip() + "..."

    raise MetadataError("no license information published")


def pick_url(info: dict) -> str:
    """Prefer the project's own URL over PyPI's project page."""
    project_urls = info.get("project_urls") or {}
    for key in UPSTREAM_URL_KEYS:
        if url := project_urls.get(key):
            return url
    # Any remaining project URL beats the PyPI page.
    for key, url in project_urls.items():
        if url and "pypi.org" not in url:
            return url
    return info.get("home_page") or info.get("project_url") or ""


def fetch_license(package: str, version: str) -> tuple[str, str, str]:
    """Return (package, license, project URL) for a pinned PyPI release."""
    if override := LICENSE_OVERRIDES.get(package):
        license_name, project_url = override
        return package, license_name, project_url

    url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            info = json.load(response)["info"]
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
        raise MetadataError(f"could not read metadata from PyPI: {exc}") from exc

    classifiers = [c for c in info.get("classifiers", []) if c.startswith("License ::")]
    license_name = summarise_license(
        info.get("license") or "",
        info.get("license_expression") or "",
        classifiers,
    )
    return package, license_name, pick_url(info)


def render(
    licenses: list[tuple[str, str, str]],
    git_deps: list[tuple[str, str]],
) -> str:
    width = max((len(name) for name, _, _ in licenses), default=0)
    lines = [
        "Monte Carlo Hermes Agent",
        "Copyright 2025 Monte Carlo Data, Inc. (now known as Monte Carlo AI, Inc.)",
        "",
        "This product bundles the third-party Python packages listed below. They are",
        "installed into the published container image from the versions pinned in",
        "requirements.txt, plus the packages the Dockerfile installs directly into the",
        "virtual environment. They remain under their own licenses and copyrights.",
        "",
        "Regenerate with: python scripts/generate_notice.py",
        "",
        "Scope: this file covers the Python packages bundled into the image. Operating",
        "system packages come from the Debian-based image this one is built on and are",
        "not listed here; they retain their own licenses, and their per-package copyright",
        "files ship inside the image under /usr/share/doc/*/copyright.",
        "",
        "=" * 78,
        "Monte Carlo components (first-party, not third-party dependencies)",
        "=" * 78,
        "",
    ]
    lines.extend(f"{name} - {source}" for name, source in sorted(git_deps))
    lines += [
        "",
        "=" * 78,
        f"Third-party Python packages ({len(licenses)})",
        "=" * 78,
        "",
    ]
    for name, license_name, project_url in sorted(licenses, key=lambda x: x[0].lower()):
        entry = f"{name.ljust(width)}  {license_name}"
        if project_url:
            entry += f"  <{project_url}>"
        lines.append(entry)
    lines.append("")
    return "\n".join(lines)


def build_notice() -> str:
    pypi, git_deps = parse_requirements(REQUIREMENTS)
    docker_pins = parse_dockerfile_pins(DOCKERFILE)
    pinned = {name: version for name, version in pypi}
    for name, version in docker_pins:
        pinned.setdefault(name, version)
    print(
        f"Reading {len(pypi)} pinned packages, {len(docker_pins)} Dockerfile pins "
        f"and {len(git_deps)} git dependencies"
    )

    packages = sorted(pinned.items())
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(fetch_license, name, version): name
            for name, version in packages
        }
        licenses = []
        failures = []
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                licenses.append(future.result())
            except MetadataError as exc:
                failures.append(f"{name}: {exc}")

    if failures:
        # Abort before writing: a partial run must not overwrite a good NOTICE.
        for failure in sorted(failures):
            print(f"  ERROR: {failure}", file=sys.stderr)
        raise MetadataError(f"{len(failures)} package(s) could not be resolved")

    return render(licenses, git_deps)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed NOTICE matches what would be generated",
    )
    args = parser.parse_args()

    try:
        content = build_notice()
    except MetadataError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        print("NOTICE was left unchanged.", file=sys.stderr)
        return 1

    if args.check:
        current = NOTICE.read_text() if NOTICE.exists() else ""
        if current == content:
            print("NOTICE is up to date")
            return 0
        print(
            "NOTICE is out of date. Run 'python scripts/generate_notice.py' "
            "and commit the result.",
            file=sys.stderr,
        )
        return 1

    NOTICE.write_text(content)
    print(f"Wrote {NOTICE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
