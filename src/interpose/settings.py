"""Layered settings: each setting is declared once and resolved from four places.

The first place that has a value wins:

1. ``<PREFIX><KEY>_FILE``: a file that holds the value (Docker and Kubernetes secrets)
2. ``<PREFIX><KEY>``: an environment variable
3. the YAML config file (``<PREFIX>CONFIG``, else the project's default path), or in its place
   the `Layer`s a project passes to `Settings.resolve(layers=...)`, first one first
4. the declared default

``<KEY>`` is the dotted key in upper case, with the dots as underscores: with the prefix
``SLUICE_``, ``listen.port`` is ``SLUICE_LISTEN_PORT``.

The type comes from the default, and coercion is strict: a value that isn't the declared type is a
`ConfigError`, never a silent conversion. Values are never logged, and an error never contains a
secret setting's value. Nothing here does I/O until `Settings.resolve()` is called.

A setting may keep old names (`aliases`), read in each of those places alongside its key, each
with a warning (design 0009). An unknown key in a layer, or a prefixed variable no setting reads, is
still ignored, and gives a `ConfigWarning` on `Resolved.warnings` for the project to log. interpose
itself never logs, prints or raises one.

A project may opt in to ``${VAR}`` expansion in its config file (`Settings(..., expand=True)`,
design 0009): ``${NAME}`` and ``${NAME:-default}`` in a value a setting reads from the file take the
variable's value, before typing. Variables, ``_FILE`` contents and a `Layer` the project builds
never expand, unless that layer says `expand=True`.

A setting may be a section (`Setting(..., is_section=True)`, design 0009): its value is the YAML
subtree at its key, deep-frozen as a `Section` (or, with `is_list=True`, a tuple of them), read from
``<PREFIX><KEY>_FILE`` as a YAML file or from the layers; never from a plain variable. interpose
checks only its shape. Inside a section, ``{secret_file: <path>}`` becomes a `Secret` holding that
file, read only from local configuration (a `Layer` with `secret_files=True`).

A list section may declare its item fields (`Setting(..., fields=(...))`, design 0009): each item
is then a `Section` holding every field, each typed and checked as a setting, and the list may also
come from indexed variables, ``<PREFIX><KEY>_<N>_<FIELD>`` (and its ``_FILE``) for ``N`` from 0.
"""

from __future__ import annotations

import builtins
import datetime
import difflib
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence, Set
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast, overload

import yaml

from interpose.secret import MASK, Secret

__all__ = [
    "ConfigError",
    "ConfigWarning",
    "Layer",
    "Resolved",
    "Section",
    "Setting",
    "Settings",
    "Value",
    "non_negative",
    "one_of",
    "port",
    "positive",
]

_KEY = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)*")
_FIELD = re.compile(r"[a-z0-9_]+")  # a list section's field: a key of one part
# ASCII digits only: int() would also take "1_000" and other scripts' digits.
_INT = re.compile(r"[+-]?[0-9]+")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
# What a str setting (or a list item) accepts from YAML: a scalar. A date is a scalar YAML types for
# you (`2026-11-01`); its text is what was written.
_SCALAR = (str, int, float, datetime.date)
# One `${` in a value that expands (design 0009, section 4): the escape `$${`, a variable with an
# optional `:-` default (literal text up to the first `}`), or, with no group, a `${` that is
# neither: unclosed, or a name that isn't an environment variable's.
_EXPANSION = re.compile(r"\$\$\{|\$\{(?:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\})?")
_UNSET = ("error", "empty")


class ConfigError(Exception):
    """The configuration can't be read, or a value in it is invalid."""


@dataclass(frozen=True)
class ConfigWarning:
    """Something in the configuration that resolution ignored, for the project to log (design 0009).

    `kind` is `"unknown"` for a key, item field or prefixed variable no setting reads, `"alias"` for
    a setting's old name that is set, used or not, or `"ignored"` for a list section's indexed
    variable past the end of its list. `name` is the key or variable it is about (`"listen.prot"`,
    `"SLUICE_PORT"`); `message` is one line naming keys, variables and where, never a value.
    """

    kind: Literal["unknown", "alias", "ignored"]
    name: str
    message: str

    def __str__(self) -> str:
        return self.message


# What a section holds (design 0009, section 5): scalars as YAML loads them, secret leaves, nested
# sections and tuples. A datetime is a date.
type Node = str | int | float | bool | datetime.date | Secret | Section | tuple[Node, ...] | None


class Section(Mapping[str, Node]):
    """A section setting's value: a read-only, deep-frozen mapping (design 0009, section 5).

    Nested mappings are `Section`s, lists are tuples, scalars are as YAML loaded them (str, int,
    float, bool, date, datetime, `None`), and a secret leaf is a `Secret`. Nothing in it can be
    changed, at any depth. A `Section` is hashable, and equal only to another `Section` with the
    same keys and values of the same types (`1` is not `True`, and `1` is not `1.0`), so a reload
    can compare old and new. `repr()` masks every `Secret`.

    `Section(mapping)` freezes a mapping of the same shape (a check builds its replacement this
    way): a non-str key, or a value of any other type, is a `TypeError`.
    """

    __slots__ = ("_hash", "_items", "_strict")

    _items: Mapping[str, Node]
    _strict: frozenset[tuple[str, object]]
    _hash: int

    def __init__(self, mapping: Mapping[str, Any] | None = None) -> None:
        items: dict[str, Node] = {}
        for key, child in ({} if mapping is None else mapping).items():
            if not isinstance(key, str):
                raise TypeError(f"a Section's keys are str, not {type(key).__name__}")
            items[key] = _freeze(child)
        self._init(items)

    @classmethod
    def _of(cls, items: dict[str, Node]) -> Section:
        """A `Section` of items already frozen, which only it holds from now on."""
        section = object.__new__(cls)
        section._init(items)
        return section

    def _init(self, items: dict[str, Node]) -> None:
        if hasattr(self, "_hash"):  # `section.__init__(...)` again would replace its items
            raise AttributeError("a Section is read-only")
        strict = frozenset((key, _strict(child)) for key, child in items.items())
        object.__setattr__(self, "_items", MappingProxyType(items))
        object.__setattr__(self, "_strict", strict)
        object.__setattr__(self, "_hash", hash(strict))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a Section is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("a Section is read-only")

    def __getitem__(self, key: str) -> Node:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Section):
            return NotImplemented
        return self._strict == other._strict

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        # A Secret's repr() is masked, and a nested Section's or tuple's uses its items' repr().
        return "Section({" + ", ".join(f"{k!r}: {v!r}" for k, v in self._items.items()) + "})"

    def __reduce__(self) -> tuple[type[Section], tuple[dict[str, Node]]]:
        # copy and pickle would otherwise set the slots through the blocked __setattr__.
        return (Section, (dict(self._items),))


def _freeze(node: object) -> Node:
    """A value for `Section(...)`, frozen: a mapping becomes a `Section`, a list or tuple a
    tuple."""
    if node is None or isinstance(node, Section | Secret | str | int | float | datetime.date):
        return cast(Node, node)  # bool is an int; datetime is a date
    if isinstance(node, Mapping):
        return Section(node)
    if isinstance(node, list | tuple):
        return tuple(_freeze(item) for item in node)
    raise TypeError(f"a Section holds mappings, lists and scalars, not {type(node).__name__}")


