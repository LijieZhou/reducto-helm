"""
Reducto OTEL Collector Load Test
=================================
Explores how the collector behaves under:
  - varying concurrency and spawn rate
  - small vs large payloads (single metric vs batched)
  - simple vs attribute-heavy payloads
  - trace spans alongside metrics
  - sustained load vs burst patterns

The StageShape below drives users through a sequence of stages that progressively
stress the system to find the point of degradation.

Stages (each stage = { duration_seconds, users, spawn_rate }):
  1.  Warm-up          —  10 users,  ramp  2/s   — establish baseline latency
  2.  Sustained low    —  30 users,  ramp  5/s   — steady-state behaviour
  3.  Ramp medium      —  80 users,  ramp 10/s   — watch queue depth grow
  4.  Burst peak       — 200 users,  ramp 50/s   — sudden spike, find first failure
  5.  Recovery         —  30 users,  ramp 10/s   — does the collector recover?
  6.  Ramp to ceiling  — 400 users,  ramp 20/s   — find hard limits
  7.  Cool-down        —  10 users,  ramp 10/s   — confirm graceful recovery
"""

import random
import string
import time
from locust import HttpUser, task, between, constant_throughput
from locust import LoadTestShape


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    return str(int(time.time() * 1e9))


def _attr(key: str, val: str) -> dict:
    return {"key": key, "value": {"stringValue": val}}


def _random_string(n: int) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _make_metric(name: str, value: float, extra_attrs: list[dict] | None = None) -> dict:
    attrs = [
        _attr("service.name", "reducto-load-test"),
        _attr("host.name", _random_string(8)),
        _attr("env", "load-test"),
    ]
    if extra_attrs:
        attrs.extend(extra_attrs)
    return {
        "name": name,
        "gauge": {
            "dataPoints": [{
                "attributes": attrs,
                "timeUnixNano": _ts(),
                "asDouble": value,
            }]
        }
    }


def _make_span(trace_id: str, span_id: str, name: str, duration_ms: int) -> dict:
    start = int(time.time() * 1e9)
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + duration_ms * 1_000_000),
        "attributes": [
            _attr("http.method", "POST"),
            _attr("http.url", "/api/infer"),
            _attr("http.status_code", "200"),
        ],
        "status": {"code": 1}
    }


def _resource_metrics(metrics: list[dict]) -> dict:
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [_attr("service.name", "reducto-load-test")]},
            "scopeMetrics": [{"metrics": metrics}]
        }]
    }


def _resource_spans(spans: list[dict]) -> dict:
    return {
        "resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", "reducto-load-test")]},
            "scopeSpans": [{"spans": spans}]
        }]
    }


JSON_HEADERS = {"Content-Type": "application/json"}


def _post(user: HttpUser, path: str, payload: dict, name: str) -> None:
    with user.client.post(path, json=payload, headers=JSON_HEADERS,
                          name=name, catch_response=True) as r:
        if r.status_code in (200, 204):
            r.success()
        else:
            r.failure(f"HTTP {r.status_code}: {r.text[:120]}")


# ---------------------------------------------------------------------------
# User classes — each models a different request characteristic
# ---------------------------------------------------------------------------

class SmallMetricUser(HttpUser):
    """
    Single metric per request — minimal payload (~300 bytes).
    Models a thinly instrumented service emitting one counter at a time.
    High concurrency of this class reveals per-request overhead.
    """
    wait_time = between(0.5, 1.5)
    weight = 3

    @task
    def single_metric(self):
        payload = _resource_metrics([
            _make_metric("reducto.request.count", 1.0)
        ])
        _post(self, "/v1/metrics", payload, "metric:single")


class BatchMetricUser(HttpUser):
    """
    50–150 metrics per request (~15–45 KB).
    Models an SDK batching metrics before export.
    Stresses the collector's ingestion throughput and memory under large payloads.
    """
    wait_time = between(1, 3)
    weight = 2

    @task
    def batch_metrics(self):
        count = random.randint(50, 150)
        metrics = [
            _make_metric(
                f"reducto.batch.metric_{i}",
                random.uniform(0, 1000),
            )
            for i in range(count)
        ]
        payload = _resource_metrics(metrics)
        _post(self, "/v1/metrics", payload, f"metric:batch({count})")


