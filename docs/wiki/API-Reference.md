# API Reference

## Public API

Import from the submodules: `interpose.settings`, `interpose.secret`, `interpose.metrics` and `interpose.pipeline`. The package itself exports only `__version__` and `__description__`. Anything starting with `_` is internal.

### `interpose.settings`

**`Setting(key, default, help, *, secret=False, is_list=False, check=None)`**: one setting, a frozen dataclass.

- `key`: dotted lower case, `[a-z0-9_]+` joined by dots (`listen.port`).
- `default`: a `str`, `int` or `bool`. Its type is the setting's type. A list setting's default is `""`.
- `help`: Markdown, as users read it in the generated settings table.
- `secret`: the value resolves to a `Secret`. Only for str settings.
- `is_list`: the value is a `tuple[str, ...]`, from a YAML list or a comma-separated string.
- `check`: a callable that gets the coerced value and raises `ValueError(reason)` if it's invalid. It may return a replacement of the setting's kind (a `str`, an `int` that isn't a `bool`, a `bool`, a `tuple` of `str`); `None` keeps the value. Returning the value unchanged keeps it. A replacement of another kind is a `ConfigError` naming the key and the type, never the value. A check on a secret setting must return `None` (or the value unchanged).
- `env` (property): the environment variable, the prefix plus the upper-cased key with dots as underscores (`MYAPP_LISTEN_PORT`). `prefix` is set by `Settings`, never by hand.

**`Settings(prefix, settings, *, default_path)`**: a `Sequence[Setting]`, each one bound to `prefix`.

- `config_env`: `prefix + "CONFIG"`, the variable that names the YAML file. Also `prefix` and `default_path`.
- Raises `ValueError` for a bad or duplicate key, a key part YAML doesn't read as a string (`on`, `no`, `null`, `1`), two settings that would read the same variable (`a.b` and `a_b`; `x_file` and `x`'s `_FILE`; a key `config`), a list setting whose default isn't `""`, or a secret setting that isn't a str setting.
- **`resolve(path=None, environ=None) -> Resolved`**: reads `path` (else `$<PREFIX>CONFIG`, else `default_path`; missing means defaults) and `environ` (else `os.environ`), and resolves every setting. Raises `ConfigError`.

```python
r = SETTINGS.resolve(Path("config.yaml"), {"MYAPP_LISTEN_PORT": "9090"})
r.values["listen.port"], r.sources["listen.port"]  # (9090, "MYAPP_LISTEN_PORT")
```

**`Resolved(values, sources)`**: frozen, read-only mappings keyed by setting key, in declaration order.

- `values[key]`: the typed value, a `Value` (`str | int | bool | tuple[str, ...] | Secret`).
- `sources[key]`: `"<PREFIX><KEY>_FILE"`, `"<PREFIX><KEY>"`, `"file"` or `"default"`.
- **Typed accessors** `str(key)`, `int(key)` (never a `bool`), `bool(key)`, `list(key)` (a `tuple[str, ...]`) and `secret(key)` (a `Secret`) give a value with its type instead of a `cast()`. Each raises `TypeError` naming the key when the setting is another kind; on a secret setting, every one but `secret()` does, so a masked `"********"` can't reach a credential. A key that isn't declared is a `KeyError`.

```python
r.str("log_level")  # "INFO"
r.int("listen.port")  # 9090
r.secret("upstream.token").reveal()
# TypeError: 'upstream.token' is a secret setting, not str: use secret() and reveal()
r.str("upstream.token")
```

**`ConfigError`**: the configuration can't be read, or a value in it is invalid. The message names the key, the expected type and the source, and never holds a secret setting's value.

**Checks**, for `check=`. Each raises `ValueError`, which `resolve()` turns into `ConfigError("<key>: <reason>")`; `one_of` also replaces the value:

| Check | Passes |
|---|---|
| `port` | an int from 1 to 65535 |
| `positive` | an int above 0 |
| `non_negative` | an int, 0 or more |
| `one_of(*choices)` | a str equal to one of `choices`, in any case; the value becomes the declared spelling (`info` → `INFO`) |

### `interpose.secret`

**`Secret(value: str)`**: a secret value.

- `str()`, `repr()`, `format()` and f-strings give `********` (`MASK`); `reveal()` gives the value.
- Equal to another `Secret` with the same value (compared in constant time), never to a plain `str`. Hashable. Falsy when the value is empty.
- Raises `TypeError` for anything but a `str`.

**`read_secret_dir(directory: Path) -> Mapping[str, Secret]`**: each regular file in `directory` (symlinks followed) as a `Secret`, keyed by file name, minus one trailing newline. Dot-entries are skipped, which covers a Kubernetes secret volume's `..data`. A missing directory gives an empty mapping. Raises `ConfigError` if the directory or a file can't be read.

**`MASK`**: `"********"`.

### `interpose.metrics`

Prometheus text exposition (format 0.0.4). Every label takes its values from a declared set, so the series count is fixed by code and configuration, never by traffic.

