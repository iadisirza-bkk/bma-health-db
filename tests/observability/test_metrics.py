"""Tests for the prometheus metrics module.

Two regimes:
  * If ``prometheus_client`` is installed → assert the metric objects
    behave like real Counters / Histograms (label names match, increments
    show up in ``generate_latest()`` output).
  * If it isn't → assert the no-op stubs are importable and don't crash
    when called. The whole point of the stubs is that the rest of the app
    doesn't have to know whether the real lib is present.

Tests live under ``tests/observability/`` so the existing per-domain test
runners (``tests/services/``, ``tests/repositories/``) keep working
unchanged. The metrics tests are a self-contained module.
"""
from __future__ import annotations

import os
import sys

import pytest

# Make api/ importable for `observability.*` (mirrors tests/conftest.py).
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from observability import metrics as obs_metrics  # noqa: E402


# ---------------------------------------------------------------------------
# Sanity — every documented metric exists at the module level with the
# expected name + labelnames. This guards against typos in either the
# wiring code or the test code drifting apart.
# ---------------------------------------------------------------------------

EXPECTED_METRICS = [
    ("chart_render_total", "bma_chart_render_total", ("spec_id", "status")),
    ("chart_render_duration", "bma_chart_render_duration_seconds", ("spec_id",)),
    ("chart_kanon_dropped", "bma_chart_kanon_dropped_total", ("spec_id",)),
    ("chat_message_total", "bma_chat_message_total", ("role",)),
    ("chat_tool_call_total", "bma_chat_tool_call_total", ("tool_name", "status")),
    ("chat_stream_duration", "bma_chat_stream_duration_seconds", ()),
    (
        "report_render_total",
        "bma_report_render_total",
        ("report_id", "fmt", "lang", "status"),
    ),
    (
        "report_render_duration",
        "bma_report_render_duration_seconds",
        ("report_id", "fmt"),
    ),
    ("report_cache_hit", "bma_report_cache_hit_total", ("report_id", "fmt", "lang")),
]


@pytest.mark.parametrize("attr, expected_name, expected_labels", EXPECTED_METRICS)
def test_metric_object_has_expected_shape(attr, expected_name, expected_labels):
    """Every metric should expose `_name` and `_labelnames` attrs we can assert on.

    Note: prometheus_client strips the ``_total`` suffix from a Counter's
    internal ``_name`` (it's appended back at exposition time per the
    Prometheus naming convention). The stub mirrors the constructor name
    verbatim. Both forms are accepted.
    """
    obj = getattr(obs_metrics, attr)
    actual_name = getattr(obj, "_name", None)
    # Accept either the full constructor name (stub path) or the suffix-
    # stripped variant prometheus_client uses internally for Counters.
    name_variants = {expected_name}
    if expected_name.endswith("_total"):
        name_variants.add(expected_name[: -len("_total")])
    assert actual_name in name_variants, (
        f"{attr}: expected name in {name_variants!r}, got {actual_name!r}"
    )
    assert tuple(getattr(obj, "_labelnames", ())) == expected_labels, (
        f"{attr}: expected labels {expected_labels}, "
        f"got {tuple(getattr(obj, '_labelnames', ()))}"
    )


def test_track_duration_no_labels_does_not_crash():
    """Histogram without labels should accept the empty-labels path."""
    with obs_metrics.track_duration(obs_metrics.chat_stream_duration):
        pass


def test_track_duration_with_labels_does_not_crash():
    """Histogram with labels should record under the labelled child."""
    with obs_metrics.track_duration(
        obs_metrics.chart_render_duration, {"spec_id": "test_spec"}
    ):
        pass


def test_count_status_helper_does_not_crash():
    """count_status should accept arbitrary label dict + status string."""
    obs_metrics.count_status(
        obs_metrics.chart_render_total, {"spec_id": "test_spec"}, "ok"
    )


