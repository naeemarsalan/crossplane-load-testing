# etcd Capacity Planning Guide for Crossplane on Kubernetes

This guide covers how to plan, measure, and operate etcd capacity for Kubernetes clusters running Crossplane at scale. It is vendor-neutral where possible, with platform-specific caveats noted inline. The advice here applies whether you run self-managed Kubernetes, OpenShift, EKS, AKS, GKE, or ROSA — the physics of etcd are the same everywhere.

---

## 1. The Crossplane Object Multiplication Problem

Kubernetes stores all cluster state in etcd. Every object — Pods, ConfigMaps, Secrets, custom resources — is an etcd key-value pair. Crossplane changes the arithmetic because a single user-facing *claim* fans out into multiple Kubernetes objects.

### How Multiplication Works

When you create a Crossplane claim, the composition pipeline produces a composite resource (XR), which in turn creates one or more managed resources. The total etcd footprint is always larger than what the user sees:

```
Claim (1) → Composite Resource / XR (1) → Managed Resources (N)
```

**1 claim = N + 2 etcd objects**, where N is the number of managed resources defined in your composition.

This multiplication is the core reason capacity planning matters for Crossplane — you exhaust headroom faster than you'd expect from raw claim counts alone.

### Measuring Your Own Multiplier

Before you can plan capacity, you need to know your multiplier. Deploy a single claim for each of your composition types and count the resulting objects:

```bash
# Count all objects created by a single claim
kubectl get managed -l crossplane.io/claim-name=my-test-claim | wc -l
# Add 2 (the XR + claim themselves) to get the total etcd objects per claim
```

Do this for every composition in your environment. Different compositions will have different multipliers.

### Scaling Arithmetic

Once you know the multiplier, the math is straightforward. Here's a reference table for common multipliers:

| Managed Resources per Claim | Multiplier (N+2) | 1,000 Claims | 5,000 Claims | 10,000 Claims |
|----------------------------|-------------------|--------------|--------------|---------------|
| 3 | 5 | 5,000 | 25,000 | 50,000 |
| 6 | 8 | 8,000 | 40,000 | 80,000 |
| 8 | 10 | 10,000 | 50,000 | 100,000 |
| 12 | 14 | 14,000 | 70,000 | 140,000 |

If your environment runs multiple composition types, compute the weighted sum across all claim types to get the total expected etcd object count. This number is the foundation — everything else in this guide derives from it.

---

## 2. Estimating etcd Cluster Size

With a projected object count in hand, you can estimate the infrastructure required to support it. The key dimensions are storage, memory, CPU, and node count.

### etcd Database Size

Each Kubernetes object occupies roughly 2–8 KB in etcd, depending on the CRD schema complexity, the number of status fields, annotations, and labels. Simple custom resources sit at the low end; resources with large status blocks, multiple conditions, and finalizer lists trend toward the high end.

A reasonable planning estimate:

```
estimated_db_size = total_object_count × avg_object_size_bytes
```

For most Crossplane managed resources, 4–6 KB per object is a reasonable starting point. At 100,000 objects with an average of 5 KB each, expect roughly 500 MB of active etcd data — well within a single etcd cluster's capacity, but large enough that compaction, defragmentation, and quota management become operational concerns.

### Node Count

etcd uses the Raft consensus protocol, which requires a majority quorum for writes. The standard configurations are:

- **3 nodes** — tolerates 1 failure. Suitable for most production deployments up to ~200k objects.
- **5 nodes** — tolerates 2 failures. Recommended for large-scale deployments, multi-AZ high availability, or environments where the cost of a quorum loss is severe.
- **Never use even numbers** — a 4-node cluster tolerates the same 1 failure as a 3-node cluster but adds write latency from the extra Raft round-trip.

### Storage

etcd is extremely sensitive to disk I/O latency, particularly for WAL (write-ahead log) fsync operations. SSDs are required; NVMe is strongly preferred for production workloads.

Size the disk to accommodate:

- 2× the expected peak database size (headroom for compaction lag)
- WAL files (typically 60–80 MB of active WAL segments)
- Snapshot files (size roughly equals the DB size at snapshot time)
- Minimum: 50 GB per node for small deployments
- Recommended: 100 GB per node for production

Dedicated disks for etcd — separate from the OS and container runtime — eliminate I/O contention that manifests as WAL fsync latency spikes.

### Memory

etcd caches its entire dataset in memory via BoltDB's mmap. Controller processes (Crossplane, provider controllers, the API server itself) also hold in-memory caches of watched objects through Kubernetes informers. Both must fit within the node's available memory.