**`Label(name, values=())`**: a label name and the only values it may take.

- `name`: matches `[a-zA-Z_][a-zA-Z0-9_]*`, not starting with `__`.
- `values`: an iterable of `str` (a list, not a bare string: `Label("kind", "ca")` is a `TypeError`); read back as a `frozenset[str]`. `value in label` tests one.
- **`declare(*values)`**: allow more values, from configuration (the middleware names a reload brings), never from measured data. Values are never removed. One `Label` can be shared by several metrics, so one `declare` covers all of them.

**`Counter(name, help, labels=())`**: `inc(*values, amount=1.0)`. `amount` must be finite and zero or more.

**`Gauge(name, help, labels=(), read=None)`**: `set(value, *values)`, `add(amount, *values)`. With `read` (a callable returning `{label values: number}`), the samples come from it when the gauge renders.

**`Histogram(name, help, buckets, labels=())`**: `observe(value, *values)`. Classic cumulative buckets (inclusive), plus `_sum` and `_count`; `+Inf` is added for you. Buckets are finite, distinct and sorted for you. `NaN` is refused. `samples()` gives each label set's count.

- `labels` is a sequence of `Label`. Each call passes one value per label, in order. A value outside its label's set, or the wrong number of values, raises `ValueError`, which names the metric and the label but never the value.
- Declaration errors are `ValueError` (or `TypeError` for a label that isn't a `Label`): a bad metric or label name, a repeated label name, a histogram label called `le`, bad buckets.

**`Registry(metrics=())`**: the metrics one endpoint exports.

- **`register(metric) -> metric`**: adds it and returns it. A second metric with the same name, or one whose samples clash with another's (a counter `foo_count` next to a histogram `foo`), raises `ValueError`.
- Iterates in registration order; `len()` counts the metrics.
- **`render() -> str`**: every metric's `# HELP`, `# TYPE` and samples (sorted by label values), joined by newlines and ending in one; `""` when empty.

**`CONTENT_TYPE`**: `"text/plain; version=0.0.4; charset=utf-8"`, for an HTTP response's `Content-Type`.

```python
from interpose.metrics import CONTENT_TYPE, Counter, Label, Registry

OUTCOME = Label("outcome", ["ok", "denied", "error"])
REGISTRY = Registry()
CALLS = REGISTRY.register(Counter("myapp_calls_total", "Calls, by outcome.", (OUTCOME,)))

CALLS.inc("ok")
CALLS.inc("example.org")  # ValueError: label 'outcome' got a value outside its declared set
body = REGISTRY.render()  # serve with CONTENT_TYPE, or as a file
```

interpose doesn't serve the text: an HTTP endpoint or a virtual file is the project's.

### `interpose.pipeline`

The middleware loop and the guard around every hook. interpose defines no hooks and no unit of work: the project keeps its own `Middleware` class and unit (a flow, a call), and passes `invoke` callables that call its hooks.

**`Policy(on_error, after_order, after_local, observe_failed, *, timeout=None, answers=(), skip_inherited=None)`**: the project's answers, a frozen dataclass. The first four have no default.

- `on_error`: `"skip"` (a failing middleware is out for the rest of the unit, which carries on) or `"fail"` (`HookFailed` is raised), unless the middleware says.
- `after_order`: `"chain"` (as registered) or `"reverse"` (the onion).
- `after_local`: whether after hooks run when a before hook stopped the unit (answered locally).
- `observe_failed`: whether observer hooks still reach a middleware that has failed.
- `timeout`: seconds per hook call, unless the middleware says; `None` is no limit.
- `answers`: exception types a hook raises as its answer (a deny), re-raised unchanged whatever `on_error` says.
- `skip_inherited`: the project's base class, whose hooks must do nothing; a hook a middleware doesn't override, in its class or on the instance (`mw.pre = gate` counts as an override), isn't called (no timing). In `each` and `before` it counts as returning `None`, so `until` is still asked (a stop that reads the unit's state still happens there); elsewhere it gives `SKIPPED`.
- Raises `ValueError` for a value outside these.

**`Entry(middleware, name, on_error=None, timeout=None)`**: one middleware in a chain with its own policy (`None`: the project's; a `timeout` of **`NO_LIMIT`** opts it out of the project's limit). Compared by identity. **`Entry.of(middleware, name=None)`** reads the middleware's `name`, `on_error` and `timeout` attributes; `name=` wins. **`name_of(middleware)`**: its `name` attribute if a non-empty `str`, else its class name.

**`Pipeline(middleware, policy)`**: a frozen chain (middleware or `Entry`s) and its policy. `entries`, `names`, `policy`.

- **`start(*, select=None, select_hook="select", on_failure=None, timings=None, state=None) -> Run`**: one unit's run. `select(middleware) -> bool` picks what applies, once, now; one that raises fails like a hook named `select_hook`. `on_failure(Failure)` is called for every failure (log and count it there). `timings` and `state`, if given, are filled in place.

