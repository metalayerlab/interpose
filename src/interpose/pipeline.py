"""The middleware pipeline's mechanism: the loop, the guard around every hook, and the policies
that make one project fail open and another fail closed (design 0003).

interpose defines no hooks and no unit of work. A project keeps its own `Middleware` base class
(mitm-proxy's `on_request`/`on_response_chunk`/`on_done`, sluice's `pre`/`post`/`failed`) and its
own unit (a `Flow`, a `Context`), and drives a `Run` with them:

- `each` runs one hook over the chain until a result says stop (a before phase, or mitm-proxy's
  `on_response`);
- `fold` threads a value through a hook (a body chunk, a result);
- `observe` runs a hook that can't change the outcome (`on_done`, `failed`): always fail open;
- `before`/`after`/`observe_ran` do the same while remembering which middleware ran, with which
  context, so the after phase reaches exactly those, in the project's order.

Every hook call goes through one guard: it is timed into `Run.timings` under the middleware's
name, bounded by its timeout, and what it raises is either an answer the project declared
(`Policy.answers`, re-raised as it is) or a failure. A failure is reported to `on_failure` and
then, by the middleware's `on_error` (else the project's), either skips that middleware for the
rest of the unit or raises `HookFailed` for the project to turn into its own deny (a 502, an
errno). Nothing is retried, nothing is killed, nothing is scheduled as a background task: every
hook is awaited in turn, which is what carries backpressure. Nothing here logs or does I/O.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, MutableMapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, cast, overload

__all__ = [
    "NO_LIMIT",
    "SKIPPED",
    "Entry",
    "Failure",
    "HookFailed",
    "InvalidResult",
    "OnError",
    "Order",
    "Pipeline",
    "Policy",
    "Run",
    "ScopedState",
    "Stopped",
    "name_of",
    "require_async",
]

OnError = Literal["skip", "fail"]
Order = Literal["chain", "reverse"]

_ON_ERROR = ("skip", "fail")
_ORDER = ("chain", "reverse")


class _Skipped:
    """What a guarded call gives when the hook didn't produce a result. One instance: `SKIPPED`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "SKIPPED"

    def __bool__(self) -> bool:
        return False


SKIPPED = _Skipped()


def _check_timeout(timeout: object, where: str) -> None:
    if timeout is None:
        return
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not timeout > 0:
        raise ValueError(f"{where}: timeout must be a number of seconds above 0, or None")


NO_LIMIT = math.inf  # an entry's timeout that opts out of the project's


def _check_on_error(on_error: object, where: str, *, optional: bool) -> None:
    if on_error is None and optional:
        return
    if on_error not in _ON_ERROR:
        raise ValueError(f"{where}: on_error must be 'skip' or 'fail'")


@dataclass(frozen=True)
class Policy:
    """A project's answers to the questions its pipeline must settle. The first four have no
    default: interpose doesn't pick fail-open or fail-closed for anyone.

    - `on_error`: what a failing hook does, unless the middleware says: `"skip"` (it is out for
      the rest of the unit, which carries on) or `"fail"` (`HookFailed` is raised).
    - `after_order`: the order of after hooks, `"chain"` (as registered) or `"reverse"` (the
      onion: the first in is the last out).
    - `after_local`: whether after hooks run when a before hook answered locally (stopped).
    - `observe_failed`: whether observer hooks (`observe`, `observe_ran`) still reach a
      middleware that has failed.
    - `timeout`: seconds per hook call, unless the middleware says; `None` is no limit.
    - `answers`: exception types a hook raises *as its answer* (sluice's `FsError`), re-raised
      unchanged whatever `on_error` says. Everything else a hook raises is a failure.
    - `skip_inherited`: the project's base class, whose hooks must do nothing; a hook a
      middleware doesn't override (in its class or on the instance) isn't called at all (no
      timing). In `each` and `before` it counts as having returned `None`, so `until` is still
      asked; elsewhere it gives `SKIPPED`. `None` calls every hook.
    """

    on_error: OnError
    after_order: Order
    after_local: bool
    observe_failed: bool
    timeout: float | None = None
    answers: tuple[type[Exception], ...] = ()
    skip_inherited: type | None = None

    def __post_init__(self) -> None:
        _check_on_error(self.on_error, "policy", optional=False)
        if self.after_order not in _ORDER:
            raise ValueError("policy: after_order must be 'chain' or 'reverse'")
        for flag in ("after_local", "observe_failed"):
            if not isinstance(getattr(self, flag), bool):
                raise ValueError(f"policy: {flag} must be a bool")
        _check_timeout(self.timeout, "policy")
        if not isinstance(self.answers, tuple) or not all(
            isinstance(a, type) and issubclass(a, Exception) for a in self.answers
        ):
            raise ValueError("policy: answers must be a tuple of Exception subclasses")
        if self.skip_inherited is not None and not isinstance(self.skip_inherited, type):
            raise ValueError("policy: skip_inherited must be a class or None")


