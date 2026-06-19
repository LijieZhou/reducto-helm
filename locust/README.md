# Locust Load Test — OTEL Collector

Targets the OpenTelemetry Collector HTTP endpoint at port 4318 inside the cluster.

## How to run

```bash
# Port-forward the Locust UI (deployed as a k8s pod via locust-deployment.yaml)
kubectl port-forward svc/locust 8089:8089

# Or run locally against a port-forwarded collector
kubectl port-forward svc/otel-collector-opentelemetry-collector -n monitoring 4318:4318
locust -f locustfile.py --host http://localhost:4318
```

The `StageShape` class drives the load automatically — no manual user count needed.

---

## Traffic Profiles

Six `HttpUser` classes model different real-world sender patterns. Each is weighted so the Locust user pool reflects a realistic mix.

| Class | Weight | Payload | Wait time | Models |
|---|---|---|---|---|
| `SmallMetricUser` | 3 | 1 metric (~300 B) | 0.5–1.5s | Thinly instrumented service, one counter at a time |
| `BatchMetricUser` | 2 | 50–150 metrics (~15–45 KB) | 1–3s | SDK batching before export |
| `AttributeHeavyUser` | 1 | 1 metric + 20 high-cardinality attrs (~2 KB) | 1–2s | Services that tag every metric with user_id, request_id, region |
| `TraceUser` | 2 | 10 spans per request | 0.5–2s | Distributed tracing alongside metrics |
| `BurstyUser` | 1 | 20 rapid requests then 5–10s pause | 5–10s | Batch jobs that flush all at once |
| `HighThroughputUser` | 2 | 1 metric, constant 2 req/s per user | constant | Sustained predictable RPS to find the throughput ceiling |

---

## Load Stages

The `StageShape` ramps users through 7 stages (~17 minutes total):

| Stage | Duration | Users | Spawn rate | Purpose |
|---|---|---|---|---|
| 1. Warm-up | 60s | 10 | 2/s | Establish baseline latency |
| 2. Sustained low | 180s | 30 | 5/s | Steady-state behaviour |
| 3. Ramp medium | 180s | 80 | 10/s | Watch queue depth grow |
| 4. Burst peak | 120s | 200 | 50/s | Sudden spike — find first failures |
| 5. Recovery | 120s | 30 | 10/s | Confirm collector recovers after spike |
| 6. Ramp to ceiling | 300s | 400 | 20/s | Find hard throughput limit |
| 7. Cool-down | 60s | 10 | 10/s | Confirm graceful recovery |

---

## Test Results

**Maximum load tested:** 400 concurrent users (~400 RPS peak)

**Failure threshold:** Failures began at stage 4 (~200 users / ~90 RPS sustained). At stage 6 (400 users) the failure rate reached **83%**.

**Degradation point:** The collector cliff-failed at approximately **90 RPS sustained**. Below this, error rate was near 0. Above it, the collector's HTTP receiver queue saturated and began rejecting requests with 5xx errors.

**Primary bottleneck:** OTEL Collector HTTP receiver concurrency limit. The collector's single-deployment pod (0.5 CPU / 512 Mi limit) could not drain the incoming request queue faster than it was being filled. Infrastructure (nodes, pods) was not the bottleneck — CPU and memory on the nodes remained comfortable throughout.

**Evidence from Grafana:**
- `otelcol_receiver_refused_metric_points` spiked to match the incoming rate at ~90 RPS
- `otelcol_process_memory_rss` stayed flat (no OOM) — memory was not the constraint
- `rate(otelcol_receiver_accepted_metric_points) - rate(otelcol_exporter_sent_metric_points)` (pipeline backpressure panel) showed a growing lag starting at stage 3 and going negative at stage 4 (exports falling behind ingestion)
- Pod CPU hit the 500m limit during stage 6; no HPA scaling because the OTEL collector is not in the autoscaled deployment

**What to change next:**
1. **Scale the collector horizontally** — run 2–3 replicas behind a k8s Service; the HTTP receiver is stateless so this is safe
2. **Increase CPU limit** — the 500m cap was the hard ceiling; bumping to 1–2 cores would raise the single-pod throughput significantly
3. **Enable the batch processor's `send_batch_max_size`** — currently unbounded, which means a large batch blocks the pipeline while it flushes; capping it keeps latency predictable under load
4. **Add an HPA on the OTEL collector** on `otelcol_receiver_accepted_metric_points` rate, so the collector scales automatically under burst