def _strict(node: Node) -> object:
    """What `Section` equality and hashing compare for a value: its type with it, so `1`, `True`
    and `1.0` differ, as do a date and a datetime (a Section or Secret compares itself)."""
    if isinstance(node, tuple):
        return (tuple, tuple(_strict(item) for item in node))
    if isinstance(node, Section | Secret):
        return node
    return (type(node), node)


Value = str | int | bool | tuple[str, ...] | Secret | Section | tuple[Section, ...]


@dataclass(frozen=True)
class Setting:
    """One setting: its dotted key, default (which sets its type), help and flags.

    `prefix` is set by `Settings`, never by hand. `check` gets the coerced value (before it is
    wrapped in `Secret`) and raises `ValueError` with a reason. It may return a replacement of the
    setting's type (`one_of` returns the declared spelling); `None` keeps the value. A secret
    setting's value can't be replaced (design 0007).

    `aliases` are the setting's old dotted keys, read wherever its key is (design 0009): the key in
    every layer, `<PREFIX><ALIAS>` and `<PREFIX><ALIAS>_FILE`. Each one that is set gives an `alias`
    warning.

    `is_section` makes it a section (design 0009): its value is a `Section`, or with `is_list` a
    tuple of them, and its default must be `""` (empty). A section can't be `secret`, but may hold
    secret leaves. Its `check` gets the `Section` (or tuple) and may return another.

    `fields` declares a list section's item fields, each a `Setting` with a one-part key, typed
    and checked as a setting (a field may be secret, or a free-form section). Each item is then a
    `Section` holding every field, and the list may come from indexed variables too,
    `<PREFIX><KEY>_<N>_<FIELD>` and its `_FILE`, for N from 0. `required` (a field's only) makes a
    missing field an error; a missing optional field takes its default.
    """

    key: str
    default: str | int | bool
    help: str
    secret: bool = False
    is_list: bool = False
    check: Callable[[Value], Value | None] | None = None
    prefix: str = ""
    aliases: tuple[str, ...] = ()
    is_section: bool = False
    fields: tuple[Setting, ...] = ()
    required: bool = False

    @property
    def env(self) -> str:
        return _env_name(self.prefix, self.key)


def _env_name(prefix: str, key: str) -> str:
    """The variable a dotted key is read from: `listen.port` is `<PREFIX>LISTEN_PORT`."""
    return prefix + key.upper().replace(".", "_")


def _check_kind(label: str, setting: Setting) -> None:
    """The flags and default agree: a section's or list's default is empty, a secret is a str
    setting, and a section is never secret. Raises `ValueError` starting with `label`."""
    if setting.is_section and setting.default != "":
        raise ValueError(f'{label}: a section setting\'s default must be ""')
    if setting.is_section and setting.secret:
        # A Secret holds one string. A section holds secrets as secret leaves instead.
        raise ValueError(f"{label}: a section can't be secret: use secret_file leaves in it")
    if setting.is_list and setting.default != "":
        raise ValueError(f'{label}: a list setting\'s default must be ""')
    if setting.secret and (setting.is_list or not isinstance(setting.default, str)):
        # A Secret holds one string; a secret int, bool or list has no masked form.
        raise ValueError(f"{label}: a secret setting must be a str setting")


def _check_fields(setting: Setting) -> None:
    """A setting's `fields` and `required` (design 0009): only a list section has fields, each a
    `Setting` with a one-part key of its own and no fields or aliases, and only a field is
    `required`. No two fields read the same indexed variable (`x`'s _FILE is `x_file`'s)."""
    label = repr(setting.key)
    if setting.required:
        raise ValueError(f"{label}: required is for a list section's field, not a setting")
    if not isinstance(setting.fields, tuple):
        raise ValueError(f"{label}: fields are a tuple of Setting")
    if setting.fields and not (setting.is_section and setting.is_list):
        raise ValueError(f"{label}: only a list section (is_section=True, is_list=True) has fields")
    keys: set[str] = set()
    suffixes: dict[str, str] = {}  # what a field's variable ends with -> the field
    for item in setting.fields:
        if not isinstance(item, Setting):
            raise ValueError(f"{label}: a field is a Setting, not {type(item).__name__}")
        where = f"{label}: field {item.key!r}"
        if not _FIELD.fullmatch(item.key):
            raise ValueError(f"{where}: a field's key is one lower-case word of [a-z0-9_]")
        if not _is_yaml_str(item.key):
            raise ValueError(f"{where}: YAML doesn't read {item.key!r} as a string")
        if item.key in keys:
            raise ValueError(f"{where}: declared twice")
        if item.fields:
            raise ValueError(f"{where}: a field can't have fields of its own")
        if item.aliases:
            raise ValueError(f"{where}: a field has no aliases")
        _check_kind(where, item)
        for suffix in _suffixes(item):
            if suffix in suffixes:
                raise ValueError(
                    f"{where}: its variable <KEY>_<N>_{suffix} is also field {suffixes[suffix]!r}'s"
                )
            suffixes[suffix] = item.key
        keys.add(item.key)


def _suffixes(item: Setting) -> tuple[str, str]:
    """What a field's indexed variables end with: `CRON` and `CRON_FILE`."""
    return item.key.upper(), item.key.upper() + "_FILE"


@dataclass(frozen=True)
class Layer:
    """A YAML-shaped tree of values from one place, with where it came from (design 0009).

    `data` is what `yaml.safe_load` gives for a config file: nested mappings keyed by the parts of
    a dotted key. `source` is what `Resolved.sources` says for a value from here (`"file"`,
    `"control plane"`); `where` is what an error names (a path, `"control plane (config v12)"`).
    `origins` maps a dotted key to a finer `where` for that value. `resolve()` reads `data` and
    never changes or keeps it.

    `expand` makes `${VAR}` in the values settings read from here expand. `read_file()` sets it to
    the project's `Settings(expand=...)` (as a section's `_FILE` expands with it), and is the only
    place interpose sets it. Never set it on a layer built from a remote payload: it would let the
    sender read this process's environment.

    `secret_files` lets a section here hold `{secret_file: <path>}` leaves, read into `Secret`s;
    without it, such a leaf is a `ConfigError` and its file is never opened (design 0009, decision
    E). `read_file()` sets it, as does a section's `_FILE`. Never set it on a layer built from a
    remote payload: it would let the sender read any file this process can.
    """

    data: Mapping[str, Any]
    source: str
    where: str
    origins: Mapping[str, str] = field(default_factory=dict)
    expand: bool = False
    secret_files: bool = False


