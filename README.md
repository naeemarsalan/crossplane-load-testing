# Crossplane etcd Capacity Planning on ROSA

A complete framework for capacity planning Crossplane workloads on Red Hat OpenShift Service on AWS (ROSA). Uses load testing with mock resources to build predictive models for memory, CPU, and API latency as a function of etcd object count — then deploys those models as Prometheus recording rules and Grafana dashboards for continuous capacity monitoring.

## TL;DR

- **1 Crossplane claim = 8 etcd objects** (for a VMDeployment with 6 NopResources + 1 XR + 1 Claim)
- A **2-node m5.xlarge ROSA cluster tops out at ~29,500 objects** (~3,700 VMDeployment claims) — bottlenecked by API server P99 latency hitting the 2-second threshold
- Memory is the most predictable dimension (R²=0.93); latency models are noisy and advisory-only
- The entire pipeline is automated: `make setup && make monitor && make test-full && make analyze`

![Crossplane Memory vs Object Count](report/charts/crossplaneMemory.png)

---

## Table of Contents

1. [Cluster Setup](#1-cluster-setup)
2. [Object Multiplication Model](#2-object-multiplication-model)
3. [How We Tested](#3-how-we-tested)
4. [Capacity Model — How Predictions Work](#4-capacity-model--how-predictions-work)
5. [Key Findings](#5-key-findings)
6. [Scaling Rules of Thumb](#6-scaling-rules-of-thumb)
7. [Monitoring Architecture](#7-monitoring-architecture)
8. [Alert Framework](#8-alert-framework)
9. [Capacity Calculator — Validation](#9-capacity-calculator--validation)
10. [Project Structure](#10-project-structure)
11. [How to Reproduce](#11-how-to-reproduce)
12. [How to Run](#12-how-to-run)

---

## 1. Cluster Setup

### ROSA Cluster

| Parameter | Value |
|-----------|-------|
| Platform | Red Hat OpenShift on AWS (ROSA) |
| Kubernetes version | 1.33.6 |
| Worker nodes | 2 × m5.xlarge |
| vCPU per node | 4 (3.5 allocatable) |
| Memory per node | 16 GiB (14.5 GiB allocatable) |
| Region | us-east-2 |
| System overhead | 0.5 CPU cores + 2 GiB memory (total) |
| Effective per-node capacity | 2.6 cores / 10.8 GiB (at 80% utilization target) |

### Crossplane Stack

| Component | Version |
|-----------|---------|
| Crossplane | Helm chart (latest) |
| provider-nop | v0.5.0 |
| function-patch-and-transform | v0.10.1 |
| function-auto-ready | v0.6.1 |
| Test namespace | `crossplane-loadtest` |

All components are installed idempotently via `setup/install.sh`.

---

## 2. Object Multiplication Model

Each Crossplane claim creates multiple etcd objects: the claim itself, a composite resource (XR), and the managed resources defined in the composition. We built 4 XRDs to model different multiplication factors:

| XRD | Managed Resources | + XR + Claim | Total etcd Objects |
|-----|-------------------|--------------|-------------------|
| **VMDeployment** | 6 NopResources (VM, NIC, Disk, SecurityGroup, PublicIP, DNS) | +2 | **8** |
| **Disk** | 2 NopResources (Disk, Snapshot) | +2 | **4** |
| **DNSZone** | 4 NopResources (Zone, RecordA, RecordMX, RecordTXT) | +2 | **6** |
| **FirewallRuleSet** | 5 NopResources (RuleSet, IngressHTTP, IngressHTTPS, EgressAll, LogConfig) | +2 | **7** |

### Why provider-nop?

[provider-nop](https://github.com/crossplane-contrib/provider-nop) creates real Kubernetes/etcd objects (NopResource CRs) without provisioning actual cloud infrastructure. This gives us:
- **Real etcd pressure**: every NopResource is a real object stored in etcd
- **No cloud cost**: no AWS/GCP/Azure resources are created
- **Fast reconciliation**: NopResources reconcile instantly (no API calls to cloud providers)
- **Repeatable tests**: no external dependencies, no cloud rate limits

---

## 3. How We Tested

### Load Generator: kube-burner

We used [kube-burner](https://kube-burner.io/) to create Crossplane claims at controlled rates. The test configuration (`kube-burner/config.yaml`) defines a 9-job ramp:

| Job | Resource | Count | Cumulative Claims | Cumulative etcd Objects | QPS |
|-----|----------|-------|-------------------|------------------------|-----|
| 1. crossplane-ramp-100 | VM | 100 | 100 | ~800 | 20 |
| 2. crossplane-ramp-500 | VM | 400 | 500 | ~4,000 | 20 |
| 3. crossplane-ramp-1000 | VM | 500 | 1,000 | ~8,000 | 25 |
| 4. crossplane-disks-1000 | Disk | 1,000 | 2,000 | ~12,000 | 30 |
| 5. crossplane-dns-1000 | DNS | 1,000 | 3,000 | ~18,000 | 30 |
| 6. crossplane-firewall-1000 | Firewall | 1,000 | 4,000 | ~25,000 | 30 |
| 7. crossplane-vm-large-2000 | VM | 2,000 | 6,000 | ~41,000 | 30 |
| 8. crossplane-mixed-5000 | VM | 5,000 | 11,000 | ~81,000 | 40 |
| 9. crossplane-final-push | VM | 2,000 | 13,000 | ~97,000 | 40 |

Jobs pause between steps (30s–120s) to let reconciliation settle and capture steady-state metrics.

### Cron Growth Test

A separate automated test (`scripts/cron-grow.sh`) creates 500 VM claims (~4,000 etcd objects) every 15 minutes to simulate organic growth. State is tracked in `scripts/cron-state.json` and per-batch results are saved to `results/batch-NNN/`.

### Data Collection

kube-burner collects 25+ Prometheus metrics at each step via `kube-burner/metrics-profile.yaml`:
- `apiserver_storage_objects` (etcd object counts by resource)
- `etcd_request_duration_seconds` (P50, P99)
- `apiserver_request_duration_seconds` (P50, P99)
- `container_memory_working_set_bytes` (Crossplane controller memory)
- `container_cpu_usage_seconds_total` (Crossplane controller CPU)
- `apiserver_current_inflight_requests`, `apiserver_request_total`, and more

**Actual test data**: 37 data points spanning 6,514 to 48,035 etcd objects. Raw metrics are preserved in `kube-burner/collected-metrics/` for reproducibility.

---

## 4. Capacity Model — How Predictions Work

### Model Selection

We fit 7 candidate models to each metric and select the best one:

1. Linear: `y = a*x + b`
2. Quadratic: `y = a*x² + b*x + c`
3. Power-law: `y = a * x^b`
4. Log-linear: `y = a * ln(x) + b`
5. Piecewise linear: two segments with a breakpoint
6. Saturating exponential: `y = a * (1 - e^(-b*x))`
7. Square root: `y = a * sqrt(x) + b`

Selection criteria:
1. **Holdout validation** (80/20 split) — reject models with MAPE > 25%
2. **R² ranking** — highest coefficient of determination wins
3. **Physical plausibility** — model must be monotonically increasing in the valid range

The Python analysis (`analysis/capacity_model.py`) uses `scipy.optimize.curve_fit` with automatic bounds.

### Final Model Coefficients

For the Prometheus recording rules and Grafana dashboard, we use **power-law** models (`y = a × x^b`) because they extrapolate well and are easy to encode in PromQL:

| Metric | a | b | R² | Confidence |
|--------|---|---|-----|------------|
| **Controller Memory** (bytes) | 2.41 × 10⁶ | 0.676 | 0.9335 | **High** |
| **Controller CPU** (cores) | 2.41 × 10⁻³ | 0.590 | 0.7608 | **Medium** |
| **etcd P99 Latency** (seconds) | 8.14 × 10⁻⁴ | 0.545 | 0.3278 | **Low** |
| **API P99 Latency** (seconds) | 7.49 × 10⁻³ | 0.543 | 0.5184 | **Low** |

**Valid range**: 6,514 – 48,035 objects. Extrapolation beyond 48,035 objects degrades confidence.

### Confidence Classification

| Class | Criteria | Use |
|-------|----------|-----|
| **High** | MAPE < 10%, R² > 0.90 | Safe for forecast alerts and paging |
| **Medium** | MAPE < 25%, R² > 0.70 | Warning-level alerts, dashboard display |
| **Low** | MAPE ≥ 25% or R² < 0.70 | Advisory display only, not for paging |

---

## 5. Key Findings

All numbers are from the actual test run with 37 data points (6,514 – 48,035 objects):

- **Memory is the most predictable dimension** (R²=0.93). At 65k objects, the controller uses ~3.3 GiB memory — the power-law model predicted 4.0 GiB (within 20%, conservative).

- **API P99 latency is the binding constraint**. It hits the 2-second critical threshold at ~29,500 objects — well before memory or CPU become a problem. This makes API latency the cluster's effective capacity limit.

- **etcd P99 latency is noisy** (R²=0.33). The continuous-ramp test methodology conflates burst effects with steady-state behavior. This model is advisory-only until refitted with stepped-soak data.

- **2-node m5.xlarge cluster capacity**:
  - Max objects: **~29,500** (limited by API latency)
  - Max VMDeployment claims: **~3,700** (at 8 objects per claim)

- **Per-dimension capacity limits** (reverse capacity at 80% utilization):

  | Dimension | Max Objects | Limiting Factor |
  |-----------|-------------|-----------------|
  | Memory | 783,061 | 4 GiB critical threshold |
  | CPU | 451,608 | 3 cores critical threshold |
  | etcd P99 | 131,604 | 500ms critical threshold |
  | **API P99** | **29,505** | **2s critical threshold** |

  API latency is 26× more restrictive than memory — it's the bottleneck by a wide margin.

- **Growth rate during batch operations**: 377k objects/day. At this rate, the cluster would need 9 nodes in 14 days and 14 nodes in 30 days. This rate reflects batch loading, not steady-state growth — production growth rates will be much lower.

- **Grafana and Python calculators match within 0.1%**: forward capacity (nodes required) matches exactly; reverse capacity (max objects) differs by only 36 objects due to binary search vs closed-form formula.

### Charts

| Metric | Chart |
|--------|-------|
| Controller Memory | ![Memory](report/charts/crossplaneMemory.png) |
| Controller CPU | ![CPU](report/charts/crossplaneCPU.png) |
| etcd P99 Latency | ![etcd P99](report/charts/etcdLatencyP99.png) |
| API P99 Latency | ![API P99](report/charts/apiserverLatencyP99.png) |
| etcd P50 Latency | ![etcd P50](report/charts/etcdLatencyP50.png) |
| API P50 Latency | ![API P50](report/charts/apiserverLatencyP50.png) |
| API Request Rate | ![Request Rate](report/charts/apiserverRequestRate.png) |
| API Inflight Requests | ![Inflight](report/charts/apiserverInflightRequests.png) |
| API Error Rate | ![Errors](report/charts/apiserverErrorRate.png) |

---

## 6. Scaling Rules of Thumb

Practical guidelines derived from the data:

### Object Budget
**1 VMDeployment = 8 etcd objects.** Always plan etcd capacity at 8× your VMDeployment claim count (or the appropriate multiplier for your composition — see the [Object Multiplication Model](#2-object-multiplication-model) table).

### Memory Planning
```
memory_GiB ≈ 2.41 × (objects / 1000)^0.676 / 1000
```
At 100k objects, expect ~4 GiB controller memory. This is the highest-confidence model (R²=0.93).

### Node Sizing
Each m5.xlarge worker effectively supports **~15,000 Crossplane-managed objects** after accounting for:
- 80% utilization target
- System overhead (0.25 CPU + 1 GiB per node)
- Headroom for burst absorption

### Growth Alerts
- **Warning** at >24,000 objects/day growth rate (indicates batch operations or runaway reconciliation)
- **Forecast alerts** at 14-day and 3-day lead times for threshold breaches
- Track `crossplane:days_until_object_limit:30k` for proactive scaling

### Latency Caveat
etcd and API latency models have **low confidence** (R²=0.33 and R²=0.52). Use them for dashboard display and advisory alerts only — not for paging. Symptom-based threshold alerts (current value > threshold) remain fully active.

### Refit Cadence
Refit capacity models after:
- Major Crossplane version upgrades
- Composition changes (different object multiplier)
- etcd configuration changes (compaction interval, quota size)
- New worker node instance types

### Headroom
Target **50% capacity headroom** for burst absorption. If your cluster supports 29,500 objects max, alert at 15,000 and plan scaling at 20,000.

### Quick Reference: Capacity Limits per Dimension

| Dimension | Max Objects | Max VM Claims | Threshold |
|-----------|-------------|---------------|-----------|
| Memory | 783,061 | 97,883 | 4 GiB critical |
| CPU | 451,608 | 56,451 | 3 cores critical |
| etcd latency | 131,604 | 16,451 | 500ms P99 critical |
| **API latency** | **29,505** | **3,688** | **2s P99 critical** |

---

## 7. Monitoring Architecture

### Data Flow

```
┌──────────────────────────────────────────────────────────┐
│                     ROSA Cluster                          │
│                                                           │
│  ┌─────────────────────┐    ┌──────────────────────────┐ │
│  │ Platform Prometheus  │    │ User-Workload Prometheus │ │
│  │ (openshift-monitoring│    │ (openshift-user-workload │ │
│  │                     )│    │  -monitoring)            │ │
│  │                      │    │                          │ │
│  │ Metrics:             │    │ Metrics:                 │ │
│  │ - apiserver_storage_ │    │ - crossplane:* recording │ │
│  │   objects            │    │   rules                  │ │
│  │ - etcd_request_      │    │                          │ │
│  │   duration_seconds   │    │                          │ │
│  │ - apiserver_request_ │    │                          │ │
│  │   duration_seconds   │    │                          │ │
│  │ - container_* (for   │    │                          │ │
│  │   crossplane-system) │    │                          │ │
│  └──────────┬───────────┘    └────────────┬─────────────┘ │
│             │ remote-write                │ remote-write   │
└─────────────┼─────────────────────────────┼────────────────┘
              │                             │
              ▼                             ▼
     ┌────────────────────────────────────────────┐
     │        External Prometheus                  │
     │        (prom.arsalan.io)                    │
     │                                             │
     │  Runs: crossplane-rules-external.yml        │
     │  - 3 rule groups:                           │
     │    1. Raw metric aggregation                │
     │    2. Power-law predictions                 │
     │    3. Capacity alerts & forecasts           │
     │  - Deduplication: max by (resource)         │
     │    (handles 2 Prometheus replicas)           │
     └──────────────────┬──────────────────────────┘
                        │ query
                        ▼
              ┌───────────────────┐
              │     Grafana       │
              │                   │
              │  Dashboard:       │
              │  crossplane-      │
              │  capacity         │
              │  (41 panels,      │
              │   9 rows)         │
              │                   │
              │  Row categories:  │
              │  - Current Health │
              │  - Forecasts      │
              │  - Node Sizing    │
              │    Advisor        │
              └───────────────────┘
```

### ROSA Limitation

ROSA does not expose direct `etcd_mvcc_*` metrics. We use proxy metrics instead:
- `etcd_request_duration_seconds` — measures etcd request latency from the API server side
- `apiserver_request_duration_seconds` — measures end-to-end API request latency
- `apiserver_storage_objects` — counts objects stored in etcd per resource type

### Deduplication

OpenShift runs two Prometheus replicas (`prometheus-k8s-0` and `prometheus-k8s-1`) that both remote-write identical data. The recording rules on the external Prometheus use `max by (resource)` to deduplicate.

---

## 8. Alert Framework

### Alert Classes

| Class | Purpose | Response Time | Channel |
|-------|---------|--------------|---------|
| **Symptom** (page) | Current state breaches threshold | < 15 minutes | PagerDuty / Slack #alerts-critical |
| **Forecast** (ticket) | Projected breach within lead time | 1 business day | Jira / Slack #capacity-planning |
| **Drift** (refit) | Model diverges from reality | 1 business day | Slack #capacity-planning |

### Symptom Alerts

| Alert | Threshold | `for` | Severity |
|-------|-----------|-------|----------|
| Controller Memory Critical | > 4 GiB | 5m | critical |
| Controller Memory Warning | > 2 GiB | 10m | warning |
| etcd Latency Critical | > 500ms P99 | 5m | critical |
| etcd Latency Warning | > 100ms P99 | 10m | warning |
| API Latency Critical | > 2s P99 | 5m | critical |
| API Latency Warning | > 1s P99 | 10m | warning |
| Object Count Warning | > 30,000 | 5m | warning |
| Object Count Critical | > 80,000 | 5m | critical |
| Rapid Object Growth | > 24,000/day | 15m | warning |

### Forecast Alerts

| Alert | Condition | `for` | Severity | Model Confidence |
|-------|-----------|-------|----------|-----------------|
| Object Count 30k in 14 days | Linear projection | 30m | warning | N/A |
| Object Count 30k in 3 days | Linear projection | 15m | critical | N/A |
| Object Count 100k in 14 days | Linear projection | 30m | warning | N/A |
| Memory Breach in 14 days | Predicted > 4 GiB | 30m | warning | High (R²=0.93) |
| Memory Breach in 30 days | Predicted > 4 GiB | 1h | info | High (R²=0.93) |
| etcd Latency Breach in 14 days | Predicted > 100ms | 30m | warning | Low (R²=0.33) — advisory |

### Drift Alerts

| Alert | Condition | `for` | Action |
|-------|-----------|-------|--------|
| Memory Model Drift | > 30% deviation from predicted | 30m | Refit models |
| etcd Latency Model Drift | > 50% deviation from predicted | 30m | Refit models |

For full details, see:
- [Capacity Contract](monitoring/capacity-contract.md) — thresholds, confidence levels, response procedures
- [Alert Matrix](monitoring/capacity-alert-matrix.md) — routing, silencing, escalation

---

## 9. Capacity Calculator — Validation

The capacity model is implemented in two independent paths to ensure correctness:

### Forward Capacity (given current state → nodes needed)

| Metric | Grafana (PromQL) | Python | Delta | Match |
|--------|-----------------|--------|-------|-------|
| Workers Now | 1 | 1 | 0 | Yes |
| Workers 14d | 9 | 9 | 0 | Yes |
| Workers 30d | 14 | 14 | 0 | Yes |

### Reverse Capacity (given cluster → max objects)

| Metric | Grafana (PromQL) | Python | Delta | Match |
|--------|-----------------|--------|-------|-------|
| Max Objects | 29,505 | 29,541 | 36 (0.1%) | Yes |
| Max Claims | 3,688 | 3,692 | 4 | Yes |
| Bottleneck | API latency | API latency | — | Yes |

The 36-object difference (0.1%) is due to Grafana using a closed-form formula (`exp(ln(threshold/a) / b)`) while Python uses binary search over a 0–500,000 range.

### Test Suite

The Python calculator has 29 unit tests (`analysis/test_capacity_calculator.py`) — all passing:
- Forward capacity calculations
- Reverse capacity (per-dimension limits)
- Edge cases (zero objects, single node, extreme growth rates)
- Model coefficient validation

---

## 10. Project Structure

```
crossplabn/
├── README.md                          # This file
├── Makefile                           # Build orchestration (setup, monitor, test, analyze)
├── .env.example                       # Template for environment variables
│
├── setup/                             # Cluster setup (idempotent)
│   ├── 00-namespace.yaml              #   crossplane-loadtest namespace
│   ├── 01-provider-nop.yaml           #   provider-nop v0.5.0
│   ├── 02-provider-config.yaml        #   ProviderConfig for NopResources
│   ├── 03-functions.yaml              #   function-patch-and-transform + function-auto-ready
│   └── install.sh                     #   Idempotent install script (Helm + kubectl)
│
├── xrds/                              # Composite Resource Definitions
│   ├── vm-deployment.yaml             #   VMDeployment (6 NopResources)
│   ├── disk.yaml                      #   Disk (2 NopResources)
│   ├── dns-zone.yaml                  #   DNSZone (4 NopResources)
│   └── firewall-ruleset.yaml          #   FirewallRuleSet (5 NopResources)
│
├── compositions/                      # Compositions (Pipeline mode)
│   ├── vm-deployment.yaml             #   function-patch-and-transform + function-auto-ready
│   ├── disk.yaml
│   ├── dns-zone.yaml
│   └── firewall-ruleset.yaml
│
├── kube-burner/                       # Load test configuration
│   ├── config.yaml                    #   9-job ramp test definition
│   ├── config-cron-batch.yaml         #   Cron growth test (500 VMs/batch)
│   ├── metrics-profile.yaml           #   25+ Prometheus metrics to collect
│   ├── alerts-profile.yaml            #   kube-burner alert thresholds
│   ├── templates/                     #   Claim YAML templates
│   │   ├── vm-claim.yaml
│   │   ├── disk-claim.yaml
│   │   ├── dns-claim.yaml
│   │   └── firewall-claim.yaml
│   └── collected-metrics/             #   Raw test metrics (committed for reproducibility)
│
├── monitoring/                        # Observability stack
│   ├── 01-cluster-monitoring-config.yaml    # Enable user-workload monitoring
│   ├── 02-user-workload-monitoring-config.yaml  # Remote-write configuration
│   ├── prometheus-rules.yaml                # PrometheusRule CRD for ROSA
│   ├── crossplane-rules-external.yml        # Recording rules for external Prometheus
│   ├── prometheus-external.yml              # External Prometheus config
│   ├── grafana-dashboard.json               # Dashboard (41 panels, 9 rows)
│   ├── capacity-contract.md                 # Thresholds, owners, response procedures
│   ├── capacity-alert-matrix.md             # Alert routing and silencing
│   └── capacity-model-scorecard.md          # Model accuracy tracking
│
├── analysis/                          # Python analysis pipeline
│   ├── analyze.py                     #   Entry point: metrics → report
│   ├── capacity_model.py              #   Curve fitting (7 models, holdout validation)
│   ├── capacity_calculator.py         #   Forward/reverse capacity calculations
│   ├── report_generator.py            #   Markdown report + chart generation
│   ├── capacity-comparison-report.json#   Grafana vs Python comparison data
│   ├── requirements.txt               #   Python dependencies
│   ├── test_analyze.py                #   Tests
│   ├── test_capacity_model.py         #   Tests
│   └── test_capacity_calculator.py    #   Tests (29 tests, all passing)
│
├── scripts/                           # Automation scripts
│   ├── cron-grow.sh                   #   Cron growth test driver
│   ├── install-cron.sh                #   Install cron job
│   ├── stop-cron.sh                   #   Stop cron job
│   ├── cron-status.sh                 #   Check cron status
│   └── analyze-cron-results.py        #   Analyze cron growth data
│
└── report/                            # Generated analysis output
    ├── capacity-report.md             #   Full capacity report
    └── charts/                        #   Metric charts (PNG)
        ├── crossplaneMemory.png
        ├── crossplaneCPU.png
        ├── etcdLatencyP99.png
        ├── etcdLatencyP50.png
        ├── apiserverLatencyP99.png
        ├── apiserverLatencyP50.png
        ├── apiserverRequestRate.png
        ├── apiserverInflightRequests.png
        └── apiserverErrorRate.png
```

---

## 11. How to Reproduce

### Prerequisites

- A ROSA (or OpenShift) cluster with at least 2 worker nodes
- `oc` / `kubectl` CLI, authenticated to the cluster
- [kube-burner](https://kube-burner.io/) installed
- Python 3.10+ with pip
- An external Prometheus instance (for remote-write receiver)
- Grafana (for dashboards)

### Environment Variables

Copy `.env.example` and fill in your values:

```bash
cp .env.example .env
source .env
```

Required variables:
- `ROSA_API_URL` — your ROSA API server URL
- `ROSA_USERNAME` / `ROSA_PASSWORD` — cluster credentials
- `PROM_URL` — external Prometheus URL (for remote-write target)
- `GRAFANA_URL` — Grafana URL
- `GRAFANA_SERVICE_ACCOUNT_TOKEN` — Grafana API token

### Step-by-Step

```bash
# 1. Login to your cluster
oc login --username=$ROSA_USERNAME --password=$ROSA_PASSWORD --server=$ROSA_API_URL

# 2. Install Crossplane, provider-nop, XRDs, compositions
make setup

# 3. Deploy monitoring (remote-write, recording rules, dashboard)
make monitor

# 4. Run a smoke test (100 claims, ~800 objects)
make test-small

# 5. Run the full ramp test (~12,500 claims, ~100k objects)
make test-full

# 6. Generate the capacity report
make analyze

# 7. Import the Grafana dashboard
#    File: monitoring/grafana-dashboard.json
#    Import into Grafana and point it at your external Prometheus
```

---

## 12. How to Run

### Make Targets

| Target | Description |
|--------|-------------|
| `make setup` | Install Crossplane, provider-nop, functions, XRDs, compositions |
| `make monitor` | Enable user-workload monitoring, configure remote-write, deploy PrometheusRule |
| `make test-small` | Smoke test: 100 VM claims (~800 etcd objects) |
| `make test-full` | Full ramp test: ~12,500 claims (~100k etcd objects) |
| `make analyze` | Run Python analysis on collected metrics → generate report |
| `make venv` | Create Python virtual environment |
| `make status` | Show current state of Crossplane and test resources |
| `make count` | Count current etcd objects |
| `make clean` | Delete all test resources (claims, XRs, NopResources) |
| `make clean-all` | Full cleanup: remove Crossplane, monitoring, namespace |
| `make all` | Full pipeline: setup → monitor → test-full → analyze |
| `make help` | Show available targets |

### Python Analysis Only

If you have existing metrics data and just want to run the analysis:

```bash
# Create virtual environment
make venv
source .venv/bin/activate

# Run analysis (metrics data must be in kube-burner/collected-metrics/)
make analyze

# Run tests
cd analysis && python -m pytest -v
```

### Cron Growth Test

To run the automated growth test (500 VM claims every 15 minutes):

```bash
# Install the cron job
bash scripts/install-cron.sh

# Check status
bash scripts/cron-status.sh

# Stop the cron job
bash scripts/stop-cron.sh

# Analyze results
python scripts/analyze-cron-results.py
```

---

## License

This project is provided as-is for educational and capacity planning purposes.
