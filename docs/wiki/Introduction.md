# Introduction

## What it is

The mechanism metalayerlab's interceptors would otherwise each copy. mitm-proxy (an HTTP/TLS proxy) and sluice (a FUSE filesystem) were generated from the same template, and each got its own copy of the settings engine in a project-owned `config.py`. Template fixes never reached those copies, and they drifted: the same YAML value was coerced three different ways, two of them silently wrong. interpose holds one engine, and a fix reaches every consumer as a version bump.

It holds **mechanism, not policy**. A project keeps its own settings, its `Config` dataclass and its rules that span settings. interpose never touches transports (HTTP, TLS, FUSE): it renders the metrics text, and the project serves it.

| Version | What |
|---|---|
| v0.1 | `interpose.settings` (the layered settings engine) and `interpose.secret` (`Secret`, `read_secret_dir`) |
| next release | `interpose.metrics`: Prometheus text exposition with declared label sets (design 0002) |
| later | the middleware pipeline core (design 0003) |

## How it works

A project declares its settings once, as `Setting(key, default, help, ...)` entries in a `Settings` object bound to its environment-variable prefix and default config path. `Settings.resolve()` reads the YAML file, then gives every setting the first value found in this order:

1. `<PREFIX><KEY>_FILE`: a file holding the value (Docker and Kubernetes secrets)
2. `<PREFIX><KEY>`: an environment variable
3. the YAML file (`<PREFIX>CONFIG`, else the default path)
4. the declared default

The default's type is the setting's type, and coercion is strict: a YAML `0.5` for an int setting is an error, not `0`. The result, `Resolved`, holds the typed values and where each came from. A `secret=True` setting comes back as a `Secret`, which prints as `********` until `reveal()` is called. The same declarations generate each project's wiki settings table. The rules are on [Architecture Internals](Architecture-Internals).

## When not to use it

- **Outside metalayerlab's own projects.** It isn't on PyPI, and its API is shaped by its consumers' needs. For general use, pydantic-settings or dynaconf do more.
- **Float, nested-object or per-request settings.** Values are str, int, bool or a list of strings, read once at startup.
- **Rules between settings** (two settings set together or not at all). Write those in the project's `load_config`, after `resolve()`.
