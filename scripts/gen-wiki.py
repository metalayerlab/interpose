#!/usr/bin/env python3
"""Regenerate the generated sections of docs/wiki/ from the code. Template-owned: template-python.

    uv run scripts/gen-wiki.py           # rewrite the sections
    uv run scripts/gen-wiki.py --check   # CI: fail if any section is stale

A generated section sits between markers, and everything between them is replaced:

    <!-- generated:start settings -->
    ...
    <!-- generated:end settings -->

Blocks: settings (config.SETTINGS), cli (the Typer app), http-api (the OpenAPI schema).
Write prose around the markers, never inside them.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path

WIKI = Path("docs/wiki")


BLOCKS: dict[str, Callable[..., str]] = {}


def render(text: str) -> str:
    def replace(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in BLOCKS:
            raise SystemExit(f"error: unknown generated block {name!r}")
        return f"{m.group(0).split('-->')[0]}-->\n{BLOCKS[name]()}\n<!-- generated:end {name} -->"

    pattern = re.compile(
        r"<!--\s*generated:start\s+(\S+)\s*-->.*?<!--\s*generated:end\s+\1\s*-->", re.S
    )
    return pattern.sub(replace, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a section is stale")
    args = parser.parse_args(argv)

    stale = []
    for page in sorted(WIKI.glob("*.md")):
        text = page.read_text(encoding="utf-8")
        new = render(text)
        if new != text:
            stale.append(page)
            if not args.check:
                page.write_text(new, encoding="utf-8")
    if args.check and stale:
        for page in stale:
            print(f"error: {page}: generated section is stale")
        print("Run `uv run scripts/gen-wiki.py` and commit the result.")
        return 1
    print(("stale: " if args.check else "updated: ") + (", ".join(map(str, stale)) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
