"""Layered settings: each setting is declared once and resolved from four places.

The first place that has a value wins:

1. ``<PREFIX><KEY>_FILE``: a file that holds the value (Docker and Kubernetes secrets)
2. ``<PREFIX><KEY>``: an environment variable
3. the YAML config file (``<PREFIX>CONFIG``, else the project's default path)
4. the declared default

``<KEY>`` is the dotted key in upper case, with the dots as underscores: with the prefix
``SLUICE_``, ``listen.port`` is ``SLUICE_LISTEN_PORT``.

The type comes from the default, and coercion is strict: a value that isn't the declared type is a
`ConfigError`, never a silent conversion. Values are never logged, and an error never contains a
secret setting's value. Nothing here does I/O until `Settings.resolve()` is called.
"""

from __future__ import annotations

import builtins
import datetime
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast, overload

import yaml

from interpose.secret import MASK, Secret

__all__ = [
    "ConfigError",
    "Resolved",
    "Setting",
    "Settings",
    "Value",
    "non_negative",
    "one_of",
    "port",
    "positive",
]

Value = str | int | bool | tuple[str, ...] | Secret

_KEY = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)*")
# ASCII digits only: int() would also take "1_000" and other scripts' digits.
_INT = re.compile(r"[+-]?[0-9]+")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
# What a str setting (or a list item) accepts from YAML: a scalar. A date is a scalar YAML types for
# you (`2026-11-01`); its text is what was written.
_SCALAR = (str, int, float, datetime.date)


class ConfigError(Exception):
    """The configuration can't be read, or a value in it is invalid."""


@dataclass(frozen=True)
class Setting:
    """One setting: its dotted key, default (which sets its type), help and flags.

    `prefix` is set by `Settings`, never by hand. `check` gets the coerced value (before it is
    wrapped in `Secret`) and raises `ValueError` with a reason. It may return a replacement of the
    setting's type (`one_of` returns the declared spelling); `None` keeps the value. A secret
    setting's value can't be replaced (design 0007).
    """

    key: str
    default: str | int | bool
    help: str
    secret: bool = False
    is_list: bool = False
    check: Callable[[Value], Value | None] | None = None
    prefix: str = ""

    @property
    def env(self) -> str:
        return self.prefix + self.key.upper().replace(".", "_")


@dataclass(frozen=True)
class Resolved:
    """Typed values and where each came from, both keyed by setting key, in declaration order.

    `values` holds `Secret` for secret settings and a tuple for list settings. `sources` holds the
    `_FILE` variable's name, the environment variable's name, `"file"` or `"default"`.

    The typed accessors (`str`, `int`, `bool`, `list`, `secret`) give a value with its type, and
    raise `TypeError` naming the key when the setting is another kind (`str()` on a secret
    setting, above all), or `KeyError` for a key that isn't declared (design 0007).
    """

    values: Mapping[builtins.str, Value]
    sources: Mapping[builtins.str, builtins.str]

    def _kind(self, key: builtins.str, kind: builtins.str) -> Value:
        value = self.values[key]
        if _kind_of(value) != kind:
            hint = ": use secret() and reveal()" if isinstance(value, Secret) else ""
            raise TypeError(f"{key!r} is a {_kind_of(value)} setting, not {kind}{hint}")
        return value

    def str(self, key: builtins.str) -> builtins.str:
        """A str setting's value. A secret setting is `TypeError`: use `secret()`."""
        return cast(builtins.str, self._kind(key, "str"))

    def int(self, key: builtins.str) -> builtins.int:
        """An int setting's value (never a bool)."""
        return cast(builtins.int, self._kind(key, "int"))

    def bool(self, key: builtins.str) -> builtins.bool:
        """A bool setting's value."""
        return cast(builtins.bool, self._kind(key, "bool"))

    def list(self, key: builtins.str) -> tuple[builtins.str, ...]:
        """A list setting's value."""
        return cast(tuple[builtins.str, ...], self._kind(key, "list"))

    def secret(self, key: builtins.str) -> Secret:
        """A secret setting's value; `reveal()` it at the point of use."""
        return cast(Secret, self._kind(key, "secret"))


def _kind_of(value: object) -> str:
    """The kind of setting a resolved value comes from: exact, since coercion is strict."""
    if isinstance(value, Secret):
        return "secret"
    if isinstance(value, tuple):
        return "list"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    return (
        "other"  # only a check's replacement can be anything else (a list is not a list setting's)
    )


