# 0003: Pipeline core

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: andronics; drafted by Claude

## Context

mitm-proxy and sluice each run named async middleware around a unit of work: an HTTP flow
(`pipeline.py`'s `Pipeline`/`FlowPipeline`) and a FUSE call (`middleware.py`'s `run_pipeline`). Strip the
transports away and both are the same loop: select what applies, run before hooks in chain order (any may
answer locally or reject), run the real operation, run after hooks, tell observers how it ended. Around
every hook sits the same wrapper: time it, bound it, catch what it raises.

The differences are deliberate, and each is written down in a design record:

| | mitm-proxy | sluice |
|---|---|---|
| Hook failure | fail open: the middleware is skipped for the rest of the flow (0001) | fail closed: the call fails with an errno (0001) |
| After-hook order | registration order | reverse (the onion) |
| After hooks on a local answer | no: `on_response` never runs after `STOP`/`DENY` (0005) | yes: every middleware whose `pre` ran gets `post` |
| Observers reach a failed middleware | no: `on_done` skips it (0006) | yes: `failed()` reaches every hook that ran |
| Timeout | `middleware.hook_timeout_ms`, 5 s | none (`pipeline.slow_ms` only warns about loop blocking) |
| Rejecting | return `Action.DENY` → 403 (request) or 502 (response), naming the middleware | raise `Deny`/`FsError(errno)` |
| Body hooks | chunk-shaped hooks (`on_request_chunk`, `on_response_chunk`); today called once on the buffered body | none: a read is already a call |
| Timings | `flow.timings.middleware[name]` | none |
| Free state | `flow.metadata` | `ctx.state` |

Each global failure choice is wrong for half the middleware: in mitm-proxy a
crashing blocklist is skipped, so the traffic it should stop gets through; in sluice a crashing access log
fails the user's `read` with `EIO`. The fix is per middleware, `on_error` and a timeout, with the project
default covering middleware that don't say.

