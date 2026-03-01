# Crossplane Capacity Contract

> Auto-generated with sensible defaults. Review and adjust thresholds, owners, and actions for your environment.

## Key Metrics and Limits

| Metric | Recording Rule | Unit | Warning | Critical | Hard Limit | Valid Range (tested) |
|--------|---------------|------|---------|----------|------------|---------------------|
| Controller Memory | `crossplane:controller_memory_bytes` | bytes | 2 GiB | 4 GiB | 6 GiB (OOMKill) | 6,514 – 48,035 objects |
| Controller CPU | `crossplane:controller_cpu_cores` | cores | 1.5 | 3.0 | 4.0 (throttle) | 6,514 – 48,035 objects |
| etcd Request Latency P99 | `crossplane:etcd_request_latency:p99` | seconds | 0.100 | 0.500 | 1.000 | 6,514 – 48,035 objects |
| API Server Request Latency P99 | `crossplane:apiserver_request_latency:p99` | seconds | 1.000 | 2.000 | 5.000 | 6,514 – 48,035 objects |

## Alert Classes

### Symptom Alerts (Page)

Trigger on **current observed state** breaching a threshold. These require immediate human response.

| Alert | Condition | `for` | Severity | Owner | Action |
|-------|-----------|-------|----------|-------|--------|
| Controller Memory Critical | `crossplane:controller_memory_bytes > 4GiB` | 5m | critical | platform-team | Scale controller resources, investigate memory leak |
| Controller Memory Warning | `crossplane:controller_memory_bytes > 2GiB` | 10m | warning | platform-team | Review controller resource limits, check object growth |
| etcd Latency Critical | `crossplane:etcd_request_latency:p99 > 0.5` | 5m | critical | platform-team | Investigate etcd health, reduce write rate |
| etcd Latency Warning | `crossplane:etcd_request_latency:p99 > 0.1` | 10m | warning | platform-team | Monitor, check object count growth |
| API Server Latency Critical | `crossplane:apiserver_request_latency:p99 > 2.0` | 5m | critical | platform-team | Check inflight requests, throttle workload |
| API Server Latency Warning | `crossplane:apiserver_request_latency:p99 > 1.0` | 10m | warning | platform-team | Monitor, review request patterns |

### Forecast Alerts (Warn / Ticket)

Trigger on **projected future breach** within a lead time window. These drive proactive capacity planning, not incident response.

| Alert | Condition | `for` | Severity | Owner | Action | Model Confidence |
|-------|-----------|-------|----------|-------|--------|-----------------|
| Memory Breach in 14 Days | Predicted memory > 4GiB within 14 days | 30m | warning | platform-team | Plan capacity increase, file ticket | High (R²=0.93) |
| Memory Breach in 30 Days | Predicted memory > 4GiB within 30 days | 1h | info | platform-team | Add to sprint planning | High (R²=0.93) |
| etcd Latency Breach in 14 Days | Predicted P99 > 0.1s within 14 days | 30m | info | platform-team | Advisory only — plan scaling or object reduction | **Low (R²=0.33)** — advisory until soak refit |
| Object Count 30k in 14 Days | Projected object count > 30k within 14 days | 30m | warning | platform-team | Review growth rate, plan cleanup | N/A (linear extrapolation) |
| Object Count 100k in 14 Days | Projected object count > 100k within 14 days | 30m | warning | platform-team | Initiate scaling plan | N/A (linear extrapolation) |

> **Note on latency forecasts**: etcd and API server latency models have low R² (0.33 and 0.52 respectively) due to the continuous-ramp test methodology conflating burst effects with steady-state behavior. These forecasts are marked **advisory-only** and downgraded to `info` severity until refitted with stepped-soak test data. Symptom alerts (threshold-based, no model) remain fully active.

## Forecast Valid Range

| Parameter | Value |
|-----------|-------|
| Minimum tested object count | 6,514 |
| Maximum tested object count | 48,035 |
| Extrapolation limit (1.5x max) | 72,000 |
| Hard extrapolation ceiling (2x max) | 96,000 |
| Confidence degrades beyond | 48,035 |
| Predictions above ceiling | Advisory only, not for paging |

## Confidence Classification

| Class | Criteria | Use |
|-------|----------|-----|
| High | Holdout MAPE < 10%, R² > 0.90, RMSE within 1 stddev of data | Safe for forecast alerts |
| Medium | Holdout MAPE < 25%, R² > 0.70 | Warning-level alerts, dashboard display |
| Low | MAPE ≥ 25% or R² < 0.70 | Advisory display only, not for paging |

### Current Model Confidence (as of 2026-02-28)

| Metric | Model | R² | Confidence | Status |
|--------|-------|----|------------|--------|
| Controller Memory | power_law | 0.9335 | High | Active — safe for forecast alerts |
| Controller CPU | power_law | 0.7608 | Medium | Active — warning-level alerts |
| etcd P99 Latency | power_law | 0.3278 | Low | **Advisory only** — awaiting soak refit |
| API Server P99 Latency | power_law | 0.5184 | Low | **Advisory only** — awaiting soak refit |
| Inflight Requests | — | 0.12 | — | **Dropped** — no correlation with object count |
| Error Rate | — | 0.29 | — | **Dropped** — flat zero, not modelable |
| Request Rate | — | 0.44 | — | **Dropped from forecast** — kept as recording rule input |

## Response Owners

| Owner | Scope | Escalation |
|-------|-------|------------|
| platform-team | All capacity alerts, forecast review | Escalate critical pages to on-call SRE |
| sre-oncall | Critical symptom pages after hours | Engage platform-team next business day |

## Response Actions by Severity

### Critical (Page)
1. Acknowledge within 15 minutes
2. Identify root cause (runaway reconciliation, object leak, etcd degradation)
3. Mitigate: scale resources, pause reconciliation, or reduce object count
4. Post-incident: update capacity contract thresholds if needed

### Warning (Ticket)
1. Review within 1 business day
2. Assess growth trend and forecast confidence
3. Plan capacity adjustment for next maintenance window
4. Update forecasting models if drift detected

### Info (Dashboard)
1. Review during weekly capacity review
2. No immediate action required
3. Track trend for planning purposes