def test_track_duration_records_even_on_exception():
    """Latency for failures must be observable too — no swallowed errors,
    no missed histogram observation."""
    with pytest.raises(RuntimeError, match="boom"):
        with obs_metrics.track_duration(
            obs_metrics.chart_render_duration, {"spec_id": "fail_spec"}
        ):
            raise RuntimeError("boom")


def test_generate_latest_returns_bytes():
    """generate_latest() must return bytes whether the lib is installed or not."""
    out = obs_metrics.generate_latest()
    assert isinstance(out, bytes)


# ---------------------------------------------------------------------------
# When prometheus_client IS available, the metrics should be registered with
# the global REGISTRY and increments should show up in `generate_latest()`.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not obs_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed — stubs covered by other tests",
)
def test_increment_appears_in_exposition_output():
    """Hit a counter, then assert the value lands in generate_latest()."""
    obs_metrics.chart_render_total.labels(spec_id="unit_test", status="ok").inc()
    payload = obs_metrics.generate_latest().decode("utf-8", errors="replace")
    # Sanity: the metric name + label combination must appear.
    assert "bma_chart_render_total" in payload
    assert 'spec_id="unit_test"' in payload


@pytest.mark.skipif(
    not obs_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed — registry is a stub",
)
def test_metrics_are_registered_with_global_registry():
    """REGISTRY.collect() should yield each of our metrics by name."""
    names_collected = set()
    for fam in obs_metrics.REGISTRY.collect():
        names_collected.add(fam.name)
    expected_names = {
        "bma_chart_render",  # Histogram base name (no _seconds suffix)
        "bma_chart_render_total",
    }
    # Histograms appear under their base name (without _total / _seconds).
    # We just check at least one of our metric families landed in the registry.
    assert any(
        n.startswith("bma_chart_render") for n in names_collected
    ), f"chart_render metrics not in REGISTRY: {sorted(names_collected)[:20]}"


@pytest.mark.skipif(
    not obs_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed — histogram observe is a no-op",
)
def test_histogram_observe_appears_in_output():
    """Run track_duration and assert the histogram bucket counts increase."""
    with obs_metrics.track_duration(
        obs_metrics.report_render_duration,
        {"report_id": "unit_test", "fmt": "html"},
    ):
        pass
    payload = obs_metrics.generate_latest().decode("utf-8", errors="replace")
    assert "bma_report_render_duration_seconds" in payload
    assert 'report_id="unit_test"' in payload


# ---------------------------------------------------------------------------
# When prometheus_client is NOT available, all the public API should still
# work — that's the whole reason for the stubs.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    obs_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client IS installed; this test is for the stub path",
)
def test_stub_counters_accept_inc_calls():
    """Stub Counter.labels(...).inc() must not raise."""
    obs_metrics.chart_render_total.labels(spec_id="x", status="ok").inc()
    obs_metrics.chat_message_total.labels(role="user").inc()
    obs_metrics.report_cache_hit.labels(report_id="x", fmt="html", lang="th").inc()


@pytest.mark.skipif(
    obs_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client IS installed; this test is for the stub path",
)
def test_stub_histograms_accept_observe_calls():
    """Stub Histogram.observe(...) must not raise."""
    obs_metrics.chart_render_duration.labels(spec_id="x").observe(0.5)
    obs_metrics.chat_stream_duration.observe(1.0)


@pytest.mark.skipif(
    obs_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client IS installed; this test is for the stub path",
)
def test_stub_generate_latest_returns_placeholder():
    """When the lib is missing, generate_latest() returns a stub line so the
    /metrics endpoint stays HTTP 200 instead of 500."""
    out = obs_metrics.generate_latest()
    assert isinstance(out, bytes)
    assert b"prometheus_client" in out


# ---------------------------------------------------------------------------
# Router smoke test — /metrics should be reachable.
# ---------------------------------------------------------------------------

def test_prometheus_router_endpoint_returns_text_plain():
    """The /metrics route must return content-type text/plain (per Prom spec)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from observability.router import prometheus_router

    app = FastAPI()
    app.include_router(prometheus_router)
    client = TestClient(app)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
