# interpose

[![CI](https://github.com/metalayerlab/interpose/actions/workflows/base-ci.yml/badge.svg)](https://github.com/metalayerlab/interpose/actions/workflows/base-ci.yml)
[![Release](https://img.shields.io/github/v/release/metalayerlab/interpose?sort=semver)](https://github.com/metalayerlab/interpose/releases)

Shared mechanism for metalayerlab interceptors — the layered settings loader, Secret values, Prometheus text exposition, and a policy-configurable async middleware pipeline.

Every metalayerlab interceptor (mitm-proxy, sluice) needs the same settings engine: each setting declared
once, read from a `_FILE` variable, the environment, a YAML file or its default, and typed strictly. Copies
of it drifted, so it lives here, with the `Secret` type that keeps secrets out of logs. Settings and secrets
are v0.1; Prometheus output and the middleware pipeline core follow.

## Quick Start

Pin a release tag (interpose isn't on PyPI):

```sh
uv add "interpose @ git+https://github.com/metalayerlab/interpose@v0.1.0"
```

```python
from pathlib import Path

from interpose.settings import Setting, Settings, one_of, port

SETTINGS = Settings(
    "MYAPP_",
    (
        Setting("log_level", "INFO", "Log level.", check=one_of("DEBUG", "INFO", "ERROR")),
        Setting("listen.port", 8080, "Port to listen on.", check=port),
        Setting("upstream.token", "", "API token.", secret=True),
        Setting("allow.hosts", "", "Hosts to allow.", is_list=True),
    ),
    default_path=Path("/etc/myapp/config.yaml"),
)

r = (
    SETTINGS.resolve()
)  # MYAPP_<KEY>_FILE > MYAPP_<KEY> > $MYAPP_CONFIG or the default path > default
r.values["listen.port"]  # 8080, an int; a YAML 0.5 or true is a ConfigError
r.sources["listen.port"]  # "default", "file", "MYAPP_LISTEN_PORT" or "MYAPP_LISTEN_PORT_FILE"
r.values["upstream.token"]  # Secret('********'); .reveal() gives the value
r.values["allow.hosts"]  # ("example.org", ...) from a YAML list or "a, b"
```

The full walk-through is in the wiki's [Quick Start](https://github.com/metalayerlab/interpose/wiki/Quick-Start).

## Documentation

Everything is in the **[wiki](https://github.com/metalayerlab/interpose/wiki)**. Its source is [`docs/wiki/`](docs/wiki/), in this
repository, reviewed with the code.

- [Introduction](https://github.com/metalayerlab/interpose/wiki/Introduction): what it is and how it works
- [API Reference](https://github.com/metalayerlab/interpose/wiki/API-Reference): the public API
- [Troubleshooting](https://github.com/metalayerlab/interpose/wiki/Troubleshooting) and [FAQ](https://github.com/metalayerlab/interpose/wiki/Frequently-Asked-Questions)
- [Contributing](https://github.com/metalayerlab/interpose/wiki/Contributing) and [Release Process](https://github.com/metalayerlab/interpose/wiki/Release-Process)

## For AI assistants

Built with AI assistants; every change gets human review. **[AGENTS.md](AGENTS.md) and
[.agents/stack.md](.agents/stack.md) are the only instruction files** (stack, commands, rules, definition of
done, docs map). Read them instead of this README.

| Assistant | Entry point |
|---|---|
| Claude Code | [CLAUDE.md](CLAUDE.md) (imports both) + [.claude/settings.json](.claude/settings.json) |
| Codex, Cursor, Copilot coding agent | AGENTS.md, natively ([Copilot](.github/copilot-instructions.md), [Cursor](.cursor/rules/project.mdc) pointers) |
| Gemini CLI | [GEMINI.md](GEMINI.md) (imports both) |
| Chat without repo access | [docs/ai/prompts.md](docs/ai/prompts.md), "Context for a chat assistant" |

Humans: kick-off prompts are in [docs/ai/prompts.md](docs/ai/prompts.md), and the review checklist in
[docs/ai/review-checklist.md](docs/ai/review-checklist.md).

## Development

```sh
uv sync                           # Python >= 3.13, dev tools included
uv run pytest                     # tests (80% coverage floor)
uv run ruff check && uv run mypy  # lint and type check
```

New project? Fill in `.github/repository.toml` (the description, `homepage` = the wiki, `topics`), then run
`python3 scripts/repo-metadata.py` to set GitHub's About box from it; CI warns when the two drift.

The workflow is: a branch, Conventional Commit PR titles, squash merge. Releases are automated by
release-please. See [Contributing](https://github.com/metalayerlab/interpose/wiki/Contributing).

This project was generated from [template-base](https://github.com/andronics/template-base) and
[template-python](https://github.com/andronics/template-python). Template
fixes arrive as a weekly *Template update* PR per template.

## License

Released into the public domain under the [Unlicense](LICENSE).
