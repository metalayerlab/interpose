"""interpose.metrics: declared label sets, the text format, and parity with mitm-proxy's output."""

from __future__ import annotations

import math

import pytest

from interpose.metrics import (
    CONTENT_TYPE,
    Counter,
    Gauge,
    Histogram,
    Label,
    Metric,
    Registry,
)

# --- Label -------------------------------------------------------------------------------------


def test_label_holds_its_declared_values() -> None:
    label = Label("outcome", ["origin", "local"])
    assert label.name == "outcome"
    assert label.values == frozenset({"origin", "local"})
    assert "origin" in label
    assert "aborted" not in label
    assert repr(label) == "Label('outcome', ['local', 'origin'])"


def test_label_declare_adds_values_and_never_removes() -> None:
    label = Label("middleware")
    assert label.values == frozenset()
    label.declare("auth", "log")
    label.declare("auth")
    assert label.values == frozenset({"auth", "log"})


def test_label_values_are_a_read_only_copy() -> None:
    label = Label("x", ["a"])
    values = label.values
    assert isinstance(values, frozenset)
    label.declare("b")
    assert values == frozenset({"a"})


@pytest.mark.parametrize("name", ["", "1st", "a-b", "a.b", "a b", "é"])
def test_label_name_must_be_a_prometheus_label_name(name: str) -> None:
    with pytest.raises(ValueError, match="must match"):
        Label(name)


def test_label_name_must_be_a_str() -> None:
    with pytest.raises(ValueError, match="must match"):
        Label(5)  # type: ignore[arg-type]  # the check under test


def test_label_names_starting_with_two_underscores_are_reserved() -> None:
    with pytest.raises(ValueError, match="reserved"):
        Label("__name__")
    assert Label("_private").name == "_private"


def test_label_values_must_be_str() -> None:
    with pytest.raises(TypeError, match="values must be str"):
        Label("x", ["a", 1])  # type: ignore[list-item]  # the check under test
    label = Label("y")
    with pytest.raises(TypeError):
        label.declare(None)  # type: ignore[arg-type]  # the check under test
    assert label.values == frozenset()


# --- Metric declarations -----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "1x", "a-b", "a.b", "a b"])
def test_metric_name_must_be_a_prometheus_metric_name(name: str) -> None:
    with pytest.raises(ValueError, match="must match"):
        Counter(name, "help")


def test_metric_name_must_be_a_str() -> None:
    with pytest.raises(ValueError, match="must match"):
        Counter(None, "help")  # type: ignore[arg-type]  # the check under test


def test_metric_names_may_use_colons() -> None:
    assert Counter("job:requests:rate5m", "help").name == "job:requests:rate5m"


def test_metric_labels_must_be_label_objects() -> None:
    with pytest.raises(TypeError, match="Label objects"):
        Counter("c", "help", ("type",))  # type: ignore[arg-type]  # the old mitm-proxy shape


def test_metric_label_names_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="appears twice"):
        Counter("c", "help", (Label("a"), Label("a")))


def test_metric_exposes_its_label_names() -> None:
    c = Counter("c", "help", (Label("a"), Label("b")))
    assert c.label_names == ("a", "b")
    assert c.kind == "counter"


def test_histogram_cannot_have_a_label_called_le() -> None:
    with pytest.raises(ValueError, match="reserved"):
        Histogram("h", "help", (1,), (Label("le"),))


