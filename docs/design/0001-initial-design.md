# 0001: Initial design

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Proposed
Date: 2026-11-01
Authors: andronics; drafted by Claude

## Context

mitm-proxy and sluice are both async interceptors: one runs a middleware chain over HTTP flows, the other
over FUSE calls. Both were generated from project-template, and the template gives each its own copy of
the settings engine (`src/<pkg>/config.py`). The copies are project-owned (`src/**` is in
`_skip_if_exists`), so fixes never travel and the copies have drifted:

| | template | sluice | mitm-proxy |
|---|---|---|---|
| YAML float for an int setting (`0.5`) | silently `0` | `ConfigError` | silently `0` |
| YAML bool for an int setting | silently `1` | `ConfigError` | `ConfigError` |
| YAML list/mapping for a str setting | its repr | `ConfigError` | its repr |
| List settings | no | no (split by hand) | `is_list` |

Each project also needs things the other has already built: sluice's `Secret` type and its secrets-
directory reader (which mitm-proxy needs to configure middleware), mitm-proxy's hand-written Prometheus
exposition (sluice has no metrics), and the pipeline policies the two runners hard-code differently.

interpose is where that shared mechanism lives. This record covers v0.1: **settings and secrets**, the part
with demonstrated drift and no behaviour to argue about. Metrics and the pipeline core get their own
records (see [Consequences](#consequences)).

Constraints:

- The consumer surface stays as it is. `config.SETTINGS` is iterated by each project's `config` CLI
  command, `scripts/gen-wiki.py` and mitm-proxy's `server.py`, which read `key`, `default`, `help`,
  `secret`, `is_list` and `env`. Those keep working unchanged.
- The lookup order is the template's contract, documented in every project's AGENTS.md:
  `<PREFIX>_<KEY>_FILE` > `<PREFIX>_<KEY>` > YAML file > default.
- Dependencies: the stdlib and `pyyaml` (both consumers already have it). Nothing else.
- Distribution: GitHub only. Consumers depend on a release tag:
  `interpose @ git+https://github.com/metalayerlab/interpose@vX.Y.Z`. No PyPI.
- No I/O at import time. Values are never logged, only where each came from.

## Decision

### Package layout

```text
src/interpose/
  __init__.py      # __version__ only; import from the submodules
  settings.py      # Setting, Settings, Resolved, ConfigError, the checks
  secret.py        # Secret, read_secret_dir
  py.typed
```

The submodules are the public API. Anything starting with `_` is private.

### `interpose.settings`

```python
@dataclass(frozen=True)
class Setting:
    key: str                         # dotted: "listen.port"
    default: str | int | bool
    help: str                        # Markdown, as users read it in the generated wiki table
    secret: bool = False
    is_list: bool = False            # a YAML list, or a comma-separated string; default ""
    check: Callable[[Value], None] | None = None   # raises ValueError with a reason
    prefix: str = ""                 # set by Settings, never by hand

    @property
    def env(self) -> str: ...        # prefix + KEY.upper().replace(".", "_")


class Settings(Sequence[Setting]):
    def __init__(
        self,
        prefix: str,                 # "SLUICE_"
        settings: Iterable[Setting],
        *,
        default_path: Path,          # /etc/sluice/config.yaml
    ) -> None: ...

    config_env: str                  # prefix + "CONFIG"

    def resolve(
        self, path: Path | None = None, environ: Mapping[str, str] | None = None
    ) -> Resolved: ...


@dataclass(frozen=True)
class Resolved:
    values: Mapping[str, Value]      # key -> typed value; secret settings are Secret
    sources: Mapping[str, str]       # key -> "SLUICE_X_FILE" | "SLUICE_X" | "file" | "default"


class ConfigError(Exception): ...
```

`Value = str | int | bool | tuple[str, ...] | Secret`.

- `Settings` binds `prefix` onto each `Setting` (a `dataclasses.replace` copy), so `setting.env` works
  wherever a project iterates `SETTINGS` today. It rejects duplicate keys, keys that don't match
  `[a-z0-9_]+(\.[a-z0-9_]+)*`, and a list setting whose default isn't `""`.
- `resolve()` reads the YAML file (a missing file means defaults; a non-mapping top level is a
  `ConfigError`), applies the lookup order and coerces each value:
  - **bool:** a bool, or one of `1/true/yes/on` and `0/false/no/off`, in any case.
  - **int:** an int (not a bool) or a decimal string. A float, bool, list or mapping is an error.
  - **str:** a scalar. A list, mapping or set is an error, never its repr.
  - **list:** a YAML list of scalars, or a comma-separated string. Items are stripped and empties
    dropped.
  - **`_FILE`:** the file's contents, trailing whitespace stripped. Unreadable is a `ConfigError` that
    names the variable and the path.
  - Then `check`, if set. Its `ValueError` becomes `ConfigError("<key>: <reason>")`.
  - A `secret=True` setting's value is wrapped in `Secret` after coercion.
- Messages name the key and the expected type. They never contain a secret setting's value.
- Single-value checks ship as functions: `port`, `positive`, `non_negative`, `one_of(*choices)` (case-
  insensitive), and `host_patterns` is left to mitm-proxy. Cross-setting rules (`ca.cert` with `ca.key`)
  stay in the project's `load_config`.

A project's `config.py` keeps its `Config` dataclass and `load_config`, which become:

```python
SETTINGS = Settings("SLUICE_", (Setting(...), ...), default_path=Path("/etc/sluice/config.yaml"))
CONFIG_ENV = SETTINGS.config_env

def load_config(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    r = SETTINGS.resolve(path, environ)
    # cross-setting rules here
    return Config(log_level=str(r.values["log_level"]).upper(), ..., sources=dict(r.sources))
```

`from interpose.settings import ConfigError` is re-exported by each project's `config.py`, so `except
ConfigError` and the CLI's "one line, exit 2" behaviour stay as they are.

### `interpose.secret`

```python
class Secret:
    """str() and repr() are masked; reveal() gives the value. Equality compares values."""
    def reveal(self) -> str: ...

MASK = "********"

def read_secret_dir(directory: Path) -> Mapping[str, Secret]:
    """Each regular file -> Secret(text minus one trailing newline). Missing dir -> empty.
    Skips dot-entries (Kubernetes' ..data and timestamped dirs). Unreadable -> ConfigError."""
```

This is sluice's `context.Secret` and `_read_secrets`, moved as they are. The `Secret` class is
`__slots__`-based and doesn't subclass `str`, so it can't reach a log format or an f-string as its value
by accident.

### Versioning and releases

- release-please, Conventional Commits. While below 1.0, a breaking change is a minor bump marked `!`.
- Consumers pin a tag. interpose never asks a consumer to track `main`.
- Each public name has a test in interpose. A consumer's behaviour test belongs to the consumer.

## Alternatives considered

- **Keep the copies and fix each by hand (status quo):** the table in [Context](#context) is the result.
  Three copies, three behaviours.
- **A template-owned engine file (`src/<pkg>/_settings.py`, updated by the template PR), no library:** no
  new repo or dependency, and it would fix the drift. It lost because it only covers settings. `Secret`,
  metrics and the pipeline core are needed by some projects, not all, and don't belong in every generated
  project. It would also tie engine fixes to the template's update cadence. The template's part is the
  thin `config.py` on top of interpose ([project-template#84](https://github.com/metalayerlab/project-template/issues/84)).
- **pydantic-settings or dynaconf:** large dependencies with their own precedence rules. Neither has the
  `_FILE` convention exactly as the template defines it, and neither carries the per-setting `help`
  metadata the generated wiki table needs.
- **`Setting` with a global prefix (module-level `ENV_PREFIX`, as today):** works for one project, but a
  library can't hold a global for its caller. Binding through `Settings` keeps `setting.env` without one.
- **Publishing to PyPI:** not needed while the consumers are our own repos. A git tag is enough, and we
  avoid owning a PyPI name.
- **A float type:** no setting needs one yet. Time settings are integer `_ms` by convention.

## Consequences

- One coercion behaviour everywhere. mitm-proxy gets sluice's fixes; sluice gets `is_list` (its
  `env.expose` stops being split by hand).
- Each consumer's switch is a refactor with no change to its config keys or environment variables. It
  adds a dependency, though, so each consumer writes a short design record of its own (both AGENTS.md
  files require one for a new dependency).
- `secret=True` values become `Secret`, so a consumer's `Config` holds `Secret` and calls `.reveal()` at
  the point of use. mitm-proxy's `proxy_auth: str = field(repr=False)` becomes `proxy_auth: Secret`.
- The template's `config.py.jinja` becomes a thin file on top of interpose (project-template#84, part 2).
  Until then the consumers switch by hand.
- A interpose fix reaches a consumer through a version bump, not a copy-paste. That makes interpose's tests the
  only guard for every consumer at once, so its coverage floor matters more than usual.
- Later records, in this order:
  - **0002 metrics:** mitm-proxy's `Counter`/`Gauge`/`Histogram` text exposition, with labels limited to
    declared value sets so the "no unbounded labels" rule is enforced, not documented.
  - **0003 pipeline core:** a runner with per-project policies (after-hook order, failure policy, error
    mapper, after-hooks on local answers) and per-middleware ones (`on_error`, timeout). It must
    reconcile mitm-proxy's "decided against" entries (designs 0001, 0005, 0006) before approval.
- Wiki pages: Introduction (what interpose is and isn't), Quick-Start (a consumer's `config.py` before and
  after), Architecture-Internals (the lookup and coercion rules), Release-Process (pinning tags).

## Open questions

1. **Should `secret=True` values resolve to `Secret`?** Recommend yes. It's the only change consumers
   notice, but the point of the type is that a secret can't be logged by accident, and settings are
   where secrets enter.
2. **A per-setting `check`, or all validation in `load_config`?** Recommend `check` for single-value
   rules (ports, positives, choices). Both consumers repeat these helpers, and the error message format
   becomes uniform. Cross-setting rules stay in the project.
3. **Should v0.1 also include sluice's env allowlist (`env.expose` → a frozen mapping)?** Recommend not
   yet. It's six lines, and mitm-proxy's middleware-config design will show whether it's the right shape.
4. **How do consumers hear about a new interpose tag?** I haven't verified that Dependabot's `uv` support
   updates `git+https` dependencies pinned to a tag. Recommend checking before approval. The fallback is
   a scheduled workflow in each consumer that opens a PR when a newer tag exists.
5. **Name (resolved 2026-11-01):** `weir`, the first choice, is taken on PyPI (a ZFS API, v0.4.0), and
   an installed copy would shadow the import even though we never publish there. `interpose` was free on
   PyPI and GitHub, so the repo was generated under that name. Nothing to answer.
