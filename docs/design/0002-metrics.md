# 0002: Metrics

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: andronics; drafted by Claude

## Context

mitm-proxy writes its own Prometheus text exposition (`src/mitm_proxy/metrics.py`, about 250 lines):
`Counter`, `Gauge` (optionally read from a callback) and a classic `Histogram`, rendered for its admin
API's `GET /metrics`. Its designs 0001, 0002 and 0007 chose to write the format by hand rather than
depend on `prometheus_client`, and its module docstring states the one rule that keeps the series count
bounded: "every label has a small, fixed set of values: the intercepted host is never a label". That
rule is a comment. Nothing stops the next metric from taking a host, a path or a user name as a label.

sluice is the second user. It has no metrics yet, and its design 0001 rejects an HTTP listener ("An HTTP
control/metrics API": a network surface and a dependency for nothing it needs). It would publish the same
text as a virtual file, `/.sluice/metrics`, read through the mount, or as a node_exporter textfile. So
interpose provides the mechanism (the metric types and the text they render) and nothing about
transport: no HTTP server, no file writer, no scrape handler.

Constraints:

- **No new dependency.** The text format is small. mitm-proxy's 0007 rejected `prometheus_client` ("a
  dependency for about 40 lines, and its process collectors add series nobody asked for").
- **Bounded series, enforced.** A label takes values only from a set declared up front. A value outside it
  is an error at the call that used it, not a new series.
- **mitm-proxy's output stays byte-for-byte the same** when it switches, for the same operations: the
  dashboards and recording rules built on 0007 keep working.
- **No I/O at import, nothing logged.** One event loop per process (both consumers are asyncio), so no
  locking.

## Decision

A new module, `interpose.metrics`. Its public names:

```python
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"  # what a /metrics response declares


class Label:
    """A label name and the only values it may take."""

    def __init__(self, name: str, values: Iterable[str] = ()) -> None: ...

    name: str
    values: frozenset[str]  # property, read-only

    def declare(self, *values: str) -> None: ...  # add values, e.g. after a config reload


class Metric:  # base class: name, help, kind, labels; samples() and render()
    ...


class Counter(Metric):
    def __init__(self, name: str, help: str, labels: Sequence[Label] = ()) -> None: ...
    def inc(self, *values: str, amount: float = 1.0) -> None: ...


class Gauge(Metric):
    def __init__(
        self,
        name: str,
        help: str,
        labels: Sequence[Label] = (),
        read: Callable[[], Mapping[tuple[str, ...], float]] | None = None,
    ) -> None: ...
    def set(self, value: float, *values: str) -> None: ...
    def add(self, amount: float, *values: str) -> None: ...


class Histogram(Metric):
    def __init__(
        self, name: str, help: str, buckets: Sequence[float], labels: Sequence[Label] = ()
    ) -> None: ...
    def observe(self, value: float, *values: str) -> None: ...


class Registry:
    """The metrics one endpoint exports, in registration order."""

    def __init__(self, metrics: Iterable[Metric] = ()) -> None: ...
    def register(self, metric: M) -> M: ...  # returns it, so `self.x = reg.register(Counter(...))`
    def __iter__(self) -> Iterator[Metric]: ...
    def render(self) -> str: ...
```

The shapes are mitm-proxy's, so its call sites (`flows.inc("http")`, `client_connections.add(1)`,
`certificate_expiry.set(ts, "ca")`, `flow_duration.observe(t, kind)`, `upstream_connections.read = ...`)
keep working. Two things change: a label is a `Label`, not a bare name, and a label value must be one the
`Label` declares.

### Labels from declared value sets

