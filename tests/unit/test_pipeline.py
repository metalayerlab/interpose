"""interpose.pipeline: the guard, the phases, the policies, and both consumers' semantics."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import time
from collections.abc import Coroutine
from typing import Any

import pytest

from interpose.pipeline import (
    NO_LIMIT,
    SKIPPED,
    Entry,
    Failure,
    HookFailed,
    InvalidResult,
    Pipeline,
    Policy,
    Run,
    ScopedState,
    Stopped,
    name_of,
    require_async,
)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


SKIP = Policy(on_error="skip", after_order="chain", after_local=False, observe_failed=False)
FAIL = Policy(on_error="fail", after_order="reverse", after_local=True, observe_failed=True)


class Boom(Exception):
    pass


class Answer(Exception):
    """A deliberate deny, raised (sluice's FsError)."""


class Mw:
    """A configurable middleware that records what was called on it."""

    def __init__(
        self,
        name: str,
        *,
        result: Any = None,
        raises: Exception | None = None,
        sleep: float = 0.0,
        on_error: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.name = name
        self.result = result
        self.raises = raises
        self.sleep = sleep
        if on_error is not None:
            self.on_error = on_error
        if timeout is not None:
            self.timeout = timeout
        self.calls: list[tuple[str, Any]] = []

    async def hook(self, arg: Any = None) -> Any:
        self.calls.append(("hook", arg))
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.raises is not None:
            raise self.raises
        return self.result

    async def fold(self, value: Any) -> Any:
        self.calls.append(("fold", value))
        if self.raises is not None:
            raise self.raises
        return self.result if self.result is not None else f"{value}+{self.name}"

    async def post(self, ctx: Any, value: Any) -> Any:
        self.calls.append(("post", ctx))
        if self.raises is not None:
            raise self.raises
        return f"{value}>{self.name}"

    async def seen(self, ctx: Any = None) -> None:
        self.calls.append(("seen", ctx))
        if self.raises is not None:
            raise self.raises


def failures() -> tuple[list[Failure], Any]:
    got: list[Failure] = []
    return got, got.append


# --- Policy ------------------------------------------------------------------------------------


def test_policy_has_no_default_for_the_semantics() -> None:
    with pytest.raises(TypeError):
        Policy()  # type: ignore[call-arg]  # the four semantics fields are required
    with pytest.raises(TypeError):
        Policy(on_error="skip")  # type: ignore[call-arg]  # still three missing


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"on_error": "ignore"}, "on_error must be"),
        ({"on_error": None}, "on_error must be"),
        ({"after_order": "random"}, "after_order must be"),
        ({"after_local": 1}, "after_local must be a bool"),
        ({"observe_failed": "yes"}, "observe_failed must be a bool"),
        ({"timeout": 0}, "timeout must be"),
        ({"timeout": -1.0}, "timeout must be"),
        ({"timeout": True}, "timeout must be"),
        ({"timeout": "5"}, "timeout must be"),
        ({"answers": [Answer]}, "answers must be"),
        ({"answers": (KeyboardInterrupt,)}, "answers must be"),
        ({"answers": ("x",)}, "answers must be"),
        ({"skip_inherited": Mw("x")}, "skip_inherited must be"),
    ],
)
def test_policy_is_validated(kwargs: dict[str, Any], match: str) -> None:
    base: dict[str, Any] = {
        "on_error": "skip",
        "after_order": "chain",
        "after_local": False,
        "observe_failed": False,
    }
    with pytest.raises(ValueError, match=match):
        Policy(**{**base, **kwargs})


def test_policy_accepts_each_project_shape() -> None:
    p = Policy(
        on_error="fail",
        after_order="reverse",
        after_local=True,
        observe_failed=True,
        timeout=0.5,
        answers=(Answer,),
        skip_inherited=Mw,
    )
    assert p.timeout == 0.5 and p.answers == (Answer,) and p.skip_inherited is Mw
    assert (
        Policy(
            on_error="skip", after_order="chain", after_local=False, observe_failed=False, timeout=5
        ).timeout
        == 5
    )


# --- Names and entries -------------------------------------------------------------------------


def test_name_of_prefers_a_name_attribute_then_the_class_name() -> None:
    class Plain:
        pass

    class Blank:
        name = ""

    class NotStr:
        name = 5

    assert name_of(Mw("auth")) == "auth"
    assert name_of(Plain()) == "Plain"
    assert name_of(Blank()) == "Blank"
    assert name_of(NotStr()) == "NotStr"


def test_entry_of_reads_the_middlewares_own_policy() -> None:
    mw = Mw("gate", on_error="fail", timeout=0.25)
    e = Entry.of(mw)
    assert (e.middleware, e.name, e.on_error, e.timeout) == (mw, "gate", "fail", 0.25)
    plain = Entry.of(Mw("log"))
    assert (plain.on_error, plain.timeout) == (None, None)


def test_entry_of_takes_an_explicit_name() -> None:
    assert Entry.of(Mw("x"), name="registry-key").name == "registry-key"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"name": ""}, "needs a name"),
        ({"name": 3}, "needs a name"),
        ({"on_error": "maybe"}, "on_error must be"),
        ({"timeout": 0}, "timeout must be"),
    ],
)
def test_entry_is_validated(kwargs: dict[str, Any], match: str) -> None:
    args: dict[str, Any] = {"middleware": object(), "name": "n", **kwargs}
    with pytest.raises(ValueError, match=match):
        Entry(**args)


def test_entries_compare_by_identity() -> None:
    mw = Mw("same")
    a, b = Entry(mw, "same"), Entry(mw, "same")
    assert a != b and a == a
    assert len({a, b}) == 2


def test_an_entry_names_its_middleware_in_a_bad_attribute_error() -> None:
    with pytest.raises(ValueError, match="middleware 'x': on_error"):
        Entry.of(Mw("x", on_error="sometimes"))


# --- Pipeline ----------------------------------------------------------------------------------