def name_of(middleware: object) -> str:
    """A middleware's name: its `name` attribute if it is a non-empty str, else its class name."""
    name = getattr(middleware, "name", None)
    return name if isinstance(name, str) and name else type(middleware).__name__


@dataclass(frozen=True, eq=False)
class Entry[M]:
    """One middleware in a chain, with its name and its own policy (`None`: the project's).

    `timeout=NO_LIMIT` opts one middleware out of the project's timeout.

    Compared by identity: the same middleware twice in a chain is two entries.
    """

    middleware: M
    name: str
    on_error: OnError | None = None
    timeout: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a middleware needs a name")
        _check_on_error(self.on_error, f"middleware {self.name!r}", optional=True)
        _check_timeout(self.timeout, f"middleware {self.name!r}")

    @classmethod
    def of(cls, middleware: M, name: str | None = None) -> Entry[M]:
        """An entry from the middleware's own `name`, `on_error` and `timeout` attributes.

        `name`, if given, wins (mitm-proxy names middleware by its registry key).
        """
        return cls(
            middleware,
            name or name_of(middleware),
            getattr(middleware, "on_error", None),
            getattr(middleware, "timeout", None),
        )


@dataclass(frozen=True)
class Failure:
    """A hook that raised, timed out or returned something invalid. Holds no unit data."""

    middleware: str
    hook: str
    error: Exception
    timed_out: bool
    action: OnError  # what the pipeline did about it
    timeout: float | None = None  # the limit that applied to the call, in seconds


class HookFailed(Exception):
    """A hook failed under `on_error="fail"`. The project turns this into its deny.

    `failure` says which middleware and hook; `__cause__` is what the hook raised (a
    `TimeoutError` for a timeout). The message names the middleware and hook, never a value.
    """

    def __init__(self, failure: Failure) -> None:
        self.failure = failure
        verb = "timed out" if failure.timed_out else "failed"
        super().__init__(f"middleware {failure.middleware!r} {verb} in {failure.hook}")


class InvalidResult(Exception):
    """A hook returned a result the project's `valid` check refused. Names the type only."""


@dataclass(frozen=True)
class Stopped[M]:
    """A phase stopped early: which entry stopped it, and the result that did."""

    entry: Entry[M]
    result: Any