@pytest.mark.parametrize(
    ("buckets", "match"),
    [
        ((), "at least one"),
        ((1, math.inf), "finite"),
        ((math.nan,), "finite"),
        ((1, 2, 1), "twice"),
        ((1, 1.0), "twice"),
    ],
)
def test_histogram_buckets_are_checked(buckets: tuple[float, ...], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Histogram("h", "help", buckets)


def test_histogram_buckets_are_sorted_floats() -> None:
    assert Histogram("h", "help", (5, 0.1, 1)).buckets == (0.1, 1.0, 5.0)


# --- Declared label sets, enforced -------------------------------------------------------------


def test_an_undeclared_value_is_an_error_and_never_named() -> None:
    c = Counter("c_total", "help", (Label("type", ["http"]),))
    with pytest.raises(ValueError, match="label 'type' got a value outside its declared set") as e:
        c.inc("api.example.org")
    assert "api.example.org" not in str(e.value)
    assert c.values == {}


@pytest.mark.parametrize(
    "call",
    [
        lambda g: g.set(1, "nope"),
        lambda g: g.add(1, "nope"),
    ],
)
def test_gauge_checks_label_values(call: object) -> None:
    g = Gauge("g", "help", (Label("kind", ["ca"]),))
    with pytest.raises(ValueError, match="outside its declared set"):
        call(g)  # type: ignore[operator]  # a lambda from the parametrize list
    assert g.values == {}


def test_histogram_checks_label_values() -> None:
    h = Histogram("h", "help", (1,), (Label("outcome", ["origin"]),))
    with pytest.raises(ValueError, match="outside its declared set"):
        h.observe(0.5, "/some/path")
    assert h.series == {}


def test_the_number_of_values_must_match_the_labels() -> None:
    c = Counter("c", "help", (Label("a", ["x"]), Label("b", ["y"])))
    with pytest.raises(ValueError, match="1 label values for 2 labels"):
        c.inc("x")
    with pytest.raises(ValueError, match="3 label values for 2 labels"):
        c.inc("x", "y", "z")
    plain = Counter("p", "help")
    with pytest.raises(ValueError, match="1 label values for 0 labels"):
        plain.inc("x")


def test_a_non_str_value_is_outside_any_set() -> None:
    c = Counter("c", "help", (Label("a", ["1"]),))
    with pytest.raises(ValueError, match="outside"):
        c.inc(1)  # type: ignore[arg-type]  # the check under test


def test_declare_admits_a_value_for_every_metric_sharing_the_label() -> None:
    middleware = Label("middleware")
    failures = Counter("f_total", "help", (middleware,))
    duration = Histogram("d_seconds", "help", (1,), (middleware,))
    with pytest.raises(ValueError):
        failures.inc("auth")
    middleware.declare("auth")  # e.g. a config reload adds a middleware
    failures.inc("auth")
    duration.observe(0.5, "auth")
    assert failures.values == {("auth",): 1.0}
    assert duration.samples() == {("auth",): 1.0}


def test_a_read_gauge_is_checked_when_it_renders() -> None:
    g = Gauge("g", "help", (Label("protocol", ["h1", "h2"]),), read=lambda: {("h3",): 1.0})
    with pytest.raises(ValueError, match="outside its declared set"):
        g.render()
    g.read = lambda: {("h2",): 3.0, ("h1",): 1.0}
    assert g.render()[2:] == ['g{protocol="h1"} 1', 'g{protocol="h2"} 3']


# --- Values ------------------------------------------------------------------------------------


def test_counter_counts_and_refuses_to_go_down() -> None:
    c = Counter("c_total", "help")
    c.inc()
    c.inc(amount=2.5)
    c.inc(amount=0)
    assert c.values == {(): 3.5}
    with pytest.raises(ValueError, match="zero or more"):
        c.inc(amount=-1)
    with pytest.raises(ValueError, match="zero or more"):
        c.inc(amount=math.nan)
    assert c.values == {(): 3.5}


def test_gauge_sets_and_adds() -> None:
    g = Gauge("g", "help", (Label("k", ["a"]),))
    g.set(5, "a")
    g.add(-2, "a")
    assert g.samples() == {("a",): 3.0}
    fresh = Gauge("p", "help")
    fresh.add(2)
    fresh.add(-1)
    assert fresh.render()[-1] == "p 1"


def test_histogram_refuses_nan() -> None:
    h = Histogram("h", "help", (1,))
    with pytest.raises(ValueError, match="NaN"):
        h.observe(math.nan)
    assert h.series == {}


def test_histogram_takes_infinity_into_the_last_bucket() -> None:
    h = Histogram("h", "help", (1,))
    h.observe(math.inf)
    assert h.render()[2:] == [
        'h_bucket{le="1.0"} 0',
        'h_bucket{le="+Inf"} 1',
        "h_sum +Inf",
        "h_count 1",
    ]


# --- The text format ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (2, "2"),
        (2.0, "2"),
        (-3.0, "-3"),
        (1.5, "1.5"),
        (0.1, "0.1"),
        (1e-05, "1e-05"),
        (math.inf, "+Inf"),
        (-math.inf, "-Inf"),
        (math.nan, "NaN"),
    ],
)
def test_numbers(value: float, text: str) -> None:
    g = Gauge("g", "help")
    g.set(value)
    assert g.render()[-1] == f"g {text}"


def test_label_values_are_escaped() -> None:
    odd = 'we"ird\\name\n'
    c = Counter("c", "help", (Label("middleware", [odd]),))
    c.inc(odd)
    assert c.render()[-1] == 'c{middleware="we\\"ird\\\\name\\n"} 1'


def test_help_is_escaped() -> None:
    c = Counter("c", 'two\nlines, a \\ and a "quote"')
    assert c.render()[0] == '# HELP c two\\nlines, a \\\\ and a "quote"'


def test_samples_are_sorted_by_label_values() -> None:
    c = Counter("c", "help", (Label("t", ["b", "a", "c"]),))
    for value in ("c", "a", "b"):
        c.inc(value)
    assert c.render()[2:] == ['c{t="a"} 1', 'c{t="b"} 1', 'c{t="c"} 1']


def test_a_metric_without_samples_renders_its_header_only() -> None:
    assert Counter("c", "help").render() == ["# HELP c help", "# TYPE c counter"]
    assert Histogram("t", "help", (1,)).render() == ["# HELP t help", "# TYPE t histogram"]