@dataclass(frozen=True)
class Resolved:
    """Typed values and where each came from, both keyed by setting key, in declaration order.

    `values` holds `Secret` for secret settings, a tuple for list settings, a `Section` for section
    settings and a tuple of them for list sections. `sources` holds the `_FILE` variable's name,
    the environment variable's name (an alias's, when that is what was read), `"file"` (or the
    `source` of the `Layer` the value came from) or `"default"`. `warnings`
    holds a `ConfigWarning` for each unknown key and prefixed variable and each alias that is set,
    in the order found: layers first, in order, then variables by name.

    The typed accessors (`str`, `int`, `bool`, `list`, `secret`, `section`, `sections`) give a
    value with its type, and raise `TypeError` naming the key when the setting is another kind
    (`str()` on a secret setting, above all), or `KeyError` for a key that isn't declared (design
    0007).
    """

    values: Mapping[builtins.str, Value]
    sources: Mapping[builtins.str, builtins.str]
    warnings: tuple[ConfigWarning, ...] = ()
    # Each section setting's kind, from its declaration: an empty list section's () would
    # otherwise look like an empty list setting's.
    _kinds: Mapping[builtins.str, builtins.str] = field(
        default_factory=lambda: MappingProxyType({}), repr=False, compare=False
    )

    def _kind(self, key: builtins.str, kind: builtins.str) -> Value:
        value = self.values[key]
        actual = self._kinds.get(key) or _kind_of(value)
        if actual != kind:
            hint = _HINTS.get(actual, "")
            raise TypeError(f"{key!r} is a {actual} setting, not {kind}{hint}")
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

    def section(self, key: builtins.str) -> Section:
        """A section setting's value (design 0009)."""
        return cast(Section, self._kind(key, "section"))

    def sections(self, key: builtins.str) -> tuple[Section, ...]:
        """A list section's value: a `Section` for each item (design 0009)."""
        return cast(tuple[Section, ...], self._kind(key, "list of sections"))


# What a TypeError from a typed accessor adds for a kind with its own accessor.
_HINTS = {
    "secret": ": use secret() and reveal()",
    "section": ": use section()",
    "list of sections": ": use sections()",
}