- `Label(name, values)` holds the label name and its allowed values. Every `inc`, `set`, `add` and
  `observe` checks each value against its label's set. A value outside it raises `ValueError` naming the
  metric and the label, not the value (it is the kind of value that shouldn't be there, a host or a path,
  and errors don't carry values).
- A gauge's `read` callback is checked the same way, when the gauge renders.
- `declare(*values)` adds values to a label. It is the only way to grow the set, and it is a deliberate,
  reviewable call in the project's code: mitm-proxy declares its middleware names when it builds or
  reloads a pipeline, from configuration, not from traffic. Values are never removed (a series that has
  existed keeps rendering until the process restarts, as today).
- One `Label` can serve several metrics. mitm-proxy's `middleware` label is shared by
  `mitm_proxy_middleware_failures_total` and `mitm_proxy_middleware_duration_seconds`, so one `declare`
  covers both.
- The number of values passed must match the number of labels, checked at the call (mitm-proxy found a
  mismatch only when it rendered).

### The text

The Prometheus text format, version 0.0.4, exactly as mitm-proxy renders it today:

- `# HELP <name> <help>` and `# TYPE <name> <kind>` for every registered metric, then one sample per label
  set, sorted by label values. A metric with no samples renders only its two comment lines.
- Numbers: integral values without a decimal point (`2`, not `2.0`), others as Python's `repr` of the
  float; `+Inf`, `-Inf` and `NaN` for the non-finite values a gauge can hold.
- Label values escape `\`, `"` and newline. HELP text escapes `\` and newline, as the format requires
  (mitm-proxy didn't; none of its help strings contain either, so its output doesn't change).
- Histograms: cumulative `_bucket{...,le="<bound>"}` lines with `le` as `repr(float(bound))` (`"1.0"`) and
  a final `le="+Inf"`, then `_sum` and `_count`. A bucket is inclusive (`le`), via `bisect_left`.
- `Registry.render()` joins every metric's lines with `\n` and ends with one `\n`; an empty registry is
  `""`.

### Validation at declaration

`ValueError` at construction, so a bad declaration fails at the consumer's import or startup, never in a
live request:

- metric names match `[a-zA-Z_:][a-zA-Z0-9_:]*`; label names `[a-zA-Z_][a-zA-Z0-9_]*` and don't start
  with `__` (reserved by Prometheus); a metric's label names are distinct; a histogram can't have a label
  called `le`;
- histogram buckets are finite, distinct and at least one (they are sorted, as mitm-proxy did; `+Inf` is
  always added);
- `Registry` rejects two metrics with the same name, or whose sample names clash (a histogram `foo`
  writes `foo_bucket`, `foo_sum` and `foo_count`, so a counter `foo_count` beside it is refused);
- a label's values are an iterable of `str`, never a bare `str` (which would declare its characters).

At use: a counter's `amount` must be finite and zero or more (counters only go up, and `+Inf` would pin
the series), and a histogram refuses `NaN`
(it would land in no bucket and poison `_sum`).

### What stays in the projects

- **Which metrics exist, their names, help and buckets.** mitm-proxy's `NETWORK_BUCKETS` and
  `MIDDLEWARE_BUCKETS` stay in mitm-proxy: fixed buckets are a dashboard contract (its 0007), and interpose
  offers no defaults to drift from.
- **Transport.** mitm-proxy's API answers `GET /metrics` with `Registry.render()` and `CONTENT_TYPE`.
  sluice serves the same text as `/.sluice/metrics`.
- **The naming scheme** (`<project>_middleware_seconds{middleware}`, and so on): a convention, written
  in each project's metrics table, not code.

## Alternatives considered

- **`prometheus_client`.** Rejected, as mitm-proxy's 0007 did: a dependency for a small format, and its
  default process and platform collectors add series nobody asked for. Its label values are unbounded.
- **Labels as names only, with the rule left to review (mitm-proxy today).** That is the status quo the
  synergy review flagged: a comment can't stop a host becoming a label. Declared sets make it an error.
- **Undeclared values folded into an `"other"` series.** Keeps a request from failing over a metric, but
  hides the bug: the dashboard quietly fills with `other`. A `ValueError` is caught by the project's
  tests the first time the code path runs.
- **Declaring label values as a fixed `frozenset` only, with no `declare()`.** mitm-proxy's middleware
  names come from configuration, which it can reload at runtime (`Runtime.apply`). Building new metrics on
  every reload would reset every counter. `declare()` grows the set from configuration, never from data.