def test_histogram_renders_cumulative_buckets_sum_and_count() -> None:
    h = Histogram("h_seconds", "help", (0.1, 1), (Label("outcome", ["origin"]),))
    for value in (0.05, 0.1, 0.5, 7):  # 0.1 lands in le="0.1": le is inclusive
        h.observe(value, "origin")
    assert h.render() == [
        "# HELP h_seconds help",
        "# TYPE h_seconds histogram",
        'h_seconds_bucket{outcome="origin",le="0.1"} 2',
        'h_seconds_bucket{outcome="origin",le="1.0"} 3',
        'h_seconds_bucket{outcome="origin",le="+Inf"} 4',
        'h_seconds_sum{outcome="origin"} 7.65',
        'h_seconds_count{outcome="origin"} 4',
    ]


def test_histogram_without_labels() -> None:
    h = Histogram("t", "help", (1,))
    h.observe(2)
    expected = ['t_bucket{le="1.0"} 0', 't_bucket{le="+Inf"} 1', "t_sum 2", "t_count 1"]
    assert h.render()[2:] == expected


def test_metric_base_class_renders_its_values() -> None:
    m = Metric("untyped_thing", "help", "untyped")
    m.values[()] = 4
    assert m.render() == [
        "# HELP untyped_thing help",
        "# TYPE untyped_thing untyped",
        "untyped_thing 4",
    ]


def test_content_type_is_the_text_format() -> None:
    assert CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


# --- Registry ----------------------------------------------------------------------------------


def test_registry_renders_in_registration_order() -> None:
    reg = Registry()
    b = reg.register(Counter("b_total", "B."))
    a = reg.register(Gauge("a", "A."))
    b.inc()
    a.set(2)
    assert list(reg) == [b, a]
    assert len(reg) == 2
    assert reg.render() == (
        "# HELP b_total B.\n# TYPE b_total counter\nb_total 1\n# HELP a A.\n# TYPE a gauge\na 2\n"
    )


def test_registry_takes_metrics_at_construction() -> None:
    c = Counter("c", "help")
    assert list(Registry([c])) == [c]


def test_registry_rejects_a_duplicate_name() -> None:
    reg = Registry([Counter("x", "help")])
    with pytest.raises(ValueError, match="registered twice"):
        reg.register(Gauge("x", "help"))


def test_registry_takes_only_metrics() -> None:
    with pytest.raises(TypeError, match="Metric objects"):
        Registry(["x"])  # type: ignore[list-item]  # the check under test


def test_an_empty_registry_renders_nothing() -> None:
    assert Registry().render() == ""


# --- Parity: mitm-proxy's /metrics, rebuilt on interpose, renders the same bytes ----------------

NETWORK_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


def test_mitm_proxy_shaped_metrics_render_as_before() -> None:
    """The lines mitm-proxy's own tests assert, from metrics declared the interpose way."""
    reg = Registry()
    build_info = reg.register(
        Gauge(
            "mitm_proxy_build_info", "Always 1; the version label.", (Label("version", ["1.2.3"]),)
        )
    )
    build_info.set(1, "1.2.3")
    flows = reg.register(
        Counter(
            "mitm_proxy_flows_total",
            "Flows handled, by type: http (intercepted HTTPS), plain_http, websocket.",
            (Label("type", ["http", "plain_http", "websocket"]),),
        )
    )
    expiry = reg.register(
        Gauge(
            "mitm_proxy_certificate_expiry_timestamp_seconds",
            "When a certificate expires (Unix time), by kind.",
            (Label("kind", ["ca"]),),
        )
    )
    ttfb = reg.register(
        Histogram(
            "mitm_proxy_origin_ttfb_seconds",
            "The origin's share: from the request being sent to its response headers.",
            NETWORK_BUCKETS,
        )
    )
    flows.inc("http")
    flows.inc("http")
    expiry.set(1.5, "ca")
    ttfb.observe(0.02)
    text = reg.render()
    assert "# TYPE mitm_proxy_flows_total counter" in text
    assert 'mitm_proxy_flows_total{type="http"} 2' in text
    assert 'mitm_proxy_build_info{version="1.2.3"} 1' in text
    assert 'mitm_proxy_certificate_expiry_timestamp_seconds{kind="ca"} 1.5' in text
    assert 'mitm_proxy_origin_ttfb_seconds_bucket{le="0.01"} 0' in text
    assert 'mitm_proxy_origin_ttfb_seconds_bucket{le="0.025"} 1' in text
    assert 'mitm_proxy_origin_ttfb_seconds_bucket{le="60.0"} 1' in text
    assert (
        "mitm_proxy_origin_ttfb_seconds_sum 0.02\nmitm_proxy_origin_ttfb_seconds_count 1\n" in text
    )
    assert text.endswith("\n")