General sizing guidance per control plane node:

| Object Count Range | etcd Memory | API Server + Controllers | Total Recommended |
|-------------------|-------------|--------------------------|-------------------|
| < 25,000 | 2–3 GB | 2–4 GB | 8 GB |
| 25,000 – 75,000 | 3–5 GB | 4–8 GB | 16 GB |
| 75,000 – 150,000 | 5–8 GB | 8–12 GB | 32 GB |
| > 150,000 | 8+ GB | 12+ GB | 64 GB |

These are rough guidelines. Your actual requirements depend on object size, watch density, and controller count. Load testing (Section 7) gives you real numbers.

### CPU

etcd is CPU-bound during compaction, snapshot creation, and under high write throughput. The API server and controllers add their own CPU pressure from watch processing and reconciliation loops.

- **Minimum**: 2 dedicated cores per etcd member
- **Recommended**: 4 dedicated cores for clusters above 50,000 objects
- **API server and controllers**: 4–8 cores depending on provider count and reconciliation frequency

### Control Plane Node Sizing Reference

| Profile | Object Count | vCPUs | Memory | Storage | Notes |
|---------|-------------|-------|--------|---------|-------|
| **Small** | < 25k | 4 | 16 GB | 50 GB SSD | Dev/test, single composition type |
| **Medium** | 25k–75k | 8 | 32 GB | 100 GB SSD | Production, moderate claim volume |
| **Large** | 75k–150k | 16 | 64 GB | 200 GB NVMe | High-scale, multiple providers |
| **XL** | > 150k | 16+ | 64+ GB | 200+ GB NVMe | Evaluate sharding (Section 8) |

---

## 3. Why Memory Fails Before Latency

A common expectation is that etcd latency will be the first thing to degrade as object count grows. In practice, you are more likely to hit memory exhaustion on controller pods or control plane nodes before etcd latency becomes the bottleneck. Understanding why changes how you plan.

### The Informer Cache Problem

Kubernetes controllers — including Crossplane's core controller, every installed provider, and the API server — use *informers* to maintain in-memory caches of the objects they watch. When a controller starts (or restarts), it performs a full LIST of every relevant resource type and stores the result in memory. As object count grows, this cache grows.

The relationship between object count and memory consumption is typically sublinear — a power-law curve, not a straight line. Doubling the object count does not double memory usage, but it does increase it substantially. At scale, controller memory consumption will eventually exceed the pod's memory limit or the node's available capacity.

### The OOM Cascade

When a controller pod exceeds its memory limit, the OOM killer terminates it. On restart, the controller immediately performs a full re-list of all watched resources, which spikes API server load and etcd read traffic. If multiple controllers restart simultaneously — which is common during a memory pressure event — the resulting *re-list storm* looks indistinguishable from an etcd overload incident:

1. Controller memory grows beyond limit → OOM kill
2. Controller restarts → full LIST of all watched resources
3. API server fans out the LIST to etcd → read load spike
4. Other controllers, already under memory pressure, may also OOM → cascade
5. etcd latency spikes from the read storm, not from inherent degradation

This cascade is why memory is the binding constraint, not etcd latency. By the time etcd latency is genuinely degraded from object count alone, you have usually already experienced memory-related failures.

### Sizing Implications

Set controller memory limits for projected peak object count, not current. Apply a minimum 2× headroom factor. If your load test shows controllers consuming 2 GB at 50,000 objects, set the limit to at least 4 GB — you need room for re-list spikes, garbage collection pauses, and organic growth between capacity reviews.

The binding constraint can shift by platform. On managed Kubernetes (EKS, AKS, GKE), where the control plane is abstracted and auto-scaled by the cloud provider, API server latency may surface before controller memory does. On self-managed clusters (OpenShift, kubeadm), where you control node sizing, controller memory exhaustion is the more common failure mode.

---

## 4. etcd Hard Limits, Tuning Levers, and What Actually Helps

etcd has several configurable parameters that affect capacity behavior. Understanding which ones matter — and which do not — saves you from cargo-culting tuning that changes nothing meaningful.

### The Quota Alarm

etcd enforces a storage quota. When the database size (including historical revisions) exceeds this quota, etcd triggers a **quota alarm** and the cluster becomes **read-only**. No new writes are accepted until the alarm is cleared and the database is compacted and defragmented below the quota.