def test_pipeline_needs_a_policy() -> None:
    with pytest.raises(TypeError, match="Policy"):
        Pipeline([], object())  # type: ignore[call-overload]  # the check under test


def test_pipeline_takes_middleware_or_entries() -> None:
    explicit = Entry(Mw("a"), "first")
    p = Pipeline([explicit, Mw("b")], SKIP)
    assert p.names == ["first", "b"]
    assert p.entries[0] is explicit
    assert p.policy is SKIP


def test_start_selects_once_and_fills_timings_and_state_in_place() -> None:
    a, b = Mw("a"), Mw("b")
    timings: dict[str, float] = {}
    state: dict[str, Any] = {"k": 1}
    r = Pipeline([a, b], SKIP).start(select=lambda mw: mw is b, timings=timings, state=state)
    assert [e.middleware for e in r.entries] == [b]
    assert r.timings is timings and r.state is state
    run(r.each("hook", lambda mw: mw.hook()))
    assert set(timings) == {"b"} and timings["b"] >= 0


def test_start_without_select_takes_everything() -> None:
    r = Pipeline([Mw("a"), Mw("b")], SKIP).start()
    assert [e.name for e in r.active] == ["a", "b"]
    assert r.timings == {} and r.state == {}


def test_a_failing_select_skips_under_skip() -> None:
    got, report = failures()
    bad = Mw("bad")

    def select(mw: Mw) -> bool:
        if mw is bad:
            raise Boom("no")
        return True

    r = Pipeline([bad, Mw("ok")], SKIP).start(
        select=select, select_hook="applies_to", on_failure=report
    )
    assert [e.name for e in r.entries] == ["ok"]
    assert [(f.middleware, f.hook, f.action, f.timed_out) for f in got] == [
        ("bad", "applies_to", "skip", False)
    ]
    assert isinstance(got[0].error, Boom)
    assert r.has_failed(Pipeline([bad], SKIP).entries[0]) is False  # a different entry


def test_a_failing_select_raises_under_fail() -> None:
    def select(mw: Mw) -> bool:
        raise Boom

    with pytest.raises(HookFailed, match="middleware 'a' failed in select"):
        Pipeline([Mw("a")], FAIL).start(select=select)


def test_an_answer_from_select_passes_through() -> None:
    policy = Policy(
        on_error="skip",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        answers=(Answer,),
    )

    def select(mw: Mw) -> bool:
        raise Answer

    with pytest.raises(Answer):
        Pipeline([Mw("a")], policy).start(select=select)


# --- The guard ---------------------------------------------------------------------------------


def test_call_returns_the_hooks_result_and_times_it() -> None:
    mw = Mw("a", result=42)
    r = Pipeline([mw], SKIP).start()
    assert run(r.call(r.entries[0], "hook", lambda: mw.hook("x"))) == 42
    assert mw.calls == [("hook", "x")]
    assert r.timings["a"] >= 0


def test_a_failure_under_skip_takes_the_middleware_out_for_the_rest_of_the_unit() -> None:
    got, report = failures()
    mw = Mw("a", raises=Boom("secret-value"))
    r = Pipeline([mw], SKIP).start(on_failure=report)
    entry = r.entries[0]
    assert run(r.call(entry, "hook", lambda: mw.hook())) is SKIPPED
    assert r.has_failed(entry) and r.active == []
    assert "a" in r.timings  # failed calls are timed too
    assert run(r.call(entry, "hook", lambda: mw.hook())) is SKIPPED
    assert len(mw.calls) == 1  # not called again
    assert len(got) == 1 and got[0].action == "skip" and isinstance(got[0].error, Boom)


def test_a_failure_under_fail_raises_hook_failed_with_the_cause() -> None:
    got, report = failures()
    mw = Mw("gate", raises=Boom("secret-value"))
    r = Pipeline([mw], FAIL).start(on_failure=report)
    with pytest.raises(HookFailed) as e:
        run(r.call(r.entries[0], "pre", lambda: mw.hook()))
    assert str(e.value) == "middleware 'gate' failed in pre"
    assert "secret-value" not in str(e.value)
    assert isinstance(e.value.__cause__, Boom)
    assert e.value.failure == got[0]
    assert got[0].action == "fail" and r.has_failed(r.entries[0])


def test_the_middlewares_on_error_beats_the_projects() -> None:
    gate = Mw("gate", raises=Boom(), on_error="fail")
    log = Mw("log", raises=Boom(), on_error="skip")
    r = Pipeline([gate], SKIP).start()
    with pytest.raises(HookFailed):
        run(r.call(r.entries[0], "hook", lambda: gate.hook()))
    r2 = Pipeline([log], FAIL).start()
    assert run(r2.call(r2.entries[0], "hook", lambda: log.hook())) is SKIPPED


def test_answers_pass_through_whatever_on_error_says() -> None:
    policy = Policy(
        on_error="skip",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        answers=(Answer,),
    )
    got, report = failures()
    mw = Mw("auth", raises=Answer("denied"), on_error="skip")
    r = Pipeline([mw], policy).start(on_failure=report)
    with pytest.raises(Answer):
        run(r.call(r.entries[0], "hook", lambda: mw.hook()))
    assert got == [] and not r.has_failed(r.entries[0])
    assert "auth" in r.timings


def test_the_projects_timeout_bounds_a_hook() -> None:
    policy = Policy(
        on_error="skip",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=0.01,
    )
    got, report = failures()
    mw = Mw("slow", sleep=5)
    r = Pipeline([mw], policy).start(on_failure=report)
    assert run(r.call(r.entries[0], "hook", lambda: mw.hook())) is SKIPPED
    assert got[0].timed_out is True and isinstance(got[0].error, TimeoutError)