class Settings(Sequence[Setting]):
    """A project's settings, bound to its environment-variable prefix and default config path.

    Iterating gives each `Setting` with `prefix` set, so `setting.env` is the full variable name.
    """

    def __init__(self, prefix: str, settings: Iterable[Setting], *, default_path: Path) -> None:
        bound: list[Setting] = []
        seen: set[str] = set()
        # Every variable name a setting reads, so no one variable feeds two settings
        # (`a.b` and `a_b` are both APP_A_B; `x_file`'s variable is `x`'s _FILE variable).
        names: dict[str, str] = {prefix + "CONFIG": "the config file variable"}
        for setting in settings:
            if not _KEY.fullmatch(setting.key):
                raise ValueError(
                    f"{setting.key!r}: a key is lower-case words of [a-z0-9_] joined by dots"
                )
            if setting.key in seen:
                raise ValueError(f"{setting.key!r}: declared twice")
            for part in setting.key.split("."):
                if not _is_yaml_str(part):
                    # pyyaml loads `on`, `no`, `null` or `1` as a bool, None or int key, so
                    # the file's value would never be found under the string.
                    raise ValueError(f"{setting.key!r}: YAML doesn't read {part!r} as a string")
            env = prefix + setting.key.upper().replace(".", "_")
            for name in (env, env + "_FILE"):
                if name in names:
                    raise ValueError(f"{setting.key!r}: {name} is also {names[name]}")
                names[name] = f"read by {setting.key!r}"
            if setting.is_list and setting.default != "":
                raise ValueError(f'{setting.key!r}: a list setting\'s default must be ""')
            if setting.secret and (setting.is_list or not isinstance(setting.default, str)):
                # A Secret holds one string; a secret int, bool or list has no masked form.
                raise ValueError(f"{setting.key!r}: a secret setting must be a str setting")
            seen.add(setting.key)
            bound.append(replace(setting, prefix=prefix))
        self.prefix = prefix
        self.default_path = default_path
        self.config_env = prefix + "CONFIG"
        self._settings = tuple(bound)

    @overload
    def __getitem__(self, index: int) -> Setting: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[Setting]: ...
    def __getitem__(self, index: int | slice) -> Setting | Sequence[Setting]:
        return self._settings[index]

    def __len__(self) -> int:
        return len(self._settings)

    def __iter__(self) -> Iterator[Setting]:
        return iter(self._settings)

    def __repr__(self) -> str:
        return (
            f"Settings({self.prefix!r}, {len(self)} settings, default_path={self.default_path!r})"
        )

    def resolve(
        self, path: Path | None = None, environ: Mapping[str, str] | None = None
    ) -> Resolved:
        """Read the config file and the environment, and resolve every setting.

        `path` defaults to `<PREFIX>CONFIG`, else `default_path`; a missing file means defaults.
        `environ` defaults to `os.environ`. Raises `ConfigError` on the first invalid value.
        """
        env = os.environ if environ is None else environ
        if path is None:
            path = Path(env.get(self.config_env, str(self.default_path)))
        data = _read_yaml(path)
        values: dict[str, Value] = {}
        sources: dict[str, str] = {}
        for setting in self._settings:
            values[setting.key], sources[setting.key] = _resolve_one(setting, data, env)
        return Resolved(values=MappingProxyType(values), sources=MappingProxyType(sources))


def _read_yaml(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"cannot read {path}: not UTF-8") from None
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # str(e) quotes the offending line, which may hold a secret: give its position only.
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(e, "problem", None) or "invalid YAML"
        raise ConfigError(f"cannot parse {path}{where}: {problem}") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    return loaded


def _is_yaml_str(part: str) -> bool:
    """Whether pyyaml loads `part`, as a mapping key, as that string."""
    loaded = yaml.safe_load(f"{part}: 0")
    return isinstance(loaded, dict) and list(loaded) == [part]


def _lookup(data: Mapping[str, Any], dotted: str) -> Any:
    """The value at a dotted key, or None when it's absent (or null) at any level."""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _resolve_one(
    setting: Setting, data: Mapping[str, Any], environ: Mapping[str, str]
) -> tuple[Value, str]:
    file_var = f"{setting.env}_FILE"
    raw: Any
    if file_var in environ:
        path = Path(environ[file_var])
        try:
            raw = path.read_text(encoding="utf-8").rstrip()
        except OSError as e:
            raise ConfigError(f"cannot read {file_var} ({path}): {e.strerror}") from None
        except UnicodeDecodeError:
            raise ConfigError(f"cannot read {file_var} ({path}): not UTF-8") from None
        source = file_var
    elif setting.env in environ:
        raw, source = environ[setting.env], setting.env
    else:
        raw = _lookup(data, setting.key)
        source = "file"
        if raw is None:
            raw, source = setting.default, "default"
    value = _coerce(setting, raw, source)
    if setting.check is not None:
        try:
            replacement = setting.check(value)
        except ValueError as e:
            reason = str(e)
            if setting.secret and isinstance(value, str) and value:
                # The value as written, and inside a repr() or ascii() (escaped backslashes,
                # quotes, control characters). A check shouldn't echo a secret at all.
                for form in sorted({value, repr(value)[1:-1], ascii(value)[1:-1]}, key=len)[::-1]:
                    reason = reason.replace(form, MASK)
            raise ConfigError(f"{setting.key}: {reason}") from None
        if replacement is not None:
            value = _replace(setting, value, replacement)
    if setting.secret:
        # Settings only allows str secrets, so the coerced value is a str.
        return Secret(str(value)), source
    return value, source


