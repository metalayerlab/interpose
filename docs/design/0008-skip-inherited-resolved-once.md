# 0008: `skip_inherited` resolved once

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: andronics; drafted by Claude

## Context

`skip_inherited` ([0004](0004-pipeline-core-decisions.md), decision 6) asked "does this entry
override `hook`?" on every guarded call: an instance `__dict__` lookup and two `getattr`s. Since
[0006](0006-guard-hot-path.md) made a guarded no-op await about 0.6 µs, that check costs more than the
await it skips, and sluice measured `skip_inherited` making calls slower (its record 0008) and turned it
back off ([#25](https://github.com/metalayerlab/interpose/issues/25)). Stephen decided on 2026-11-02 to
fix the cost here so sluice can try again, with no other change in behaviour.

## Decision

No public interface change.

1. **Resolved when the `Pipeline` is built.** With `skip_inherited` set, the pipeline works out, per
   entry, the set of hooks it leaves to the base: every name the base class defines (`dir(base)`) that
   the middleware neither overrides in its class nor sets on the instance (0004's rule, unchanged). The
   guard then asks a dict and a set. An entry the pipeline wasn't built with (a project changed
   `Run.entries`) is resolved on its first call and cached the same way.
2. **A hook assigned after the build isn't seen.** In practice instance hooks (sluice's `as_middleware`)
   are set before the chain is frozen, which 0004's fix (#12) keeps honouring. One assigned on the
   instance or its class afterwards is not: written on `Policy` and in the wiki.
3. **Still opt-in.** Decision 6 stands: `skip_inherited` drops the zero-length timings mitm-proxy keeps.
4. **`benchmarks/bench-pipeline.py`** gains sluice's two chains, with and without `skip_inherited`.

### Measured

Same workstation, Python 3.13, a unit (`start` + `before` + `after`) through five middleware, no
timeout, best of three, two runs:

| Chain | every hook awaited | `skip_inherited`, per call | `skip_inherited`, resolved once |
|---|---|---|---|
| typical (2 of 10 hooks inherited) | 8.0–8.5 µs | 9.6–10.3 µs | 7.8–8.2 µs |
| five `pre`-only gates (5 of 10 inherited) | 7.9–8.3 µs | 8.9 µs | 6.7 µs |

## Alternatives considered

- **Resolve lazily per `(entry, hook)`.** Equally cheap, but a hook set after the build would be seen
  until its first call and not after: a rule that depends on timing. Resolving at the build gives one.
- **Keep the per-call check and make it cheaper.** It would still cost about what it saves.
- **Make `skip_inherited` the default now that it is cheap.** Decision 6 is about timings, not cost.

## Consequences

- sluice can turn `skip_inherited` back on: it now ties or beats awaiting every hook.
- A project that assigns a hook on a middleware after building its `Pipeline` must rebuild it.
- Wiki: API-Reference (`Policy.skip_inherited`), Architecture-Internals (the guard, Performance),
  Testing-Guide.

## Open questions

None: decided by Stephen on 2026-11-02 (#25).