def test_the_middlewares_timeout_beats_the_projects() -> None:
    policy = Policy(
        on_error="fail",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=60,
    )
    mw = Mw("slow", sleep=5, timeout=0.01)
    r = Pipeline([mw], policy).start()
    with pytest.raises(HookFailed, match="middleware 'slow' timed out in hook") as e:
        run(r.call(r.entries[0], "hook", lambda: mw.hook()))
    assert e.value.failure.timed_out and isinstance(e.value.__cause__, TimeoutError)


def test_a_failure_carries_the_limit_that_applied() -> None:
    policy = Policy(
        on_error="skip",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=60,
    )
    got, report = failures()
    own, default = Mw("own", sleep=5, timeout=0.01), Mw("default", raises=Boom())
    r = Pipeline([own, default], policy).start(on_failure=report)
    run(r.each("hook", lambda mw: mw.hook()))
    assert [(f.middleware, f.timeout) for f in got] == [("own", 0.01), ("default", 60)]
    r2 = Pipeline([Mw("none", raises=Boom())], SKIP).start(on_failure=report)
    run(r2.each("hook", lambda mw: mw.hook()))
    assert got[-1].timeout is None


def test_no_limit_opts_a_middleware_out_of_the_projects_timeout() -> None:
    policy = Policy(
        on_error="fail",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=0.001,
    )
    mw = Mw("slow_auth", sleep=0.02, result="ok", timeout=NO_LIMIT)
    r = Pipeline([mw], policy).start()
    assert run(r.call(r.entries[0], "hook", lambda: mw.hook())) == "ok"


def test_a_valid_check_that_raises_is_the_middlewares_failure() -> None:
    got, report = failures()
    mw = Mw("a", result=object())
    r = Pipeline([mw], SKIP).start(on_failure=report)

    def valid(result: Any) -> bool:
        return bool(result.status < 400)

    assert run(r.call(r.entries[0], "hook", lambda: mw.hook(), valid=valid)) is SKIPPED
    assert isinstance(got[0].error, AttributeError) and r.has_failed(r.entries[0])


def test_no_timeout_lets_a_hook_finish() -> None:
    mw = Mw("slowish", sleep=0.01, result="done")
    r = Pipeline([mw], SKIP).start()
    assert run(r.call(r.entries[0], "hook", lambda: mw.hook())) == "done"


def test_a_timeout_error_from_the_hook_itself_is_an_ordinary_failure() -> None:
    got, report = failures()
    mw = Mw("net", raises=TimeoutError("upstream"))
    policy = Policy(
        on_error="skip",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=60,
    )
    r = Pipeline([mw], policy).start(on_failure=report)
    assert run(r.call(r.entries[0], "hook", lambda: mw.hook())) is SKIPPED
    assert got[0].timed_out is False


def test_cancellation_is_never_caught() -> None:
    mw = Mw("c", raises=asyncio.CancelledError())  # type: ignore[arg-type]  # a BaseException
    r = Pipeline([mw], SKIP).start()
    with pytest.raises(asyncio.CancelledError):
        run(r.call(r.entries[0], "hook", lambda: mw.hook()))
    assert not r.has_failed(r.entries[0])


def _limited(on_error: str = "skip", **kw: Any) -> Policy:
    return Policy(
        on_error=on_error,  # type: ignore[arg-type]  # a literal, from the caller
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=0.01,
        **kw,
    )


async def _guarded_then_awaits(r: Run[Any], entry: Entry[Any], invoke: Any) -> Any:
    """The guarded call, then proof nothing is left pending for the caller's next await."""
    result = await r.call(entry, "hook", invoke)
    task = asyncio.current_task()
    assert task is not None and task.cancelling() == 0
    await asyncio.sleep(0)
    return result


def test_a_hook_that_overruns_its_limit_in_sync_code_is_a_late_timeout() -> None:
    # Design 0005: the deadline passed while the hook blocked, so the cancellation never
    # reached it and asyncio.timeout exits cleanly; the late result is still refused.
    async def blocks() -> str:
        time.sleep(0.03)  # the misbehaviour under test: blocking the event loop
        return "late"

    got, report = failures()
    r: Run[Any] = Pipeline([Entry(object(), "blocker")], _limited()).start(on_failure=report)
    assert run(_guarded_then_awaits(r, r.entries[0], blocks)) is SKIPPED
    f = got[0]
    assert (f.timed_out, f.late, f.timeout) == (True, "overran", 0.01)
    assert isinstance(f.error, TimeoutError) and "late" not in str(f.error)
    assert r.has_failed(r.entries[0])


def test_a_hook_that_swallows_its_cancellation_is_a_late_timeout() -> None:
    async def swallows() -> str:
        # The misbehaviour under test: the cancellation is caught and dropped.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(5)
        return "late"

    r: Run[Any] = Pipeline([Entry(object(), "swallower")], _limited("fail")).start()
    with pytest.raises(HookFailed, match="'swallower' timed out in hook") as e:
        run(_guarded_then_awaits(r, r.entries[0], swallows))
    f = e.value.failure
    assert (f.timed_out, f.late, f.action) == (True, "swallowed", "fail")
    assert isinstance(e.value.__cause__, TimeoutError)


def test_a_late_timeout_is_never_an_answer() -> None:
    async def blocks() -> str:
        time.sleep(0.03)  # the misbehaviour under test: blocking the event loop
        return "late"

    got, report = failures()
    policy = _limited(answers=(TimeoutError,))
    r: Run[Any] = Pipeline([Entry(object(), "blocker")], policy).start(on_failure=report)
    assert run(r.call(r.entries[0], "hook", blocks)) is SKIPPED
    assert got[0].late == "overran"


def test_a_hook_that_blocks_then_awaits_is_an_ordinary_timeout() -> None:
    # The cancellation arrives at the first await that lets the loop fire the deadline:
    # asyncio.timeout raises, as for any timeout.
    async def blocks_then_awaits() -> str:
        time.sleep(0.03)  # the misbehaviour under test: blocking the event loop
        await asyncio.sleep(0.01)
        return "late"

    got, report = failures()
    r: Run[Any] = Pipeline([Entry(object(), "blocker")], _limited()).start(on_failure=report)
    assert run(_guarded_then_awaits(r, r.entries[0], blocks_then_awaits)) is SKIPPED
    assert (got[0].timed_out, got[0].late) == (True, None)