**`Run`**: one unit's pass. `entries` (selected), `active` (minus failed), `timings` (seconds per middleware name), `state`, `ran` (`(entry, context)` pairs), `stopped`, `policy`, `has_failed(entry)`.

| Method | Calls | Over (default) | Returns |
|---|---|---|---|
| `call(entry, hook, invoke, *, valid=None)` | `invoke()` | one entry | the result, or `SKIPPED` |
| `each(hook, invoke, *, entries, until, valid)` | `invoke(mw)` until `until(result)` | `active` | `Stopped(entry, result)` or `None` |
| `fold(hook, value, invoke, *, entries, valid, keep_none=False)` | `invoke(mw, value)`; the result replaces `value` | `active` | the value |
| `observe(hook, invoke, *, entries)` | `invoke(mw)`; always fails open | `entries` | `None` |
| `before(hook, context, invoke, *, entries, until, valid)` | `invoke(mw, context)`, recording `(entry, context)` in `ran` first; a stop is kept in `stopped` | `active` | `Stopped` or `None` |
| `after_pairs()` | | `ran` in `after_order`, minus failed; `[]` after a stop unless `after_local` | the pairs |
| `after(hook, value, invoke, *, valid, keep_none=False)` | `invoke(mw, context, value)` | `after_pairs()` | the value |
| `observe_ran(hook, invoke, *, pairs)` | `invoke(mw, context)`; always fails open | `ran` in `after_order` | `None` |
| `scoped(entry)` | | | a `ScopedState` |

- **The guard**, around every call: skips a failed entry (observers: unless `observe_failed`) or an inherited hook; times the call into `timings[name]`; bounds it with the entry's or the policy's timeout; re-raises `answers` (not in observers); anything else it raises, or a result `valid` refuses (or a `valid` that raises), is a failure: the entry leaves `active`, `on_failure` is called, and under `"fail"` `HookFailed` is raised (under `"skip"`, `SKIPPED` is returned). `CancelledError` is never caught. A hook that returns after its limit (it ran past the deadline in synchronous code, `late="overran"`, or caught its cancellation, `late="swallowed"`) has its result discarded: it is a timeout failure (`timed_out=True`, a `TimeoutError`), handled by `on_error`.
- **Hooks and timeouts:** a timeout cancels the hook at its current `await`. Hooks must be cancel-safe (cleanup in `try/finally` or `async with`) and must never swallow `CancelledError` or `BaseException`. Blocking or CPU-bound work goes off the event loop: a timeout can't interrupt synchronous code. A thread started with `run_in_executor` or `asyncio.to_thread` keeps running after its `await` is cancelled.
- `keep_none=True` makes a `None` result keep the value (a chunk hook that passes a chunk on).
- `until` is the project's predicate and must not raise. Under `"fail"`, `start()` raises `HookFailed` when a `select` fails.

**`Failure(middleware, hook, error, timed_out, action, timeout, late=None)`**: what failed, with what, and whether the guard's timeout did it; `action` is `"skip"` or `"fail"`; `timeout` is the limit that applied (seconds, or `None`); `late` (**`Late`**) is `"overran"` or `"swallowed"` for a result that came back after the limit, else `None`. Holds no unit data.

**`HookFailed`**: raised under `"fail"`. `failure` is the `Failure`; `__cause__` is what the hook raised (a `TimeoutError` for a timeout). The message names the middleware and the hook, never a value.

**`InvalidResult`**: the `Failure.error` for a result `valid` refused; names the result's type only.

**`Stopped(entry, result)`**: where a phase stopped. **`SKIPPED`**: a call that gave no result (falsy). **`ScopedState(state, name)`**: a `MutableMapping` view storing keys as `"<name>.<key>"`.

**`require_async(middleware, hooks, *, name=None)`**: `TypeError` if any of `hooks` the middleware has isn't `async def`, for refusing sync hooks at registration.

```python
from interpose.pipeline import HookFailed, Pipeline, Policy

POLICY = Policy(on_error="fail", after_order="reverse", after_local=True, observe_failed=True)
PIPELINE = Pipeline(chain, POLICY)  # built once


async def handle(ctx):
    run = PIPELINE.start(select=lambda mw: mw.applies(ctx), state=ctx.state)
    try:
        await run.before("pre", ctx, lambda mw, c: mw.pre(c), until=lambda _: ctx.responded)
        result = ctx.response if run.stopped else await operation(ctx)
        return await run.after("post", result, lambda mw, c, r: mw.post(c, r))
    except HookFailed as e:
        await run.observe_ran("failed", lambda mw, c: mw.failed(c, e))
        raise MyDeny(e.failure.middleware) from e
```

## Stability

The public API follows [semantic versioning](https://semver.org/): breaking changes only in a major release (a minor one before 1.0), and they're announced in the CHANGELOG.

Everything above is stable within 0.1.x. The message text of `ConfigError` and `ValueError` is for people: match on the exception type, not the words.