- **Pre-registering every label combination at zero.** Prometheus recommends it for known label sets, but
  mitm-proxy renders only series that have happened, and switching would change its output. A project can
  still call `inc(..., amount=0)` for the combinations it wants present.
- **OpenMetrics output.** mitm-proxy's 0002 mentions it for `Accept` negotiation; nothing implements it
  yet. It can be added as a second `render` method when a consumer needs exemplars or `_created`.
- **Summaries, native histograms.** Rejected in mitm-proxy's 0007 (quantiles don't aggregate across a
  fleet; native histograms need protobuf). Not offered.
- **An HTTP handler or textfile writer in interpose.** Transport is project code: mitm-proxy has its own
  API, and sluice has no listener by design (its 0001).

## Consequences

- One exposition implementation, with the bounded-labels rule enforced. A project whose code passes an
  undeclared value finds out in its tests, not in Prometheus's memory.
- **No new dependency**: stdlib only (`bisect`, `math`, `re`).
- interpose's coverage guards both consumers' `/metrics` at once.
- Wiki: API-Reference (the module), Architecture-Internals (the module, the text rules), Introduction (the
  version table), Testing-Guide (the test file).
- `TODO.md` "Decided against": `prometheus_client`; folding undeclared label values into `other`.

### Consumer adoption

What each project's switch will need (each writes its own short design record first; this list is the
brief):

**mitm-proxy**

1. Bump the pin to the interpose tag that ships this record.
2. `metrics.py` keeps its `Metrics` class, `NETWORK_BUCKETS`, `MIDDLEWARE_BUCKETS` and `observe_flow`, and
   drops `_escape`, `_format`, `Metric`, `Counter`, `Gauge`, `Histogram` and `_le` for
   `interpose.metrics`. `Metrics.all()`/`render()` become a `Registry` (keep `all()` if tests use it).
3. Declare every label's values: `type` from `FlowType`, `reason` from the tunnel-decision enum,
   `direction` (`request`, `response`), `protocol` (`h1`, `h2`), `result` (every result
   `config_applies` counts), `kind` (`ca`), `outcome` from the outcome kinds (0006), `reused`
   (`true`, `false`), and `version` with the one running version.
4. The `middleware` label: one shared `Label("middleware")`, declared with the names in
   `middleware.available` (the registry, so a reload that adds a chain entry is covered) in `_build` before
   the pipeline starts. Names reach the metrics from that registry only.
5. `api.py`'s `PROMETHEUS_TEXT` can become `interpose.metrics.CONTENT_TYPE` (the same string).
6. Tests: `test_metrics.py`'s escaping test passes an undeclared, odd middleware name
   (`'we"ird\\name\n'`); it must declare it first. The golden strings elsewhere should pass unchanged,
   which is the check that the output is identical.

**sluice**

1. Pin the tag; a new design record decides which metrics (the synergy note suggests
   `sluice_middleware_seconds{middleware}`, `sluice_middleware_failures_total{middleware}`,
   `sluice_call_duration_seconds{op,outcome}`; `op` is declared from its `OPS` set, `outcome` from a
   fixed list, the middleware label from the app's frozen route tree at mount time).
2. Publish `Registry.render()` as a read-only virtual file at `/.sluice/metrics`, computed when the file
   is opened (compute-per-call), and never as an HTTP listener (its 0001).
3. Never a path, a route parameter or a user as a label: per-path detail is what its access-log example
   is for.

## Open questions

Decided when this record was implemented (2026-11-02), on Stephen's standing instruction to build 0002;
each is the recommended answer and can be reversed before a consumer adopts it.

1. **An undeclared label value raises `ValueError`.** Recommended over folding into `other` (see
   Alternatives): the error surfaces in the consumer's tests. The cost is that a missed declaration fails
   the call that observed it; mitm-proxy's metric calls sit inside a flow, so its adoption must declare
   every value up front (the list above).
2. **Label sets grow through `declare()` and never shrink.** Recommended: keeps counters across
   mitm-proxy's config reloads, and keeps growth in configuration code.
3. **HELP is escaped.** A format fix with no effect on today's help strings.
