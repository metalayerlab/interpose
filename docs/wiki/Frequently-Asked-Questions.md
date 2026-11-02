# Frequently Asked Questions

## Why isn't interpose on PyPI?

Its consumers are metalayerlab's own repositories, and a GitHub tag is enough for them (design 0001). Install it with `uv add "interpose @ git+https://github.com/metalayerlab/interpose@vX.Y.Z"`. See [Quick Start](Quick-Start).

## Does Dependabot update interpose?

Not yet. Dependabot's `uv` support drops the git URL of a `name @ git+https://...@vX.Y.Z` dependency and looks the bare name up on PyPI, so it never proposes a new tag ([dependabot-core#16129](https://github.com/dependabot/dependabot-core/issues/16129)). [dependabot-core#16151](https://github.com/dependabot/dependabot-core/pull/16151), still open, would update a `[tool.uv.sources]` entry with `tag = "..."`. Until then, bump the tag by hand or with a scheduled workflow. Design 0001's open question 4 has the details.

## Why does a YAML `0.5` fail for an int setting, when my project's old `config.py` took it?

The old copy called `int()`, which truncates `0.5` to `0` without a word. interpose refuses any value that isn't the declared type. See [Architecture Internals](Architecture-Internals#data-flow).

## Where do rules between settings go?

In the project's `load_config`, after `SETTINGS.resolve()`, raising `ConfigError`. `check=` is for rules about one value. See [Quick Start](Quick-Start#first-run).

## Why isn't `Secret` a `str`?

So it can't be logged or formatted as its value by accident. A `str` subclass would pass straight into f-strings and log calls. See [Security](Security#secrets).
