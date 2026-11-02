# API Reference

## Public API

Import from the submodules, `interpose.settings` and `interpose.secret`. The package itself exports only `__version__` and `__description__`. Anything starting with `_` is internal.

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
- Raises `ValueError` for a bad or duplicate key, a list setting whose default isn't `""`, or a secret setting that isn't a str setting.
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

## Stability

The public API follows [semantic versioning](https://semver.org/): breaking changes only in a major release (a minor one before 1.0), and they're announced in the CHANGELOG.

Everything above is stable within 0.1.x. The message text of `ConfigError` and `ValueError` is for people: match on the exception type, not the words.