def _kind_of(value: object) -> str:
    """The kind of setting a resolved value comes from: exact, since coercion is strict."""
    if isinstance(value, Secret):
        return "secret"
    if isinstance(value, Section):
        return "section"
    if isinstance(value, tuple):
        if value and all(isinstance(item, Section) for item in value):
            return "list of sections"
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
    `extra_env` names prefixed variables the project reads itself, so they give no warning.

    `expand=True` turns on `${VAR}` expansion in the config file (`read_file()` gives its layer
    `expand=True`). A variable that is unset and has no default is a `ConfigError`, or with
    `expand_unset="empty"`, the empty string.
    """

    def __init__(
        self,
        prefix: str,
        settings: Iterable[Setting],
        *,
        default_path: Path,
        extra_env: Iterable[str] = (),
        expand: bool = False,
        expand_unset: Literal["error", "empty"] = "error",
    ) -> None:
        if expand_unset not in _UNSET:
            raise ValueError(f'expand_unset: "error" or "empty", not {expand_unset!r}')
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
            env = _env_name(prefix, setting.key)
            for name in (env, env + "_FILE"):
                if name in names:
                    raise ValueError(f"{setting.key!r}: {name} is also {names[name]}")
                names[name] = f"read by {setting.key!r}"
            _check_kind(repr(setting.key), setting)
            _check_fields(setting)
            seen.add(setting.key)
            bound.append(replace(setting, prefix=prefix))
        # A section owns its whole subtree: a key below it would be read as part of its value too,
        # and a key above it would make its value a mapping. (Aliases are checked against every
        # key, sections' included, below.)
        for setting in bound:
            if setting.is_section:
                for other in sorted(seen):
                    if other.startswith(setting.key + ".") or setting.key.startswith(other + "."):
                        raise ValueError(
                            f"{setting.key!r}: a section, and {other!r}: "
                            "one is a parent of the other"
                        )
        # Aliases once every key is known: an alias may equal no key, declared before or after.
        alias_of: dict[str, str] = {}  # alias -> its setting's key
        alias_env: dict[str, str] = {}  # an alias's variable -> the key's ("_FILE" for "_FILE")
        for setting in bound:
            for alias in self._check_aliases(setting, seen, alias_of):
                alias_of[alias] = setting.key
                env = _env_name(prefix, alias)
                for name, current in ((env, setting.env), (env + "_FILE", setting.env + "_FILE")):
                    if name in names:
                        raise ValueError(
                            f"{setting.key!r}: alias {alias!r}: {name} is also {names[name]}"
                        )
                    names[name] = f"read by {setting.key!r} (its alias {alias!r})"
                    alias_env[name] = current
        if isinstance(extra_env, str):
            raise ValueError("extra_env: a tuple of variable names, not one str")
        extra: list[str] = []
        for name in extra_env:
            if not name.startswith(prefix):
                raise ValueError(f"extra_env {name!r}: doesn't start with the prefix {prefix!r}")
            if name in extra:
                raise ValueError(f"extra_env {name!r}: named twice")
            if name in names:
                raise ValueError(f"extra_env {name!r}: already {names[name]}")
            extra.append(name)
        # A list section with fields reads `<VAR>_<N>_<FIELD>` for its key's variable and each
        # alias's: no other variable may have that form, or one name would feed two settings.
        indexed: list[tuple[re.Pattern[str], str, str, Setting]] = []
        for setting in bound:
            if setting.fields:
                for name in (setting.key, *setting.aliases):
                    base = _env_name(prefix, name)
                    pattern = re.compile(re.escape(base) + r"_([0-9]+)_(.+)")
                    indexed.append((pattern, base, setting.env, setting))
        taken = [*names.items(), *((name, "named in extra_env") for name in extra)]
        for pattern, base, _, setting in indexed:
            for name, what in taken:
                if pattern.fullmatch(name):
                    raise ValueError(
                        f"{setting.key!r}: {name} is {what}, but has the form of "
                        f"{base}_<N>_<FIELD>, the list's indexed variables"
                    )
        self.prefix = prefix
        self.default_path = default_path
        self.config_env = prefix + "CONFIG"
        self.extra_env = tuple(extra)
        self.expand = expand
        self.expand_unset: Literal["error", "empty"] = expand_unset
        self._settings = tuple(bound)
        # The names the unknown-key and unknown-variable checks accept (design 0009, section 2):
        # every key and alias, their parents, and every variable a setting or alias reads. A
        # section's key (or alias) is a leaf, so nothing below it is checked: its content is the
        # project's, except a list section's declared fields, checked in each item. A list section
        # with fields reads its indexed variables (an alias's too), matched by `_indexed`.
        # Suggestions come from current names only, never aliases.
        self._alias_of = MappingProxyType(alias_of)
        self._alias_env = MappingProxyType(alias_env)
        self._leaf_keys = frozenset(seen) | frozenset(alias_of)
        self._known_keys = _with_parents(self._leaf_keys)
        self._suggest_keys = _with_parents(seen)
        self._known_env = frozenset(names) | frozenset(extra)
        self._suggest_env = self._known_env - frozenset(alias_env)
        self._indexed = tuple(indexed)
        self._typed = MappingProxyType(
            {name: s for s in bound if s.fields for name in (s.key, *s.aliases)}
        )

    @staticmethod
    def _check_aliases(
        setting: Setting, keys: Set[str], aliases: Mapping[str, str]
    ) -> tuple[str, ...]:
        """A setting's aliases, each well-formed and clashing with no key, no other alias of its
        own and no alias of an earlier setting (`aliases`). Raises `ValueError` as for a key."""
        if not isinstance(setting.aliases, tuple):
            raise ValueError(f"{setting.key!r}: aliases are a tuple of dotted keys")
        own: list[str] = []
        for alias in setting.aliases:
            if not isinstance(alias, str) or not _KEY.fullmatch(alias):
                raise ValueError(
                    f"{setting.key!r}: alias {alias!r}: a key is lower-case words of [a-z0-9_] "
                    "joined by dots"
                )
            for part in alias.split("."):
                if not _is_yaml_str(part):
                    raise ValueError(
                        f"{setting.key!r}: alias {alias!r}: YAML doesn't read {part!r} as a string"
                    )
            if alias in keys:
                raise ValueError(f"{setting.key!r}: alias {alias!r} is a setting's key")
            if alias in own:
                raise ValueError(f"{setting.key!r}: alias {alias!r} named twice")
            if alias in aliases:
                raise ValueError(
                    f"{setting.key!r}: alias {alias!r} is already an alias of {aliases[alias]!r}"
                )
            # A YAML path can't be both a value and a mapping: with `listen` an alias and
            # `listen.port` a key, every file that sets `listen.port` would set `listen` too.
            for other in sorted(keys | set(aliases) | set(own)):
                if other.startswith(alias + ".") or alias.startswith(other + "."):
                    raise ValueError(
                        f"{setting.key!r}: alias {alias!r} and {other!r}: "
                        "one is a parent of the other"
                    )
            own.append(alias)
        return tuple(own)

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

    def read_file(
        self, path: Path | None = None, environ: Mapping[str, str] | None = None
    ) -> Layer:
        """The config file as a `Layer`: source `"file"`, `where` its path, `expand` the project's,
        and `secret_files` on.

        `path` defaults to `<PREFIX>CONFIG`, else `default_path`; a missing file is an empty layer.
        `environ` defaults to `os.environ`. Raises `ConfigError` if it can't be read or parsed.
        Nothing expands here: `resolve()` expands what each setting reads, with its `environ`.
        """
        env = os.environ if environ is None else environ
        if path is None:
            path = Path(env.get(self.config_env, str(self.default_path)))
        # With a section's `_FILE`, the places interpose sets `expand` (design 0009, decision C)
        # and `secret_files` (decision E): local configuration only.
        return Layer(
            data=_read_yaml(path),
            source="file",
            where=str(path),
            expand=self.expand,
            secret_files=True,
        )

    def resolve(
        self,
        path: Path | None = None,
        environ: Mapping[str, str] | None = None,
        *,
        layers: Sequence[Layer] | None = None,
    ) -> Resolved:
        """Read the config file and the environment, and resolve every setting.

        `path` defaults to `<PREFIX>CONFIG`, else `default_path`; a missing file means defaults.
        `environ` defaults to `os.environ`. Raises `ConfigError` on the first invalid value.

        `layers` replaces the file: no file is read, and each setting takes its `_FILE` variable,
        then its variable, then the first layer that has it, then its default. Passing `path` too
        is a `ValueError`. Without `layers`, the file is the one layer (`read_file(path, environ)`).

        A section takes its `_FILE` (a YAML file), then the first layer that has it, then empty;
        its plain variable is a `ConfigError`. Its secret leaves are read on every call. A list
        section with fields takes its indexed variables after its `_FILE`, replacing the layers'
        list whole.
        """
        env = os.environ if environ is None else environ
        if layers is None:
            layers = (self.read_file(path, env),)
        elif path is not None:
            raise ValueError("resolve(): pass path or layers, not both (layers replace the file)")
        values: dict[str, Value] = {}
        sources: dict[str, str] = {}
        kinds: dict[str, str] = {}
        # Unknown fields in a list section's _FILE, which no layer walk sees.
        found_warnings: list[ConfigWarning] = []
        for setting in self._settings:
            if setting.is_section:
                kinds[setting.key] = "list of sections" if setting.is_list else "section"
                values[setting.key], sources[setting.key] = _resolve_section(
                    setting, layers, env, self.expand, self.expand_unset, found_warnings
                )
            else:
                values[setting.key], sources[setting.key] = _resolve_one(
                    setting, layers, env, self.expand_unset
                )
        warnings = [w for layer in layers for w in self._layer_warnings(layer)]
        warnings += found_warnings
        warnings += self._env_warnings(env)
        return Resolved(
            values=MappingProxyType(values),
            sources=MappingProxyType(sources),
            warnings=tuple(warnings),
            _kinds=MappingProxyType(kinds),
        )

    def _layer_warnings(
        self, layer: Layer, node: Mapping[Any, Any] | None = None, parent: str = ""
    ) -> Iterator[ConfigWarning]:
        """In document order, an `unknown` warning for each path in the layer that is neither a key
        or alias nor a parent of one, at its first unknown level, and an `alias` warning for each
        alias that is set (as `_lookup` sees it: present and not null). Never a value."""
        for part, child in (layer.data if node is None else node).items():
            # A non-string key (`on:` loads as True) is named by its YAML text.
            text = part if isinstance(part, str) else _yaml_text(part)
            path = f"{parent}.{text}" if parent else text
            # A key with a dot in it (`listen.port:` at the top) is never a part of a declared
            # key, and resolution never reads it, though its dotted path would look known.
            if not isinstance(part, str) or "." in part or path not in self._known_keys:
                where = layer.origins.get(path, layer.where)
                message = f"unknown key {path!r} in {where}"
                # Suggest a current key or parent at the same depth: `listne` gets `listen`, and
                # `listen.1` never gets its own parent `listen`. Never an alias.
                depth = path.count(".")
                peers = (k for k in self._suggest_keys if k.count(".") == depth)
                yield ConfigWarning("unknown", path, message + _did_you_mean(path, peers))
            elif path in self._alias_of:
                if child is not None:  # a null alias is unset, as for a key
                    where = layer.origins.get(path, layer.where)
                    yield _alias_warning(path, where, self._alias_of[path])
                    if path in self._typed:
                        yield from _unknown_fields(self._typed[path], path, where, child)
            elif path in self._typed:
                where = layer.origins.get(path, layer.where)
                yield from _unknown_fields(self._typed[path], path, where, child)
            elif path not in self._leaf_keys and isinstance(child, Mapping):
                yield from self._layer_warnings(layer, child, path)

    def _env_warnings(self, environ: Mapping[str, str]) -> Iterator[ConfigWarning]:
        """By name, an `alias` warning for each alias variable that is set, an `ignored` warning
        for each list section's indexed variable past the end of its list, and an `unknown` warning
        for each prefixed variable no setting reads. Only names are read, never values. With no
        prefix, every variable would be "prefixed": none is reported unknown, though aliases and
        ignored variables are."""
        counts = {base: _item_count(s, base, environ) for _, base, _, s in self._indexed}
        for name in sorted(n for n in environ if n.startswith(self.prefix)):
            if name in self._alias_env:
                yield _alias_warning(name, "the environment", self._alias_env[name])
            elif name in self._known_env:
                continue
            elif (match := self._indexed_variable(name)) is not None:
                yield from self._indexed_warnings(name, match, counts)
            elif self.prefix:
                message = f"unknown variable {name!r} in the environment"
                yield ConfigWarning(
                    "unknown", name, message + _did_you_mean(name, self._suggest_env)
                )

    def _indexed_variable(self, name: str) -> tuple[str, str, Setting, str, str] | None:
        """`(base, the key's base, setting, N, rest)` when `name` has the form of a list section's
        indexed variable, `<base>_<N>_<rest>`, where base is its key's or an alias's variable."""
        for pattern, base, current, setting in self._indexed:
            if match := pattern.fullmatch(name):
                return base, current, setting, match[1], match[2]
        return None

    def _indexed_warnings(
        self, name: str, match: tuple[str, str, Setting, str, str], counts: Mapping[str, int]
    ) -> Iterator[ConfigWarning]:
        base, current, setting, index, rest = match
        suffixes = sorted(suffix for item in setting.fields for suffix in _suffixes(item))
        # N as written plainly: `_01_` is not item 1's. Never int() it: a name may hold more
        # digits than int() converts.
        plain = index.lstrip("0") or "0"
        if rest in suffixes and index == plain:
            if base != current:
                yield _alias_warning(name, "the environment", current + name[len(base) :])
            count = str(counts[base])
            if (len(index), index) >= (len(count), count):  # N >= the item count
                message = (
                    f"ignored variable {name!r} in the environment: the list {setting.key!r} "
                    f"ends before it, as no {base}_{count}_* is set"
                )
                yield ConfigWarning("ignored", name, message)
        elif self.prefix:
            # An unknown field, or an index written another way: suggest that item's variables.
            peers = [f"{current}_{plain}_{suffix}" for suffix in suffixes]
            message = f"unknown variable {name!r} in the environment"
            yield ConfigWarning("unknown", name, message + _did_you_mean(name, peers))


def _alias_warning(old: str, where: str, current: str) -> ConfigWarning:
    """`'port' (in /etc/sluice/config.yaml) is the old name of 'listen.port': rename it`."""
    message = f"{old!r} (in {where}) is the old name of {current!r}: rename it"
    return ConfigWarning("alias", old, message)


def _read_yaml(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"cannot read {path}: not UTF-8") from None
    loaded = _parse_yaml(text, str(path))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    return loaded


def _parse_yaml(text: str, label: str) -> Any:
    """`yaml.safe_load(text)`; an error names `label` (a path) and the position, never the text."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        # str(e) quotes the offending line, which may hold a secret: give its position only.
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(e, "problem", None) or "invalid YAML"
        raise ConfigError(f"cannot parse {label}{where}: {problem}") from None