The default quota is 2 GiB. The maximum recommended value is 8 GiB. Exceeding 8 GiB is technically possible but unsupported by upstream etcd and can cause performance degradation from the larger BoltDB mmap.

A quota alarm in production is an emergency — your cluster cannot schedule pods, update statuses, or process any writes. It requires immediate manual intervention (compaction, defragmentation, alarm disarm).

### Five Tuning Levers

**1. `quota-backend-bytes`** — Raises the storage ceiling. This does not improve performance; it gives you more runway before the quota alarm triggers. Set it to 8 GiB if your projected database size might exceed 2 GiB. Think of it as raising the flood wall — the water still rises at the same rate.

**2. `auto-compaction-retention`** — Controls how aggressively etcd compacts old revisions. Shorter retention (e.g., 5 minutes instead of 1 hour) keeps the database smaller by discarding historical revisions sooner. However, managed Kubernetes platforms may silently ignore this setting if they control the etcd configuration.

**3. `snapshot-count`** — The number of committed Raft entries before etcd triggers a snapshot. Lower values (e.g., 10,000 — the default on OpenShift) produce more frequent snapshots, which speeds up recovery at the cost of minor I/O overhead. Rarely needs tuning.

**4. Defragmentation** — After compaction removes old revisions, the freed space is not returned to the filesystem until you defragment. Defragmentation must be run per-member (not cluster-wide simultaneously) and briefly takes the member offline. Schedule it during maintenance windows.

**5. Storage performance (WAL fsync latency)** — This is a diagnostic signal, not a tuning knob. If WAL fsync P99 exceeds 10 ms consistently, your disk is too slow or contended. The fix is hardware: faster SSDs, dedicated disks, or NVMe. No software tuning compensates for slow storage.

### What Tuning Actually Changes

Raising `quota-backend-bytes` provides operational safety margin — you have more time to react before a quota alarm. Adjusting compaction and defrag keeps the database tidy. But none of these tuning levers change the fundamental performance characteristics of etcd under load. Memory consumption on controllers, API server latency, and etcd request latency at a given object count remain essentially unchanged regardless of etcd tuning parameters.

Invest your time in right-sizing nodes, optimizing compositions, and building monitoring — not in searching for a magic etcd configuration.

### Managed Kubernetes Caveats

On EKS, AKS, GKE, and ROSA, you typically cannot tune etcd at all. The cloud provider manages the control plane, and etcd configuration is not exposed. Your only levers for managing etcd pressure on managed platforms are:

- Reducing the total object count (fewer claims, simpler compositions)
- Sharding workloads across multiple clusters
- Choosing larger control plane tiers where available

On OpenShift (self-managed), etcd tuning is possible through the etcd operator's `unsupportedConfigOverrides`. The `quotaBackendBytes` field works reliably. Other fields like `autoCompactionRetention` may be silently ignored depending on the OpenShift version and etcd operator implementation — verify that your changes actually took effect by checking the etcd pod's environment variables or command-line arguments.

---

## 5. Building a Monitoring Strategy

You cannot plan capacity without observability. A monitoring strategy for Crossplane etcd capacity requires collecting the right metrics, processing them into actionable signals, and presenting them in a way that supports both real-time operations and long-term planning.

### Monitor from Outside the Cluster

If your monitoring stack runs entirely inside the cluster it monitors, a control plane failure takes your visibility offline at exactly the moment you need it most. Use Prometheus remote-write to push metrics to an external Prometheus instance (or any compatible TSDB) that is independent of the cluster under test. This also avoids measurement bias — the monitoring workload itself consumes etcd and API server resources.

### Minimum Metric Set

Not every Kubernetes metric matters for capacity planning. Focus on these:

**Always available (any platform):**
- `apiserver_storage_objects` — current object count by resource type. This is your primary capacity indicator.
- `container_memory_working_set_bytes` — actual memory consumption for controller pods. Filter by namespace and pod labels for Crossplane, providers, and the API server.
- `container_cpu_usage_seconds_total` — CPU consumption for the same pods.
- `apiserver_request_duration_seconds` — API server request latency histogram. Focus on P99 for LIST and WATCH verbs.

**Self-managed only (requires direct etcd metric access):**
- `etcd_request_duration_seconds` — etcd backend request latency. P99 is the key signal.
- `etcd_mvcc_db_total_size_in_bytes` — physical database size including free pages.
- `etcd_disk_wal_fsync_duration_seconds` — WAL fsync latency. P99 above 10 ms indicates storage problems.
- `etcd_server_leader_changes_seen_total` — leader elections. Frequent changes indicate network instability, resource starvation, or split-brain scenarios.

