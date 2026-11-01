"""Shared mechanism for metalayerlab interceptors — the layered settings loader, Secret values,
Prometheus text exposition, and a policy-configurable async middleware pipeline.
"""

from importlib.metadata import PackageNotFoundError, metadata, version

try:
    __version__ = version("interpose")
    # pyproject.toml's description, read rather than repeated as a literal ruff would have to wrap.
    __description__ = metadata("interpose")["Summary"]
except PackageNotFoundError:  # pragma: no cover - a source tree that was never installed
    __version__ = "0+unknown"
    __description__ = ""

__all__ = ["__description__", "__version__"]