def _yaml_text(key: object) -> str:
    """A non-string YAML key as YAML writes it: `true` for True, `null` for None, `1` for 1."""
    try:
        return yaml.safe_dump(key, default_flow_style=True).removesuffix("\n...\n").strip()
    except yaml.YAMLError:
        # Only a project-built `Layer` can hold a key YAML can't write (a tuple, say).
        return repr(key)


def _with_parents(keys: Iterable[str]) -> frozenset[str]:
    """The keys and every parent of each: `listen.port` gives `listen` and `listen.port`."""
    return frozenset(
        ".".join(parts[:end])
        for parts in (key.split(".") for key in keys)
        for end in range(1, len(parts) + 1)
    )


def _did_you_mean(name: str, known: Iterable[str]) -> str:
    """`: did you mean '<closest>'?` for the closest known name, or nothing (design 0009)."""
    close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.75)
    return f": did you mean {close[0]!r}?" if close else ""


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


@dataclass(frozen=True)
class _Found:
    """A value one of a setting's names has at one level of the order, before coercion."""

    raw: Any
    source: str  # what `sources` says: a variable's name, a layer's `source`, "default"
    where: str | None  # for a layer's value, where errors say it came from; else None
    name: str  # what a same-level conflict names: the variable, or the key or alias
    expand: bool = False  # a layer's `expand`, or a section's `_FILE`: never a variable
    secret_files: bool = False  # secret leaves may be read: a layer's `secret_files`, or `_FILE`
    # Indexed variables: for each item, what each field that is set holds, in field order.
    items: tuple[Mapping[str, _Found], ...] | None = None
    warn_fields: bool = False  # a list section's _FILE: its unknown fields warn as it is read

    @property
    def origin(self) -> str:
        """What a coercion error names: `where` for a layer's value, else `source`."""
        return self.source if self.where is None else self.where


def _found(
    setting: Setting, layers: Sequence[Layer], environ: Mapping[str, str], expand: bool = False
) -> list[_Found]:
    """What the setting's key and aliases hold at the first level of the order where any is set:
    `_FILE`, then the variable, then each layer; else the default. The key comes first, then the
    aliases in declaration order. A list section with fields has its indexed variables as its
    variable level, the key's and each alias's.

    A section's `_FILE` is a YAML file, which expands with the project's `expand`; its plain
    variable is never read, and setting it is a `ConfigError`."""
    names = (setting.key, *setting.aliases)
    variables = [_env_name(setting.prefix, name) for name in names]
    if setting.is_section and (plain := [var for var in variables if var in environ]):
        # A mapping in a variable is unreadable (design 0009): say so, rather than ignore it.
        if setting.fields:
            raise ConfigError(
                f"{setting.key}: {plain[0]} is set, but a list section is read from a YAML file "
                f"or indexed variables: set {plain[0]}_FILE to the file's path, or "
                f"{plain[0]}_<N>_<FIELD> for each item's fields"
            )
        raise ConfigError(
            f"{setting.key}: {plain[0]} is set, but a section is read from a YAML file, never a "
            f"variable: set {plain[0]}_FILE to the file's path"
        )
    files = [f"{var}_FILE" for var in variables if f"{var}_FILE" in environ]
    if files:
        if setting.is_section:
            return [_read_section_file(setting, var, environ, expand) for var in files]
        return [_Found(_read_file_variable(var, environ), var, None, var) for var in files]
    if setting.fields:
        indexed = [_indexed_found(setting, var, environ, expand) for var in variables]
        if found := [f for f in indexed if f is not None]:
            return found
    if set_vars := [var for var in variables if var in environ]:
        return [_Found(environ[var], var, None, var) for var in set_vars]
    for layer in layers:
        found = [
            _Found(
                raw,
                layer.source,
                layer.origins.get(name, layer.where),
                name,
                layer.expand,
                layer.secret_files,
            )
            for name in names
            if (raw := _lookup(layer.data, name)) is not None
        ]
        if found:
            return found
    return [_Found(setting.default, "default", None, setting.key)]


def _read_file_variable(file_var: str, environ: Mapping[str, str]) -> str:
    path = Path(environ[file_var])
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except OSError as e:
        raise ConfigError(f"cannot read {file_var} ({path}): {e.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"cannot read {file_var} ({path}): not UTF-8") from None


def _read_section_file(
    setting: Setting, file_var: str, environ: Mapping[str, str], expand: bool
) -> _Found:
    """A section's `_FILE`: the YAML file it names, local configuration like the config file
    (`secret_files` on, `expand` the project's). An empty file is an empty section."""
    path = Path(environ[file_var])
    label = f"{file_var} ({path})"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {label}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"cannot read {label}: not UTF-8") from None
    loaded = _parse_yaml(text, label)
    if loaded is None:
        loaded = [] if setting.is_list else {}
    return _Found(
        loaded,
        file_var,
        label,
        file_var,
        expand,
        secret_files=True,
        warn_fields=bool(setting.fields),
    )