On managed platforms, etcd metrics are generally not exposed. You are limited to API server metrics and controller pod metrics — which is sufficient for capacity planning (since memory is the binding constraint), but means you cannot diagnose etcd-level issues directly.

### Recording Rules

Raw metrics need processing to become capacity signals. Define Prometheus recording rules for:

- **Growth rate**: `deriv(metric[6h])` — how fast is the metric changing? Use a 6-hour or longer window to smooth out reconciliation spikes and batch effects.
- **Days until breach**: `(threshold - current_value) / deriv(metric[6h])` — when will you hit the limit at the current growth rate? Guard against division by zero (a stable metric with zero growth is good news, not an error).
- **Capacity headroom**: `(threshold - current_value) / threshold × 100` — percentage remaining. Simple, intuitive, dashboardable.

### Dashboard Design

Build a single dashboard with four logical sections:

1. **Current state** — object count, memory usage, latency percentiles, etcd DB size. Gauges and single-stat panels.
2. **Trend** — time-series graphs showing the same metrics over 24 hours, 7 days, and 30 days.
3. **Forecast** — projected values and days-until-breach based on recording rules.
4. **Capacity status** — color-coded panels (green/yellow/red) showing whether each dimension is within safe, warning, or critical thresholds.

---

## 6. Three-Tier Alerting and Forecasting

Effective alerting for capacity planning requires distinguishing between three types of signals: symptoms that need immediate response, forecasts that need planned action, and drift that needs model maintenance.

### Tier 1: Symptom Alerts (Page)

These fire when a current metric value breaches a threshold. They require immediate investigation.

| Metric | Warning | Critical | Hard Limit |
|--------|---------|----------|------------|
| Controller memory | 2 GiB | 5 GiB | 6 GiB (OOM) |
| etcd P99 latency | 100 ms | 500 ms | 1 s |
| API P99 latency | 1 s | 2 s | 5 s |
| Total object count | 50,000 | 100,000 | Platform-dependent |

Tune these thresholds to your environment. The values above are starting points based on general Kubernetes scaling characteristics — your specific composition complexity, controller count, and node sizing may shift them. The warning threshold should give you enough lead time to take planned action; the critical threshold should trigger incident response.

### Tier 2: Forecast Alerts (Ticket)

These fire when the current growth rate projects a threshold breach within a defined lead time. They should create tickets, not pages.

Use `deriv()` over a 6-hour or longer window for the growth rate estimate. Shorter windows produce noisy forecasts dominated by batch reconciliation patterns. Define two lead times:

- **14-day forecast**: enough time for procurement, node scaling, or architecture changes
- **3-day forecast**: enough time for operational response (defrag, compaction, workload migration)

Only alert on forecasts with high confidence. If your model fit has an R² below 0.85, the forecast is speculative — log it for review but do not page on it.

Guard against division-by-zero in the days-until-breach calculation. A zero growth rate means the metric is stable, which is good — your rule should handle this gracefully (e.g., return a sentinel value like 9999 or suppress the alert).

### Tier 3: Drift Alerts (Refit)

These fire when actual metric values diverge from model predictions by more than 25–30%. Drift alerts do not indicate an operational problem — they indicate that your capacity model is stale and needs refitting.

Common causes of model drift:

- **Crossplane or provider upgrade** — changed reconciliation behavior, different memory profile
- **Composition changes** — altered object multiplier or resource complexity
- **Traffic pattern shift** — different claim creation rate or deletion patterns
- **Node type change** — different memory/CPU/storage characteristics

When a drift alert fires, re-run your load test and refit your capacity models. Do not treat stale model predictions as reliable — a model trained on a previous software version or hardware configuration will give you false confidence.

### Refit Cadence

At minimum, refit models after:
- Major Crossplane version upgrades
- Composition schema changes that alter the object multiplier
- Control plane node type changes
- Any drift alert

---

## 7. Load Testing Methodology

General-purpose Kubernetes benchmarks (e.g., Kubernetes scalability SIGs) tell you about the platform. Load testing tells you about *your* specific stack — your compositions, your providers, your CRD schemas, your node sizes. There is no substitute for running your own tests.

### Batch-Based Approach

Structure your load test as a series of batches rather than a continuous ramp:

1. Deploy a fixed number of claims (e.g., 100 or 500 per batch)
2. Wait for full reconciliation — all managed resources reach a Ready state
3. Snapshot all metrics (object count, memory, latency, DB size)
4. Repeat until a stop condition is hit

This approach gives you clean data points at known object counts. A continuous ramp makes it harder to correlate metrics because you are always measuring a system in transition.

### Define Stop Conditions Upfront

Before starting the test, define the conditions that will stop it automatically:

- **Maximum latency threshold**: etcd P99 > 1 s or API P99 > 5 s
- **Maximum memory**: controller memory > 80% of node capacity
- **etcd quota percentage**: DB size > 75% of `quota-backend-bytes`
- **Manual kill switch**: a sentinel file (e.g., `/tmp/stop-test`) that you can create to stop gracefully

Encoding stop conditions in the test harness prevents runaway tests from damaging the cluster or producing misleading data from a degraded system.

### Run Overnight

Short tests (30 minutes, 1 hour) miss important behaviors that only manifest over time:

- **Memory leaks** in controllers that accumulate over hours
- **Compaction pauses** that occur on the etcd compaction interval
- **Garbage collection sawtooth** patterns in Go-based controllers
- **Steady-state behavior** after initial reconciliation storms settle

Run your test for a minimum of 8 hours. Overnight runs (10–12 hours) are ideal — they cover multiple compaction cycles and GC intervals while using off-peak infrastructure.

### Capture Milestones

Record metric snapshots at specific object counts — for example, 25,000, 50,000, 75,000, and 100,000 objects. These milestones enable cross-run comparison. When you compare a baseline run against a tuned run, compare metrics at identical object counts, not at the end of each run (which may differ in total objects).

### Annotate Dashboards

Create Grafana (or equivalent) annotations at batch boundaries. When reviewing results, you need to correlate metric changes with specific batch deployments. Without annotations, a latency spike is ambiguous — with annotations, you can see exactly which batch caused it.

### Model Fitting

Fit power-law curves to your metric data:

```
y = a × x^b
```

Where `x` is the object count and `y` is the metric value (memory, latency, DB size). Power-law models capture the sublinear scaling behavior of most Kubernetes metrics better than linear regression.

Classify model fit quality by R²:
- **> 0.85**: Reliable for forecasting and alerting
- **0.70 – 0.85**: Useful for trend analysis, not reliable for precise forecasts
- **< 0.70**: Advisory only — the metric has too much variance for confident prediction

### A/B Comparison

Before making any configuration change (etcd tuning, node resize, Crossplane upgrade), archive your baseline data. After the change, run the same test with the same parameters. Compare at identical object-count milestones, not just peak values. This eliminates confounding variables and gives you a clear picture of what the change actually affected.

### Use Mock Resources for Isolation

