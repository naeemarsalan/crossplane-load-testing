# Crossplane Capacity Monitoring Guide

This guide explains the full monitoring stack for Crossplane capacity planning: how raw metrics flow from the cluster through Prometheus recording rules into a Grafana dashboard, and how to operate the pipeline day-to-day. It covers all 8 Prometheus rule groups, the 41-panel Grafana dashboard, and the remote-write pipeline that ties them together.

**Audience**: Platform engineers setting up or operating the monitoring stack.

## 1. Data Flow and Architecture

```
┌───────────────────────────────────────────────────────────────┐
│  OpenShift Cluster                                            │
│                                                               │
│  ┌────────────────────────┐  ┌────────────────────────────┐   │
│  │ Platform Prometheus    │  │ User-Workload Prometheus   │   │
│  │ (openshift-monitoring) │  │ (user-workload-monitoring) │   │
│  │                        │  │                            │   │
│  │ Scrapes:               │  │ Scrapes:                   │   │
│  │  apiserver_storage_*   │  │  crossplane:* recording    │   │
│  │  etcd_request_*        │  │  rules (PrometheusRule)    │   │
│  │  container_memory_*    │  │                            │   │
│  │  container_cpu_*       │  │                            │   │
│  │  etcd_mvcc_* (direct)  │  │                            │   │
│  │  etcd_disk_* (direct)  │  │                            │   │
│  └──────────┬─────────────┘  └─────────────┬──────────────┘   │
│             │ remote-write                 │ remote-write     │
└─────────────┼──────────────────────────────┼──────────────────┘
              │                               │
              ▼                               ▼
┌───────────────────────────────────────────────────────────────┐
│  External Prometheus                                          │
│                                                               │
│  Evaluates recording rules & alerts from                      │
│  crossplane-rules-self-managed.yml (8 groups)                 │
│                                                               │
│  raw metrics → recording rules → predictions → alerts         │
└──────────────────────────────┬────────────────────────────────┘
                               │ query
                               ▼
┌───────────────────────────────────────────────────────────────┐
│  Grafana                                                      │
│  Dashboard: "Crossplane Capacity Planning"                    │
│  UID: crossplane-capacity                                     │
└───────────────────────────────────────────────────────────────┘
```

### What gets remote-written

**Platform Prometheus** (`monitoring/01-cluster-monitoring-config.yaml`) writes only container metrics for the `crossplane-system` namespace:

```yaml
writeRelabelConfigs:
  - sourceLabels: [__name__, namespace]
    regex: "container_memory_working_set_bytes;crossplane-system|container_cpu_usage_seconds_total;crossplane-system"
    action: keep
```

**User-Workload Prometheus** (`monitoring/02-user-workload-monitoring-config.yaml`) writes the broader metric set — recording rule outputs, API server metrics, etcd metrics, flow control, and node metrics:

```yaml
writeRelabelConfigs:
  - sourceLabels: [__name__]
    regex: "crossplane:.*|apiserver_storage_objects|container_memory_working_set_bytes|..."
    action: keep
```

### Why remote-write instead of federation

OpenShift's built-in Prometheus endpoints require bearer-token auth and mTLS. Federation from an external Prometheus is impractical without maintaining long-lived tokens. Remote-write pushes data out of the cluster, sidestepping OpenShift auth entirely.

### Deduplication and external labels

OpenShift runs Prometheus in HA pairs. Recording rules like `crossplane:etcd_object_count:total` use `max by (resource)` to collapse duplicate series from both replicas. The `source_cluster` external label (e.g., `crossplane1`) enables multi-cluster setups — each cluster writes to the same external Prometheus under its own label.

## 2. Prometheus Recording Rules

The external Prometheus evaluates 8 rule groups defined in `monitoring/crossplane-rules-self-managed.yml`. Rules are organized in a dependency chain:

