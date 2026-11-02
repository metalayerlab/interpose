"""Prometheus text exposition: counters, gauges and classic histograms (design 0002).

Every label takes its values from a set declared up front (`Label`), so the series count is fixed
by the code and its configuration, never by traffic: a host, a path or a user name can't become a
label by accident. A value outside the set is a `ValueError` at the call that used it.

This is the mechanism only. Which metrics exist, their buckets, and how the text reaches a
scraper (an HTTP endpoint, a virtual file) belong to each project. Nothing here does I/O or logs,
and nothing is locked: each consumer runs one event loop.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TypeVar

__all__ = [
    "CONTENT_TYPE",
    "Counter",
    "Gauge",
    "Histogram",
    "Label",
    "LabelValues",
    "Metric",
    "Registry",
]

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

LabelValues = tuple[str, ...]

_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _escape_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if value == int(value) else repr(float(value))


def _sample(name: str, labels: Sequence[str], values: Sequence[str], value: float) -> str:
    if labels:
        pairs = ",".join(f'{k}="{_escape_value(v)}"' for k, v in zip(labels, values, strict=True))
        name = f"{name}{{{pairs}}}"
    return f"{name} {_number(value)}"


class Label:
    """A label name and the only values it may take.

    `declare()` adds values, from configuration (say, the middleware names a reload brings), never
    from the data being measured. Values are never removed. One `Label` can serve several metrics.
    """

    __slots__ = ("_values", "name")

    def __init__(self, name: str, values: Iterable[str] = ()) -> None:
        if not isinstance(name, str) or not _LABEL_NAME.fullmatch(name):
            raise ValueError(f"label name {name!r} must match [a-zA-Z_][a-zA-Z0-9_]*")
        if name.startswith("__"):
            raise ValueError(f"label name {name!r}: names starting with __ are reserved")
        if isinstance(values, str):
            # A bare str is an iterable of characters: Label("kind", "ca") would allow "c", "a".
            raise TypeError(f"label {name!r}: values must be an iterable of str, not a str")
        self.name = name
        self._values: set[str] = set()
        self.declare(*values)

    @property
    def values(self) -> frozenset[str]:
        return frozenset(self._values)

    def declare(self, *values: str) -> None:
        """Allow these values from now on."""
        for value in values:
            if not isinstance(value, str):
                raise TypeError(f"label {self.name!r}: values must be str")
        self._values.update(values)

    def __contains__(self, value: object) -> bool:
        return value in self._values

    def __repr__(self) -> str:
        return f"Label({self.name!r}, {sorted(self._values)!r})"


class Metric:
    """What every metric has: a name, help text, a type and its labels."""

    def __init__(self, name: str, help: str, kind: str, labels: Sequence[Label] = ()) -> None:
        if not isinstance(name, str) or not _METRIC_NAME.fullmatch(name):
            raise ValueError(f"metric name {name!r} must match [a-zA-Z_:][a-zA-Z0-9_:]*")
        labels = tuple(labels)
        for label in labels:
            if not isinstance(label, Label):
                raise TypeError(f"metric {name!r}: labels must be Label objects")
        names = [label.name for label in labels]
        if len(set(names)) != len(names):
            raise ValueError(f"metric {name!r}: a label name appears twice")
        self.name = name
        self.help = help
        self.kind = kind
        self.labels = labels
        self.values: dict[LabelValues, float] = {}

    @property
    def series_names(self) -> frozenset[str]:
        """Every sample name this metric writes."""
        return frozenset({self.name})

    @property
    def label_names(self) -> tuple[str, ...]:
        return tuple(label.name for label in self.labels)

    def _check(self, values: Sequence[str]) -> LabelValues:
        """The label values, if they fit this metric's labels and their declared sets."""
        if len(values) != len(self.labels):
            raise ValueError(
                f"metric {self.name!r}: {len(values)} label values for {len(self.labels)} labels"
            )
        for label, value in zip(self.labels, values, strict=True):
            # The value isn't in the message: it's the kind that shouldn't be there at all.
            if value not in label:
                raise ValueError(
                    f"metric {self.name!r}: label {label.name!r} got a value outside its "
                    "declared set"
                )
        return tuple(values)

    def samples(self) -> Mapping[LabelValues, float]:
        return self.values

    def _header(self) -> list[str]:
        return [f"# HELP {self.name} {_escape_help(self.help)}", f"# TYPE {self.name} {self.kind}"]

    def render(self) -> list[str]:
        lines = self._header()
        names = self.label_names
        for values, value in sorted(self.samples().items()):
            lines.append(_sample(self.name, names, self._check(values), value))
        return lines