def _item_count(setting: Setting, base: str, environ: Mapping[str, str]) -> int:
    """How many items a list section's indexed variables under `base` hold: item N exists when
    any of its field variables is set, and the first N with none ends the list."""
    suffixes = [suffix for item in setting.fields for suffix in _suffixes(item)]
    count = 0
    while any(f"{base}_{count}_{suffix}" in environ for suffix in suffixes):
        count += 1
    return count


def _indexed_found(
    setting: Setting, base: str, environ: Mapping[str, str], expand: bool
) -> _Found | None:
    """A list section's items from `<base>_<N>_<FIELD>` and `_FILE` (design 0009), or None when
    item 0 has none. Each field takes its `_FILE`, then its variable; a section field, only its
    `_FILE` (a YAML file, as a section's)."""
    rows: list[Mapping[str, _Found]] = []
    for index in range(_item_count(setting, base, environ)):
        row: dict[str, _Found] = {}
        for item in setting.fields:
            var = f"{base}_{index}_{item.key.upper()}"
            if var + "_FILE" in environ:
                if item.is_section:
                    row[item.key] = _read_section_file(item, var + "_FILE", environ, expand)
                else:
                    text = _read_file_variable(var + "_FILE", environ)
                    row[item.key] = _Found(text, var + "_FILE", var + "_FILE", var + "_FILE")
            elif var in environ:
                if item.is_section:
                    raise ConfigError(
                        f"{setting.key}[{index}].{item.key}: {var} is set, but a section is read "
                        f"from a YAML file, never a variable: set {var}_FILE to the file's path"
                    )
                # `where` is the variable, so an error about the field names it.
                row[item.key] = _Found(environ[var], var, var, var)
        rows.append(row)
    if not rows:
        return None
    name = f"{base}_<N>_*"
    return _Found(None, name, None, name, items=tuple(rows))


def _conflict(setting: Setting, first: _Found, other: _Found) -> ConfigError:
    """The key and an alias (or two aliases) set at one level to different values. The message
    names both, never either value."""
    place = "the environment" if other.where is None else other.where
    return ConfigError(
        f"{setting.key}: set as both {first.name!r} and its old name {other.name!r} "
        f"(from {place}), to different values"
    )


def _resolve_one(
    setting: Setting,
    layers: Sequence[Layer],
    environ: Mapping[str, str],
    unset: Literal["error", "empty"] = "error",
) -> tuple[Value, str]:
    first, *others = _found(setting, layers, environ)
    return _typed(setting, first, others, environ, unset), first.source


def _typed(
    setting: Setting,
    first: _Found,
    others: Sequence[_Found],
    environ: Mapping[str, str],
    unset: Literal["error", "empty"],
) -> Value:
    """A setting's value from what its names hold at one level: expanded, coerced, agreeing
    across names, checked, and wrapped in `Secret` for a secret setting."""
    where = first.where
    # Expansion comes once a level is chosen (an expanded value can't make a null fall through)
    # and before typing, so `${PORT:-5000}` is the int 5000. Errors show the text as written.
    expanded = _expanded(setting, first, environ, unset)
    value = _coerce(setting, expanded, first.origin, first.raw)
    for other in others:
        # The key and an alias (or two aliases) set at one level must agree once expanded and
        # typed: neither can quietly win. The message names both, never either value.
        other_value = _coerce(
            setting, _expanded(setting, other, environ, unset), other.origin, other.raw
        )
        if other_value != value:
            raise _conflict(setting, first, other)
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
            elif not setting.secret and expanded != first.raw:
                # A non-secret value can embed a secret variable (`https://user:${PASS}@host`).
                reason = _as_written(reason, value, first.raw, expanded)
            # A layer's value names where it came from; a variable's or the default's, as before,
            # names no source.
            origin = "" if where is None else f" (from {where})"
            raise ConfigError(f"{setting.key}: {reason}{origin}") from None
        if replacement is not None:
            value = _replace(setting, value, replacement)
    if setting.secret:
        # Settings only allows str secrets, so the coerced value is a str.
        return Secret(str(value))
    return value


_Sections = Section | tuple[Section, ...]  # a section setting's value


def _resolve_section(
    setting: Setting,
    layers: Sequence[Layer],
    environ: Mapping[str, str],
    expand: bool,
    unset: Literal["error", "empty"],
    warnings: list[ConfigWarning],
) -> tuple[Value, str]:
    """A section setting's value (design 0009, section 5): `_resolve_one`'s order, alias rule and
    check, with a shape check and secret leaves in place of coercion. Unknown fields in a list
    section's `_FILE` are added to `warnings`."""
    first, *others = _found(setting, layers, environ, expand)
    return _sectioned(setting, first, others, environ, unset, warnings), first.source


def _sectioned(
    setting: Setting,
    first: _Found,
    others: Sequence[_Found],
    environ: Mapping[str, str],
    unset: Literal["error", "empty"],
    warnings: list[ConfigWarning],
) -> _Sections:
    """A section's value from what its names hold at one level: shaped (or, with fields, each
    item's fields typed), agreeing across names, and checked."""
    value = _section_value(setting, first, environ, unset, warnings)
    for other in others:
        if _section_value(setting, other, environ, unset, warnings) != value:
            raise _conflict(setting, first, other)
    if setting.check is not None:
        try:
            replacement = setting.check(value)
        except ValueError as e:
            reason = str(e)
            if first.expand:
                # A leaf can embed a secret variable (`https://user:${PASS}@host`): show the text
                # as written, as for any setting.
                written = _read_part(setting, first.raw)
                expanded = _expanded(setting, replace(first, raw=written), environ, unset)
                reason = _swap(reason, list(_changed_strings(written, expanded)), bounded=False)
            # A check shouldn't echo a secret leaf at all; if it reveals one, it is masked.
            secrets = {
                v for s in _secrets_in(value) if s for v in (s, repr(s)[1:-1], ascii(s)[1:-1])
            }
            for form in sorted(secrets, key=len, reverse=True):
                reason = reason.replace(form, MASK)
            origin = "" if first.where is None else f" (from {first.where})"
            raise ConfigError(f"{setting.key}: {reason}{origin}") from None
        if replacement is not None:
            value = _section_replacement(setting, replacement)
    return value


def _section_value(
    setting: Setting,
    found: _Found,
    environ: Mapping[str, str],
    unset: Literal["error", "empty"],
    warnings: list[ConfigWarning],
) -> _Sections:
    if setting.fields:
        return _typed_items(setting, found, environ, unset, warnings)
    return _section_of(setting, _expanded(setting, found, environ, unset), found)


def _read_part(setting: Setting, raw: Any) -> Any:
    """What resolution reads of a section's raw value: all of it, or of a list section with
    fields, each item's declared fields (an unknown field is never expanded or typed)."""
    if not setting.fields or not isinstance(raw, list | tuple):
        return raw
    names = {item.key for item in setting.fields}
    return [
        {k: v for k, v in item.items() if k in names} if isinstance(item, Mapping) else item
        for item in raw
    ]