```
raw metrics ──► recording-rules ──► prediction-rules ──► forecast-alerts
                      │                    │
                      ├──► symptom-alerts   ├──► drift-alerts
                      │
                 etcd-direct ──► etcd-alerts             node-sizing
```

### Group 1: `crossplane1-capacity.recording-rules` (19 rules, 60s interval)

Core metrics derived from raw Prometheus data. Every other group depends on these.

| Rule | Description |
|------|-------------|
| `crossplane:etcd_object_count:total` | Total objects in etcd (`sum(max by (resource) (apiserver_storage_objects))`) |
| `crossplane:object_growth_rate:per_day` | Linear growth rate extrapolated from 6h window |
| `crossplane:object_growth_rate:per_hour` | Growth rate from 1h window |
| `crossplane:days_until_object_limit:50k` | Days until 50k objects at current growth |
| `crossplane:days_until_object_limit:100k` | Days until 100k objects at current growth |
| `crossplane:etcd_request_latency:p99` | etcd request P99 latency (seconds) |
| `crossplane:etcd_request_latency:p50` | etcd request P50 latency |
| `crossplane:apiserver_request_latency:p99` | API server P99 latency (excludes WATCH/CONNECT) |
| `crossplane:apiserver_request_latency:p50` | API server P50 latency |
| `crossplane:controller_memory_bytes` | Total working set memory, `crossplane-system` namespace |
| `crossplane:controller_cpu_cores` | Total CPU usage, `crossplane-system` namespace |
| `crossplane:apiserver_request_rate:total` | Request rate (excludes WATCH/CONNECT) |
| `crossplane:capacity_status` | Composite health: 0=GREEN, 1=DEGRADED, 2=CRITICAL |
| `crossplane:model_drift:memory_pct` | Memory model drift percentage |
| `crossplane:model_drift:cpu_pct` | CPU model drift percentage |
| `crossplane:model_drift:etcd_latency_pct` | etcd latency model drift percentage |
| `crossplane:model_drift:api_latency_pct` | API latency model drift percentage |
| `crossplane:days_until_memory_breach` | Days until memory hits 5 GiB critical |
| `crossplane:days_until_api_latency_breach` | Days until API P99 exceeds 2s |

**Example** — composite health score:

```promql
# 0=GREEN, 1=DEGRADED (any warning), 2=CRITICAL (any critical)
clamp(
  clamp_max(
    (memory > 5GiB) + (etcd_p99 > 500ms) + (api_p99 > 2s) + (objects > 100k)
  , 1) * 2
  +
  clamp_max(
    (memory > 2GiB) + (etcd_p99 > 100ms) + (api_p99 > 1s) + (objects > 50k)
  , 1)
, 0, 2)
```

### Group 2: `crossplane1-capacity.etcd-direct` (8 rules, 60s interval)

Direct etcd metrics available only on self-managed clusters with etcd metric access. Not present in the in-cluster PrometheusRule CRD.

| Rule | Description |
|------|-------------|
| `crossplane:etcd_db_size_bytes` | Database size (`etcd_mvcc_db_total_size_in_bytes`) |
| `crossplane:etcd_keys_total` | Total keys in etcd (`etcd_mvcc_keys_total`) |
| `crossplane:etcd_wal_fsync_latency:p99` | WAL fsync P99 (`etcd_disk_wal_fsync_duration_seconds`) |
| `crossplane:etcd_backend_commit:p99` | Backend commit P99 (`etcd_disk_backend_commit_duration_seconds`) |
| `crossplane:etcd_leader_changes:rate1h` | Leader elections per hour |
| `crossplane:etcd_peer_rtt:p99` | Peer round-trip time P99 |
| `crossplane:etcd_proposals_committed:rate5m` | Raft proposal commit rate |
| `crossplane:etcd_db_size_pct` | DB size as percentage of quota |

### Group 3: `crossplane1-capacity.prediction-rules` (12 rules, 60s interval)

