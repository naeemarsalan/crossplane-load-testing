# Crossplane etcd Scaling Guide

A standalone technical reference for platform engineers and SREs operating Crossplane on Kubernetes. Covers why etcd is the binding constraint, how to monitor capacity, the predictive models behind our alerts, and what to do when you approach the limits.

**Key takeaway:** On a self-managed OpenShift cluster (crossplane1: 3 masters + 3 workers), controller memory — not API latency — is the bottleneck. The cluster tops out at ~66,048 etcd objects (~8,256 VMDeployment claims) before controller memory hits the 5 GiB critical threshold.

**Based on:** Load test of 18,047 to 115,856 etcd objects on self-managed OpenShift (crossplane1), March 2026. 67 data points collected via kube-burner with provider-nop mock resources.

**Audience:** Platform engineers, SREs, Crossplane operators.

---

## Table of Contents

1. [Why etcd Is the Bottleneck](#1-why-etcd-is-the-bottleneck)
2. [How to Monitor — Capacity Planning in Practice](#2-how-to-monitor--capacity-planning-in-practice)
3. [Models and Formulas](#3-models-and-formulas)
4. [Results and Recommendations](#4-results-and-recommendations)
5. [Scaling Crossplane Beyond the Limits](#5-scaling-crossplane-beyond-the-limits)
6. [Appendix](#6-appendix)

---

## 1. Why etcd Is the Bottleneck

### 1.1 Kubernetes and etcd

Every Kubernetes object — Pod, ConfigMap, CRD instance — is stored as a key-value entry in etcd. etcd uses a single-leader Raft consensus protocol with sequential writes, which means write throughput doesn't scale horizontally. On managed Kubernetes (EKS, GKE, AKS, ROSA), the etcd cluster is part of the managed control plane: you can't tune compaction intervals, quota sizes, or add etcd members. On self-managed clusters like crossplane1, you have full access to etcd configuration and metrics.

As object count grows, three things happen:
1. etcd write and read latency increases
2. API server request latency increases (it fronts etcd)
3. Controllers that list/watch objects consume more memory and CPU

### 1.2 The Crossplane Multiplication Problem

A single Crossplane *claim* fans out into multiple Kubernetes objects. The user creates one claim, Crossplane creates a composite resource (XR), and the XR's composition creates N managed resources. Every one of these is a real etcd key.

```
Claim (1) ──> Composite Resource / XR (1) ──> Managed Resources (6)
                                                ├── VM
                                                ├── NIC
                                                ├── Disk
                                                ├── SecurityGroup
                                                ├── PublicIP
                                                └── DNS

Total: 1 claim = 8 etcd objects
```

The multiplication factor depends on the composition:

| Resource Type | Managed Resources | + XR + Claim | Total etcd Objects |
|---------------|-------------------|--------------|-------------------|
| **VMDeployment** | 6 NopResources | +2 | **8** |
| **Disk** | 2 NopResources | +2 | **4** |
| **DNSZone** | 4 NopResources | +2 | **6** |
| **FirewallRuleSet** | 5 NopResources | +2 | **7** |

This means 1,000 VMDeployment claims create 8,000 etcd objects. At the default multiplier of 8, you hit capacity limits 8x faster than you'd expect from claim count alone.

### 1.3 How Compositions Create Objects

Each resource type has a **Composition** that defines how a claim maps to managed resources. Our compositions use **Pipeline mode** with two functions:

1. **function-patch-and-transform** — maps claim fields to managed resource specs
2. **function-auto-ready** — marks the XR as ready when all managed resources are ready

We use [provider-nop](https://github.com/crossplane-contrib/provider-nop) instead of real cloud providers. Each NopResource is a real Kubernetes object stored in etcd, but it doesn't provision any actual cloud infrastructure. This gives us real etcd pressure at zero cloud cost, with instant reconciliation and repeatable, isolated tests.

```
User creates      Crossplane         Crossplane creates          All stored
a Claim      -->  creates an XR  --> Managed Resources      -->  in etcd
                                     (NopResources)

  [1 object]       [1 object]        [6 objects for VM]          [8 total]
```

### 1.4 What Degrades

Four things grow with etcd object count:

| Dimension | What Happens | When It Becomes Critical |
|-----------|-------------|------------------------|
| **Controller memory** | Crossplane caches grow | Hits 5 GiB at ~66,048 objects |
| **API server P99 latency** | List/watch operations slow down | Hits 2s at ~29,500 objects |
| **etcd P99 latency** | Storage operations slow down | Hits 500ms at ~131,600 objects |
| **Controller CPU** | Reconciliation loops consume more cycles | Hits 3 cores at ~178,000 objects |

With the updated 5 GiB memory threshold, controller memory is the binding constraint at ~66,048 objects. On managed platforms without direct etcd access, API latency may become the practical limit earlier.

### 1.5 Self-Managed vs. Managed Kubernetes

On **self-managed clusters** (like crossplane1), you have direct access to etcd metrics such as `etcd_mvcc_db_total_size_in_bytes`, `etcd_mvcc_keys_total`, and `etcd_disk_wal_fsync_duration_seconds`. This gives full visibility into etcd health.

On **managed Kubernetes** (EKS, GKE, AKS, ROSA), you don't have access to these direct metrics — they live inside the managed control plane. Instead, use these proxy metrics:

| Proxy Metric | What It Tells You |
|-------------|-------------------|
| `etcd_request_duration_seconds` | etcd request latency from the API server side |
| `apiserver_request_duration_seconds` | End-to-end API request latency (includes etcd) |
| `apiserver_storage_objects` | Count of objects stored in etcd per resource type |

Both sets of metrics are sufficient for capacity planning. The recording rules in this project aggregate them into actionable composite metrics.

---

## 2. How to Monitor — Capacity Planning in Practice

### 2.1 Five Metrics That Matter

| Metric | What It Measures | Warning | Critical | Recording Rule |
|--------|-----------------|---------|----------|----------------|
| **etcd Object Count** | Total objects in etcd | 50,000 | 100,000 | `crossplane:etcd_object_count:total` |
| **etcd P99 Latency** | etcd request responsiveness | 100ms | 500ms | `crossplane:etcd_request_latency:p99` |
| **API Server P99 Latency** | End-to-end API request health | 1s | 2s | `crossplane:apiserver_request_latency:p99` |
| **Controller Memory** | Crossplane controller working set | 2 GiB | 5 GiB | `crossplane:controller_memory_bytes` |
| **Controller CPU** | Crossplane controller CPU usage | 1.5 cores | 3.0 cores | `crossplane:controller_cpu_cores` |

These five metrics, with the thresholds above, cover the full capacity picture. Object count is the leading indicator; the others are lagging confirmation.

### 2.2 Monitoring Architecture

Data flows from the OpenShift cluster to an external Prometheus via remote-write, where recording rules encode the capacity models:

```
OpenShift Cluster
├── Platform Prometheus (openshift-monitoring)
│   Remote-writes: apiserver_storage_objects, etcd_request_duration_seconds,
│                  apiserver_request_duration_seconds, container_* (crossplane-system)
│
├── User-Workload Prometheus (openshift-user-workload-monitoring)
│   Remote-writes: crossplane:* recording rules
│
└──────── remote-write ────────>  External Prometheus
                                  ├── Raw metric aggregation
                                  ├── Power-law prediction rules
                                  ├── Capacity alerts & forecasts
                                  └── Deduplication: max by (resource)
                                           │
                                           │ query
                                           ▼
                                        Grafana
                                     (crossplane-capacity dashboard, 41 panels)
```

**Why remote-write?** OpenShift's Prometheus requires authentication that makes federation impractical. Remote-write pushes data out of the cluster, avoiding auth issues.

**Deduplication:** OpenShift runs two Prometheus replicas that both remote-write identical data. Recording rules use `max by (resource)` to collapse duplicates.

### 2.3 Composite Health Indicator

The `crossplane:capacity_status` recording rule produces a single value:

| Value | Status | Meaning |
|-------|--------|---------|
| **0** | GREEN | All metrics within normal range |
| **1** | DEGRADED | At least one warning threshold breached |
| **2** | CRITICAL | At least one critical threshold breached |

The logic checks four dimensions (memory > 5 GiB, etcd P99 > 500ms, API P99 > 2s, objects > 100k for critical; memory > 2 GiB, etcd P99 > 100ms, API P99 > 1s, objects > 50k for warning). Any single critical breach sets status to 2; any warning breach (without a critical) sets status to 1.

Use this metric in a Grafana stat panel as a single-glance health check.

### 2.4 Growth Forecasting

Two forecasting mechanisms provide early warning:

**Linear extrapolation** — `crossplane:days_until_object_limit:50k` uses `deriv()` over a 6-hour window to project when the 50k object threshold will be reached:

```
(50000 - current_count) / (growth_rate_per_day)
```

This drives three alert classes:

| Alert Class | What It Detects | Response Model |
|-------------|----------------|----------------|
| **Symptom** | Current threshold breach | Page: respond in < 15 min |
| **Forecast** | Projected breach in N days (14-day and 3-day lead) | Ticket: respond in 1 business day |
| **Drift** | Model prediction diverges from reality (>30% memory, >50% etcd) | Refit: run `make analyze` |

**Power-law prediction** — The capacity models (see next section) predict future metric values based on projected object count. These feed alerts like "predicted memory breach in 14 days."

---

## 3. Models and Formulas

### 3.1 Core Idea

Resource usage follows a power-law relationship with etcd object count:

```
y = a * x^b
```

Where `x` is the total etcd object count, `y` is the resource metric (memory, CPU, latency), and `a` and `b` are fitted coefficients.

**Why power-law over other models?**

| Alternative | Why Not |
|-------------|---------|
| Linear | Over-predicts at high object counts; real growth is sub-linear |
| Piecewise linear | Higher R² in some cases but dangerous for extrapolation (breakpoint is arbitrary) |
| Log-linear | Similar fit quality but harder to invert for reverse capacity |
| Quadratic | Over-fits, can go negative at low counts |

Power-law was chosen because: (a) it captures sub-linear growth via `b < 1`, (b) it extrapolates monotonically (never goes negative), (c) it's trivially encodable in PromQL (`a * (x ^ b)`), and (d) it has a closed-form inverse for reverse capacity calculations.

The memory model illustrates this — the power-law curve closely tracks the observed data across the full range:

![Controller Memory vs etcd Object Count](../report/charts/crossplaneMemory.png)

### 3.2 How Models Were Fitted

1. **Data**: 67 data points from 18,047 to 115,856 etcd objects
2. **Method**: `scipy.optimize.curve_fit` with automatic bounds
3. **Candidates**: 7 model families tested (linear, quadratic, power-law, log-linear, piecewise linear, saturating exponential, square root)
4. **Selection**: Highest R² after holdout validation (80/20 split), with monotonicity constraint
5. **Deployment choice**: Power-law selected for all Prometheus rules, even when piecewise linear had higher R², because power-law extrapolates safely

For example, memory's best statistical fit may be piecewise linear, but the power-law fit (R²=0.94) is used in production because piecewise models have an arbitrary breakpoint that makes extrapolation unreliable.

### 3.3 Model Coefficients

These coefficients are embedded in the Prometheus recording rules (`crossplane-rules-self-managed.yml`) and updated with `fit_date` labels:

| Metric | Formula | R² | Confidence | Fit Date |
|--------|---------|-----|-----------|----------|
| **Controller Memory** (bytes) | `2.74e+07 * x^0.476` | 0.94 | **HIGH** | 2026-03-03 |
| **Controller CPU** (cores) | `3.29e-03 * x^0.595` | 0.94 | **HIGH** | 2026-03-03 |
| **etcd P99 Latency** (seconds) | `6.57e-01 * x^-0.201` | 0.59 | **LOW** | 2026-03-03 |
| **API P99 Latency** (seconds) | `1.03e+00 * x^-0.005` | 0.48 | **LOW** | 2026-03-03 |

**Confidence classification:**

| Class | Criteria | Safe For |
|-------|----------|----------|
| **HIGH** | R² > 0.90, MAPE < 10% | Forecast alerts, paging |
| **MEDIUM** | R² > 0.70, MAPE < 25% | Warning-level alerts, dashboards |
| **LOW** | R² < 0.70 or MAPE >= 25% | Advisory display only — not for paging |

The latency models are low-confidence because the continuous-ramp test methodology conflated burst effects with steady-state behavior. The negative exponents on the latency models indicate that latency actually decreases slightly with scale in the tested range — likely reflecting the cluster stabilizing at higher object counts on the self-managed infrastructure.

#### Fitted Curves

![Controller CPU vs etcd Object Count](../report/charts/crossplaneCPU.png)

![etcd P99 Latency vs etcd Object Count](../report/charts/etcdLatencyP99.png)

![API Server P99 Latency vs etcd Object Count](../report/charts/apiserverLatencyP99.png)

### 3.4 Forward and Reverse Capacity

**Forward capacity** answers: "Given N objects, how many worker nodes do I need?"

```
predicted_memory = 2.74e+07 * (objects ^ 0.476)
predicted_cpu    = 3.29e-03 * (objects ^ 0.595)

nodes = max(
    ceil(predicted_memory / 10.8 GiB),    # effective memory per node
    ceil(predicted_cpu / 2.6)              # effective CPU per node
)
```

Effective per-node capacity for a 4 vCPU / 16 GiB worker: 10.8 GiB memory and 2.6 cores (after allocatable limits, system overhead, and 80% utilization target).

**Reverse capacity** answers: "Given our cluster, what's the maximum safe object count?"

Solve `threshold = a * x^b` for `x`:

```
x_max = (threshold / a) ^ (1 / b)
```

Applied to each dimension using the critical alert thresholds:

| Dimension | Threshold | Max Objects | Max VM Claims | Formula |
|-----------|-----------|-------------|---------------|---------|
| **Memory** | **5 GiB (critical)** | **66,048** | **8,256** | `(5e9 / 2.74e7) ^ (1/0.476)` |
| CPU | 3 cores (critical) | 451,608 | 56,451 | `(3.0 / 3.29e-3) ^ (1/0.595)` |
| etcd P99 | 500ms (critical) | 131,604 | 16,451 | Advisory — low R² |
| API P99 | 2s (critical) | 29,505 | 3,688 | Advisory — low R² |

The minimum across all dimensions is the cluster's effective limit: **66,048 objects / 8,256 VM claims**, limited by controller memory at the 5 GiB critical threshold. Note that memory and CPU limits use the worker node capacity, while latency limits are tied to the etcd/API server in the control plane. The latency models have low R² and are advisory only.

### 3.5 Model Drift Detection

Two drift alerts fire when reality diverges from the model:

| Alert | Condition | Duration | Action |
|-------|-----------|----------|--------|
| `CrossplaneMemoryModelDrift` | Actual memory deviates >30% from predicted | 30 min | Refit via `make analyze` |
| `CrossplaneEtcdLatencyModelDrift` | Actual etcd P99 deviates >50% from predicted | 30 min | Refit via `make analyze` |

The memory drift threshold is tighter (30%) because the model is high-confidence (R²=0.94). The etcd threshold is wider (50%) because the model is low-confidence (R²=0.59) — a tighter threshold would cause false positives.

---

## 4. Results and Recommendations

### 4.1 Test Environment

| Parameter | Value |
|-----------|-------|
| Platform | Self-managed OpenShift (crossplane1) |
| Kubernetes | 1.33.6 |
| Topology | 3 masters + 3 workers |
| Provider | provider-nop v0.5.0 (mock resources) |
| Load generator | kube-burner |
| Data points | 67 |
| Object range | 18,047 to 115,856 etcd objects |
| Test type | Continuous ramp (9-job progressive) + overnight soak |

### 4.2 Key Findings

1. **Controller memory is the binding constraint** with the 5 GiB critical threshold. It is reached at ~66,048 objects (~8,256 VM claims).

2. **Memory and CPU are the most predictable metrics** (R²=0.94 for both). These models are safe for forecast alerts and paging.

3. **106k objects proven stable.** The overnight soak test on crossplane1 demonstrated the cluster handling 115,856 etcd objects without degradation, validating the model well beyond the critical threshold.

4. **Latency models are noisy but improved.** etcd P99 R²=0.59, API P99 R²=0.48. The self-managed cluster with direct etcd access shows that latency remains stable or even decreases slightly at higher object counts, likely due to better etcd configuration control.

5. **Self-managed clusters have significantly higher ceilings** than managed platforms due to direct etcd tuning, NVMe storage access, and full metric visibility.

The API P99 latency chart shows behavior across the tested range:

![API Server P99 Latency](../report/charts/apiserverLatencyP99.png)

### 4.3 Sizing Table

Predictions at key object counts (using power-law models):

| Objects | VM Claims | Memory | CPU | etcd P99 | API P99 | Status |
|---------|-----------|--------|-----|----------|---------|--------|
| 10,000 | 1,250 | ~1.5 GiB | ~0.50 cores | stable | stable | GREEN |
| 30,000 | 3,750 | ~2.5 GiB | ~1.1 cores | stable | stable | GREEN |
| 50,000 | 6,250 | ~3.5 GiB | ~1.5 cores | stable | stable | **WARNING** (objects) |
| 66,048 | 8,256 | ~5.0 GiB | ~1.9 cores | stable | stable | **CRITICAL** (memory) |
| 100,000 | 12,500 | ~5.5 GiB | ~2.4 cores | stable | stable | CRITICAL |

Note: On self-managed clusters, etcd and API latency remain stable across the tested range due to direct etcd tuning. The binding constraint is controller memory at the 5 GiB threshold. On managed platforms, API latency may become the limit earlier.

The etcd P99 latency chart shows the trajectory approaching the 500ms critical threshold at higher object counts:

![etcd P99 Latency Trajectory](../report/charts/etcdLatencyP99.png)

### 4.4 Node Sizing

This section is platform-agnostic. The methodology applies whether you run on AWS, Azure, GCP, bare metal, or any other infrastructure. The specific numbers in this guide were measured on a self-managed OpenShift cluster (crossplane1: 3 masters + 3 workers), but the sizing process works the same everywhere — only the raw node specs and control plane constraints change.

#### General Estimation Formula (No Load Test Required)

If you haven't run your own load test and don't have fitted model coefficients, use these formulas. They require nothing beyond your claim count and composition complexity.

**Step 1 — Count your etcd objects:**

```
etcd_objects = claims × (managed_resources_per_claim + 2)
```

The `+2` accounts for the claim itself and the composite resource (XR). Count your managed resources by inspecting your composition — each `resources` entry that creates a managed resource adds 1.

**Step 2 — Estimate controller memory and CPU:**

```
controller_memory_GiB = 0.5 + (etcd_objects / 10,000)
controller_cpu_cores  = 0.25 + (etcd_objects / 20,000)
```

These are deliberately conservative (they overestimate by 30–90% depending on scale). The 0.5 GiB / 0.25 cores base covers Crossplane's startup overhead. The linear term covers cache growth and reconciliation cost per object.

**Step 3 — Size the worker node:**

```
worker_memory_GiB = controller_memory_GiB × 1.5 + 2.5
worker_cpu_cores  = controller_cpu_cores × 1.5 + 0.75
workers_needed    = 1 (+ 1 for HA)
```

The `×1.5` gives 50% headroom for burst reconciliation. The `+2.5 GiB` and `+0.75 cores` cover kubelet, system daemons, monitoring agents, and other pods on the node.

**Step 4 — Size the control plane (self-managed only):**

```
etcd_nodes           = 3              (minimum for production; 5 for >50k objects)
etcd_memory_per_node = 8 GiB          (etcd holds full dataset in RAM)
etcd_cpu_per_node    = 2-4 cores      (Raft consensus + compaction)
etcd_disk            = SSD required   (NVMe preferred; WAL fsync is the latency driver)
```

On managed Kubernetes you don't control this — the provider sizes the control plane. Monitor API P99 latency and request a tier upgrade if it exceeds 1s sustained.

**Quick reference table:**

| Claims | MR/Claim | etcd Objects | Controller Memory | Controller CPU | Min Worker Node |
|--------|----------|-------------|-------------------|----------------|-----------------|
| 100 | 3 | 500 | 0.6 GiB | 0.28 cores | 2 vCPU / 4 GiB |
| 500 | 3 | 2,500 | 0.8 GiB | 0.38 cores | 2 vCPU / 4 GiB |
| 500 | 6 | 4,000 | 0.9 GiB | 0.45 cores | 2 vCPU / 4 GiB |
| 1,000 | 3 | 5,000 | 1.0 GiB | 0.50 cores | 2 vCPU / 8 GiB |
| 1,000 | 6 | 8,000 | 1.3 GiB | 0.65 cores | 2 vCPU / 8 GiB |
| 2,500 | 3 | 12,500 | 1.8 GiB | 0.88 cores | 2 vCPU / 8 GiB |
| 2,500 | 6 | 20,000 | 2.5 GiB | 1.25 cores | 4 vCPU / 8 GiB |
| 5,000 | 3 | 25,000 | 3.0 GiB | 1.50 cores | 4 vCPU / 8 GiB |
| 5,000 | 6 | 40,000 | 4.5 GiB | 2.25 cores | 4 vCPU / 16 GiB |
| 10,000 | 3 | 50,000 | 5.5 GiB | 2.75 cores | 4 vCPU / 16 GiB |
| 10,000 | 6 | 80,000 | 8.5 GiB | 4.25 cores | 8 vCPU / 32 GiB |

**Important caveats:**

- These formulas estimate **worker node requirements only** (memory and CPU for the Crossplane controller). They say nothing about control plane capacity. On most managed platforms, API server latency becomes the real bottleneck at 25,000–30,000 objects — long before you run out of worker memory or CPU.
- The estimates are intentionally conservative. Real usage is typically 30–50% lower at scale because controller memory and CPU grow sub-linearly (power-law with exponent < 1), while these formulas assume linear growth. That means you'll overprovision, which is safer than underprovisioning.
- If you need tighter estimates, run a load test against your own compositions and infrastructure, then fit power-law models to the data. See Section 3.2 for methodology and `analysis/capacity_model.py` for the implementation.

The rest of this section provides the detailed methodology for teams that want to go deeper.

#### Step 1: Count Your etcd Objects

Before sizing anything, figure out how many etcd objects your workload creates. Every Crossplane claim fans out into multiple objects:

```
total_etcd_objects = number_of_claims × multiplier
```

The multiplier depends entirely on your compositions. Count it by inspecting what each claim creates:

```
multiplier = 1 (claim) + 1 (XR) + N (managed resources)
```

For example, this project's test compositions:

| Composition | Managed Resources | Multiplier |
|-------------|-------------------|------------|
| VMDeployment | 6 | 8 |
| Disk | 2 | 4 |
| DNSZone | 4 | 6 |
| FirewallRuleSet | 5 | 7 |

Your compositions will differ. A composition that provisions a VPC, 3 subnets, a NAT gateway, and a route table creates 6 managed resources — multiplier of 8. A simple S3 bucket composition might create 1 managed resource — multiplier of 3. **Count yours before sizing.**

If you run mixed workloads, use a weighted average:

```
weighted_multiplier = (fraction_A × multiplier_A) + (fraction_B × multiplier_B) + ...
```

#### Step 2: Size the Worker Nodes

Worker nodes run the Crossplane controller pod. As etcd object count grows, the controller needs more memory (it caches objects) and more CPU (reconciliation loops). Two things to calculate: how much the controller will consume, and how much your nodes can provide.

**Predicting controller resource usage:**

The power-law models from Section 3.3 predict controller resource consumption at a given object count:

```
predicted_memory_bytes = 2.74e7 × objects^0.476
predicted_cpu_cores    = 3.29e-3 × objects^0.595
```

| Total etcd Objects | Predicted Memory | Predicted CPU |
|--------------------|------------------|---------------|
| 5,000 | 0.7 GiB | 0.35 cores |
| 10,000 | 1.1 GiB | 0.55 cores |
| 25,000 | 2.1 GiB | 0.92 cores |
| 50,000 | 3.4 GiB | 1.42 cores |
| 100,000 | 5.4 GiB | 2.14 cores |
| 200,000 | 8.6 GiB | 3.23 cores |

These predictions are from a specific test environment. Your controller usage may differ based on Crossplane version, provider, composition complexity, and reconciliation interval. Treat these as ballpark estimates until you run your own load test and refit the models (see Section 4.6).

**Calculating effective node capacity:**

Raw node specs don't equal usable capacity. Apply three deductions:

```
effective_capacity = (raw - system_reserves - crossplane_overhead) × 0.80
```

| Deduction | Memory | CPU | Why |
|-----------|--------|-----|-----|
| System reserves (kubelet, OS, node agents) | ~1.5 GiB | ~0.5 cores | Varies by OS and platform — check `kubectl describe node` for `Allocatable` |
| Crossplane overhead (monitoring, sidecars) | ~1.0 GiB | ~0.25 cores | Other pods in the crossplane-system namespace |
| 80% utilization target | ×0.80 | ×0.80 | Headroom for burst reconciliation during batch operations |

**Worked example:** A node with 16 GiB RAM and 4 vCPU:

| Step | Memory | CPU |
|------|--------|-----|
| Raw specs | 16 GiB | 4.0 cores |
| After system reserves | 14.5 GiB | 3.5 cores |
| After Crossplane overhead | 13.5 GiB | 3.25 cores |
| After 80% utilization target | **10.8 GiB** | **2.6 cores** |

**How many workers:**

```
workers_needed = max(
    ceil(predicted_memory / effective_memory_per_node),
    ceil(predicted_cpu / effective_cpu_per_node),
    1
)
```

At 100k objects with the node above: `max(ceil(5.4/10.8), ceil(2.14/2.6), 1)` = **1 worker**. A single 4 vCPU / 16 GiB node handles even 100k etcd objects. A second worker provides redundancy, not capacity.

**General worker sizing guidance:**

| Total etcd Objects | Minimum Worker Spec | Workers Needed |
|--------------------|---------------------|----------------|
| < 25,000 | 2 vCPU / 8 GiB | 1 (+ 1 for HA) |
| 25,000 – 100,000 | 4 vCPU / 16 GiB | 1 (+ 1 for HA) |
| 100,000 – 250,000 | 8 vCPU / 32 GiB | 1–2 (+ 1 for HA) |
| > 250,000 | 8 vCPU / 32 GiB | 2+ (+ 1 for HA) |

These are conservative — they include the 80% utilization buffer. If you're cost-sensitive, the raw predictions in the table above show you can go smaller, but you'll lose burst headroom.

#### Step 3: Size the Control Plane

The control plane (API server + etcd) is where the real scaling limit lives. Worker sizing is straightforward because memory and CPU grow slowly. The hard constraint is etcd write latency, which drives API server P99.

**Managed Kubernetes (ROSA, EKS, GKE, AKS):**

You don't control control plane node specs directly. The provider sizes it for you, usually based on cluster tier or node count. What you can do:

| Object Count | Action |
|-------------|--------|
| < 10,000 | Default control plane tier is fine |
| 10,000 – 30,000 | Monitor API P99 latency — request tier upgrade if P99 > 1s sustained |
| 30,000 – 50,000 | Request the largest available control plane tier; consider multi-cluster |
| > 50,000 | Multi-cluster is strongly recommended (see Section 5.3) |

On managed platforms, you cannot tune etcd directly. API P99 latency is your primary indicator of control plane health. On self-managed clusters like crossplane1, you have direct etcd metrics and tuning options that significantly raise the ceiling. Monitor, don't assume.

**Self-managed Kubernetes:**

When you control the control plane, size etcd nodes explicitly:

| Component | Recommendation | Why |
|-----------|---------------|-----|
| **etcd node count** | 3 (production minimum) or 5 (large scale) | Odd number for Raft quorum. 3 tolerates 1 failure, 5 tolerates 2 |
| **etcd CPU** | 2–4 dedicated cores per node | Raft consensus is CPU-intensive during leader election and compaction |
| **etcd memory** | 8 GiB minimum per node | etcd holds the full dataset in memory. At 100k objects, expect 2–4 GiB DB size |
| **etcd disk** | SSD required, NVMe strongly preferred | WAL fsync latency directly drives write latency. Network-attached storage adds 2–5ms per write |
| **etcd disk size** | 50 GiB minimum | Room for snapshots and WAL. Set `--quota-backend-bytes` (default 2 GiB, max 8 GiB) |
| **Dedicated nodes** | Yes, if > 25,000 objects | Co-locating etcd with other workloads introduces noisy-neighbor latency spikes |

etcd-specific tuning levers that affect the object ceiling:

| Tunable | Default | Tuned | Effect |
|---------|---------|-------|--------|
| `--auto-compaction-retention` | 5m | 1–2m | More frequent compaction keeps DB size smaller |
| `--quota-backend-bytes` | 2 GiB | 4–8 GiB | Allows more objects before quota alarm |
| `--snapshot-count` | 100,000 | 10,000–50,000 | More frequent snapshots, faster recovery |
| WAL on NVMe vs network SSD | — | — | Can reduce P99 latency by 40–60% |

With NVMe storage, tuned compaction, and dedicated etcd nodes, the ceiling moves significantly higher. Our crossplane1 self-managed cluster demonstrated stability at 115,856 objects — well above what managed platforms typically support. Run your own load test to establish the actual ceiling for your configuration.

#### Quick Estimation Worksheet

Use this to get a rough size without running load tests:

```
1. Count your claims:                    _______ claims
2. Determine your multiplier:            _______ objects/claim
3. Total etcd objects (1 × 2):           _______ objects
4. Look up predicted memory (table):     _______ GiB
5. Look up predicted CPU (table):        _______ cores
6. Pick a node spec where:
   - effective memory > predicted memory
   - effective CPU > predicted CPU
7. Add 1 worker for HA
8. Monitor API P99 after deployment — if > 1s, investigate control plane
```

**Example:** 2,000 claims of a composition with multiplier 5 = 10,000 objects. Predicted: ~1.1 GiB memory, ~0.55 cores CPU. A 2 vCPU / 8 GiB node handles this easily. Add a second node for HA. Monitor API P99.

#### Sizing by Claim Count

The tables above are indexed by etcd objects. In practice, you think in claims. This table translates directly — pick your claim count and composition complexity to read off the CPU and memory your Crossplane controller will need.

Three composition complexity tiers:
- **Simple (×3):** 1 managed resource per claim (e.g., a single bucket, database, or DNS record)
- **Moderate (×5):** 3 managed resources per claim (e.g., a storage account + container + access policy)
- **Complex (×8):** 6 managed resources per claim (e.g., a VM + NIC + disk + security group + IP + DNS)

| Claims | Complexity | etcd Objects | Memory | CPU | Min Worker Spec |
|--------|-----------|-------------|--------|-----|-----------------|
| 100 | Simple (×3) | 300 | 0.1 GiB | 0.07 cores | Any |
| 100 | Moderate (×5) | 500 | 0.1 GiB | 0.09 cores | Any |
| 100 | Complex (×8) | 800 | 0.2 GiB | 0.12 cores | Any |
| 500 | Simple (×3) | 1,500 | 0.3 GiB | 0.18 cores | Any |
| 500 | Moderate (×5) | 2,500 | 0.4 GiB | 0.24 cores | Any |
| 500 | Complex (×8) | 4,000 | 0.6 GiB | 0.32 cores | 2 vCPU / 8 GiB |
| 1,000 | Simple (×3) | 3,000 | 0.5 GiB | 0.27 cores | 2 vCPU / 8 GiB |
| 1,000 | Moderate (×5) | 5,000 | 0.7 GiB | 0.37 cores | 2 vCPU / 8 GiB |
| 1,000 | Complex (×8) | 8,000 | 1.0 GiB | 0.48 cores | 2 vCPU / 8 GiB |
| 2,500 | Simple (×3) | 7,500 | 0.9 GiB | 0.46 cores | 2 vCPU / 8 GiB |
| 2,500 | Moderate (×5) | 12,500 | 1.3 GiB | 0.63 cores | 2 vCPU / 8 GiB |
| 2,500 | Complex (×8) | 20,000 | 1.8 GiB | 0.83 cores | 4 vCPU / 16 GiB |
| 5,000 | Simple (×3) | 15,000 | 1.5 GiB | 0.70 cores | 2 vCPU / 8 GiB |
| 5,000 | Moderate (×5) | 25,000 | 2.1 GiB | 0.94 cores | 4 vCPU / 16 GiB |
| 5,000 | Complex (×8) | 40,000 | 2.9 GiB | 1.25 cores | 4 vCPU / 16 GiB |
| 10,000 | Simple (×3) | 30,000 | 2.4 GiB | 1.05 cores | 4 vCPU / 16 GiB |
| 10,000 | Moderate (×5) | 50,000 | 3.4 GiB | 1.42 cores | 4 vCPU / 16 GiB |
| 10,000 | Complex (×8) | 80,000 | 4.6 GiB | 1.87 cores | 8 vCPU / 32 GiB |

**How to read this table:** Find your claim count and composition complexity. The Memory and CPU columns tell you what the Crossplane controller pod will need. The Min Worker Spec column is the smallest node that can host it (after system reserves, overhead, and the 80% utilization buffer).

**Remember:** These are controller pod requirements, not total cluster requirements. Other workloads on the same nodes need their own capacity. And this table only covers the worker side — the control plane (etcd/API server) may hit its latency limit before you reach these memory or CPU numbers. On managed platforms, API P99 latency typically becomes the bottleneck around 25,000–30,000 objects regardless of worker size.

#### Caveats

- **These models are from one test environment.** The power-law coefficients (Section 3.3) were fitted against a specific Crossplane version, provider-nop, and the crossplane1 self-managed OpenShift cluster. Different providers, real cloud resources (vs. nop), and different compositions will shift the curves. Use these as starting estimates, then refit with your own data.
- **Worker sizing does not fix control plane limits.** Adding bigger or more workers helps with memory and CPU only. It has zero effect on API or etcd latency. If your bottleneck is API P99 (which it likely is), no amount of worker scaling will help — you need control plane scaling or multi-cluster.
- **The predictions assume a single Crossplane controller.** If you run multiple controllers, shard compositions across them, or use external-secret-operator or similar sidecars, adjust the overhead estimates accordingly.

### 4.5 Operational Recommendations

1. **Alert at 30,000 objects, plan scaling at 40,000, critical at 50,000.** These give you time to react before hitting the memory limit.

2. **Target 50% headroom.** If the cluster supports ~66,048 objects max, keep steady-state below 33,000. This absorbs bursts from batch operations without tripping alerts.

3. **Monitor `crossplane:days_until_object_limit:50k`.** This is your primary forecasting metric. Act when it drops below 14 days.

4. **Set controller memory limits >= 5 GiB.** The power-law model predicts ~5.5 GiB at 100k objects. Default limits may be too low. OOMKill at 6 GiB is the hard limit.

5. **Watch controller memory as the primary scaling signal.** Memory is the most predictable metric (R²=0.94) and the binding constraint with the 5 GiB threshold.

6. **Refit after changes.** Run `make analyze` after Crossplane upgrades, composition schema changes, node type changes, or etcd configuration changes.

7. **Rate-limit batch claim creation.** Creating thousands of claims at once causes a transient latency spike that can trigger false-positive alerts. Use kube-burner's QPS controls or stagger creation.

### 4.6 When to Refit Models

Refit the capacity models (run `make analyze` with fresh test data) when any of the following occur:

- Crossplane version upgrade (reconciliation behavior may change)
- Composition schema change (different number of managed resources per claim)
- Worker node instance type change (different memory/CPU allocatable)
- etcd configuration change (compaction interval, quota size)
- A drift alert fires and persists for more than 24 hours
- Moving to a different managed Kubernetes provider (EKS, GKE, AKS)

---

## 5. Scaling Crossplane Beyond the Limits

### 5.1 Scaling Hierarchy

When you approach the ~66,048 object limit, consider these options from least to most complex:

1. **Optimize compositions** — Reduce the number of managed resources per claim. If you can drop from 6 MRs to 4, each claim creates 6 objects instead of 8, giving you 33% more headroom. This is the highest-leverage change.

2. **Clean stale resources** — Delete claims that are no longer needed. Orphaned XRs and managed resources accumulate over time. This is the cheapest lever — it costs nothing and is immediately effective.

3. **Vertical scaling** — On self-managed clusters, increase worker node memory or add etcd resources. On managed platforms, request a larger control plane tier. This can increase both controller memory headroom and etcd throughput limits.

4. **Horizontal splitting** — Split workloads across multiple clusters. Each cluster gets its own etcd, providing linear capacity scaling. A 2-cluster setup doubles your effective object limit to ~59,000.

5. **Runtime tuning** — Increase Crossplane controller reconciliation intervals, add rate limiters, or reduce the watch scope. These reduce etcd pressure at the cost of slower convergence.

### 5.2 Object Budget Planning

Calculate your available claim budget using the weighted average multiplier:

```
available_claims = max_objects / weighted_avg_multiplier
```

Example: If your workload is 60% VMDeployment (8x) and 40% Disk (4x):

```
weighted_avg = 0.60 * 8 + 0.40 * 4 = 6.4
available_claims = 66,048 / 6.4 ≈ 10,320 claims
```

Compare with a pure VMDeployment workload: `66,048 / 8 = 8,256 claims`. The mixed workload allows 25% more claims because Disk has a lower multiplier.

For your own compositions, count the total objects created per claim (claim + XR + managed resources) to get the multiplier. Then divide the object limit by that multiplier.

### 5.3 Multi-Cluster Strategy

Split when object count reaches **60% of tested maximum** (~40,000 objects). This gives you:
- 40% headroom for burst absorption
- Time to set up and validate the second cluster
- A safety margin for model uncertainty

Each additional cluster adds linear etcd capacity:

| Clusters | Max Objects (Total) | Max VM Claims (Total) |
|----------|--------------------|-----------------------|
| 1 | 66,048 | 8,256 |
| 2 | 132,096 | 16,512 |
| 3 | 198,144 | 24,768 |

Use a claim-routing layer (e.g., a GitOps controller or admission webhook) to distribute claims across clusters based on current object counts.

### 5.4 Infrastructure Models: VMs, HCP, and Bare Metal

The infrastructure running your Kubernetes control plane fundamentally determines how far you can push Crossplane. There are three models, each with different etcd characteristics, and the right choice depends on your object count, operational maturity, and willingness to trade simplicity for headroom.

#### Traditional VMs (Self-Managed Control Plane on VMs)

This is the default for most on-premises Kubernetes deployments: you run control plane components (API server, etcd, controller-manager, scheduler) on virtual machines that you manage yourself — whether on VMware, OpenStack, Hyper-V, or cloud VMs with direct access.

**What you get:**
- Full etcd configuration control — compaction interval, quota size, snapshot count, WAL placement
- Direct etcd metrics — `etcd_mvcc_db_total_size_in_bytes`, `etcd_mvcc_keys_total`, `etcd_disk_wal_fsync_duration_seconds`
- Choice of etcd topology — co-located with API server or dedicated etcd nodes
- Ability to tune kernel parameters (I/O scheduler, transparent huge pages)

**Crossplane-specific challenges at scale:**
- VM disk I/O becomes the bottleneck for etcd WAL fsync. Network-attached storage (EBS, Azure Disk, Cinder) adds 1–5ms per write. At 30k+ Crossplane objects with continuous reconciliation, this compounds into P99 spikes
- etcd compaction storms — Crossplane's high write rate (status updates on every reconciliation) means frequent compaction. On VMs with shared storage, compaction can cause I/O contention that spikes latency across the entire control plane
- Noisy neighbor effects — unless etcd runs on dedicated VMs, other control plane components compete for I/O and CPU during peak reconciliation
- Certificate and version management is your responsibility — etcd, Kubernetes, and Crossplane version compatibility must be tracked manually

**When VMs work well for Crossplane:**
- Object counts under 50,000 where storage latency is tolerable
- Organizations with existing VM-based Kubernetes operations (kubeadm, Kubespray, Cluster API)
- Environments where you already have low-latency local storage (local SSDs in the hypervisor)

#### Hosted Control Planes (HCP)

Hosted control planes (ROSA HCP, EKS, GKE Autopilot, AKS) move etcd and the API server to provider-managed infrastructure. You only manage worker nodes.

**What you get:**
- Zero etcd operations — the provider handles backup, restore, compaction, defrag, upgrades, and certificate rotation
- Independent scaling — the provider can resize the control plane without touching your workers
- Faster cluster provisioning — HCP clusters typically spin up in 5–10 minutes vs. 30–40 for classic
- Built-in HA — the provider runs etcd across availability zones with automated failover

**Crossplane-specific challenges at scale:**
- **No etcd tuning.** You cannot adjust compaction intervals, quota sizes, or snapshot frequency. When Crossplane's write rate overwhelms the provider's default settings, your only option is to request a larger tier (if one exists) or split clusters
- **No direct etcd metrics.** Same proxy-metric limitation as any managed platform — you're blind to `etcd_mvcc_db_total_size_in_bytes`, WAL fsync duration, and compaction timing. You rely on `apiserver_request_duration_seconds` as an indirect signal
- **API latency wall is unchanged.** The fundamental bottleneck — etcd's single-leader Raft consensus with sequential writes — doesn't change just because the provider runs it on better hardware. The ceiling may be slightly higher (better disks, tuned compaction), but the shape of the problem is the same
- **Rate limits and quotas.** Some providers impose API request rate limits or object count quotas at the control plane tier level. Crossplane's reconciliation loops can hit these during burst operations, causing throttling that looks like an etcd problem but is actually a provider-side limit
- **Cost premium.** HCP pricing typically adds $70–150/month per cluster for the managed control plane. At scale (10+ clusters in a multi-cluster strategy), this adds up
- **Less visibility when debugging.** When Crossplane claims get stuck in a non-ready state, you can't check etcd directly. You're limited to API server logs and metrics, which may not reveal whether the root cause is etcd compaction, leader election, or storage latency

**When HCP works well for Crossplane:**
- Object counts under 30,000 where the provider's default etcd configuration is sufficient
- Teams that prioritize operational simplicity over fine-grained control
- Multi-cluster strategies where the overhead of managing N control planes is the real bottleneck
- Environments where the managed control plane's built-in HA and backup are table stakes

**Net effect on capacity:** HCP doesn't raise the object ceiling in a fundamental way. The etcd Raft consensus bottleneck is the same whether it runs on your VMs or the provider's. HCP may push the ceiling 10–20% higher (better storage, optimized compaction), but won't give you 2x or 5x improvement. Its real value is operational: less toil, faster recovery, and simpler multi-cluster management.

#### Bare Metal

Running Kubernetes on bare metal — with etcd on local NVMe SSDs and dedicated physical nodes — removes the storage latency penalty entirely. This is the highest-ceiling option for Crossplane at scale.

**What you get:**
- NVMe WAL performance — local NVMe SSDs deliver <100μs write latency vs. 1–5ms for network-attached storage. This directly reduces etcd P99 and pushes the API latency wall significantly higher
- Full etcd tunability — same as VM-based self-managed, plus the ability to optimize BIOS settings, NUMA topology, and interrupt affinity for etcd workloads
- Dedicated etcd nodes with no hypervisor overhead — bare metal eliminates the 5–15% CPU and I/O tax from virtualization
- Predictable performance — no noisy neighbors, no storage I/O contention, no hypervisor scheduling jitter
- Direct metrics and debugging — full access to etcd internals, including WAL fsync histograms, Raft proposal latency, and compaction duration

**Crossplane-specific challenges at scale:**
- **Operational burden is significant.** You own etcd backup, restore, compaction, defragmentation, certificate rotation, version upgrades, and disaster recovery. A missed compaction or a failed restore can take the entire cluster down — and every Crossplane claim with it
- **Hardware procurement and lifecycle.** NVMe drives have write endurance limits. Crossplane's high write rate (continuous status updates) can burn through SSD endurance faster than typical Kubernetes workloads. Monitor SMART data and plan for replacement
- **Capacity models must be refitted.** All coefficients in this guide were measured on managed infrastructure with network-attached storage. Bare metal with NVMe will produce fundamentally different curves — lower latency coefficients, higher object ceilings. Run `make test-full && make analyze` on your bare metal setup to establish your own baselines
- **Blast radius.** A hardware failure on a bare metal etcd node is harder to recover from than a VM failure. There's no live migration, no automatic re-provisioning. Your recovery time depends entirely on your backup and restore procedures
- **Network partitions hit harder.** Bare metal networks lack the software-defined networking resilience of cloud environments. A switch failure or cable issue can cause etcd quorum loss, which freezes all Crossplane reconciliation
- **Scaling is slower.** Adding a bare metal node means racking hardware, installing an OS, bootstrapping Kubernetes, and joining the etcd cluster — days or weeks vs. minutes for a cloud VM

**When bare metal makes sense for Crossplane:**
- Object counts consistently above 50,000 where managed platforms and VMs hit their latency walls
- Latency-sensitive environments where etcd WAL fsync on network-attached storage is the measured bottleneck (not a theoretical concern)
- Organizations with existing bare metal Kubernetes operations and an established hardware lifecycle
- Cost optimization at very large scale (>10 clusters) where managed control plane fees and cloud VM costs dominate the budget

#### Comparison Matrix

| Factor | VMs (Self-Managed) | HCP (Managed) | Bare Metal |
|--------|-------------------|---------------|------------|
| **etcd tuning** | Full | None | Full |
| **etcd metrics** | Direct | Proxy only | Direct |
| **Storage latency** | 1–5ms (network) | Provider-dependent | <0.1ms (NVMe) |
| **Object ceiling** | ~30–60k | ~25–40k | ~80–200k+ |
| **Operational burden** | High | Low | Very high |
| **Time to add capacity** | Minutes–hours | Minutes | Days–weeks |
| **Cost at small scale** | Medium | Medium–high | High |
| **Cost at large scale** | Medium | High | Low–medium |
| **Recovery from failure** | VM re-provision | Automatic | Manual |
| **Crossplane debugging** | Full | Limited | Full |

The object ceiling estimates are rough — they depend heavily on composition complexity, reconciliation interval, provider configuration, and hardware. The point is directional: bare metal gives you 2–5x the headroom of managed platforms, but the operational cost is substantial.

#### Choosing the Right Model

```
Start with managed (HCP or standard managed K8s)
        │
        ├── Object count < 30,000?
        │       └── Yes → Stay managed. It's sufficient.
        │
        ├── Object count 30,000–50,000?
        │       └── Can you split into multiple clusters?
        │               ├── Yes → Multi-cluster on managed (Section 5.3)
        │               └── No → Move to self-managed VMs with dedicated etcd nodes
        │
        └── Object count > 50,000 per cluster?
                └── Do you have bare metal ops expertise?
                        ├── Yes → Bare metal with NVMe etcd
                        └── No → Multi-cluster on managed, or invest in bare metal team
```

In practice, most Crossplane deployments stay under 30,000 objects per cluster and never need to leave managed platforms. Multi-cluster is almost always simpler than bare metal — it trades operational complexity for infrastructure cost.

### 5.5 Summary Checklist

Before going to production with Crossplane at scale:

- [ ] **Know your multiplier.** Count etcd objects per claim for every composition in use.
- [ ] **Deploy the monitoring stack.** Recording rules, Grafana dashboard, and alerts (see `monitoring/`).
- [ ] **Set alert thresholds.** Object count warning at 50k, memory warning at 2 GiB, API P99 warning at 1s.
- [ ] **Watch controller memory as the primary scaling signal.** Memory is the most predictable metric and the binding constraint at the 5 GiB threshold.
- [ ] **Plan a capacity review at 50% utilization** (~33,000 objects). Don't wait until you hit the wall.
- [ ] **Refit models after infrastructure changes.** Crossplane upgrades, composition changes, node type changes.
- [ ] **Plan multi-cluster at 60% of tested max** (~40,000 objects). Start the second cluster before you need it.

---

## 6. Appendix

### Test Configuration

The primary ramp test used a 9-job progressive configuration, ramping from 100 to 12,500 claims. Each job creates a batch at a set QPS, with 30–120 second pauses between jobs for reconciliation to settle.

| Job | Resource | Batch Size | Cumulative Claims | ~Cumulative Objects |
|-----|----------|------------|-------------------|---------------------|
| 1 | VM | 100 | 100 | 800 |
| 2 | VM | 400 | 500 | 4,000 |
| 3 | VM | 500 | 1,000 | 8,000 |
| 4 | Disk | 1,000 | 2,000 | 12,000 |
| 5 | DNS | 1,000 | 3,000 | 18,000 |
| 6 | Firewall | 1,000 | 4,000 | 25,000 |
| 7 | VM | 2,000 | 6,000 | 41,000 |
| 8 | VM | 5,000 | 11,000 | 81,000 |
| 9 | VM | 2,000 | 13,000 | 97,000 |

The overnight test then continued with 65 batches of 500 VMDeployments each (10-minute soak between batches), producing 67 data points spanning 18,047 to 115,856 objects. See [`kube-burner/`](../kube-burner/) for full test configs.

### Glossary

| Term | Definition |
|------|-----------|
| **XR** | Composite Resource — the intermediate object Crossplane creates between a claim and managed resources |
| **XRD** | Composite Resource Definition — the schema that defines what an XR looks like |
| **Composition** | The template that maps an XR to its managed resources |
| **Claim** | The user-facing API object that triggers XR and managed resource creation |
| **provider-nop** | A Crossplane provider that creates real K8s objects without provisioning cloud resources |
| **Recording rule** | A Prometheus rule that pre-computes and stores a query result as a new time series |
| **R²** | Coefficient of determination — how much variance the model explains (1.0 = perfect, 0.0 = no predictive power) |
| **MAPE** | Mean Absolute Percentage Error — average prediction error as a percentage |
| **Power-law** | A model of the form `y = a * x^b`, used for capacity predictions |

### Related Documents

| Document | Location | Contents |
|----------|----------|----------|
| Capacity Contract | [`monitoring/capacity-contract.md`](../monitoring/capacity-contract.md) | Thresholds, owners, alert routing, response procedures |
| Model Scorecard | [`monitoring/capacity-model-scorecard.md`](../monitoring/capacity-model-scorecard.md) | Per-metric model accuracy tracking |
| etcd Thresholds | [`docs/etcd-thresholds.md`](etcd-thresholds.md) | How thresholds were derived, general guidance |
| README | [`README.md`](../README.md) | Project overview and documentation index |
| Capacity Report | [`report/capacity-report.md`](../report/capacity-report.md) | Full analysis output with charts |
