#!/usr/bin/env python3
"""Micro-benchmark of interpose.pipeline's guard: microseconds per call, stdlib only.

    uv run benchmarks/bench-pipeline.py [--n 200000]

Each case awaits a no-op hook, so the numbers are the pipeline's own cost. They vary by machine
and run; compare runs on one machine (say, this checkout against `PYTHONPATH=<other>/src`). The
wiki's Architecture-Internals page has the reference numbers. Not a test: nothing asserts timings.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from interpose.pipeline import Pipeline, Policy


class Mw:
    async def pre(self, ctx: Any) -> None:
        return None

    async def post(self, ctx: Any, value: Any) -> Any:
        return value


async def _noop() -> None:
    return None


def _policy(timeout: float | None) -> Policy:
    return Policy(
        on_error="fail",
        after_order="reverse",
        after_local=True,
        observe_failed=True,
        timeout=timeout,
    )


async def _time(fn: Callable[[], Awaitable[Any]], n: int) -> float:
    for _ in range(min(n, 2000)):  # warm up
        await fn()
    start = time.perf_counter()
    for _ in range(n):
        await fn()
    return (time.perf_counter() - start) / n * 1e6


async def main(n: int) -> None:
    one = Mw()
    five = [Mw() for _ in range(5)]
    single = {t: Pipeline([one], _policy(t)).start() for t in (None, 5.0)}
    chain = {t: Pipeline(five, _policy(t)) for t in (None, 5.0)}

    async def bare_five() -> None:
        for mw in five:
            await mw.pre(None)

    async def unit(t: float | None) -> None:
        # One sluice-shaped unit: start, a before phase, an after phase.
        r = chain[t].start()
        await r.before("pre", None, lambda mw, c: mw.pre(c))
        await r.after("post", None, lambda mw, c, v: mw.post(c, v))

    def call(t: float | None) -> Callable[[], Awaitable[Any]]:
        r = single[t]
        entry = r.entries[0]
        return lambda: r.call(entry, "pre", lambda: one.pre(None))

    def each(t: float | None) -> Callable[[], Awaitable[Any]]:
        r = chain[t].start()
        return lambda: r.each("pre", lambda mw: mw.pre(None))

    cases: list[tuple[str, Callable[[], Awaitable[Any]], int]] = [
        ("bare await of a no-op hook", _noop, n),
        ("run.call, no timeout", call(None), n),
        ("run.call, 5 s timeout", call(5.0), n),
        ("bare loop over 5 middleware", bare_five, n // 4),
        ("run.each over 5 middleware, no timeout", each(None), n // 4),
        ("run.each over 5 middleware, 5 s timeout", each(5.0), n // 4),
        ("unit: start + before + after, 5 mw, no timeout", lambda: unit(None), n // 8),
        ("unit: start + before + after, 5 mw, 5 s timeout", lambda: unit(5.0), n // 8),
    ]
    print(f"Python {sys.version.split()[0]}, n={n}")
    for label, fn, count in cases:
        print(f"{label:52s} {await _time(fn, count):7.2f} µs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=200_000, help="calls per single-hook case")
    asyncio.run(main(parser.parse_args().n))