Power-law model predictions using the form `y = a * x^b`, where `x` is the current etcd object count (`crossplane:etcd_object_count:total`), and `a` (scale) and `b` (exponent) are coefficients fitted from load test data.

**How coefficients are derived:**

1. An overnight load test (`make deploy-self-managed`) scales from ~18k to ~116k objects over ~11 hours, sampling metrics every 60 seconds.
2. `make refit-models` runs `scripts/refit-models.py`, which takes the (object_count, metric_value) pairs from the test and fits a power-law curve using log-log linear regression: `log(y) = log(a) + b * log(x)`.
3. The fit produces two coefficients per metric — `a` (scale factor) and `b` (exponent) — plus an R² goodness-of-fit score. Only models with R² >= 0.90 are considered high-confidence; lower R² models are marked advisory.
4. `make update-coefficients` runs `scripts/update-coefficients.py`, which reads the fitted coefficients from `results/` and writes them into the Prometheus rules files and Grafana dashboard JSON — so the exact numeric values in the rules are always the output of the most recent model fit, not hand-picked constants.

**Dynamic predictions** (4 rules) — predict the expected metric value at the current object count:

| Rule | What it predicts |
|------|------------------|
| `crossplane:predicted_memory_bytes` | Controller memory (bytes) — high-confidence model |
| `crossplane:predicted_cpu_cores` | Controller CPU (cores) — high-confidence model |
| `crossplane:predicted_etcd_latency_p99_seconds` | etcd P99 latency (seconds) — advisory (lower R²) |
| `crossplane:predicted_apiserver_latency_p99_seconds` | API server P99 latency (seconds) — advisory (lower R²) |

Each rule has the form:

```promql
<a> * (crossplane:etcd_object_count:total{source_cluster="crossplane1"} ^ <b>)
```

where `<a>` and `<b>` are the fitted coefficients for that metric.

**Fixed-point predictions** (8 rules) — substitute `x = 50000` or `x = 100000` into the same power-law formula to answer "what will this metric look like at scale?":

| Rule | Description |
|------|-------------|
| `crossplane:predicted_memory_bytes:at_50k` / `:at_100k` | Expected memory at 50k / 100k objects |
| `crossplane:predicted_cpu_cores:at_50k` / `:at_100k` | Expected CPU at 50k / 100k objects |
| `crossplane:predicted_etcd_latency_p99_seconds:at_50k` / `:at_100k` | Expected etcd P99 at 50k / 100k (advisory) |
| `crossplane:predicted_apiserver_latency_p99_seconds:at_50k` / `:at_100k` | Expected API P99 at 50k / 100k (advisory) |

**Refitting workflow:** When drift alerts fire or after a new load test, run `make refit-models && make update-coefficients && make deploy-prom-config` to regenerate all coefficients and propagate them to rules and dashboard.

### Group 4: `crossplane1-capacity.symptom-alerts` (9 alerts)

Current-state threshold breaches. These fire when metrics cross known-bad values now.

| Alert | Condition | Severity | Duration |
|-------|-----------|----------|----------|
| `CrossplaneControllerMemoryWarning` | memory > 2 GiB | warning | 5m |
| `CrossplaneControllerMemoryCritical` | memory > 5 GiB | critical | 5m |
| `CrossplaneEtcdLatencyWarning` | etcd P99 > 100ms | warning | 5m |
| `CrossplaneEtcdLatencyCritical` | etcd P99 > 500ms | critical | 5m |
| `CrossplaneApiServerLatencyWarning` | API P99 > 1s | warning | 5m |
| `CrossplaneApiServerLatencyCritical` | API P99 > 2s | critical | 5m |
| `CrossplaneObjectCountWarning` | objects > 50k | warning | 5m |
| `CrossplaneObjectCountCritical` | objects > 100k | critical | 5m |
| `CrossplaneRapidObjectGrowth` | growth > 10k/day | warning | 15m |

