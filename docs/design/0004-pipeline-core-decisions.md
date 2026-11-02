# 0004: Pipeline core decisions

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: andronics; drafted by Claude

## Context

[0003](0003-pipeline-core.md) shipped `interpose.pipeline` in v0.1.2 with eleven decisions left for
Stephen to confirm, each built with the option that keeps both consumers' behaviour. Implemented
records aren't rewritten, so his answers (2026-11-02) are recorded here.

Both consumers have since adopted the core. sluice's adoption (sluice design 0004) left
`skip_inherited` off because, contrary to 0003's decision 6, turning it on changes behaviour
([#12](https://github.com/metalayerlab/interpose/issues/12)):

1. The override check compared `type(entry.middleware)`, so a hook set on the instance
   (`mw.pre = gate`, which sluice's `as_middleware` accepts) was never called: a gate failed open.
2. A skipped hook returned `SKIPPED`, and `each`/`before` don't ask `until` after a skip. sluice's stop
   is state-based (`until=lambda _: ctx.responded`): when a `param()` callback has already called
   `ctx.respond()`, sluice stops after the first `pre` hook, but with that hook skipped the next
   middleware's `pre` ran.

## Decision

### Stephen's answers to 0003's decisions

| # | 0003 decision | Outcome | interpose change |
|---|---|---|---|
| 1 | No defaults for the four semantics fields | Confirmed as built | None |
| 2 | `"fail"` raises `HookFailed` and the project denies | Confirmed. mitm-proxy answers a fail-closed failure with a 502 "middleware X failed" in both phases (its own adoption change) | None |
| 3 | Observer hooks always fail open | Confirmed as built | None |
| 4 | sluice's `FsError` is an answer (`answers`) | Confirmed as built | None |
| 5 | Sync-hook rejection is opt-in (`require_async`) | Confirmed as opt-in. mitm-proxy turns it on (its change) | None |
| 6 | `skip_inherited` is opt-in | Stays opt-in, and is fixed: see below (#12) | This record |
| 7 | No default timeout for sluice | Confirmed. sluice adds `pipeline.timeout_ms`, default off, failing closed with `EIO` (its change) | None |
| 8 | State isn't renamed | Confirmed as built | None |
| 9 | Names needn't be unique in the core | Confirmed. sluice names duplicate entries distinctly (`name#2`) on its side, so their timings and metrics stay separate | None |
| 10 | No `Snapshot`/TTL middleware in interpose | Confirmed as built | None |
| 11 | A `valid` refusal is a failure (`InvalidResult`) | Confirmed as built | None |

### `skip_inherited` (decision 6, #12)

- **Instance-aware.** A hook counts as overridden if the middleware's class defines it or the
  instance itself has it (`vars(middleware)`); a middleware without an instance `__dict__`
  (`__slots__`) is judged by its class alone.
- **`until` is still asked.** In `each` and `before`, a hook `skip_inherited` leaves out counts as the
  base's no-op returning `None`: it isn't awaited or timed, `before` still records it in `ran`, and
  `until(None)` decides whether the phase stops there. A result-based stop (mitm-proxy's `r is not
  None and ...`) carries on, as it would after a hook that returned `None`; a state-based stop
  (sluice's `ctx.responded`) ends the phase where the awaited hook would have. Elsewhere (`call`,
  `fold`, `after`, observers) an inherited hook still gives `SKIPPED`, which keeps the value.
- The option's contract, now written on `Policy`: the base class's hooks do nothing (return `None`, or
  pass the value through).

With both, turning `skip_inherited` on is the no-op 0003 expected for sluice. It stays opt-in.

## Alternatives considered

- **Document the limitation instead of fixing it** (#12's other option). A gate set on the instance
  that silently never runs is a fail-open trap; the fix is a dictionary lookup.
- **Call `until(SKIPPED)` after an inherited hook.** `SKIPPED` is not `None`, so a result-based
  `until` such as mitm-proxy's would stop on it.
- **Ask `until` after any skip, failures included.** A middleware failed under `"skip"` is out of the
  unit; it shouldn't be able to stop a phase.
- **Make `skip_inherited` the default.** Decision 6 stands: it removes zero-length timings, which
  mitm-proxy keeps.

## Consequences

- sluice can turn `skip_inherited` on with no observable change, which saves a guarded call (about
  2 µs, 0003's Consequences) per hook a middleware leaves alone.
- The guard's own cost per hook call (#12's second part) is not addressed here; it stays open on #12.
- Wiki: API-Reference (`Policy.skip_inherited`), Architecture-Internals (the guard), Testing-Guide.

## Open questions

None: the decisions above are Stephen's; the per-call cost of the guard is open on #12.