Tools like `provider-nop` (Crossplane's no-op provider) let you stress the control plane — etcd, API server, controllers — without cloud API calls introducing latency variance, rate limiting, or cost. Use mock resources to establish your etcd and controller scaling baseline. Layer in real provider tests afterward to capture the additional overhead of actual cloud reconciliation.

---

## 8. Scaling Strategies — Decision Sequence

When your capacity analysis shows that you are approaching limits, resist the urge to jump to the most complex solution. Work through these strategies in order — each one is simpler and lower-risk than the next, and earlier strategies may eliminate the need for later ones.

### 1. Measure

Load test your specific stack before making any scaling decisions. General guidance (including this document) gives you a framework, but your compositions, providers, CRD schemas, and reconciliation patterns are unique. Decisions based on untested assumptions lead to either over-provisioning (waste) or under-provisioning (outages).

### 2. Tune etcd

Raise `quota-backend-bytes` to 8 GiB. Adjust compaction retention if your platform allows it. Schedule regular defragmentation. This is operational hygiene — it gives you safety margin and prevents quota alarms, but it will not change your performance ceiling. Do not expect etcd tuning to meaningfully alter latency or memory consumption.

### 3. Optimize Compositions

Reducing the number of managed resources per claim is the highest-leverage change you can make. If you halve the multiplier (e.g., from 8 objects per claim to 4), you double your effective claim capacity without any infrastructure changes.

Techniques include:
- Consolidating related resources into fewer managed resources
- Moving configuration into ConfigMaps or Secrets referenced by the managed resource rather than separate objects
- Using provider-native features that bundle multiple cloud resources in a single Kubernetes object

### 4. Vertical Scale

Larger control plane nodes — more memory, more CPU, faster storage — extend your headroom. This is straightforward but has a ceiling: etcd uses a single Raft leader for all writes, so a single node's I/O and CPU capacity bounds write throughput regardless of cluster size.

Vertical scaling buys time. Use it to bridge the gap while you implement composition optimization or sharding.

### 5. Reduce CRD Count

Every installed CRD consumes API server memory (approximately 3 MB each) and adds overhead to discovery, OpenAPI schema generation, and webhook processing. If you install a full Crossplane provider (e.g., `provider-aws` with 1,000+ CRDs) but only use a handful, you are paying the overhead for all of them.

Use **Provider Families** (available in Crossplane providers) to install only the resource types you actually need. Reducing CRD count from 1,000 to 50 can free several gigabytes of API server memory.

CRD count and instance count are orthogonal scaling axes that compound. High CRD count with high instance count is the worst-case scenario for API server and etcd pressure.

### 6. Horizontal Shard

When a single cluster cannot handle your workload regardless of optimization, split across multiple clusters. Each cluster gets its own Crossplane installation, its own etcd, and its own set of claims. This is the only strategy with no theoretical ceiling.

Sharding introduces complexity: you need a control plane for the control planes (fleet management), cross-cluster networking if resources reference each other, and a strategy for distributing claims across clusters. Do not shard until you have exhausted the simpler strategies.

### 7. Combine Strategies

Most production environments at scale end up combining multiple strategies: optimized compositions on vertically-scaled nodes with CRD reduction, and sharding when a single cluster's headroom is consumed. There is no single magic answer — the right combination depends on your growth rate, composition complexity, and operational maturity.

### Challenges You Will Face

Regardless of which strategies you pursue, several operational challenges will surface at scale:

**Object deletion is slow.** Deleting 10,000+ Crossplane claims triggers cascading deletion of managed resources, XRs, and finalizer processing. Expect bulk deletion to take hours, not minutes. Use batch parallel deletion with controlled concurrency rather than a single `kubectl delete` command.

**Auth tokens expire during long operations.** Load tests and bulk operations that run for hours will outlast most authentication token lifetimes. Script token refresh into your tooling — a test that fails at hour 6 because a token expired wastes the entire run.

**Managed platforms hide etcd metrics.** On EKS, AKS, GKE, and ROSA, you cannot see etcd latency, DB size, WAL fsync, or leader changes. You are flying partially blind. Focus your monitoring on API server latency and controller memory — these are visible on all platforms and, as discussed in Section 3, are where failures actually manifest.

**CRD count and instance count compound.** These are independent scaling axes. A cluster can handle many CRDs with few instances, or few CRDs with many instances, but struggles with both high CRD count and high instance count simultaneously. Plan for the combined pressure.

**Controller restarts cause re-list storms.** When a controller pod restarts, it performs a full LIST of all watched resources, spiking etcd read load. At 100,000+ objects, a re-list can take 30+ seconds and temporarily degrade API server responsiveness. Size your controller memory limits to prevent unnecessary OOM restarts.

**Defragmentation requires maintenance windows.** Each etcd member goes offline briefly during defragmentation. In a 3-member cluster, defragmenting all members sequentially means three brief periods of reduced fault tolerance. Schedule this during low-traffic periods and defragment one member at a time.

**Models drift after upgrades.** Capacity models trained on one version of Crossplane, a provider, or Kubernetes will not perfectly predict behavior after an upgrade. Changes in reconciliation logic, memory allocation patterns, or API server behavior invalidate previous coefficients. Refit your models after every significant upgrade — do not trust stale coefficients.

---

## Summary

etcd capacity planning for Crossplane reduces to a handful of principles:

1. **Know your multiplier.** Every composition has a different object fanout. Measure it.
2. **Memory is the binding constraint.** Size for controller and API server memory, not etcd latency.
3. **Tune etcd for safety, not performance.** Raise the quota, schedule compaction and defrag, use fast disks — but do not expect tuning to change the ceiling.
4. **Monitor from outside.** Remote-write to an independent Prometheus. Track object count, memory, and latency.
5. **Alert in three tiers.** Symptoms page, forecasts create tickets, drift triggers model refit.
6. **Load test your specific stack.** General guidance gets you started; your own data gets you accurate.
7. **Scale in order.** Optimize compositions before adding nodes. Add nodes before sharding. Shard only when necessary.

The most expensive mistake in capacity planning is skipping the measurement step and scaling based on assumptions. The second most expensive mistake is building elaborate monitoring without acting on what it tells you. Measure, model, monitor, and respond — in that order.