### Group 5: `crossplane1-capacity.etcd-alerts` (4 alerts)

etcd-specific alerts. Self-managed clusters only.

| Alert | Condition | Severity | Duration |
|-------|-----------|----------|----------|
| `CrossplaneEtcdDbSizeWarning` | DB > 50% of quota | warning | 5m |
| `CrossplaneEtcdDbSizeCritical` | DB > 80% of quota | critical | 5m |
| `CrossplaneEtcdWalFsyncSlow` | WAL fsync P99 > 100ms | warning | 5m |
| `CrossplaneEtcdLeaderFlapping` | > 3 leader changes/hour | warning | 5m |

### Group 6: `crossplane1-capacity.forecast-alerts` (3 alerts)

Predictive alerts — fire when linear extrapolation shows a breach within a time horizon.

| Alert | Condition | Severity | Duration |
|-------|-----------|----------|----------|
| `CrossplaneObjectCount50kIn14Days` | Predicted to reach 50k in 14d | warning | 1h |
| `CrossplaneObjectCount100kIn14Days` | Predicted to reach 100k in 14d | critical | 1h |
| `CrossplaneMemoryBreach14Days` | Memory predicted to hit 5 GiB in 14d | critical | 1h |

### Group 7: `crossplane1-capacity.drift-alerts` (2 alerts)

Model accuracy monitoring. Fire when actual values deviate too far from model predictions, indicating the model needs refitting.

| Alert | Condition | Severity | Duration |
|-------|-----------|----------|----------|
| `CrossplaneMemoryModelDrift` | Memory drift > 30% | warning | 30m |
| `CrossplaneEtcdLatencyModelDrift` | etcd latency drift > 50% | warning | 30m |

When these fire, refit the models: `make refit-models && make update-coefficients && make deploy-prom-config`.

### Group 8: `crossplane1-capacity.node-sizing` (8 rules, 60s interval)

Worker node sizing recommendations derived from per-node resource limits and current/projected usage.

| Rule | Description |
|------|-------------|
| `crossplane:projected_objects:14d` | Projected object count in 14 days |
| `crossplane:projected_objects:30d` | Projected object count in 30 days |
| `crossplane:nodes_required:now` | Workers required for current load |
| `crossplane:nodes_required:14d` | Workers required in 14 days |
| `crossplane:nodes_required:30d` | Workers required in 30 days |
| `crossplane:max_objects_supported` | Max objects the cluster can sustain |
| `crossplane:max_claims_supported` | Max VMDeployment claims (objects / 8) |
| `crossplane:capacity_bottleneck_code` | Which resource is the bottleneck (1=mem, 2=cpu, 3=etcd) |

## 3. In-Cluster vs External Rules

Two copies of the recording rules exist for different deployment scenarios:

| File | Format | Scope | Use case |
|------|--------|-------|----------|
| `monitoring/prometheus-rules.yaml` | `PrometheusRule` CRD | In-cluster | Managed platforms without external Prometheus |
| `monitoring/crossplane-rules-self-managed.yml` | Bare YAML | External Prometheus | Self-managed clusters with multi-cluster federation |

**Key differences:**

- The in-cluster CRD has **5 groups** (recording, prediction, symptom-alerts, forecast-alerts, drift-alerts). It omits `etcd-direct`, `etcd-alerts`, and `node-sizing` since managed platforms lack direct etcd access.
- The external file has **8 groups** and adds `source_cluster` label filters on every rule.
- The in-cluster `forecast-alerts` group includes additional alerts (`CrossplaneObjectCount50kIn3Days`, `CrossplaneMemoryBreach30Days`, `CrossplaneEtcdLatencyBreach14Days`) not present in the external file.

**Deployment:**

- In-cluster: `oc apply -f monitoring/prometheus-rules.yaml` (done by `make monitor`)
- External: `make deploy-prom-config` copies the rules file to the external Prometheus host and reloads

