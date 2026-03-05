# etcd Thresholds for Crossplane Capacity Planning

How we arrived at the alert thresholds used in this project, what they mean, and general guidance for setting your own.

---

## Lab Setup

All thresholds in this document were derived from load testing on the following environment:

| Parameter | Value |
|-----------|-------|
| Platform | Self-managed OpenShift (crossplane1) |
| Kubernetes | 1.33.6 |
| Workers | 3 workers + 3 dedicated masters |
| Crossplane | Helm chart (latest at time of test) |
| Provider | provider-nop v0.5.0 — creates real etcd objects without provisioning cloud resources |
| Functions | function-patch-and-transform v0.10.1, function-auto-ready v0.6.1 |
| Load generator | kube-burner |
| Test method | 9-job continuous ramp from 0 to ~116,000 etcd objects |
| Data points | 67 samples across 18,047 to 115,856 objects |
| Object mix | 4 composition types (VMDeployment ×8, Disk ×4, DNSZone ×6, FirewallRuleSet ×7) |

The control plane runs on dedicated master nodes with direct access to etcd configuration, etcd node specs, and direct etcd metrics (`etcd_mvcc_*`). Thresholds are based on both direct etcd metrics and API server proxy metrics.

### What provider-nop Means for These Results

provider-nop creates real Kubernetes objects in etcd (NopResource CRs) but doesn't call any cloud APIs. This means:

- **etcd pressure is real** — every object is stored, watched, and reconciled
- **Reconciliation is fast** — no cloud API latency, so Crossplane reconciles at maximum speed, producing more etcd writes per second than a real provider would
- **No external failure modes** — no cloud rate limits, transient errors, or slow API responses that would normally introduce backpressure

The net effect is that our latency measurements represent a **worst-case write rate** for a given object count. Real providers (AWS, Azure, GCP) will reconcile more slowly due to cloud API latency, producing lower etcd write pressure at the same object count. Our thresholds are therefore conservative — if your cluster stays within these limits under provider-nop load, it will perform at least as well with real providers.

---

## How We Calculated the Thresholds

### Step 1: Fit Models to Observed Data

We tested 7 model families (linear, quadratic, power-law, log-linear, piecewise linear, saturating exponential, square root) against each metric using `scipy.optimize.curve_fit` with 80/20 holdout validation. Power-law (`y = a × x^b`) was selected for all production thresholds because it extrapolates safely and encodes trivially in PromQL.

The fitted models:

| Metric | Formula | R² | Confidence |
|--------|---------|-----|-----------|
| Controller Memory (bytes) | `2.74e7 × x^0.476` | 0.94 | High |
| Controller CPU (cores) | `3.29e-3 × x^0.595` | 0.94 | High |
| etcd P99 Latency (seconds) | `6.57e-1 × x^-0.201` | 0.59 | Low |
| API P99 Latency (seconds) | `1.03e0 × x^-0.005` | 0.48 | Low |

Where `x` is the total etcd object count.

### Step 2: Determine Where Things Break

Each threshold corresponds to a real operational impact, not an arbitrary number:

**Memory thresholds:**
- **2 GiB warning** — Crossplane's default memory limit is often set around 2–4 GiB. Crossing 2 GiB means you're consuming a meaningful fraction of the pod's limit and should review resource settings.
- **5 GiB critical** — At 106k objects, 4.35 GiB steady-state was observed with GC spikes exceeding 7 GiB. At this point the controller is at risk of OOMKill. Reconciliation may slow as the Go runtime spends more time in garbage collection.
- **6 GiB hard limit** — OOMKill is imminent on most default configurations. The controller will restart, causing a reconciliation storm as it rebuilds its cache.

**etcd P99 latency thresholds:**
- **100ms warning** — etcd's own documentation considers <100ms to be healthy. Above this, read and write operations start to noticeably slow down. Crossplane's list/watch operations take longer, and reconciliation throughput drops.
- **500ms critical** — At half a second per etcd operation, the API server queues requests, informers lag behind reality, and Crossplane's eventual consistency model starts showing visible delays. Claims take noticeably longer to become ready.
- **1s hard limit** — etcd leader elections can be disrupted by sustained latency at this level. The cluster risks instability.

