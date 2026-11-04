# Quick Start

From nothing to a working interpose in a few minutes. Everything else is in [API Reference](API-Reference).

## Prerequisites

- For running from source: Python >= 3.13 and [uv](https://docs.astral.sh/uv/)
- Access to GitHub: interpose is installed from a release tag, not from PyPI. It brings `pyyaml`.

## Install

Pin a release tag, never `main`:

```sh
uv add "interpose @ git+https://github.com/metalayerlab/interpose@v0.1.0"
```

To move to a newer release, change the tag in `pyproject.toml` and run `uv lock`. Dependabot can't do this for you yet (see [FAQ](Frequently-Asked-Questions#does-dependabot-update-interpose)).

## First run

A project's `config.py` declares its settings and turns the resolved values into its own `Config`:

```python
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from interpose.secret import Secret
from interpose.settings import ConfigError, Setting, Settings, one_of, port

__all__ = ["CONFIG_ENV", "SETTINGS", "Config", "ConfigError", "load_config"]

SETTINGS = Settings(
    "MYAPP_",
    (
        Setting(
            "log_level", "INFO", "Log level.", check=one_of("DEBUG", "INFO", "WARNING", "ERROR")
        ),
        Setting("listen.port", 8080, "Port the API listens on.", check=port),
        Setting("upstream.token", "", "Token for the upstream API.", secret=True),
        Setting("allow.hosts", "", "Hosts to allow.", is_list=True),
    ),
    default_path=Path("/etc/myapp/config.yaml"),
)
CONFIG_ENV = SETTINGS.config_env  # "MYAPP_CONFIG"


@dataclass(frozen=True)
class Config:
    log_level: str
    port: int
    token: Secret
    hosts: tuple[str, ...]
    sources: Mapping[str, str] = field(default_factory=dict)


def load_config(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    r = SETTINGS.resolve(path, environ)
    # Rules that span settings go here, raising ConfigError.
    # Each accessor checks the setting's kind: r.str() on a secret setting is a TypeError.
    return Config(
        log_level=r.str("log_level"),  # one_of gives the declared spelling: "debug" -> "DEBUG"
        port=r.int("listen.port"),
        token=r.secret("upstream.token"),  # reveal() at the point of use
        hosts=r.list("allow.hosts"),
        sources=dict(r.sources),
    )
```

With this `/etc/myapp/config.yaml`:

```yaml
listen:
  port: 9090
allow:
  hosts: [example.org, "*.example.org"]
```

and `MYAPP_UPSTREAM_TOKEN_FILE=/run/secrets/token` in the environment, `load_config()` gives:

```text
Config(log_level='INFO', port=9090, token=Secret('********'), hosts=('example.org', '*.example.org'),
       sources={'log_level': 'default', 'listen.port': 'file',
                'upstream.token': 'MYAPP_UPSTREAM_TOKEN_FILE', 'allow.hosts': 'file'})
```

`config.token.reveal()` gives the token at the point of use. A bad value stops it with one line, such as `listen.port: expected int, got 0.5 (from /etc/myapp/config.yaml)`.

For secrets that aren't settings (a middleware's API keys, say), `read_secret_dir(Path("/run/secrets"))` reads each file in a directory as a `Secret`.

## Next steps

- [API Reference](API-Reference): the public API
- [Troubleshooting](Troubleshooting) if something doesn't work
