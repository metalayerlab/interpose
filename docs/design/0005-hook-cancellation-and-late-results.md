# 0005: Hook cancellation and late results

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: andronics; drafted by Claude

## Context

[0003](0003-pipeline-core.md)'s guard bounds every hook with `asyncio.timeout`. That works by
cancelling the task at the hook's current `await`, which only helps if the hook cooperates. Two ways it
doesn't, and in both `asyncio.timeout` exits cleanly, so the guard accepted the late result as if it
were on time:

1. **Overran.** The deadline passes while the hook runs synchronous code (a blocking call, CPU work)
   and it returns before the loop gets control back. The deadline never fires; the cancellation is
   never delivered.
2. **Swallowed.** The deadline fires, the cancellation is thrown into the hook, and the hook catches
   `CancelledError` (or `BaseException`) and returns normally.

A project that set a limit (mitm-proxy's 5 s `hook_timeout_ms`, sluice's coming `pipeline.timeout_ms`)
gets an answer it said it would not wait for, and nothing reports it. 0003 also never wrote down what
a timeout means for a hook's author. Stephen approved this change on 2026-11-02. It adds to 0003; it
doesn't rewrite it.

## Decision

### The cancellation contract

Written in the module docstring and on the wiki (API-Reference, Architecture-Internals,
Troubleshooting):

- A timeout *cancels* the hook at its current `await`.
- Hooks must be cancel-safe: cleanup goes in `try/finally` or `async with`.
- Hooks must never swallow `CancelledError` or `BaseException`.
- Blocking or CPU-bound work goes off the event loop: a timeout can't interrupt synchronous code.
- A thread started with `run_in_executor` or `asyncio.to_thread` keeps running after its `await` is
  cancelled; the timeout only stops the waiting.

### Late results are timeout failures

When a limit applied and the hook returned normally, the guard asks whether it was late:

- `limit.expired()` is true: the deadline fired, so the hook caught the cancellation (**swallowed**);
- otherwise, the loop clock (`loop.time()`) is at or past `limit.when()`: the hook ran out the deadline
  without yielding (**overran**).

A late result is discarded, before `valid` sees it. The call is a failure with
`timed_out=True` and a `TimeoutError` (naming the hook and the limit, never the result) as its error and
`HookFailed`'s `__cause__`. It is handled by `on_error` like any other timeout: out of the run,
`on_failure` called, `HookFailed` under `"fail"`, `SKIPPED` under `"skip"`. Observers still fail open. A
late result is never an `answers` type, even if the project lists `TimeoutError` there.

`Failure` gains one field, last and defaulted so existing constructors keep working:

```python
Late = Literal["overran", "swallowed"]

late: Late | None = None  # why a result came back after the limit; None for every other failure
```

A hook that is cancelled and raises (the normal case, or catching the cancellation and raising
something else) is unchanged: `timed_out=True`, `late=None`. So is a hook that blocks and then awaits
something that lets the loop fire the deadline: the cancellation arrives there.

### Why two labels are reliable

Both tests use public `asyncio.Timeout` and loop API only (`expired()`, `when()`, `loop.time()`), no
private state. The loop fires the deadline only while the task is suspended, so once `expired()` is
true the cancellation has been thrown into the hook, either into the future it awaits or, if that
future had just finished, on its next step; a normal return after that means the hook caught it. If
`expired()` is false the deadline never fired, so nothing was delivered; a clock past the deadline
then means the hook ran it out without giving the loop a turn. A hook that blocks past the deadline
and then yields with `await asyncio.sleep(0)` counts as overran too: the loop resumes the task before
the timer it has just found due.

### No pending cancellation leaks

`asyncio.timeout`'s exit withdraws its own cancel request (`Task.uncancel()`) whenever the deadline
fired, exception or not; when it never fired there was no request, and the exit cancels the timer. So
after a late result `Task.cancelling()` is back where it was and the caller's next `await` runs
normally, on 3.13 and 3.14 (same `Timeout.__aexit__` in both). Tests assert it. An outer cancellation
is untouched: `CancelledError` is never caught, and it passes through the guard without a failure.

## Alternatives considered

- **Accept late results (today).** The project's limit is silently not a limit.
- **A single label.** The two causes are told apart with public API, and they need different fixes
  in the hook (move blocking work off the loop; stop catching the cancellation), so the log line
  should say which.
- **A new exception type for late results.** `TimeoutError` keeps 0003's rule that a timeout's
  cause is a `TimeoutError`; `late` carries the difference.
- **Watch the loop for blocking (a watchdog thread, `loop.slow_callback_duration`).** Catches the
  overran case only, adds a thread or debug mode, and sluice's `pipeline.slow_ms` already warns about
  loop blocking.

## Consequences

- A project with a limit stops accepting late answers. In mitm-proxy, a hook that blocked past the
  5 s limit, accepted before, is now skipped for the rest of the flow (its `on_error="skip"`) and
  reported through `on_failure`. sluice has no limit yet, so nothing changes there.
- Projects can log `failure.late` to tell a blocking hook from one that swallows cancellation.
- Cost: one `loop.time()` per call, and only when a limit applies.
- Wiki: API-Reference (`Failure.late`, the contract), Architecture-Internals (the guard),
  Troubleshooting (a late timeout), Testing-Guide.

## Later

- **A per-unit deadline** (one budget across all of a unit's hooks, not per call). Stephen deferred
  it on 2026-11-02.
- The guard's per-call cost (#12's second part) is separate work.

## Open questions

None: approved by Stephen on 2026-11-02.