**API server P99 latency thresholds:**
- **1s warning** — Kubectl commands feel sluggish. Crossplane's informers experience delays receiving events. Webhook timeouts (default 10s) are not yet at risk, but batch operations (creating many claims at once) start queuing.
- **2s critical** — The API server is under significant pressure. List operations on large resource sets time out or return partial results. Crossplane may miss events and require reconciliation retries. This is where user-visible impact begins — claims get stuck, status updates lag.
- **5s hard limit** — At this point the API server is functionally degraded. Admission webhooks and controllers time out. New resource creation fails intermittently. The cluster needs immediate intervention.

**Object count thresholds:**
- **50,000 warning** — Serves as a leading indicator — object count is observable before memory pressure becomes critical. At this count the memory model predicts ~3.8 GiB, approaching the warning threshold.
- **100,000 critical** — Approaching the memory-derived ceiling of ~66,000 objects. At this point, the cluster is in a degraded state regardless of other metrics.

### Step 3: Validate via Reverse Capacity

We used the reverse capacity formula to verify that the thresholds form a coherent picture. The formula solves `threshold = a × x^b` for `x`:

```
max_objects = (threshold / a) ^ (1 / b)
```

| Dimension | Threshold | Max Objects Before Breach |
|-----------|-----------|--------------------------|
| **Memory (5 GiB critical)** | **5 GiB** | **~66,048** |
| CPU (3 cores critical) | 3 cores | very high |
| etcd P99 (500ms critical) | 500ms | very high (latencies never approached threshold) |
| API P99 (2s critical) | 2s | very high (latencies never approached threshold) |

This confirms that memory is the binding constraint — it trips at ~66,048 objects. Latency metrics (both etcd P99 and API P99) have negative exponents in the power-law fit, meaning they decrease or remain flat as object count grows, and never approached their critical thresholds during testing. CPU headroom is also very high.

### Step 4: Set Alert Timing

Each threshold has a `for` duration — how long the condition must persist before firing. This prevents transient spikes (e.g., during batch claim creation) from triggering false alarms:

| Severity | `for` Duration | Rationale |
|----------|---------------|-----------|
| Critical | 5 minutes | Long enough to filter burst noise, short enough for real incidents |
| Warning | 10 minutes | Gives transient operations (batch creates, reconciliation storms) time to settle |
| Info / forecast | 30 minutes – 1 hour | Forecasts are inherently noisy; longer windows reduce false positives |

---

## Summary of Thresholds

| Metric | Warning | Critical | Hard Limit |
|--------|---------|----------|------------|
| Controller Memory | 2 GiB | 5 GiB | 6 GiB (OOMKill) |
| Controller CPU | 1.5 cores | 3.0 cores | 4.0 cores (throttle) |
| etcd P99 Latency | 100ms | 500ms | 1,000ms |
| API P99 Latency | 1s | 2s | 5s |
| etcd Object Count | 50,000 | 100,000 | — |
| Object Growth Rate | 24,000/day | — | — |

---

## General Guidance for Setting Your Own Thresholds

The thresholds above are specific to our lab setup. Your environment will have different hardware, a different managed Kubernetes provider (or self-managed), different compositions, and different Crossplane versions. Here's how to approach setting your own.

### Start With Industry Defaults

If you haven't run your own load test yet, these are reasonable starting points based on etcd and Kubernetes community guidance:

| Metric | Conservative Warning | Conservative Critical | Source |
|--------|---------------------|----------------------|--------|
| etcd P99 latency | 100ms | 500ms | etcd documentation, SIG-scalability benchmarks |
| API P99 latency | 1s | 5s | Kubernetes SIG-scalability SLOs (the SLO for mutating API calls is 1s P99) |
| Controller memory | 50% of pod limit | 80% of pod limit | General container best practice |
| etcd DB size | 4 GiB | 6 GiB | etcd default quota is 2 GiB; max recommended is 8 GiB |

