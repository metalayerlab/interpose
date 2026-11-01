#!/usr/bin/env python3
"""Fail if any tracked file contains personal data.

Checks every file tracked by git (or the paths given) for:
  - public IPv4 addresses. RFC 5737 documentation ranges, private and loopback
    addresses are allowed, so use 192.0.2.x, 198.51.100.x or 203.0.113.x as placeholders.
  - any string listed in PERSONAL_DATA_DENYLIST (one per line, case-insensitive).
    Supply it from a repository secret, so the real values never appear in the repo.

Version numbers after a product token ("Chrome/130.0.0.0") are not treated as
addresses. A line containing "personal-data: ok" skips the IP check, for
deliberate test values; the denylist still applies to it.

Stdlib only, so CI can run it before installing anything. Template-owned.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

IPV4 = re.compile(r"(?<![\d.])(?<![A-Za-z]/)(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
ALLOW_MARKER = "personal-data: ok"
SELF = Path(__file__).resolve()


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
    return [Path(p) for p in out.stdout.decode().split("\0") if p]


def is_public(addr: str) -> bool:
    try:
        return ipaddress.IPv4Address(addr).is_global
    except ValueError:
        return False


def check(path: Path, denylist: list[str]) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []  # binary
    except OSError:
        return []  # a directory (submodule), or deleted since `git ls-files`
    problems = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        problems += [
            f"{path}:{lineno}: public IPv4 address" for a in IPV4.findall(line) if is_public(a)
        ]
    lowered = text.lower()
    if any(term in lowered for term in denylist):
        problems.append(f"{path}: contains a PERSONAL_DATA_DENYLIST entry")
    return problems


def main(argv: list[str]) -> int:
    raw = os.environ.get("PERSONAL_DATA_DENYLIST", "").splitlines()
    denylist = [t.strip().lower() for t in raw if t.strip()]
    paths = [Path(a) for a in argv] or tracked_files()
    problems = [p for path in paths if path.resolve() != SELF for p in check(path, denylist)]
    for p in problems:
        print(p)
    if problems:
        print(f"\n{len(problems)} personal-data finding(s). Scrub them before committing.")
        return 1
    active = " (denylist active)" if denylist else ""
    print(f"No personal data found in {len(paths)} file(s){active}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