## 4. Grafana Dashboard

The dashboard UID is `crossplane-capacity` (title: "Crossplane Capacity Planning"). It has 45 panels organized into 8 rows. Source: `monitoring/grafana-dashboard.json`.

**Template variables:**
- `DS_PROMETHEUS` — Datasource selector (set to your Prometheus datasource UID)
- `source_cluster` — Cluster filter (populated from label values)

### Row: Dashboard Guide (collapsed)

A text panel (`How to Read This Dashboard`) explaining the color scheme and panel layout. Expand on first visit.

### Row: Current Health

Five stat panels showing real-time cluster status at a glance.

| Panel | Metric | Thresholds |
|-------|--------|------------|
| Overall Capacity Status | `crossplane:capacity_status` | 0=green, 1=yellow, 2=red |
| etcd Objects | `crossplane:etcd_object_count:total` | green < 50k, yellow < 100k, red |
| Controller Memory | `crossplane:controller_memory_bytes` | green < 2 GiB, yellow < 5 GiB, red |
| etcd Latency | `crossplane:etcd_request_latency:p99` | green < 100ms, yellow < 500ms, red |
| API Latency | `crossplane:apiserver_request_latency:p99` | green < 1s, yellow < 2s, red |

### Row: When Will We Hit a Limit?

Forecast panels showing when capacity thresholds will be breached.

| Panel | Description |
|-------|-------------|
| Object Count Forecast (7d / 30d) | Time series with `predict_linear()` projections |
| Days to 30k | `crossplane:days_until_object_limit:50k` (mislabeled as 30k in some versions) |
| Days to 100k | `crossplane:days_until_object_limit:100k` |
| Days to Memory Breach | `crossplane:days_until_memory_breach` |
| Days to API Breach | `crossplane:days_until_api_latency_breach` |

### Row: Model Accuracy (collapsed)

Compares actual metrics against power-law model predictions. Use this to assess whether the model is still trustworthy.

| Panel | Description |
|-------|-------------|
| Memory: Actual vs Model | Overlays `controller_memory_bytes` with `predicted_memory_bytes` |
| etcd P99: Actual vs Model | Overlays `etcd_request_latency:p99` with predicted |
| API P99: Actual vs Model | Overlays `apiserver_request_latency:p99` with predicted |
| CPU: Actual vs Model | Overlays `controller_cpu_cores` with predicted |
| Memory/CPU/etcd/API Drift % | Four stat panels showing `crossplane:model_drift:*_pct` |
| Model Health & Confidence | Table summarizing R² values and drift status per metric |

### Row: Predictions at Scale (collapsed)

Fixed-point predictions showing expected metric values at 50k and 100k objects.

| Panel | Description |
|-------|-------------|
| Memory at 50k Objects | `crossplane:predicted_memory_bytes:at_50k` |
| Memory at 100k Objects | `crossplane:predicted_memory_bytes:at_100k` |
| etcd P99 at 50k (ADVISORY) | `crossplane:predicted_etcd_latency_p99_seconds:at_50k` |
| etcd P99 at 100k (ADVISORY) | `crossplane:predicted_etcd_latency_p99_seconds:at_100k` |

### Row: How Many Nodes Do We Need? (collapsed)

Node sizing advisor panels driven by the `node-sizing` rule group.

| Panel | Metric |
|-------|--------|
| Workers Required Now | `crossplane:nodes_required:now` |
| Workers Required (14d) | `crossplane:nodes_required:14d` |
| Workers Required (30d) | `crossplane:nodes_required:30d` |
| Max Supported Objects | `crossplane:max_objects_supported` |
| Max Supported Claims | `crossplane:max_claims_supported` |
| Bottleneck | `crossplane:capacity_bottleneck_code` (1=memory, 2=CPU, 3=etcd) |

### Row: Data Pipeline Health (collapsed)

Monitors the remote-write pipeline itself. Check here first when data looks stale.