These are deliberately conservative. Tighten them after you have data from your own environment.

### Adjust for Your Platform

**Managed Kubernetes (ROSA, EKS, GKE, AKS):**
- You can't see direct etcd metrics. Use `apiserver_request_duration_seconds` and `etcd_request_duration_seconds` as proxies.
- The provider controls etcd configuration. Your latency ceiling depends on their hardware and tuning — you can't change it.
- Start with our thresholds and adjust based on observed behavior. If your API P99 is consistently under 200ms at 20,000 objects, your provider may have a higher ceiling than ours.

**Self-managed Kubernetes:**
- You have direct etcd metrics. Add thresholds for `etcd_mvcc_db_total_size_in_bytes` (warn at 50% of quota, critical at 80%), `etcd_disk_wal_fsync_duration_seconds` (warn at 10ms P99, critical at 50ms), and `etcd_server_leader_changes_seen_total` (any non-zero rate is worth investigating).
- With NVMe storage and tuned compaction, your latency thresholds can be tighter (lower warning, lower critical) because the baseline is lower.
- With network-attached storage, your latency thresholds may need to be wider to avoid false positives from I/O variance.

**Hosted control planes (HCP):**
- Same constraints as managed Kubernetes — no direct etcd access.
- HCP providers may expose additional control plane metrics. Check your provider's documentation.

### Adjust for Your Workload

**High object multiplier (>6 per claim):**
- Lower the object count warning threshold. If your multiplier is 10, then 3,000 claims = 30,000 objects. A warning at 20,000 objects gives you more lead time.

**Low object multiplier (2–3 per claim):**
- You can afford a higher object count warning. 30,000 objects is only 10,000–15,000 claims — you may want more headroom before alerting.

**Bursty workloads (batch claim creation):**
- Widen the `for` duration on warning alerts (15–20 minutes instead of 10) to avoid false positives during planned batch operations.
- Keep critical alert `for` durations short (5 minutes) — if a burst causes sustained degradation, you need to know.

**Steady-state workloads (slow organic growth):**
- Forecast alerts become more valuable. Set `crossplane:days_until_object_limit` thresholds at 14 and 30 days.
- Symptom alerts can have shorter `for` durations since transient spikes are rare.

### The 80% Rule

Set your warning threshold at roughly 80% of the critical threshold, and your critical threshold at 80% of the hard limit. This gives you two stages of response:

```
Normal → Warning (investigate) → Critical (act now) → Hard limit (service impact)
```

For example, if your controller memory pod limit is 8 GiB:
- Warning: 8 × 0.5 = 4 GiB (50% — gives time to investigate)
- Critical: 8 × 0.8 = 6.4 GiB (80% — act before OOMKill)
- Hard limit: 8 GiB (OOMKill)

### Refit When Things Change

Thresholds derived from load tests are only valid for the environment they were tested on. Refit when:

- **Crossplane version changes** — reconciliation behavior, cache implementation, and memory footprint change between versions
- **Composition changes** — more managed resources per claim means more objects, more watches, more reconciliation
- **Infrastructure changes** — new node types, different storage, different managed Kubernetes tier
- **Provider changes** — switching from provider-nop to a real provider (AWS, Azure, GCP) changes reconciliation patterns and write rates
- **etcd configuration changes** — compaction interval, quota size, snapshot frequency (self-managed only)

A simple smoke test (create 1,000 claims, observe metrics, compare to predictions) is enough to spot gross drift. A full refit requires a ramp test across the range you care about.

---

## Further Reading

- [Crossplane etcd Scaling Guide](crossplane-etcd-scaling-guide.md) — Full capacity models, node sizing, and scaling strategies
- [Capacity Contract](../monitoring/capacity-contract.md) — Alert definitions, routing, silencing, owners, and response procedures
- [etcd documentation: Hardware recommendations](https://etcd.io/docs/v3.5/op-guide/hardware/)
- [Kubernetes SIG-scalability SLOs](https://github.com/kubernetes/community/blob/master/sig-scalability/slos/slos.md)
