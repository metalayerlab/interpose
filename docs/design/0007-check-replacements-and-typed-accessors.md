# 0007: Check replacements and typed accessors

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: andronics; drafted by Claude

## Context

Two gaps in `interpose.settings` ([0001](0001-initial-design.md)), both found by the consumers:

- **[#8](https://github.com/metalayerlab/interpose/issues/8).** `one_of(*choices)` matches in any case
  but `resolve()` keeps the case it was given, and a check returns `None`, so it can't fix the
  spelling. mitm-proxy and sluice both upper-case `log_level` in `load_config` because `logging` wants
  upper case, and every similar setting repeats it.
- **[#13](https://github.com/metalayerlab/interpose/issues/13).** `Resolved.values` is a
  `Mapping[str, Value]`, so every consumer narrows each value with `cast()`, which is unchecked.
  `cast(str, values["proxy.auth"])` on a secret setting type-checks, and `str()` of the `Secret` then
  sends `********` as the credential, silently (mitm-proxy hit this).

Stephen chose the shape of both on 2026-11-02.

## Decision

### A check may return a replacement (#8)

- `Setting.check` becomes `Callable[[Value], Value | None]`. `None` keeps the value; anything else
  replaces it. Checks that return `None` (all of interpose's, and the consumers') are unaffected. A
  check that returned something else had its result ignored; now it replaces the value, or is a
  `ConfigError` if it is another kind.
- The replacement must be the setting's kind: a `str` for a str setting, an `int` (not a `bool`) for an
  int setting, a `bool` for a bool setting, a `tuple` of `str` for a list setting. Anything else is a
  `ConfigError` naming the key and the replacement's type, never its value:
  `log_level: its check returned int, expected str` (a Python `list` for a list setting is refused
  too: `expected a tuple of str`). The check isn't run again on its replacement. A check that returns
  the value it was given, unchanged (same type, equal), keeps it: the "validate and return the value"
  style is not a replacement, for a secret setting too.
- **No replacement for a secret setting.** A check on a `secret=True` setting must return `None` (or
  the value unchanged); anything else is `ConfigError("<key>: a check can't replace a secret setting's value")`. A secret's
  value is what the operator put in the file or variable; a second, computed spelling of it is
  something nobody configured, and a check that builds one has handled the value in a way 0001's
  masking can't follow. "Only another `Secret`" was the other option; nothing needs it, and it can be
  added later without breaking anyone. A check's `ValueError` reason is still masked as 0001's review
  fixes made it.
- **`one_of` returns the declared spelling, always** (no flag): `LOG_LEVEL=info` resolves to `"INFO"`
  when `"INFO"` is declared, and so does a default spelled `"info"`. A value already spelled as
  declared returns `None` (kept), so `one_of` on a secret setting works for exact spellings and refuses
  any other. If two choices differ only in case (`one_of("a", "A")`), an exact match keeps its own
  spelling and any other case gets the first declared.

This is a breaking change for anyone who relied on the given case surviving, or whose check returned
something it expected to be ignored: the release is marked breaking.

### Typed accessors on `Resolved` (#13)

```python
r.str("log_level")  # str
r.int("listen.port")  # int, never a bool
r.bool("mount.debug")  # bool
r.list("env.expose")  # tuple[str, ...]
r.secret("proxy.auth")  # Secret
```

- Each raises `TypeError` naming the key and both kinds when the setting is another kind:
  `'listen.port' is a int setting, not str`. On a secret setting, every accessor but `secret()` adds
  `: use secret() and reveal()`; the value is never in the message.
- A key that isn't declared is a `KeyError`, as `values[key]` is.
- The kind is read from the resolved value, which is exact: coercion is strict, a replacement is
  checked against the kind, and only secret settings resolve to `Secret`. `values` and `sources` stay
  as they are.

## Alternatives considered

- **`one_of(..., canonical=True)`** (#8, option 1 as written). Stephen chose always canonical: the
  declared spelling is the one the project wrote down, and a flag would be one more thing each project
  sets the same way.
- **A `transform=` hook on `Setting`** (#8, option 2). A second per-setting hook, when the check is
  already the place that knows the value's rule.
- **Replacements for secrets, wrapped in `Secret` afterwards.** See above: refused, reversible later.
- **Accessors that coerce** (`r.int` of a str setting parsing it). That is a second coercion path with
  its own rules; the setting's declared type is the one place a type comes from.
- **A typed `Config` generated from `Settings`.** A bigger interface (a generator, or a dataclass
  factory) for what five small accessors cover.

## Consequences

- Consumers drop their `load_config` upper-casing and their `cast()`s: `r.str("log_level")` is
  already `"INFO"`, and `r.str` on a secret setting fails at load, not at the point of use.
  template-python's skeleton can switch to the accessors when its pin moves to this release.
- A project that relied on `one_of` keeping the given case sees the declared spelling instead.
- Wiki: API-Reference (`check`, `one_of`, `Resolved`), Architecture-Internals (the resolve steps),
  Quick-Start (the example), Security (the secret rule), Testing-Guide.

## Open questions

None: Stephen's choices, 2026-11-02.