| Panel | Description |
|-------|-------------|
| Remote-Write Status | Whether remote-write is sending data (based on `up` or write success metrics) |
| Data Staleness | Time since last data point for key crossplane metrics |
| Active Crossplane Series | Count of active `crossplane:*` series |

### Row: Advanced Diagnostics (collapsed)

Detailed operational metrics for deep troubleshooting. Fourteen panels covering object timelines, per-verb latency breakdowns, error rates, flow control, and per-pod resource usage.

Key panels:
- **Object Count Over Time** — full timeline of `apiserver_storage_objects`
- **Crossplane Resource Counts** — table breaking down counts by resource type
- **Request Duration P99 by Verb** — LIST, GET, PUT, PATCH, DELETE latency
- **API Server 5xx Error Rate** — server-side errors
- **Flow Control Queue Depth** — APF (API Priority and Fairness) saturation
- **Controller Memory/CPU per-pod** — individual pod resource consumption
- **Managed Resources** — Crossplane managed resource count over time

### Importing the dashboard

1. In Grafana, go to **Dashboards > Import**
2. Upload `monitoring/grafana-dashboard.json`
3. Set the `DS_PROMETHEUS` variable to your Prometheus datasource
4. Set `source_cluster` to your cluster name (e.g., `crossplane1`)

## 5. Operating the Stack

### Initial deployment

```bash
# Deploy in-cluster monitoring (ConfigMaps, PrometheusRule CRD)
make monitor

# Deploy rules to external Prometheus
make deploy-prom-config

# Import dashboard to Grafana (manual — see §4)
```

### After model refit

When you run a new load test or when drift alerts fire:

```bash
# Refit power-law models from overnight test data
make refit-models

# Propagate new coefficients to rules files + dashboard JSON
make update-coefficients

# Deploy updated rules to external Prometheus
make deploy-prom-config

# Re-import the updated dashboard JSON to Grafana
```

### Adding a new cluster

1. Configure remote-write on the new cluster's Prometheus with a unique `source_cluster` label (e.g., `crossplane2`)
2. Add the new cluster's remote-write relabel configs (use `monitoring/01-cluster-monitoring-config.yaml` and `02-user-workload-monitoring-config.yaml` as templates)
3. Duplicate the rule groups in `crossplane-rules-self-managed.yml`, changing `crossplane1` to the new cluster name in all `source_cluster` filters
4. The Grafana dashboard's `source_cluster` variable will auto-discover the new cluster

### Troubleshooting

| Symptom | Check |
|---------|-------|
| No data on dashboard | Expand "Data Pipeline Health" row; check Remote-Write Status panel |
| Stale data (no updates) | Check Data Staleness panel; verify cluster Prometheus is running and remote-write URL is reachable |
| Missing metrics | Check `write_relabel_configs` in the cluster monitoring ConfigMaps — the metric may be filtered out |
| Model accuracy poor | Check "Model Accuracy" row; if drift > 30%, refit: `make refit-models && make update-coefficients` |
| Alerts not firing | Verify rules are loaded: `curl -s <PROMETHEUS_URL>/api/v1/rules \| jq '.data.groups[].name'` |
| Dashboard import fails | Ensure the datasource UID matches; update the `DS_PROMETHEUS` variable after import |

## 6. Related Documents

| Document | Description |
|----------|-------------|
| [Capacity Contract](../monitoring/capacity-contract.md) | Alert definitions, routing policy, response procedures |
| [etcd Thresholds](etcd-thresholds.md) | How alert thresholds were derived from load test data |
| [etcd Scaling Guide](crossplane-etcd-scaling-guide.md) | Full technical reference: monitoring, models, scaling strategies |
| [Model Scorecard](../monitoring/capacity-model-scorecard.md) | Per-metric model accuracy tracking |
| [Capacity Report](../report/capacity-report.md) | Generated analysis output with charts |