class Counter(Metric):
    """A value that only goes up."""

    def __init__(self, name: str, help: str, labels: Sequence[Label] = ()) -> None:
        super().__init__(name, help, "counter", labels)

    def inc(self, *values: str, amount: float = 1.0) -> None:
        # NaN and +Inf would poison the series until the process restarts.
        if not (math.isfinite(amount) and amount >= 0):
            raise ValueError(f"counter {self.name!r}: amount must be finite and zero or more")
        key = self._check(values)
        self.values[key] = self.values.get(key, 0.0) + amount


class Gauge(Metric):
    """A value that goes up and down; or, with `read`, one read when the gauge renders."""

    def __init__(
        self,
        name: str,
        help: str,
        labels: Sequence[Label] = (),
        read: Callable[[], Mapping[LabelValues, float]] | None = None,
    ) -> None:
        super().__init__(name, help, "gauge", labels)
        self.read = read  # if set, the samples come from here, not from set()/add()

    def set(self, value: float, *values: str) -> None:
        self.values[self._check(values)] = value

    def add(self, amount: float, *values: str) -> None:
        key = self._check(values)
        self.values[key] = self.values.get(key, 0.0) + amount

    def samples(self) -> Mapping[LabelValues, float]:
        return self.read() if self.read is not None else self.values


class Histogram(Metric):
    """A classic histogram: cumulative buckets, a sum and a count, per label set."""

    def __init__(
        self, name: str, help: str, buckets: Sequence[float], labels: Sequence[Label] = ()
    ) -> None:
        super().__init__(name, help, "histogram", labels)
        if "le" in self.label_names:
            raise ValueError(f"histogram {name!r}: 'le' is reserved for the buckets")
        bounds = sorted(float(b) for b in buckets)
        if not bounds:
            raise ValueError(f"histogram {name!r}: needs at least one bucket")
        if not all(math.isfinite(b) for b in bounds):
            raise ValueError(f"histogram {name!r}: buckets must be finite (+Inf is implied)")
        if len(set(bounds)) != len(bounds):
            raise ValueError(f"histogram {name!r}: a bucket appears twice")
        self.buckets = tuple(bounds)
        # Per label set: a count per bucket (the last is +Inf; not cumulative), and the sum.
        self.series: dict[LabelValues, tuple[list[int], list[float]]] = {}

    @property
    def series_names(self) -> frozenset[str]:
        return frozenset({self.name, *(f"{self.name}{s}" for s in ("_bucket", "_sum", "_count"))})

    def observe(self, value: float, *values: str) -> None:
        if math.isnan(value):
            raise ValueError(f"histogram {self.name!r}: can't observe NaN")
        key = self._check(values)
        counts, total = self.series.setdefault(key, ([0] * (len(self.buckets) + 1), [0.0]))
        counts[bisect.bisect_left(self.buckets, value)] += 1  # le is inclusive
        total[0] += value

    def samples(self) -> Mapping[LabelValues, float]:
        """Each label set's observation count."""
        return {values: float(sum(counts)) for values, (counts, _) in self.series.items()}

    def render(self) -> list[str]:
        lines = self._header()
        names = self.label_names
        with_le = (*names, "le")
        bounds = [*(repr(b) for b in self.buckets), "+Inf"]
        for values, (counts, total) in sorted(self.series.items()):
            running = 0
            for bound, count in zip(bounds, counts, strict=True):
                running += count
                lines.append(_sample(f"{self.name}_bucket", with_le, (*values, bound), running))
            lines.append(_sample(f"{self.name}_sum", names, values, total[0]))
            lines.append(_sample(f"{self.name}_count", names, values, running))
        return lines


M = TypeVar("M", bound=Metric)


class Registry:
    """The metrics one endpoint exports, rendered in registration order."""

    def __init__(self, metrics: Iterable[Metric] = ()) -> None:
        self._metrics: dict[str, Metric] = {}
        for metric in metrics:
            self.register(metric)

    def register(self, metric: M) -> M:
        if not isinstance(metric, Metric):
            raise TypeError("only Metric objects can be registered")
        if metric.name in self._metrics:
            raise ValueError(f"metric {metric.name!r} is registered twice")
        # A histogram writes foo_bucket, foo_sum and foo_count: a counter called foo_count next
        # to it would make two families share a sample name, and the scrape would fail.
        for other in self._metrics.values():
            if other.series_names & metric.series_names:
                raise ValueError(
                    f"metric {metric.name!r} clashes with the samples of {other.name!r}"
                )
        self._metrics[metric.name] = metric
        return metric

    def __iter__(self) -> Iterator[Metric]:
        return iter(self._metrics.values())

    def __len__(self) -> int:
        return len(self._metrics)

    def render(self) -> str:
        """The text exposition of every metric, ending in a newline ("" when empty)."""
        lines = [line for metric in self for line in metric.render()]
        return "\n".join(lines) + "\n" if lines else ""