def test_a_normal_timeout_has_no_late_reason() -> None:
    got, report = failures()
    mw = Mw("slow", sleep=5)
    r = Pipeline([mw], _limited()).start(on_failure=report)
    assert run(_guarded_then_awaits(r, r.entries[0], lambda: mw.hook())) is SKIPPED
    assert (got[0].timed_out, got[0].late) == (True, None)


def test_a_hook_in_time_under_a_limit_keeps_its_result() -> None:
    got, report = failures()
    mw = Mw("quick", result="ok")
    r = Pipeline([mw], _limited()).start(on_failure=report)
    assert run(_guarded_then_awaits(r, r.entries[0], lambda: mw.hook())) == "ok"
    assert got == []


def test_an_outer_cancellation_passes_through_a_limited_hook() -> None:
    got, report = failures()
    mw = Mw("slow", sleep=5)
    policy = Policy(
        on_error="skip",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=60,
    )
    r = Pipeline([mw], policy).start(on_failure=report)

    async def main() -> None:
        task = asyncio.ensure_future(r.call(r.entries[0], "hook", lambda: mw.hook()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    run(main())
    assert got == [] and not r.has_failed(r.entries[0])


def test_a_sync_hook_fails_when_called() -> None:
    class Sync:
        def hook(self) -> str:
            return "not awaitable"

    got, report = failures()
    mw = Sync()
    r = Pipeline([mw], SKIP).start(on_failure=report)
    assert run(r.call(r.entries[0], "hook", mw.hook)) is SKIPPED  # type: ignore[arg-type]  # a sync hook, refused at call time
    assert isinstance(got[0].error, TypeError)


def test_an_invalid_result_is_a_failure_that_names_only_the_type() -> None:
    got, report = failures()
    mw = Mw("a", result="secret-value")
    r = Pipeline([mw], SKIP).start(on_failure=report)
    result = run(r.call(r.entries[0], "on_request", lambda: mw.hook(), valid=lambda x: x is None))
    assert result is SKIPPED
    assert isinstance(got[0].error, InvalidResult)
    assert str(got[0].error) == "on_request returned str"
    r2 = Pipeline([mw], FAIL).start()
    with pytest.raises(HookFailed) as e:
        run(r2.call(r2.entries[0], "on_request", lambda: mw.hook(), valid=lambda x: x is None))
    assert isinstance(e.value.__cause__, InvalidResult)


def test_a_valid_result_passes() -> None:
    mw = Mw("a", result=1)
    r = Pipeline([mw], SKIP).start()
    assert run(r.call(r.entries[0], "hook", lambda: mw.hook(), valid=lambda x: x == 1)) == 1


class Base:
    async def hook(self) -> Any:
        return None

    async def seen(self, ctx: Any = None) -> None:
        return None


def test_skip_inherited_skips_hooks_a_middleware_doesnt_override() -> None:
    class Overrides(Base):
        async def hook(self) -> Any:
            return "mine"

    class Inherits(Base):
        pass

    policy = Policy(
        on_error="fail",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        skip_inherited=Base,
    )
    over, inh = Overrides(), Inherits()
    r = Pipeline([over, inh], policy).start()
    a, b = r.entries
    assert run(r.call(a, "hook", over.hook)) == "mine"
    assert run(r.call(b, "hook", inh.hook)) is SKIPPED
    assert "Inherits" not in r.timings and "Overrides" in r.timings
    # A hook the base doesn't define is always called.
    assert run(r.call(b, "other", lambda: asyncio.sleep(0, "called"))) == "called"


def test_without_skip_inherited_every_hook_is_called() -> None:
    class Inherits(Base):
        pass

    mw = Inherits()
    r = Pipeline([mw], SKIP).start()
    assert run(r.call(r.entries[0], "hook", mw.hook)) is None
    assert "Inherits" in r.timings


def test_skip_inherited_calls_a_hook_set_on_the_instance() -> None:
    # Issue #12: the override check looked at the class only, so a gate assigned on the
    # instance (sluice's `as_middleware` accepts one) was never run: it failed open.
    class Inherits(Base):
        pass

    policy = Policy(
        on_error="fail",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        skip_inherited=Base,
    )
    gated, plain = Inherits(), Inherits()

    async def gate(arg: Any = None) -> str:
        return "gate"

    gated.hook = gate  # type: ignore[method-assign]  # the instance attribute is what's tested
    r = Pipeline([Entry(gated, "gated"), Entry(plain, "plain")], policy).start()
    a, b = r.entries
    assert run(r.call(a, "hook", lambda: gated.hook())) == "gate"
    assert run(r.call(b, "hook", lambda: plain.hook())) is SKIPPED
    stopped = run(r.each("hook", lambda mw: mw.hook(), until=lambda x: x == "gate"))
    assert stopped is not None and stopped.entry is a
    assert "gated" in r.timings and "plain" not in r.timings


def test_skip_inherited_works_for_middleware_without_an_instance_dict() -> None:
    class SlotBase:
        __slots__ = ()

        async def hook(self) -> Any:
            return None

    class Overrides(SlotBase):
        __slots__ = ()

        async def hook(self) -> Any:
            return "mine"

    class Inherits(SlotBase):
        __slots__ = ()

    policy = Policy(
        on_error="fail",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        skip_inherited=SlotBase,
    )
    over, inh = Overrides(), Inherits()
    r = Pipeline([Entry(over, "over"), Entry(inh, "inh")], policy).start()
    a, b = r.entries
    assert run(r.call(a, "hook", over.hook)) == "mine"
    assert run(r.call(b, "hook", inh.hook)) is SKIPPED


def test_an_inherited_hook_in_a_phase_counts_as_returning_none() -> None:
    # Issue #12: a hook skip_inherited leaves out still lets `until` decide, as if the base's
    # no-op had run and returned None: a result-based stop carries on, a state-based one stops.
    class Overrides(Base):
        def __init__(self) -> None:
            self.calls = 0

        async def hook(self) -> Any:
            self.calls += 1
            return None

    class Inherits(Base):
        pass

    policy = Policy(
        on_error="fail",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        skip_inherited=Base,
    )
    inh, over = Inherits(), Overrides()
    p: Pipeline[Base] = Pipeline([Entry[Base](inh, "inh"), Entry[Base](over, "over")], policy)
    seen: list[Any] = []

    def result_stop(x: Any) -> bool:
        seen.append(x)
        return x is not None

    r = p.start()
    assert run(r.each("hook", lambda mw: mw.hook(), until=result_stop)) is None
    assert seen == [None, None] and over.calls == 1
    assert "inh" not in r.timings

    r = p.start()
    stopped = run(r.before("hook", "ctx", lambda mw, c: mw.hook(), until=lambda _: True))
    assert stopped is not None and stopped.entry.middleware is inh and stopped.result is None
    assert r.ran == [(r.entries[0], "ctx")] and over.calls == 1
    stopped = run(r.each("hook", lambda mw: mw.hook(), until=lambda _: True))
    assert stopped is not None and stopped.entry.middleware is inh


def test_a_failed_entry_with_an_inherited_hook_stays_skipped_in_a_phase() -> None:
    class Inherits(Base):
        async def seen(self, ctx: Any = None) -> None:
            raise Boom

    mw = Inherits()
    policy = Policy(
        on_error="skip",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        skip_inherited=Base,
    )
    r = Pipeline([Entry(mw, "inh")], policy).start()
    entry = r.entries[0]
    assert run(r.call(entry, "seen", mw.seen)) is SKIPPED and r.has_failed(entry)
    asked: list[Any] = []

    def ask(x: Any) -> bool:
        asked.append(x)
        return True

    assert run(r.each("hook", lambda m: m.hook(), entries=[entry], until=ask)) is None
    assert asked == []


def test_skipped_is_falsy_and_named() -> None:
    assert not SKIPPED
    assert repr(SKIPPED) == "SKIPPED"


# --- each, fold, observe -----------------------------------------------------------------------


def test_each_runs_in_chain_order_until_a_result_says_stop() -> None:
    a, b, c = Mw("a"), Mw("b", result="stop"), Mw("c")
    r = Pipeline([a, b, c], SKIP).start()
    stopped = run(r.each("hook", lambda mw: mw.hook("u"), until=lambda x: x == "stop"))
    assert isinstance(stopped, Stopped)
    assert stopped.entry.middleware is b and stopped.result == "stop"
    assert c.calls == []
    assert r.stopped is None  # only before() records a stop


def test_each_without_a_stop_runs_everything_and_skips_failures() -> None:
    a, b, c = Mw("a"), Mw("b", raises=Boom()), Mw("c")
    r = Pipeline([a, b, c], SKIP).start()
    assert run(r.each("hook", lambda mw: mw.hook(), until=lambda x: x == "stop")) is None
    assert run(r.each("hook", lambda mw: mw.hook())) is None
    assert [len(m.calls) for m in (a, b, c)] == [2, 1, 2]


def test_each_over_explicit_entries() -> None:
    a, b = Mw("a"), Mw("b")
    r = Pipeline([a, b], SKIP).start()
    run(r.each("hook", lambda mw: mw.hook(), entries=[r.entries[1]]))
    assert a.calls == [] and len(b.calls) == 1


def test_fold_threads_a_value() -> None:
    r = Pipeline([Mw("a"), Mw("b")], SKIP).start()
    assert run(r.fold("fold", "v", lambda mw, v: mw.fold(v))) == "v+a+b"


def test_fold_keeps_the_value_on_a_skip_and_on_none_with_keep_none() -> None:
    passes = Mw("none")
    passes.result = None

    async def returns_none(mw: Mw, v: str) -> None:
        mw.calls.append(("fold", v))

    r = Pipeline([Mw("a"), Mw("bad", raises=Boom()), Mw("b")], SKIP).start()
    assert run(r.fold("fold", "v", lambda mw, v: mw.fold(v))) == "v+a+b"
    r2 = Pipeline([passes], SKIP).start()
    assert run(r2.fold("fold", "v", returns_none, keep_none=True)) == "v"
    assert run(r2.fold("fold", "v", returns_none)) is None


def test_fold_over_explicit_entries_with_valid() -> None:
    a, b = Mw("a"), Mw("b", result=7)
    got, report = failures()
    r = Pipeline([a, b], SKIP).start(on_failure=report)
    out = run(
        r.fold(
            "fold",
            "v",
            lambda mw, v: mw.fold(v),
            entries=r.entries,
            valid=lambda x: isinstance(x, str),
        )
    )
    assert out == "v+a"
    assert [f.middleware for f in got] == ["b"]


def test_observe_always_fails_open() -> None:
    got, report = failures()
    a, b = Mw("a", raises=Boom()), Mw("b")
    r = Pipeline([a, b], FAIL).start(on_failure=report)  # the project fails closed
    run(r.observe("seen", lambda mw: mw.seen()))
    assert len(b.calls) == 1
    assert [(f.middleware, f.action) for f in got] == [("a", "skip")]


def test_observe_treats_answers_as_failures() -> None:
    policy = Policy(
        on_error="fail",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        answers=(Answer,),
    )
    got, report = failures()
    a = Mw("a", raises=Answer())
    r = Pipeline([a], policy).start(on_failure=report)
    run(r.observe("seen", lambda mw: mw.seen()))
    assert isinstance(got[0].error, Answer)


def test_observe_skips_failed_middleware_unless_observe_failed() -> None:
    for policy, expected in ((SKIP, 0), (FAIL, 1)):
        bad = Mw("bad", raises=Boom(), on_error="skip")
        r = Pipeline([bad], policy).start()
        run(r.each("hook", lambda mw: mw.hook()))
        bad.raises = None
        bad.calls.clear()
        run(r.observe("seen", lambda mw: mw.seen()))
        assert len(bad.calls) == expected


def test_observe_over_explicit_entries() -> None:
    a, b = Mw("a"), Mw("b")
    r = Pipeline([a, b], SKIP).start()
    run(r.observe("seen", lambda mw: mw.seen(), entries=[r.entries[0]]))
    assert len(a.calls) == 1 and b.calls == []


def test_observe_is_bounded_by_the_timeout() -> None:
    policy = Policy(
        on_error="fail",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=0.01,
    )
    got, report = failures()
    slow = Mw("slow", sleep=5)
    r = Pipeline([slow], policy).start(on_failure=report)
    run(r.observe("hook", lambda mw: mw.hook()))
    assert got[0].timed_out and got[0].action == "skip"


# --- before, after, observe_ran ----------------------------------------------------------------


def test_before_records_what_ran_with_its_context_and_keeps_a_stop() -> None:
    a, b, c = Mw("a"), Mw("b", result="local"), Mw("c")
    r = Pipeline([a, b, c], FAIL).start()
    stopped = run(
        r.before("hook", "ctx", lambda mw, ctx: mw.hook(ctx), until=lambda x: x == "local")
    )
    assert r.stopped is stopped and stopped is not None and stopped.entry.middleware is b
    assert [(e.name, ctx) for e, ctx in r.ran] == [("a", "ctx"), ("b", "ctx")]
    assert a.calls == [("hook", "ctx")] and c.calls == []


def test_before_records_a_middleware_even_if_its_hook_fails() -> None:
    a, bad = Mw("a"), Mw("bad", raises=Boom())
    r = Pipeline([a, bad], FAIL).start()
    with pytest.raises(HookFailed):
        run(r.before("hook", "ctx", lambda mw, ctx: mw.hook(ctx)))
    assert [e.name for e, _ in r.ran] == ["a", "bad"]


def test_before_accumulates_across_contexts_and_chains() -> None:
    """sluice's rename: the target path's chain runs too, with its own context."""
    shared, only_target = Mw("auth"), Mw("quota")
    source = Pipeline([shared], FAIL)
    target = Pipeline([shared, only_target], FAIL)
    r = source.start()
    run(r.before("hook", "src", lambda mw, ctx: mw.hook(ctx)))
    assert (
        run(r.before("hook", "dst", lambda mw, ctx: mw.hook(ctx), entries=target.entries)) is None
    )
    assert [(e.name, c) for e, c in r.ran] == [("auth", "src"), ("auth", "dst"), ("quota", "dst")]
    assert shared.calls == [("hook", "src"), ("hook", "dst")]


def test_before_skips_failed_entries_in_an_explicit_chain_and_skipped_calls() -> None:
    bad, ok = Mw("bad", raises=Boom()), Mw("ok")
    r = Pipeline([bad, ok], SKIP).start()
    assert run(r.before("hook", 1, lambda mw, ctx: mw.hook(ctx), until=lambda x: True)) is not None
    # bad failed (skipped); ok's None result stopped it (until is always true)
    assert r.stopped is not None and r.stopped.entry.middleware is ok
    run(r.before("hook", 2, lambda mw, ctx: mw.hook(ctx), entries=r.entries))
    assert [(e.name, c) for e, c in r.ran] == [("bad", 1), ("ok", 1), ("ok", 2)]


def test_after_pairs_follow_the_order_and_drop_failures() -> None:
    a, b, bad = Mw("a"), Mw("b"), Mw("bad", raises=Boom())
    for policy, names in ((SKIP, ["a", "b"]), (FAIL, ["b", "a"])):
        r = Pipeline([a, bad, b], policy).start()
        bad.on_error = "skip"
        r = Pipeline([a, bad, b], policy).start()
        run(r.before("hook", "c", lambda mw, ctx: mw.hook(ctx)))
        assert [e.name for e, _ in r.after_pairs()] == names
        del bad.on_error


def test_after_skips_local_answers_unless_after_local() -> None:
    for policy, expected in ((SKIP, "r"), (FAIL, "r>b>a")):
        a, b = Mw("a"), Mw("b", result="local")
        r = Pipeline([a, b, Mw("c")], policy).start()
        run(r.before("hook", "ctx", lambda mw, ctx: mw.hook(ctx), until=lambda x: x == "local"))
        assert run(r.after("post", "r", lambda mw, ctx, v: mw.post(ctx, v))) == expected


def test_after_threads_each_entrys_own_context() -> None:
    a, b = Mw("a"), Mw("b")
    r = Pipeline([a], FAIL).start()
    run(r.before("hook", "src", lambda mw, ctx: mw.hook(ctx)))
    run(r.before("hook", "dst", lambda mw, ctx: mw.hook(ctx), entries=Pipeline([b], FAIL).entries))
    assert run(r.after("post", "r", lambda mw, ctx, v: mw.post(ctx, v))) == "r>b>a"
    assert b.calls[-1] == ("post", "dst") and a.calls[-1] == ("post", "src")


def test_after_keeps_the_value_on_skip_and_none() -> None:
    async def none(mw: Mw, ctx: Any, v: Any) -> None:
        return None

    bad = Mw("bad", raises=Boom(), on_error="skip")
    r = Pipeline([bad, Mw("ok")], FAIL).start()
    run(r.before("hook", "c", lambda mw, ctx: mw.hook(ctx)))
    bad.raises = None
    assert run(r.after("post", "r", lambda mw, ctx, v: mw.post(ctx, v))) == "r>ok"
    assert run(r.after("post", "r", none, keep_none=True)) == "r"
    assert run(r.after("post", "r", none)) is None


def test_after_validates_and_fails_closed() -> None:
    r = Pipeline([Mw("a")], FAIL).start()
    run(r.before("hook", "c", lambda mw, ctx: mw.hook(ctx)))
    with pytest.raises(HookFailed, match="failed in post"):
        run(r.after("post", "r", lambda mw, ctx, v: mw.post(ctx, v), valid=lambda x: x == "r"))


def test_observe_ran_reaches_what_ran_even_after_a_local_answer() -> None:
    a, b, c = Mw("a"), Mw("b", result="local"), Mw("c")
    r = Pipeline([a, b, c], SKIP).start()  # after_local=False, chain order
    run(r.before("hook", "ctx", lambda mw, ctx: mw.hook(ctx), until=lambda x: x == "local"))
    run(r.observe_ran("seen", lambda mw, ctx: mw.seen(ctx)))
    assert ("seen", "ctx") in a.calls and ("seen", "ctx") in b.calls and c.calls == []


def test_observe_ran_with_explicit_pairs() -> None:
    a, b = Mw("a"), Mw("b")
    p = Pipeline([a, b], FAIL)
    r = p.start()
    run(r.observe_ran("seen", lambda mw, ctx: mw.seen(ctx), pairs=[(p.entries[1], "x")]))
    assert b.calls == [("seen", "x")] and a.calls == []


# --- State -------------------------------------------------------------------------------------


def test_scoped_state_namespaces_keys_by_middleware_name() -> None:
    state: dict[str, Any] = {"other.k": 0}
    r = Pipeline([Mw("auth")], SKIP).start(state=state)
    s = r.scoped(r.entries[0])
    assert isinstance(s, ScopedState)
    s["user"] = "u1"
    assert state == {"other.k": 0, "auth.user": "u1"}
    assert s["user"] == "u1" and list(s) == ["user"] and len(s) == 1
    del s["user"]
    assert state == {"other.k": 0} and len(s) == 0
    with pytest.raises(KeyError):
        s["user"]


# --- require_async -----------------------------------------------------------------------------


def test_require_async_refuses_a_sync_hook() -> None:
    class Mixed:
        async def pre(self) -> None:
            return None

        def post(self) -> None:
            return None

    require_async(Mixed(), ["pre", "missing"])
    with pytest.raises(TypeError, match="middleware 'Mixed': post must be an async function"):
        require_async(Mixed(), ["pre", "post"])
    with pytest.raises(TypeError, match="middleware 'reg': post"):
        require_async(Mixed(), ["post"], name="reg")


# --- Both consumers' semantics, as policy ------------------------------------------------------

MITM = Policy(
    on_error="skip", after_order="chain", after_local=False, observe_failed=False, timeout=5
)


class Action:
    CONTINUE = "continue"
    STOP = "stop"
    DENY = "deny"


def is_stop(result: Any) -> bool:
    return result is not None and result != Action.CONTINUE


def test_mitm_proxy_semantics_fail_open_for_the_rest_of_the_flow() -> None:
    got, report = failures()
    broken = Mw("broken", raises=Boom())
    rewrite = Mw("rewrite", result=Action.CONTINUE)
    flow_timings: dict[str, float] = {}
    r = Pipeline([Entry.of(broken), Entry.of(rewrite)], MITM).start(
        on_failure=report, timings=flow_timings
    )
    request = run(r.each("on_request", lambda mw: mw.hook("flow"), until=is_stop))
    assert request is None  # the flow carries on
    chunk = run(
        r.fold("on_response_chunk", b"body", lambda mw, c: asyncio.sleep(0, None), keep_none=True)
    )
    assert chunk == b"body"
    run(r.observe("on_done", lambda mw: mw.seen("flow"), entries=r.active))
    assert [n for n, _ in broken.calls] == ["hook"]  # never called again, on_done included
    assert ("seen", "flow") in rewrite.calls
    assert [f.middleware for f in got] == ["broken"]
    assert set(flow_timings) == {"broken", "rewrite"}


def test_mitm_proxy_semantics_deny_and_no_response_hooks_after_a_local_answer() -> None:
    deny, after = Mw("blocklist", result=Action.DENY), Mw("after")
    r = Pipeline([deny, after], MITM).start()
    stopped = run(r.before("on_request", "flow", lambda mw, f: mw.hook(f), until=is_stop))
    assert stopped is not None and (stopped.entry.name, stopped.result) == ("blocklist", "deny")
    assert r.after_pairs() == []  # 0005: on_response never runs for STOP/DENY


def test_a_crashing_gate_blocks_the_flow_when_it_opts_in_to_fail() -> None:
    """The failure-policy note's test: on mitm-proxy's fail-open project, a gate that declares
    on_error="fail" stops the flow instead of letting it through."""
    gate = Mw("blocklist", raises=Boom(), on_error="fail")
    origin = Mw("origin")
    r = Pipeline([gate, origin], MITM).start()
    with pytest.raises(HookFailed) as e:
        run(r.each("on_request", lambda mw: mw.hook("flow"), until=is_stop))
    assert e.value.failure.middleware == "blocklist"
    assert origin.calls == []


class FsError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.errno = code


class SluiceMiddleware:
    name = ""
    ops: frozenset[str] | None = None

    async def pre(self, ctx: dict[str, Any]) -> None:
        return None

    async def post(self, ctx: dict[str, Any], result: Any) -> Any:
        return result

    async def failed(self, ctx: dict[str, Any], error: Exception) -> None:
        return None


SLUICE = Policy(
    on_error="fail",
    after_order="reverse",
    after_local=True,
    observe_failed=True,
    answers=(FsError,),
    skip_inherited=SluiceMiddleware,
)


class Recorder(SluiceMiddleware):
    def __init__(self, name: str, log: list[str], *, respond: Any = None, fail: str = "") -> None:
        self.name = name
        self.log = log
        self.respond = respond
        self.fail = fail

    async def pre(self, ctx: dict[str, Any]) -> None:
        self.log.append(f"pre:{self.name}")
        if self.fail == "pre":
            raise Boom
        if self.fail == "deny":
            raise FsError(errno.EACCES)
        if self.respond is not None:
            ctx["response"] = self.respond

    async def post(self, ctx: dict[str, Any], result: Any) -> Any:
        self.log.append(f"post:{self.name}")
        if self.fail == "post":
            raise OSError(errno.ENOENT, "gone")
        return f"{result}>{self.name}"

    async def failed(self, ctx: dict[str, Any], error: Exception) -> None:
        self.log.append(f"failed:{self.name}")


async def sluice_call(pipeline: Pipeline[Any], op: str = "read") -> Any:
    """sluice's run_pipeline, rebuilt on interpose."""
    ctx: dict[str, Any] = {"op": op}
    r = pipeline.start(select=lambda mw: mw.ops is None or op in mw.ops, state=ctx)
    try:
        await r.before("pre", ctx, lambda mw, c: mw.pre(c), until=lambda _: "response" in ctx)
        result = ctx["response"] if r.stopped is not None else "data"
        return await r.after("post", result, lambda mw, c, v: mw.post(c, v))
    except (HookFailed, FsError) as e:
        error = e.__cause__ if isinstance(e, HookFailed) else e
        assert isinstance(error, Exception)
        await r.observe_ran("failed", lambda mw, c: mw.failed(c, error))
        raise


def test_sluice_semantics_onion_order() -> None:
    log: list[str] = []
    p = Pipeline([Recorder("a", log), Recorder("b", log)], SLUICE)
    assert run(sluice_call(p)) == "data>b>a"
    assert log == ["pre:a", "pre:b", "post:b", "post:a"]


def test_sluice_semantics_post_runs_on_a_local_answer_for_what_ran() -> None:
    log: list[str] = []
    p = Pipeline(
        [Recorder("a", log), Recorder("cache", log, respond="hit"), Recorder("c", log)], SLUICE
    )
    assert run(sluice_call(p)) == "hit>cache>a"
    assert log == ["pre:a", "pre:cache", "post:cache", "post:a"]


def test_sluice_semantics_a_failing_pre_fails_the_call_and_every_hook_that_ran_observes() -> None:
    log: list[str] = []
    p = Pipeline([Recorder("a", log), Recorder("bad", log, fail="pre"), Recorder("c", log)], SLUICE)
    with pytest.raises(HookFailed) as e:
        run(sluice_call(p))
    assert isinstance(e.value.__cause__, Boom)
    # failed() reaches the raiser too (observe_failed=True), in reverse order.
    assert log == ["pre:a", "pre:bad", "failed:bad", "failed:a"]


def test_sluice_semantics_a_deny_passes_through_even_when_skip_is_declared() -> None:
    log: list[str] = []
    auth = Recorder("auth", log, fail="deny")
    auth.on_error = "skip"  # type: ignore[attr-defined]  # a per-middleware policy
    p = Pipeline([auth, Recorder("c", log)], SLUICE)
    with pytest.raises(FsError) as e:
        run(sluice_call(p))
    assert e.value.errno == errno.EACCES
    assert log == ["pre:auth", "failed:auth"]


def test_sluice_semantics_a_hooks_oserror_keeps_its_errno_for_the_mapper() -> None:
    log: list[str] = []
    p = Pipeline([Recorder("redact", log, fail="post")], SLUICE)
    with pytest.raises(HookFailed) as e:
        run(sluice_call(p))
    cause = e.value.__cause__
    assert isinstance(cause, OSError) and cause.errno == errno.ENOENT


def test_sluice_semantics_a_crashing_logger_marked_skip_no_longer_fails_the_read() -> None:
    log: list[str] = []
    logger = Recorder("access_log", log, fail="pre")
    logger.on_error = "skip"  # type: ignore[attr-defined]  # the failure-policy note's fix
    p = Pipeline([logger, Recorder("b", log)], SLUICE)
    assert run(sluice_call(p)) == "data>b"


def test_sluice_semantics_inherited_hooks_are_not_awaited_and_ops_select() -> None:
    class PreOnly(SluiceMiddleware):
        name = "pre_only"
        ops = frozenset({"read"})

        async def pre(self, ctx: dict[str, Any]) -> None:
            ctx["seen"] = True

    p = Pipeline([PreOnly()], SLUICE)
    assert run(sluice_call(p)) == "data"
    assert run(sluice_call(p, op="write")) == "data"


def test_sluice_semantics_a_respond_before_the_pre_phase_stops_after_the_first_pre() -> None:
    # Issue #12, sluice's case: a param() callback answers before any pre hook. sluice checks
    # `ctx.responded` after each pre, so the first (here inherited) pre ends the phase and the
    # next middleware's pre never runs; post still reaches the first.
    log: list[str] = []

    class PostOnly(Recorder):
        pre = SluiceMiddleware.pre

    p = Pipeline([PostOnly("a", log), Recorder("b", log)], SLUICE)
    ctx: dict[str, Any] = {"op": "read", "response": "from-param"}
    r = p.start(state=ctx)
    phase = r.before("pre", ctx, lambda mw, c: mw.pre(c), until=lambda _: "response" in ctx)
    assert run(phase) is not None
    assert run(r.after("post", ctx["response"], lambda mw, c, v: mw.post(c, v))) == "from-param>a"
    assert log == ["post:a"]


def test_sluice_semantics_a_pre_set_on_the_instance_runs() -> None:
    log: list[str] = []
    mw = SluiceMiddleware()

    async def gate(ctx: dict[str, Any]) -> None:
        log.append("gate")
        raise FsError(errno.EACCES)

    mw.pre = gate  # type: ignore[method-assign]  # the instance attribute is what's tested
    with pytest.raises(FsError) as e:
        run(sluice_call(Pipeline([Entry(mw, "gated")], SLUICE)))
    assert e.value.errno == errno.EACCES
    assert log == ["gate"]


def test_run_exposes_its_policy() -> None:
    r: Run[Mw] = Pipeline([Mw("a")], SKIP).start()
    assert r.policy is SKIP
