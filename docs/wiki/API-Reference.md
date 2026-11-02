# API Reference

## Public API

Import from the submodules: `interpose.settings`, `interpose.secret` and `interpose.metrics`. The package itself exports only `__version__` and `__description__`. Anything starting with `_` is internal.

### `interpose.settings`

**`Setting(key, default, help, *, secret=False, is_list=False, check=None)`**: one setting, a frozen dataclass.

- `key`: dotted lower case, `[a-z0-9_]+` joined by dots (`listen.port`).
- `default`: a `str`, `int` or `bool`. Its type is the setting's type. A list setting's default is `""`.
- `help`: Markdown, as users read it in the generated settings table.
- `secret`: the value resolves to a `Secret`. Only for str settings.
- `is_list`: the value is a `tuple[str, ...]`, from a YAML list or a comma-separated string.
- `check`: a callable that gets the coerced value and raises `ValueError(reason)` if it's invalid.
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

**`ConfigError`**: the configuration can't be read, or a value in it is invalid. The message names the key, the expected type and the source, and never holds a secret setting's value.

**Checks**, for `check=`. Each raises `ValueError`, which `resolve()` turns into `ConfigError("<key>: <reason>")`:

| Check | Passes |
|---|---|
| `port` | an int from 1 to 65535 |
| `positive` | an int above 0 |
| `non_negative` | an int, 0 or more |
| `one_of(*choices)` | a str equal to one of `choices`, in any case (the value keeps its case) |

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

## Stability

The public API follows [semantic versioning](https://semver.org/): breaking changes only in a major release (a minor one before 1.0), and they're announced in the CHANGELOG.

Everything above is stable within 0.1.x. The message text of `ConfigError` and `ValueError` is for people: match on the exception type, not the words.