interpose's 0001 named this record and set its condition: both projects' "Decided against" lists must be
reconciled before a shared core is approved. That reconciliation is [below](#decided-against-reconciliation).

Constraints:

- **Mechanism only.** The loop, the guard and the policy switches are shared. Hook names and signatures,
  the unit types, selection rules, deny transport (a 502, an errno), loading middleware and logging stay
  in each project.
- **Every consumer's current behaviour is the default.** A project's switch changes no observable
  behaviour until one of its middleware opts in to something else.
- **Policy is settings, never one forced semantics.** interpose has no default for the deliberate
  differences: each project states them.
- No new dependency (stdlib `asyncio`, `inspect`, `time`). No I/O, nothing logged, nothing scheduled as a
  background task.

## Decision

A new module, `interpose.pipeline`.

### Policy: the project's answers

```python
OnError = Literal["skip", "fail"]
Order = Literal["chain", "reverse"]


@dataclass(frozen=True)
class Policy:
    on_error: OnError  # what a failing hook does, unless the middleware says
    after_order: Order  # "chain" (as registered) or "reverse" (the onion)
    after_local: bool  # do after hooks run when a before hook answered locally?
    observe_failed: bool  # do observer hooks still reach a middleware that failed?
    timeout: float | None = None  # seconds per hook call, unless the middleware says
    answers: tuple[type[Exception], ...] = ()  # raised as a hook's answer, not a failure
    skip_inherited: type | None = None  # the base class; un-overridden hooks aren't called
```

The first four fields are required. Each project's policy is a constant in its own code:

```python
# mitm-proxy: today's behaviour
POLICY = Policy(
    on_error="skip",
    after_order="chain",
    after_local=False,
    observe_failed=False,
    timeout=config.hook_timeout_ms / 1000,
)

# sluice: today's behaviour
POLICY = Policy(
    on_error="fail",
    after_order="reverse",
    after_local=True,
    observe_failed=True,
    answers=(FsError,),
    skip_inherited=Middleware,
)
```

- **`on_error="skip"`**: the failing middleware is out for the rest of the unit (every later hook,
  chunk hooks included), and the unit carries on: mitm-proxy's rule.
- **`on_error="fail"`**: `HookFailed` is raised to the project, which answers with its own deny: sluice's
  errno, or (for a mitm-proxy middleware that opts in) a deny response naming the middleware. It never
  kills a connection and never retries.
- **`answers`** are how a hook says no in a project where saying no is raising (sluice's `Deny` is an
  `FsError`). They pass through unchanged, whatever `on_error` says, so a sluice middleware marked
  `on_error="skip"` still denies. Everything else a hook raises is a failure.
- **`skip_inherited`** makes a hook a middleware doesn't override cost nothing: it isn't awaited, timed or
  counted (the synergy note's "sluice: skip hooks a middleware doesn't override"). Off unless set, because
  it removes zero-length entries from timings.

### Entries: per-middleware policy and the shared name

```python
@dataclass(frozen=True, eq=False)
class Entry[M]:
    middleware: M
    name: str
    on_error: OnError | None = None  # None: the project's
    timeout: float | None = None  # None: the project's

    @classmethod
    def of(cls, middleware: M, name: str | None = None) -> Entry[M]: ...


def name_of(middleware: object) -> str: ...  # its `name` attribute, else its class name
```

- `Entry.of` reads the middleware's own `name`, `on_error` and `timeout` attributes, so a middleware
  declares its role where it is written:

  ```python
  class Blocklist(Middleware):
      on_error = "fail"  # a gate that fails open is no gate
  ```

  A project may also build `Entry(...)` itself, from configuration (mitm-proxy names middleware by its
  registry key, so it passes `name=`).
- **The name** labels timings, failures, logs and metrics. It is the `name` attribute, else the class
  name: one rule for both projects.
- Entries compare by identity, so the same class twice in a chain (two `hide` rules on different routes)
  is two entries. Names needn't be unique in the core; mitm-proxy keeps its registry's uniqueness check.

### The pipeline and one unit's run

```python
class Pipeline[M]:
    def __init__(self, middleware: Iterable[M | Entry[M]], policy: Policy) -> None: ...

    entries: tuple[Entry[M], ...]
    policy: Policy
    names: list[str]

    def start(
        self, *, select=None, select_hook="select", on_failure=None, timings=None, state=None
    ) -> Run[M]: ...


class Run[M]:
    entries: list[Entry[M]]  # what applies to this unit, decided once at start()
    active: list[Entry[M]]  # ... minus what has failed
    timings: dict[str, float]  # seconds per middleware name, every hook call added in
    state: MutableMapping[str, Any]  # the unit's free state
    ran: list[tuple[Entry[M], Any]]  # (entry, context) for every before hook called
    stopped: Stopped[M] | None  # where a before phase stopped, if it did

    async def call(self, entry, hook, invoke, *, valid=None): ...  # one guarded call
    async def each(self, hook, invoke, *, entries=None, until=None, valid=None): ...
    async def fold(self, hook, value, invoke, *, entries=None, valid=None, keep_none=False): ...
    async def observe(self, hook, invoke, *, entries=None): ...
    async def before(self, hook, context, invoke, *, entries=None, until=None, valid=None): ...
    def after_pairs(self) -> list[tuple[Entry[M], Any]]: ...
    async def after(self, hook, value, invoke, *, valid=None, keep_none=False): ...
    async def observe_ran(self, hook, invoke, *, pairs=None): ...
    def scoped(self, entry) -> ScopedState: ...
```

interpose calls no hook by name on its own: the project passes `invoke`, a small lambda that calls its
hook with its own arguments (`lambda mw: mw.on_request(flow)`, `lambda mw, ctx, result: mw.post(ctx,
result)`). The `hook` string names it in failures and timings, and is what `skip_inherited` looks up.

- **`start(select=...)`** picks what applies, once per unit (mitm-proxy's `applies_to`, sluice's `ops`
  filter). A `select` that raises fails like a hook called `select_hook`.
- **`each`**: the hook on each entry (default `active`) in chain order, until `until(result)` says stop;
  returns `Stopped(entry, result)` or `None`. mitm-proxy's `on_request` and `on_response` phases.
- **`fold`**: threads a value through the hook; each result replaces it, and with `keep_none` a `None`
  keeps it. mitm-proxy's chunk hooks (`None` passes the chunk on).
- **`observe`**: a hook that can't change the outcome (`on_done`). **Always fails open**, whatever
  `on_error` says, and `answers` count as failures here: the outcome is already fixed, so there is
  nothing left to protect. Reaches failed middleware only with `observe_failed`.
- **`before`**: `each`, but records `(entry, context)` in `ran` *before* each call, so observers reach a
  middleware even if its own before hook failed (sluice's `failed()` rule, with `observe_failed`), and
  keeps a stop in `stopped`. The after phase leaves out what failed: a skipped middleware is out for the
  rest of the unit. Called again with another context and chain (sluice's rename target), `ran` accumulates.
- **`after_pairs()`**: what ran, in `after_order`, minus what failed; nothing if a before hook stopped the
  unit and `after_local` is false. **`after`** folds a value over those pairs, each with its own context:
  sluice's `post`.
- **`observe_ran`**: an observer over what ran (local answer or not), in `after_order`: sluice's
  `failed()`.
- **`scoped(entry)`**: a `MutableMapping` view of `state` whose keys are stored as `"<name>.<key>"`, the
  one naming rule for shared state. A project passes its own dict (`ctx.state`, `flow.metadata`) as
  `state=`, so nothing is renamed.

### The guard around every hook

Every call goes through one guard:

1. **Skip** if the entry has failed (observers: unless `observe_failed`), or if `skip_inherited` says the
   hook isn't overridden. The result is `SKIPPED`.
2. **Time** the call into `timings[entry.name]` (`time.perf_counter`), failed calls included, as
   mitm-proxy does.
3. **Bound** it with `asyncio.timeout(entry.timeout or policy.timeout)`; an entry's `NO_LIMIT` (`math.inf`)
   opts it out of the project's limit. Only the guard's own limit counts
   as a timeout (`expired()`): a `TimeoutError` the hook raises from its own network call is an ordinary
   failure.
4. **Catch** `Exception` (never `CancelledError`, `KeyboardInterrupt` or `SystemExit`). An `answers` type
   is re-raised as it is. Anything else, or a result `valid(result)` refuses (`InvalidResult`, which names
   the result's type, never its value), or a `valid` that itself raises, is a **failure**:
   - the entry is out of the run (`active` drops it);
   - `on_failure(Failure(middleware, hook, error, timed_out, action, timeout))` is called (`timeout` is
     the limit that applied), so the project logs and
     counts it (mitm-proxy's `log.exception` and `middleware_failures_total`);
   - under `"fail"`, `HookFailed(failure)` is raised with the hook's exception as its `__cause__`; under
     `"skip"`, the result is `SKIPPED`.

Every hook is awaited in turn on the caller's task: no `create_task`, no gather. That is what carries
backpressure from a slow hook back to the reader (mitm-proxy 0001, 0006). A hook that blocks the event
loop can't be timed out; both projects forbid it, and so does this module's docstring.

`require_async(middleware, hooks, *, name=None)` raises `TypeError` for a hook that isn't `async def`. It
is a separate function, for a project that wants sync hooks refused at registration (sluice's
`check_coroutine`) rather than failing the first live unit.

### What stays in the projects

- Hook names and signatures, the unit types, `Action`/`STOP`/`DENY`, `ctx.respond()`.
- What a deny is: mitm-proxy's 502 naming the middleware (or, later, a status the middleware chose),
  sluice's errno mapping and its `_NOT_FS_ERRORS` subtlety.
- Logging (with the unit ID: `flow.id`, `correlation_id`) and metrics, both from `on_failure`.
- Loading middleware (mitm-proxy's registry, sluice's app module), routing, mutation-capability,
  oversize-body skipping, two-path units.
- Settings: a per-middleware `on_error` or timeout from configuration (say, mitm-proxy's
  `middleware.<name>.on_error`) is a project setting that builds an `Entry`. interpose's `Policy.timeout`
  is in seconds; a project's setting stays integer `_ms` (interpose's "no float setting type").

## Decided-against reconciliation

Every "Decided against" entry in both projects that touches a pipeline, and interpose's own, with how this
record accommodates it. Entries about transports, TLS, routing or deployment only (mitm-proxy's upstream
TLS, scope, CA, control plane; sluice's fusepy) don't touch the core and aren't listed.

| Project | Decided against (source) | How 0003 accommodates it | Why that is enough |
|---|---|---|---|
| mitm-proxy | Kill or retry the flow on middleware failure: too disruptive, undefined after side effects (0001) | mitm-proxy's policy is `on_error="skip"`. `"fail"` raises `HookFailed` to the project, which answers with a deny response; nothing kills a connection or retries a hook, anywhere | Failing closed is *answering* (a 403/502), not killing. 0001 itself notes "in a security-enforcement setting, deny-on-error might be better"; only a middleware that declares `on_error="fail"` gets it |
| mitm-proxy | Priority-number or DAG middleware ordering (0001) | Order is the chain, a plain sequence. `after_order` is that sequence forwards or backwards, no numbers | No ordering input besides the list |
| mitm-proxy | Buffered-only middleware hooks (0001) | interpose defines no hook signatures. Chunk hooks are `fold` calls the project makes, once per chunk (today once per buffered body) | The chunk-shaped API stays, and streaming stays mitm-proxy's to add |
| mitm-proxy | Runtime mutation detection and re-buffering (0001) | Not in the core: `is_mutation_capable` stays in mitm-proxy | The core never inspects bodies |
| mitm-proxy | Custom backpressure/windowing layer (0001) | Every hook is awaited in turn on the caller's task; nothing buffers or schedules | `drain()` and h2 flow control keep doing the work |
| mitm-proxy | Resetting the stream on response DENY (0001) | The core has no transport; `HookFailed` and `Stopped` go to the project, which sends its 502 | Unchanged |
| mitm-proxy | Running `on_response` for flows middleware answered itself (0005) | `after_local=False` in mitm-proxy's policy; required, no default | `after_pairs()` is empty after a stop |
| mitm-proxy | Calling `on_done` from background tasks (0006) | No `create_task` anywhere; observers are awaited like every hook, under the timeout | Same as today |
| mitm-proxy | Middleware code or module paths in managed config (0002) | The core loads nothing; it takes objects | Loading stays local |
| sluice | Rewrite-as-middleware for the real path (0001) | The core doesn't route or resolve paths | Unchanged |
| sluice | Per-call env/secret resolution: I/O on every call (0001) | The core does no I/O; a `Pipeline` and its policy are built once and frozen | No per-call work beyond the guard |
| sluice | Built-in middleware in the package (`auth()`, `access_log()`, ...) (0002) | interpose ships no middleware, not even a generic `Snapshot`/TTL one | Policies ship as project examples |
| sluice | A `sluice.contrib` subpackage (0002) | No `interpose.contrib`, no preset policies: each project's `Policy` lives in its own code | Same API-surface rule |
| sluice | `next()`-wrapping middleware (0001, alternatives) | Phase hooks only (`before`/`after`/observers); a middleware can't forget to call the operation | Structurally distinct pre and post, as sluice chose |
| sluice | Per-op replacement hooks (`on_read`) (0001, alternatives) | Hook names are the project's; the core composes them as phases, not replacements | Unchanged |
| interpose | Sharing the pipelines' semantics (idea note, 0001) | `on_error`, `after_order`, `after_local`, `observe_failed` are required fields | No project inherits another's semantics by omission |
| interpose | A float setting type (0001) | `timeout` is a `Policy`/`Entry` field in seconds, not a `Setting` | Projects keep `_ms` int settings |

Conflicts between the projects (not within one) are the table in [Context](#context). Each row is a
`Policy` field, and each project keeps its own answer.

## Decisions for Stephen to confirm

Each changes nothing a consumer can observe by default; each can be reversed before a consumer adopts this.

1. **No defaults for the four semantics fields.** A project must write `on_error`, `after_order`,
   `after_local` and `observe_failed`. Recommended: a default would quietly hand one project's semantics to
   the next.
2. **`"fail"` raises `HookFailed` and the project denies.** Only for a middleware that opts in with
   `on_error = "fail"`; mitm-proxy's own default stays `"skip"`. This is the one place 0003 touches
   mitm-proxy's "kill or retry" entry: it is a deny answer, not a kill, as the failure-policy note
   requires. The recommended answer in mitm-proxy is a 502 naming the middleware ("middleware X failed"),
   in both phases, so a crash stays distinguishable from a deliberate request-phase 403; that status is
   new behaviour (nothing fails closed today) and is mitm-proxy's adoption record to settle. A selection
   (`applies_to`) failure of a `"fail"` middleware raises from `start()`, before any hook ran, and gets the
   same answer.
3. **Observer hooks always fail open** (`on_done`, `failed`), whatever `on_error` says. Today's behaviour
   in both projects.
4. **sluice's `FsError` is an answer** (`answers=(FsError,)`): a deliberate deny passes through even from
   a middleware marked `on_error="skip"`.
5. **Sync-hook rejection is opt-in** (`require_async`). sluice already rejects sync hooks; mitm-proxy today
   lets one fail at run time (skipped), and adopting the check would turn that into a startup error. Kept
   out of the default so mitm-proxy's behaviour doesn't change.
6. **`skip_inherited` is opt-in.** sluice can turn it on with no observable change (its default hooks do
   nothing); mitm-proxy keeps calling every hook, so its timings keep every middleware.
7. **No default timeout for sluice.** The synergy note recommends one (fail closed, `ETIMEDOUT`/`EIO`);
   that is a new setting in sluice and a change in behaviour, so it is left to sluice's adoption record.
8. **State isn't renamed.** mitm-proxy keeps `flow.metadata`, sluice keeps `ctx.state`; each passes its
   dict as `state=`. `scoped()` offers the `"<name>.key"` rule without imposing it.
9. **Names needn't be unique in the core.** sluice can have the same class twice in a chain; mitm-proxy
   keeps its registry check. Timings for two entries with the same name add up.
10. **No `Snapshot`/TTL middleware in interpose.** The pipeline note suggests one; sluice's "no built-in
    middleware" and interpose's "mechanism only" say no. It would be a sluice example.
11. **A `valid` refusal is a failure** (`InvalidResult`), handled by `on_error` like an exception:
    mitm-proxy's "returned something that isn't an Action" rule, now available to both.

## Alternatives considered

- **Share one pipeline with one semantics.** Rejected in the idea note and by both projects' records:
  fail-open/streaming and fail-closed/per-call are both right for their units.
- **Project-wide policy only, no per-middleware `on_error`/timeout.** Preserves behaviour but fixes
  nothing: the blocklist and access-log failures stay. Per-middleware policy is the point.
- **A class hierarchy with hook methods in interpose** (`interpose.Middleware` with `pre`/`post`).
  Rejected: the hook vocabularies differ (`on_request`/`on_response_chunk` vs `pre`/`post`/`failed`), and
  forcing one would rename every consumer's middleware API. The core takes `invoke` callables instead.
- **`next()`-style onion** (Koa, ASGI). Rejected by sluice's 0001; mitm-proxy's phases don't fit it.
- **Retry on failure.** Rejected by mitm-proxy's 0001: undefined after side effects, and it doesn't fix a
  hang.
- **Errors mapped inside interpose** (an `error_mapper` policy returning a 502 or an errno). Rejected: the
  result type is transport (a response, an errno), and `HookFailed` carries everything a mapper needs.
- **Log failures in interpose.** Rejected: the project's log line needs its unit ID and its own
  vocabulary, and "never log values" is easier to keep with one logging site per project.
- **A shared `Snapshot` middleware.** See decision 10.

## Consequences

- One loop, one guard, one place where failure policy, timeouts and timings are implemented and tested.
  Per-middleware `on_error` and timeout become available to both projects as soon as they adopt it.
- Cost per hook call: an `asyncio.timeout` context and two clock reads, which mitm-proxy already pays.
  Measured on the workstation (Python 3.13), a guarded no-op hook takes about 2 µs against 0.1 µs bare:
  about 10 µs per sluice call with five middleware, small next to a FUSE round trip. sluice gains the
  timing it lacked; `skip_inherited` lets it stop awaiting hooks that do nothing.
- **No new dependency.**
- Wiki: API-Reference (the module), Architecture-Internals (the guard and the policies), Introduction,
  Testing-Guide.
- `TODO.md` "Decided against": an interpose `Middleware` base class; mapping errors inside interpose;
  defaults for the semantics fields.

### Consumer adoption

What each project's switch will need (each writes its own design record first; this is the brief):

**mitm-proxy**

1. Bump the pin. Keep `Middleware`, `Action`, `Entry`'s `mutation_capable`, `load_middleware` and
   `FlowPipeline`'s public methods (`run_request`, `run_request_chunk`, `run_response`,
   `run_response_chunk`, `run_done`, `applies`, `mutation_capable`, `request_skipped`,
   `response_skipped`, `failed`) as thin wrappers over a `Run`, so `exchange.py` and its tests don't change.
2. `Pipeline.__init__` builds an `interpose.pipeline.Pipeline` from `Entry.of(mw, name=registry_name)`
   with `Policy(on_error="skip", after_order="chain", after_local=False, observe_failed=False,
   timeout=hook_timeout)`.
3. `start(flow, on_failure)` → `start(select=lambda mw: mw.applies_to(flow), select_hook="applies_to",
   on_failure=..., timings=flow.timings.middleware, state=flow.metadata)`. The `on_failure` callback logs
   (`log.error(..., exc_info=failure.error)` with `flow.id`; "timed out after {failure.timeout}s" when
   `failure.timed_out`) and calls `metrics.middleware_failures.inc(failure.middleware)`.
4. `run_request` → `before("on_request", flow, lambda mw, f: mw.on_request(f), until=lambda r: r is not
   None and r is not Action.CONTINUE, valid=lambda r: r is None or isinstance(r, Action))`, mapping
   `Stopped` to `Outcome(action, by=entry.name)`. `run_response` → the same with `each("on_response",
   ..., entries=[e for e, _ in run.after_pairs()])`, so `after_local=False` is what keeps `on_response`
   off local answers (0005) rather than only `exchange.py`'s early return. An oversize request body sets
   both `request_skipped` and `response_skipped` (`_oversize`), so both phases are skipped there, as
   today.
5. Chunk hooks → `fold(hook, chunk, invoke, keep_none=True)`, where `invoke` awaits
   `mw.on_request_chunk(flow, c, is_last)` and returns `None` or `bytes(result)`, so every middleware still
   gets `bytes` from the one before (today's `chunk = bytes(result)` per middleware); a result `bytes()`
   can't take (a `str`) fails inside the guard, as the "not bytes" check does today.
6. `run_done` → `observe("on_done", lambda mw: mw.on_done(flow))`: the selected entries, where
   `observe_failed=False` skips the failed ones (0006).
7. New, opt-in: a middleware's `on_error = "fail"` makes `run_*` raise `HookFailed`, and `start()` too
   when its `applies_to` raises; `exchange.py` answers it with a deny naming `failure.middleware`
   (decision 2) and runs `run_done` when a run exists. Needs a test that a crashing gate blocks the flow.
   A per-middleware `timeout` attribute (or `NO_LIMIT`) works the same way.
8. Log wording for an invalid result changes from the result's `repr` to its type name (never log values).
9. Audit existing middleware for attributes called `name`, `on_error` or `timeout` that mean something
   else (an upstream client's `timeout`, say): `Entry.of` reads them. Rename them, or build the `Entry`
   explicitly.

**sluice**

1. Bump the pin. `run_pipeline(ctx, operation, also=...)` keeps its signature and builds a run per call
   from a `Pipeline` frozen per route resolution (at `freeze()` time, not per call) with
   `Policy(on_error="fail", after_order="reverse", after_local=True, observe_failed=True,
   answers=(FsError,), skip_inherited=Middleware)`.
2. `select=lambda mw: mw.applies(ctx.op)`; `state=ctx.state`.
3. Pre: `before("pre", ctx, lambda mw, c: mw.pre(c), until=lambda _: ctx.responded)`, then for `also`
   the same with the other context and chain (`entries=`), unless stopped.
4. Post: `after("post", result, lambda mw, c, r: mw.post(c, r))`.
5. Failure path: catch `HookFailed` and map `failure.error` (its `__cause__`) through `_as_fs_error` (so a
   hook's `OSError` keeps its errno and `_NOT_FS_ERRORS` still apply); a timeout maps to whatever its
   record decides (`ETIMEDOUT` or `EIO`). Then `observe_ran("failed", lambda mw, c: mw.failed(c, err))`;
   for a `param()` failure, pass the whole chain as `pairs=`.
6. `failed()` exceptions: today `log.exception`ed and ignored; with `observe_ran` they reach `on_failure`,
   which logs them the same way. `on_failure` also sees the `"fail"` failure that ends the call, which
   `_as_fs_error` already logs: log only `action == "skip"` failures there, or stop logging in the mapper.
7. New, opt-in: `on_error = "skip"` on an observer-like middleware (the access-log example) so its crash
   no longer fails the user's call; a timeout setting (decision 7).
8. Optional: `require_async` can back `check_coroutine`, re-raised as `RouteError` so a sync hook stays a
   one-line exit-2 error.
9. The same attribute audit as mitm-proxy's item 9; sluice's `FunctionMiddleware` gets the wrapped
   function's name (today its `repr` is the function's `__qualname__`).

## Open questions

None open: the calls that are Stephen's are listed under
[Decisions for Stephen to confirm](#decisions-for-stephen-to-confirm), each implemented with the option
that keeps every consumer's behaviour.

