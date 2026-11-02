# 0006: Guard hot path

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: andronics; drafted by Claude

## Context

The guard around every hook ([0003](0003-pipeline-core.md)) cost about 1.6 µs per call with no
timeout, against 0.09 µs for a bare `await`. sluice measured about 28 µs per call through
`run_pipeline` with five middleware, against 5 µs before adopting it
([#12](https://github.com/metalayerlab/interpose/issues/12), its second part). Stephen approved the
work on 2026-11-02, as a performance change with no change in behaviour, so that sluice can turn on
`skip_inherited` ([0004](0004-pipeline-core-decisions.md)) and pay for the guard only on hooks that do
something.

The cost, measured on v0.1.2 with a no-op hook (Python 3.13):

| Part | About |
|---|---|
| `asyncio.timeout(None)`, entered and exited | 0.75 µs |
| `call` → `_guard` (two coroutine frames) plus a `partial` per call | 0.3–0.5 µs |
| two `perf_counter` reads and the timings update | 0.16 µs |
| rebuilding `Run.active` | 0.3 µs per phase |

## Decision

No public interface changes; `call`, `each`, `fold`, `observe`, `before`, `after` and `observe_ran`
keep their signatures and results.

1. **No `asyncio.timeout` without a limit.** When neither the entry nor the policy sets a timeout (or
   the entry's is `NO_LIMIT`), the guard awaits the hook directly. A context that can never fire
   changes nothing; 0005's late-result check still runs whenever a limit applies.
2. **One coroutine frame per call.** Every phase awaits the private `_guard` directly, passing the
   hook's arguments as a tuple (`invoke(*args)`) instead of a `partial`. `call` stays a thin wrapper
   for projects that call it. The policy lookups move off the success path: the failure action is only
   worked out when a hook fails, and `_inherited` is only asked when `skip_inherited` is set. A hook
   `skip_inherited` leaves out comes back as a private marker, which `each`/`before` read as `None` and
   everything else as `SKIPPED`, as before (0004).
3. **`Run.active` is cached.** It is rebuilt when an entry fails, or when `entries` no longer equals
   the copy it was built from (`start()` appends; a project may change the list). Comparing two short
   lists of identity-compared entries is cheaper than rebuilding. The `active` property still returns
   a new list, so a caller that changes it changes nothing in the run.
4. **`scripts/bench-pipeline.py`**, stdlib only: microseconds per call for a bare await, `call` with
   and without a timeout, `each` over five middleware, and a sluice-shaped unit (`start`, `before`,
   `after`). Run it with `PYTHONPATH=<other checkout>/src` to compare. No test asserts timings.

### Measured

Same workstation, Python 3.13, a no-op hook, two runs each:

| Case | v0.1.4 | v0.1.5 |
|---|---|---|
| bare `await` | 0.09 µs | 0.09 µs |
| `call`, no timeout | 1.5–1.6 µs | 0.7 µs |
| `call`, 5 s timeout | 3.3–3.5 µs | 3.2–3.5 µs |
| `each` over five middleware, no timeout (bare loop 0.5 µs) | 9.1–9.5 µs | 2.9–3.0 µs |
| `each` over five middleware, 5 s timeout | 20.4–20.8 µs | 17.5–18.2 µs |
| unit: `start` + `before` + `after`, five middleware, no timeout | 20.4–20.5 µs | 7.6–8.3 µs |

About 0.6 µs per guarded call inside a phase, and 3 µs for five middleware: the targets. With a limit,
`asyncio.timeout` itself (scheduling and cancelling a timer) is most of the cost and isn't changed here.

## Alternatives considered

- **A cheaper timeout (one timer per unit, or `loop.call_at` by hand).** The per-unit deadline is
  deferred (0005, "Later"), and a hand-made timer would re-implement `asyncio.timeout`'s
  cancel/uncancel accounting that 0005 relies on.
- **Make `call` the guard itself.** Its public form takes a zero-argument `invoke`; the phases need
  arguments. Keeping `call` a wrapper costs one frame only for projects that use `call` directly.
- **Cache `active` by length.** A project replacing an entry in place would see a stale list.
- **Timing assertions in tests.** Flaky across machines and CI runners; the bench script is the
  measure, and the tests check behaviour only.

## Consequences

- sluice can turn on `skip_inherited` and stop paying for hooks its middleware leave alone; every
  remaining guarded call costs about a third of what it did.
- Behaviour is unchanged, checked by the existing tests and new ones for the `active` cache. One edge
  only: with no limit, a hook no longer needs to run inside an asyncio task (`asyncio.timeout`
  required one); under asyncio every coroutine already does.
- Wiki: Architecture-Internals (Performance, with these numbers), Testing-Guide (the bench script).

## Open questions

None: approved by Stephen on 2026-11-02.