class ScopedState(MutableMapping[str, Any]):
    """One middleware's view of the unit's state: its keys are stored as `"<name>.<key>"`."""

    def __init__(self, state: MutableMapping[str, Any], name: str) -> None:
        self._state = state
        self._prefix = f"{name}."

    def __getitem__(self, key: str) -> Any:
        return self._state[self._prefix + key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._state[self._prefix + key] = value

    def __delitem__(self, key: str) -> None:
        del self._state[self._prefix + key]

    def __iter__(self) -> Iterator[str]:
        n = len(self._prefix)
        return iter([k[n:] for k in self._state if k.startswith(self._prefix)])

    def __len__(self) -> int:
        return sum(1 for k in self._state if k.startswith(self._prefix))


def require_async(middleware: object, hooks: Iterable[str], *, name: str | None = None) -> None:
    """Raise `TypeError` if any of `hooks` that `middleware` has isn't an `async def`.

    For a project that wants a sync hook refused when middleware is registered, not when a
    live unit first calls it.
    """
    label = name or name_of(middleware)
    for hook in hooks:
        method = getattr(middleware, hook, None)
        if method is not None and not inspect.iscoroutinefunction(method):
            raise TypeError(f"middleware {label!r}: {hook} must be an async function")


class Pipeline[M]:
    """A frozen chain and the project's policy. `start()` makes a `Run` for one unit."""

    @overload
    def __init__(self, middleware: Iterable[Entry[M]], policy: Policy) -> None: ...
    @overload
    def __init__(self, middleware: Iterable[M | Entry[M]], policy: Policy) -> None: ...
    def __init__(self, middleware: Iterable[M | Entry[M]], policy: Policy) -> None:
        if not isinstance(policy, Policy):
            raise TypeError("policy must be a Policy")
        self.policy = policy
        self.entries: tuple[Entry[M], ...] = tuple(
            m if isinstance(m, Entry) else Entry.of(m) for m in middleware
        )

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    def start(
        self,
        *,
        select: Callable[[M], bool] | None = None,
        select_hook: str = "select",
        on_failure: Callable[[Failure], None] | None = None,
        timings: dict[str, float] | None = None,
        state: MutableMapping[str, Any] | None = None,
    ) -> Run[M]:
        """The run for one unit. `select` (sync, called once per entry, now) picks what applies;
        it fails like a hook named `select_hook`. `timings` and `state`, if given, are filled in
        place (a `flow.timings.middleware`, a `ctx.state`)."""
        run = Run(
            self,
            on_failure,
            timings if timings is not None else {},
            state if state is not None else {},
        )
        for entry in self.entries:
            if select is None or run._select(entry, select, select_hook):
                run.entries.append(entry)
        return run


@dataclass(eq=False)
class Run[M]:
    """One unit's pass through the pipeline: what applies, what has failed, what ran."""

    pipeline: Pipeline[M]
    on_failure: Callable[[Failure], None] | None = None
    timings: dict[str, float] = field(default_factory=dict)
    state: MutableMapping[str, Any] = field(default_factory=dict)
    entries: list[Entry[M]] = field(default_factory=list, init=False)
    ran: list[tuple[Entry[M], Any]] = field(default_factory=list, init=False)
    stopped: Stopped[M] | None = field(default=None, init=False)
    # Entries hash by identity (eq=False), so the same middleware twice is two entries.
    _failed: set[Entry[M]] = field(default_factory=set, init=False, repr=False)

    @property
    def policy(self) -> Policy:
        return self.pipeline.policy

    @property
    def active(self) -> list[Entry[M]]:
        """The selected entries that haven't failed, in chain order."""
        return [e for e in self.entries if e not in self._failed]

    def has_failed(self, entry: Entry[M]) -> bool:
        return entry in self._failed

    def scoped(self, entry: Entry[M]) -> ScopedState:
        return ScopedState(self.state, entry.name)

    # The guard

    def _action(self, entry: Entry[M]) -> OnError:
        return entry.on_error or self.policy.on_error

    def _inherited(self, entry: Entry[M], hook: str) -> bool:
        """Whether `skip_inherited` says not to call this hook: the base defines it and the
        middleware neither overrides it in its class nor sets it on the instance."""
        base = self.policy.skip_inherited
        if base is None or not hasattr(base, hook):
            return False
        mw = entry.middleware
        # A hook assigned on the instance (`mw.pre = gate`) is an override too: looking at the
        # class alone would skip it, and a skipped gate fails open (issue #12).
        own = getattr(mw, "__dict__", None)
        if own is not None and hook in own:
            return False
        return getattr(type(mw), hook, None) is getattr(base, hook)

    def _fail(
        self,
        entry: Entry[M],
        hook: str,
        error: Exception,
        timed_out: bool,
        action: OnError,
        timeout: float | None = None,
    ) -> None:
        """Take entry out of the run, report it, and raise if the action is "fail"."""
        self._failed.add(entry)
        failure = Failure(entry.name, hook, error, timed_out, action, timeout)
        if self.on_failure is not None:
            self.on_failure(failure)
        if action == "fail":
            raise HookFailed(failure) from error

    def _select(self, entry: Entry[M], select: Callable[[M], bool], hook: str) -> bool:
        try:
            return bool(select(entry.middleware))
        except self.policy.answers:
            raise
        except Exception as e:
            self._fail(entry, hook, e, False, self._action(entry))
            return False

    async def _guard[R](
        self,
        entry: Entry[M],
        hook: str,
        invoke: Callable[[], Awaitable[R]],
        action: OnError,
        valid: Callable[[R], bool] | None,
        observing: bool = False,
        check_inherited: bool = True,
    ) -> R | _Skipped:
        if entry in self._failed and not (observing and self.policy.observe_failed):
            return SKIPPED
        if check_inherited and self._inherited(entry, hook):
            return SKIPPED
        timeout = entry.timeout if entry.timeout is not None else self.policy.timeout
        if timeout == NO_LIMIT:
            timeout = None
        limit = asyncio.timeout(timeout)
        start = time.perf_counter()
        try:
            async with limit:
                result = await invoke()
            # Inside the try: a `valid` that raises on an odd result is that result's failure,
            # handled by on_error like any other, not an exception escaping the pipeline.
            if valid is not None and not valid(result):
                raise InvalidResult(f"{hook} returned {type(result).__name__}")
        except Exception as e:
            if not observing and isinstance(e, self.policy.answers):
                raise
            # A TimeoutError the hook raised itself (its own network call) is an ordinary
            # failure; only the guard's own limit counts as a timeout.
            self._fail(entry, hook, e, limit.expired(), action, timeout)
            return SKIPPED
        finally:
            self.timings[entry.name] = (
                self.timings.get(entry.name, 0.0) + time.perf_counter() - start
            )
        return result

    async def call[R](
        self,
        entry: Entry[M],
        hook: str,
        invoke: Callable[[], Awaitable[R]],
        *,
        valid: Callable[[R], bool] | None = None,
    ) -> R | _Skipped:
        """One guarded hook call: its result, or `SKIPPED` (failed under "skip", already
        failed, or not overridden). Raises `HookFailed` under "fail"; answers pass through."""
        return await self._guard(entry, hook, invoke, self._action(entry), valid)

    async def _step[R](
        self,
        entry: Entry[M],
        hook: str,
        invoke: Callable[[], Awaitable[R]],
        valid: Callable[[R], bool] | None,
    ) -> R | _Skipped | None:
        """A call in a phase that can stop (`each`, `before`). A hook `skip_inherited` leaves
        out counts as the base's no-op: `None`, so `until` is still asked. A stop that depends
        on the unit's state (sluice's `ctx.responded`, set by a `param()` callback) then ends
        the phase where it would have if the hook had been awaited (issue #12)."""
        if entry not in self._failed and self._inherited(entry, hook):
            return None
        return await self._guard(
            entry, hook, invoke, self._action(entry), valid, check_inherited=False
        )

    # Phases over a single context

    async def each[R](
        self,
        hook: str,
        invoke: Callable[[M], Awaitable[R]],
        *,
        entries: Sequence[Entry[M]] | None = None,
        until: Callable[[R], bool] | None = None,
        valid: Callable[[R], bool] | None = None,
    ) -> Stopped[M] | None:
        """Call `hook` on each entry (default: `active`) in order, until `until(result)` is
        true (`until` is the project's and must not raise). Returns where it stopped, or `None`
        if every entry ran."""
        for entry in self.active if entries is None else entries:
            mw = entry.middleware
            result = await self._step(entry, hook, partial(invoke, mw), valid)
            if result is SKIPPED:
                continue
            if until is not None and until(cast(R, result)):
                return Stopped(entry, result)
        return None

    async def fold[V](
        self,
        hook: str,
        value: V,
        invoke: Callable[[M, V], Awaitable[Any]],
        *,
        entries: Sequence[Entry[M]] | None = None,
        valid: Callable[[Any], bool] | None = None,
        keep_none: bool = False,
    ) -> V:
        """Thread `value` through `hook` on each entry (default: `active`): each result replaces
        it. A skipped call keeps it, and so does `None` with `keep_none=True`."""
        for entry in self.active if entries is None else entries:
            mw, current = entry.middleware, value
            result = await self.call(entry, hook, partial(invoke, mw, current), valid=valid)
            if result is SKIPPED or (keep_none and result is None):
                continue
            value = cast(V, result)
        return value

    async def observe(
        self,
        hook: str,
        invoke: Callable[[M], Awaitable[Any]],
        *,
        entries: Sequence[Entry[M]] | None = None,
    ) -> None:
        """Call a hook that only observes, on each entry (default: the selected ones, failed
        included only if `observe_failed`). Always fails open: a failure skips, whatever
        `on_error` says, and answers are failures here too."""
        for entry in self.entries if entries is None else entries:
            mw = entry.middleware
            await self._guard(entry, hook, partial(invoke, mw), "skip", None, observing=True)

    # Phases that remember what ran, with which context

    async def before[R](
        self,
        hook: str,
        context: Any,
        invoke: Callable[[M, Any], Awaitable[R]],
        *,
        entries: Sequence[Entry[M]] | None = None,
        until: Callable[[R], bool] | None = None,
        valid: Callable[[R], bool] | None = None,
    ) -> Stopped[M] | None:
        """`each`, recording `(entry, context)` in `ran` before each call, so observers reach
        it even if its own hook failed (with `observe_failed`); the after phase leaves out what
        failed. A stop is kept in `stopped`. Call it again with another context and chain
        (sluice's rename target): `ran` accumulates. `until` is the project's and must not
        raise."""
        for entry in self.active if entries is None else entries:
            if self.has_failed(entry):
                continue
            self.ran.append((entry, context))
            mw = entry.middleware
            result = await self._step(entry, hook, partial(invoke, mw, context), valid)
            if result is SKIPPED:
                continue
            if until is not None and until(cast(R, result)):
                self.stopped = Stopped(entry, result)
                return self.stopped
        return None

    def _ordered(self, pairs: Sequence[tuple[Entry[M], Any]]) -> list[tuple[Entry[M], Any]]:
        return list(reversed(pairs)) if self.policy.after_order == "reverse" else list(pairs)

    def after_pairs(self) -> list[tuple[Entry[M], Any]]:
        """Who the after phase reaches: what ran, in `after_order`, minus what failed; nothing
        if a before hook stopped the unit and the policy says after hooks skip local answers."""
        if self.stopped is not None and not self.policy.after_local:
            return []
        return self._ordered([(e, c) for e, c in self.ran if not self.has_failed(e)])

    async def after[V](
        self,
        hook: str,
        value: V,
        invoke: Callable[[M, Any, V], Awaitable[Any]],
        *,
        valid: Callable[[Any], bool] | None = None,
        keep_none: bool = False,
    ) -> V:
        """Thread `value` through `hook` over `after_pairs()`, each with its own context."""
        for entry, context in self.after_pairs():
            mw, current = entry.middleware, value
            result = await self.call(
                entry, hook, partial(invoke, mw, context, current), valid=valid
            )
            if result is SKIPPED or (keep_none and result is None):
                continue
            value = cast(V, result)
        return value

    async def observe_ran(
        self,
        hook: str,
        invoke: Callable[[M, Any], Awaitable[Any]],
        *,
        pairs: Sequence[tuple[Entry[M], Any]] | None = None,
    ) -> None:
        """An observer hook over what ran (default: `ran` in `after_order`, local answer or
        not), each with its own context. Fails open, like `observe`."""
        for entry, context in self._ordered(self.ran) if pairs is None else pairs:
            mw = entry.middleware
            await self._guard(
                entry, hook, partial(invoke, mw, context), "skip", None, observing=True
            )
