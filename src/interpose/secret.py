"""Secret values: masked wherever they are shown, revealed only on request.

A `Secret` is not a `str`, so it can't reach a log line, an f-string or an exception message as
its value by accident: `str()`, `repr()` and `format()` all give `MASK`. Call `reveal()` at the
point of use.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

__all__ = ["MASK", "Secret", "read_secret_dir"]

MASK = "********"


class Secret:
    """A secret value. `str()` and `repr()` are masked; `reveal()` gives the value.

    Equality compares values (in constant time); a Secret is truthy when its value is not empty.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            # Secret(Secret(...)) or Secret(b"...") would hide a bug, and str() of either a leak.
            raise TypeError(f"Secret takes a str, not {type(value).__name__}")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({MASK!r})"

    def __str__(self) -> str:
        return MASK

    def __format__(self, spec: str) -> str:
        # object.__format__ already uses str() for "", but raises for any other spec.
        return format(MASK, spec)

    def __bool__(self) -> bool:
        # "Is it set?" is answerable without revealing it (mitm-proxy's config hash asks).
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    def __hash__(self) -> int:
        return hash(self._value)


def read_secret_dir(directory: Path) -> Mapping[str, Secret]:
    """Read each regular file in `directory` as a `Secret`, keyed by file name.

    The value is the file's text minus one trailing newline. A missing directory means no secrets.
    Dot-entries are skipped (a Kubernetes secret volume's `..data` and timestamped directories).
    Raises `ConfigError` if the directory or a file in it can't be read.
    """
    # Imported here: interpose.settings imports this module for Secret.
    from interpose.settings import ConfigError

    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return MappingProxyType({})
    except OSError as e:
        raise ConfigError(f"cannot read secrets directory {directory}: {e.strerror}") from None
    secrets: dict[str, Secret] = {}
    for name in names:
        path = directory / name
        if name.startswith(".") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"cannot read secret {name!r} in {directory}: {e.strerror}") from None
        except UnicodeDecodeError:
            raise ConfigError(f"cannot read secret {name!r} in {directory}: not UTF-8") from None
        secrets[name] = Secret(text.removesuffix("\n").removesuffix("\r"))
    return MappingProxyType(secrets)