class AttributeHeavyUser(HttpUser):
    """
    Single metric but with 20 extra high-cardinality attributes (~2 KB).
    Models services that label every metric with user_id, request_id, region, etc.
    High-cardinality attributes stress the collector's label processing.
    """
    wait_time = between(1, 2)
    weight = 1

    @task
    def attribute_heavy_metric(self):
        extra = [_attr(f"dim_{i}", _random_string(12)) for i in range(20)]
        payload = _resource_metrics([
            _make_metric("reducto.high_cardinality.value",
                         random.uniform(0, 100), extra_attrs=extra)
        ])
        _post(self, "/v1/metrics", payload, "metric:high_cardinality")


class TraceUser(HttpUser):
    """
    Sends a batch of 10 spans per request — models distributed tracing.
    Exercises the traces pipeline alongside the metrics pipeline.
    """
    wait_time = between(0.5, 2)
    weight = 2

    @task
    def send_spans(self):
        trace_id = _random_string(32)
        spans = [
            _make_span(
                trace_id=trace_id,
                span_id=_random_string(16),
                name=f"operation_{i}",
                duration_ms=random.randint(5, 500),
            )
            for i in range(10)
        ]
        payload = _resource_spans(spans)
        _post(self, "/v1/traces", payload, "trace:10spans")


class BurstyUser(HttpUser):
    """
    Sends 20 rapid-fire requests then pauses 5–10 seconds.
    Models bursty producers — e.g. a batch job that completes and flushes all at once.
    Reveals how the collector's queue absorbs traffic spikes.
    """
    wait_time = between(5, 10)
    weight = 1

    @task
    def burst(self):
        for _ in range(20):
            payload = _resource_metrics([
                _make_metric("reducto.burst.metric", random.uniform(0, 500))
            ])
            _post(self, "/v1/metrics", payload, "metric:burst")
            time.sleep(0.05)  # 50ms between burst requests → ~400 req/s peak


class HighThroughputUser(HttpUser):
    """
    Constant 2 req/s per user regardless of response time.
    At high concurrency this generates sustained, predictable RPS.
    Use to find the RPS ceiling before latency degrades.
    """
    wait_time = constant_throughput(2)
    weight = 2

    @task
    def constant_rate(self):
        payload = _resource_metrics([
            _make_metric("reducto.throughput.probe", 1.0)
        ])
        _post(self, "/v1/metrics", payload, "metric:constant_rate")


# ---------------------------------------------------------------------------
# Staged load shape
# ---------------------------------------------------------------------------

class StageShape(LoadTestShape):
    """
    Drives users through progressively heavier stages.

    Reading the Grafana dashboard alongside this:
    - Stage 1–2: baseline — all panels green, queue near 0
    - Stage 3:   watch queue depth on "OTEL Exporter Queue Size" panel
    - Stage 4:   burst — first errors should appear here if collector saturates
    - Stage 5:   recovery check — errors should drop back to 0
    - Stage 6:   ramp to ceiling — find where pod CPU hits 100% or OOM kills
    - Stage 7:   confirm recovery after spike
    """

    stages = [
        #  duration, users, spawn_rate
        {"duration":  60, "users":  10, "spawn_rate":  2},   # 1. warm-up
        {"duration": 180, "users":  30, "spawn_rate":  5},   # 2. sustained low
        {"duration": 180, "users":  80, "spawn_rate": 10},   # 3. ramp medium
        {"duration": 120, "users": 200, "spawn_rate": 50},   # 4. burst peak
        {"duration": 120, "users":  30, "spawn_rate": 10},   # 5. recovery
        {"duration": 300, "users": 400, "spawn_rate": 20},   # 6. ramp to ceiling
        {"duration":  60, "users":  10, "spawn_rate": 10},   # 7. cool-down
    ]

    def tick(self):
        elapsed = self.get_run_time()
        cumulative = 0
        for stage in self.stages:
            cumulative += stage["duration"]
            if elapsed < cumulative:
                return stage["users"], stage["spawn_rate"]
        return None  # test complete