_Plain = str | int | bool | tuple[str, ...]  # a value before `secret=True` wraps it
_EXPECTED = {"str": "str", "int": "int", "bool": "bool", "list": "a tuple of str"}


def _replace(setting: Setting, value: _Plain, replacement: Value) -> _Plain:
    """A check's replacement, if it is the setting's kind. Errors name types, never values."""
    if type(replacement) is type(value) and replacement == value:
        return value  # "validate and return the value" is no replacement, secret or not
    if setting.secret:
        # A replacement would be a second spelling of a secret that nobody configured; a check
        # that computed one could also have leaked it. Refused (design 0007).
        raise ConfigError(f"{setting.key}: a check can't replace a secret setting's value")
    expected, got = _kind_of(value), _kind_of(replacement)
    if got != expected:
        raise ConfigError(
            f"{setting.key}: its check returned {type(replacement).__name__}, "
            f"expected {_EXPECTED[expected]}"
        )
    if isinstance(replacement, tuple) and not all(isinstance(i, str) for i in replacement):
        raise ConfigError(f"{setting.key}: its check returned a tuple with a non-str item")
    return cast(_Plain, replacement)


def _coerce(setting: Setting, raw: Any, source: str) -> _Plain:
    """Give raw (a string from a file or the environment, or a YAML value) the setting's type."""
    result: str | int | bool | tuple[str, ...] | None
    if setting.is_list:
        kind = "list"
        result = _as_list(raw)
    elif isinstance(setting.default, bool):
        kind = "bool"
        result = _as_bool(raw)
    elif isinstance(setting.default, int):
        kind = "int"
        result = _as_int(raw)
    else:
        kind = "str"
        result = _as_str(raw)
    if result is None:
        got = "" if setting.secret else f", got {raw!r}"
        raise ConfigError(f"{setting.key}: expected {kind}{got} (from {source})")
    return result


def _as_bool(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    # A YAML 1 or 0 is the same spelling as the string "1" or "0".
    if isinstance(raw, int | str):
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def _as_int(raw: Any) -> int | None:
    # bool is an int subclass: int(True) would be a silent 1. A float would truncate (0.5 -> 0).
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and _INT.fullmatch(raw.strip()):
        return int(raw)
    return None


def _as_str(raw: Any) -> str | None:
    # str() of a list or mapping would be its repr.
    if isinstance(raw, _SCALAR):
        return str(raw)
    return None


def _as_list(raw: Any) -> tuple[str, ...] | None:
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        return None
    if not all(isinstance(item, _SCALAR) for item in items):
        return None
    return tuple(text for item in items if (text := str(item).strip()))


# Single-value checks, for `Setting(check=...)`. Rules that span settings belong in the project.


def port(value: Value) -> None:
    """A TCP port: 1 to 65535."""
    if not 0 < _int_value(value) < 65536:
        raise ValueError(f"{value} is not a TCP port")


def positive(value: Value) -> None:
    """Greater than 0."""
    if _int_value(value) <= 0:
        raise ValueError(f"must be greater than 0, got {value}")


def non_negative(value: Value) -> None:
    """0 or more."""
    if _int_value(value) < 0:
        raise ValueError(f"must be 0 or more, got {value}")


def one_of(*choices: str) -> Callable[[Value], str | None]:
    """One of `choices`, in any case; the value becomes the declared spelling (`info` resolves to
    `INFO` when `INFO` is declared). A value already spelled as declared is kept as it is."""
    if not choices:
        raise ValueError("one_of() needs at least one choice")
    canonical: dict[str, str] = {}
    for choice in choices:
        canonical.setdefault(choice.lower(), choice)  # the first declared spelling wins
    exact = frozenset(choices)
    listed = ", ".join(choices)

    def check(value: Value) -> str | None:
        if not isinstance(value, str):
            raise ValueError(f"expected one of {listed}")
        if value in exact:
            return None
        if value.lower() not in canonical:
            raise ValueError(f"expected one of {listed}, got {value!r}")
        return canonical[value.lower()]

    return check


def _int_value(value: Value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected int")
    return value