def _typed_items(
    setting: Setting,
    found: _Found,
    environ: Mapping[str, str],
    unset: Literal["error", "empty"],
    warnings: list[ConfigWarning],
) -> tuple[Section, ...]:
    """A list section's items, each a `Section` holding every declared field (design 0009): a
    field that is set is typed and checked as a setting of its kind, a missing optional one takes
    its default, and a missing required one is a `ConfigError`. Errors name the item's path."""
    if found.source == "default":
        return ()
    if found.items is not None:
        rows = found.items
    else:
        raw = found.raw
        if not isinstance(raw, list | tuple):
            raise ConfigError(
                f"{setting.key}: expected a list of sections (mappings), got {_shape(raw)} "
                f"(from {found.origin})"
            )
        built: list[Mapping[str, _Found]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise ConfigError(
                    f"{setting.key}[{index}]: expected a section (a mapping), got {_shape(item)} "
                    f"(from {found.origin})"
                )
            # A null field is unset, as for a key.
            built.append(
                {
                    f.key: replace(found, raw=item[f.key], warn_fields=False)
                    for f in setting.fields
                    if item.get(f.key) is not None
                }
            )
        rows = tuple(built)
        if found.warn_fields:
            warnings.extend(_unknown_fields(setting, setting.key, found.origin, raw))
    items: list[Section] = []
    for index, row in enumerate(rows):
        values: dict[str, Node] = {}
        for item in setting.fields:
            path = f"{setting.key}[{index}].{item.key}"
            field_found = row.get(item.key)
            if field_found is None:
                if item.required:
                    raise _missing(path, found, item, index, row)
                field_found = _Found(item.default, "default", None, path)
            values[item.key] = _field_value(replace(item, key=path), field_found, environ, unset)
        items.append(Section._of(values))
    return tuple(items)


def _missing(
    path: str, found: _Found, item: Setting, index: int, row: Mapping[str, _Found]
) -> ConfigError:
    """A required field an item doesn't set. From indexed variables, it names the variable and
    one the item does set; from a layer or `_FILE`, where."""
    if found.items is None:
        return ConfigError(f"{path}: a required field, not set (from {found.origin})")
    var = f"{found.name.removesuffix('_<N>_*')}_{index}_{item.key.upper()}"
    other = next(iter(row.values())).name  # an item exists only when a field of it is set
    return ConfigError(f"{path}: {var} is not set, but {other} is")


def _field_value(
    item: Setting, found: _Found, environ: Mapping[str, str], unset: Literal["error", "empty"]
) -> Node:
    """One field's value, `item.key` being its path (`schedule[2].cron`). A section field is a
    free-form section; any other is typed as a setting. A `{secret_file: ...}` leaf is read for a
    secret field, from local configuration only (decision E)."""
    if item.is_section:
        return _sectioned(item, found, (), environ, unset, [])
    if isinstance(found.raw, Mapping) and _is_secret_leaf(found.raw):
        _refuse_remote_secret_file(item.key, found)  # before anything else: never opened
        if not item.secret:
            raise ConfigError(
                f"{item.key}: a secret_file leaf is read only for a secret field "
                f"(from {found.origin})"
            )
        secret = _secret_leaf(_expanded(item, found, environ, unset), item.key, found)
        # The file's contents never expand, as for a `_FILE`.
        found = _Found(secret.reveal(), found.source, found.where, found.name)
    return cast(Node, _typed(item, found, (), environ, unset))


def _unknown_fields(setting: Setting, path: str, where: str, raw: Any) -> Iterator[ConfigWarning]:
    """An `unknown` warning for each key in an item of a list section with fields that is no
    field (design 0009, question 2: a warning), suggesting the closest field. Never a value."""
    if not isinstance(raw, list | tuple):
        return  # resolution reports the shape, if this is the value read
    names = [item.key for item in setting.fields]
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        for part in item:
            if isinstance(part, str) and part in names:
                continue
            text = part if isinstance(part, str) else _yaml_text(part)
            name = f"{path}[{index}].{text}"
            close = difflib.get_close_matches(text, names, n=1, cutoff=0.75)
            hint = f": did you mean {f'{path}[{index}].{close[0]}'!r}?" if close else ""
            yield ConfigWarning("unknown", name, f"unknown field {name!r} in {where}{hint}")


def _section_replacement(setting: Setting, replacement: Value) -> _Sections:
    """A check's replacement for a section, if it is a `Section` (a tuple of them for a list
    section). Errors name types, never values."""
    if setting.is_list:
        if isinstance(replacement, tuple) and all(isinstance(i, Section) for i in replacement):
            return cast(tuple[Section, ...], replacement)
        expected = "a tuple of Section"
    elif isinstance(replacement, Section):
        return replacement
    else:
        expected = "Section"
    raise ConfigError(
        f"{setting.key}: its check returned {type(replacement).__name__}, expected {expected}"
    )


def _section_of(setting: Setting, raw: Any, found: _Found) -> _Sections:
    """`raw` as the section's value, deep-frozen, its secret leaves read. Only the shape is
    checked: a mapping (a list of them for a list section) with string keys at every level."""
    if found.source == "default":
        return () if setting.is_list else Section._of({})
    if not setting.is_list:
        if not isinstance(raw, Mapping):
            raise ConfigError(
                f"{setting.key}: expected a section (a mapping), got {_shape(raw)} "
                f"(from {found.origin})"
            )
        return _section_mapping(raw, setting.key, found)
    if not isinstance(raw, list | tuple):
        raise ConfigError(
            f"{setting.key}: expected a list of sections (mappings), got {_shape(raw)} "
            f"(from {found.origin})"
        )
    items: list[Section] = []
    for index, item in enumerate(raw):
        path = f"{setting.key}[{index}]"
        if not isinstance(item, Mapping):
            raise ConfigError(
                f"{path}: expected a section (a mapping), got {_shape(item)} (from {found.origin})"
            )
        items.append(_section_mapping(item, path, found))
    return tuple(items)


def _shape(node: object) -> str:
    """What an error says a value is, never the value: `a mapping`, `a list`, `str`."""
    if isinstance(node, Mapping):
        return "a mapping"
    if isinstance(node, list | tuple):
        return "a list"
    return type(node).__name__


def _is_secret_leaf(node: Mapping[Any, Any]) -> bool:
    """`{secret_file: <path>}`, exactly: the one key, nothing beside it."""
    return len(node) == 1 and "secret_file" in node


def _section_mapping(node: Mapping[Any, Any], path: str, found: _Found) -> Section:
    if _is_secret_leaf(node):
        # Only a section itself, or a list section's item, gets here (a nested leaf is a
        # `_section_node`'s): it must be a mapping, not a Secret. Decision E's error comes first.
        _refuse_remote_secret_file(path, found)
        raise ConfigError(
            f"{path}: a section is a mapping, so a secret_file leaf goes under a key in it "
            f"(from {found.origin})"
        )
    items: dict[str, Node] = {}
    for key, child in node.items():
        if not isinstance(key, str):
            raise ConfigError(
                f"{path}.{_yaml_text(key)}: a section's keys are strings, and this one is "
                f"{type(key).__name__}: quote it (from {found.origin})"
            )
        items[key] = _section_node(child, f"{path}.{key}", found)
    return Section._of(items)


def _section_node(node: Any, path: str, found: _Found) -> Node:
    if node is None or isinstance(node, Secret | str | int | float | datetime.date):
        return cast(Node, node)  # bool is an int; datetime is a date
    if isinstance(node, Mapping):
        if _is_secret_leaf(node):
            return _secret_leaf(node, path, found)
        return _section_mapping(node, path, found)
    if isinstance(node, list | tuple):
        return tuple(_section_node(item, f"{path}[{i}]", found) for i, item in enumerate(node))
    raise ConfigError(
        f"{path}: a section holds mappings, lists and scalars, not {type(node).__name__} "
        f"(from {found.origin})"
    )


def _secret_leaf(node: Mapping[Any, Any], path: str, found: _Found) -> Secret:
    """A `{secret_file: <path>}` leaf as a `Secret` holding the file, trailing whitespace stripped
    as for `_FILE`. Only from local configuration: anywhere else it is an error, and the file is
    never opened (design 0009, decision E). Errors name the path and file, never the contents."""
    _refuse_remote_secret_file(path, found)
    file = node["secret_file"]
    if not isinstance(file, str) or not file:
        raise ConfigError(f"{path}.secret_file: expected a file's path (from {found.origin})")
    try:
        text = Path(file).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(
            f"{path}: cannot read secret_file {file}: {e.strerror} (from {found.origin})"
        ) from None
    except UnicodeDecodeError:
        raise ConfigError(
            f"{path}: cannot read secret_file {file}: not UTF-8 (from {found.origin})"
        ) from None
    return Secret(text.rstrip())


def _refuse_remote_secret_file(path: str, found: _Found) -> None:
    """Decision E: a secret leaf from a layer without `secret_files` (a control-plane payload,
    above all) would make this process read a file the sender names. It is an error, never read
    and never kept as a plain mapping."""
    if not found.secret_files:
        raise ConfigError(
            f"{path}: secret files are read only from local configuration (the config file or a "
            f"section's _FILE), not from {found.origin}"
        )


def _secrets_in(node: object) -> Iterator[str]:
    """Every secret leaf's value in a section's value, for masking."""
    if isinstance(node, Secret):
        yield node.reveal()
    elif isinstance(node, Section):
        for child in node.values():
            yield from _secrets_in(child)
    elif isinstance(node, tuple):
        for child in node:
            yield from _secrets_in(child)


def _changed_strings(written: Any, expanded: Any) -> Iterator[tuple[str, str]]:
    """(expanded, written) for each string expansion changed, walking both trees together:
    expansion keeps the shape and the keys."""
    if isinstance(written, str):
        if written != expanded:
            yield expanded, written
    elif isinstance(written, Mapping):
        for key, child in written.items():
            yield from _changed_strings(child, expanded[key])
    elif isinstance(written, list | tuple):
        for w, e in zip(written, expanded, strict=True):
            yield from _changed_strings(w, e)


def _expanded(
    setting: Setting, found: _Found, environ: Mapping[str, str], unset: Literal["error", "empty"]
) -> Any:
    """`found.raw` with `${VAR}` expanded in every string in it, if it came from a layer that
    expands; else as it is. Mapping keys and non-strings are untouched."""
    if not found.expand:
        return found.raw

    def text(value: str) -> str:
        return _expand_text(value, setting.key, found.origin, environ, unset)

    return _expand_node(found.raw, text)


def _expand_node(node: Any, text: Callable[[str], str]) -> Any:
    if isinstance(node, str):
        return text(node)
    if isinstance(node, list):
        return [_expand_node(item, text) for item in node]
    if isinstance(node, tuple):  # a project-built layer's section may hold tuples
        return tuple(_expand_node(item, text) for item in node)
    if isinstance(node, Mapping):
        return {key: _expand_node(child, text) for key, child in node.items()}
    return node


def _expand_text(
    value: str,
    key: str,
    where: str,
    environ: Mapping[str, str],
    unset: Literal["error", "empty"],
) -> str:
    """One string with its `${NAME}`, `${NAME:-default}` and `$${` expanded (design 0009).

    Errors name the key, the variable and where, never the variable's value or the text (a
    secret setting may be written in the file)."""

    def one(m: re.Match[str]) -> str:
        if m.group(0) == "$${":
            return "${"
        name, default = m.group(1), m.group(2)
        if name is None:
            raise ConfigError(
                f"{key}: a ${{ at character {m.start() + 1} that isn't ${{NAME}} or "
                f"${{NAME:-default}} (from {where}): write $${{ for a literal ${{"
            )
        current = environ.get(name)
        if default is not None and not current:  # unset or empty, as in the shell
            return default
        if current is None:
            if unset == "empty":
                return ""
            raise ConfigError(f"{key}: ${{{name}}} is not set and has no default (from {where})")
        return current

    return _EXPANSION.sub(one, value)


def _as_written(reason: str, value: _Plain, written: Any, expanded: Any) -> str:
    """A check's `reason` with the expanded value in it replaced by the text as written.

    A list's items are replaced one by one where the written and expanded items line up; an item
    a variable split in two (`${HOSTS}` holding a comma) is masked instead."""
    pairs: list[tuple[str, str]] = []
    if isinstance(value, tuple):
        if isinstance(written, str):
            written, expanded = written.split(","), expanded.split(",")
        if len(written) == len(expanded):
            pairs = [
                (e.strip(), w.strip())
                for w, e in zip(written, expanded, strict=True)
                if isinstance(w, str) and w != e
            ]
        else:
            shown = {str(w).strip() for w in written}
            pairs = [(item, MASK) for item in value if item not in shown]
    else:
        pairs = [(str(value), written)]
    # An int or bool is replaced as a whole word only: 50 is not inside 500.
    return _swap(reason, pairs, bounded=not isinstance(value, str | tuple))


def _swap(reason: str, pairs: list[tuple[str, str]], bounded: bool) -> str:
    """`reason` with each pair's first text replaced by its second, longest first, as written and
    inside a repr() or ascii(); `bounded` replaces whole words only."""
    for old, new in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
        if not old:
            continue
        forms = {
            (old, new),
            (repr(old)[1:-1], repr(new)[1:-1]),
            (ascii(old)[1:-1], ascii(new)[1:-1]),
        }
        for form, replacement in sorted(forms, key=lambda f: len(f[0]), reverse=True):
            pattern = rf"(?<!\w){re.escape(form)}(?!\w)" if bounded else re.escape(form)
            reason = re.sub(pattern, _literal(replacement), reason)
    return reason


def _literal(text: str) -> Callable[[re.Match[str]], str]:
    """A `re.sub` replacement that is `text` as it is: no backslash escapes or group references."""
    return lambda _: text


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


def _coerce(setting: Setting, raw: Any, source: str, written: Any = None) -> _Plain:
    """Give raw (a string from a file or the environment, or a YAML value) the setting's type.

    `written` is the value before expansion, which an error shows in raw's place."""
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
        shown = raw if written is None else written
        got = "" if setting.secret else f", got {shown!r}"
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
