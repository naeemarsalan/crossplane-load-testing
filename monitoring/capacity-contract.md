# Crossplane Capacity Contract

> Auto-generated with sensible defaults. Review and adjust thresholds, owners, and actions for your environment.

## Key Metrics and Limits

| Metric | Recording Rule | Unit | Warning | Critical | Hard Limit | Valid Range (tested) |
|--------|---------------|------|---------|----------|------------|---------------------|
| Controller Memory | `crossplane:controller_memory_bytes` | bytes | 2 GiB | 5 GiB | 6 GiB (OOMKill) | 18,047 – 115,856 objects |
| Controller CPU | `crossplane:controller_cpu_cores` | cores | 1.5 | 3.0 | 4.0 (throttle) | 18,047 – 115,856 objects |
| etcd Request Latency P99 | `crossplane:etcd_request_latency:p99` | seconds | 0.100 | 0.500 | 1.000 | 18,047 – 115,856 objects |
| API Server Request Latency P99 | `crossplane:apiserver_request_latency:p99` | seconds | 1.000 | 2.000 | 5.000 | 18,047 – 115,856 objects |
| Object Count | `crossplane:etcd_object_count:total` | objects | 50,000 | 100,000 | — | 18,047 – 115,856 objects |

## Alert Classes

### Symptom Alerts (Page)

Trigger on **current observed state** breaching a threshold. These require immediate human response.

| Alert Name | Condition | `for` | Severity | Owner | Action |
|------------|-----------|-------|----------|-------|--------|
| CrossplaneControllerMemoryCritical | `crossplane:controller_memory_bytes > 5GiB` | 5m | critical | platform-team | Scale controller resources, investigate memory leak |
| CrossplaneControllerMemoryWarning | `crossplane:controller_memory_bytes > 2GiB` | 10m | warning | platform-team | Review controller resource limits, check object growth |
| CrossplaneEtcdLatencyCritical | `crossplane:etcd_request_latency:p99 > 0.5` | 5m | critical | platform-team | Investigate etcd health, reduce write rate |
| CrossplaneEtcdLatencyWarning | `crossplane:etcd_request_latency:p99 > 0.1` | 10m | warning | platform-team | Monitor, check object count growth |
| CrossplaneApiServerLatencyCritical | `crossplane:apiserver_request_latency:p99 > 2.0` | 5m | critical | platform-team | Check inflight requests, throttle workload |
| CrossplaneApiServerLatencyWarning | `crossplane:apiserver_request_latency:p99 > 1.0` | 10m | warning | platform-team | Monitor, review request patterns |
| CrossplaneObjectCountWarning | `crossplane:etcd_object_count:total > 50000` | 5m | warning | platform-team | Monitor API latency for degradation |
| CrossplaneObjectCountCritical | `crossplane:etcd_object_count:total > 100000` | 5m | critical | platform-team | etcd approaching capacity limits |
| CrossplaneRapidObjectGrowth | `crossplane:object_growth_rate:per_day > 24000` | 15m | warning | platform-team | Verify growth is expected |

### Forecast Alerts (Warn / Ticket)

Trigger on **projected future breach** within a lead time window. These drive proactive capacity planning, not incident response.

| Alert Name | Condition | `for` | Severity | Owner | Action | Model Confidence |
|------------|-----------|-------|----------|-------|--------|-----------------|
| CrossplaneMemoryBreach14Days | Predicted memory > 5GiB within 14 days | 30m | warning | platform-team | Plan capacity increase, file ticket | High (R²=0.94) |
| CrossplaneMemoryBreach30Days | Predicted memory > 5GiB within 30 days | 1h | info | platform-team | Add to sprint planning | High (R²=0.94) |
| CrossplaneEtcdLatencyBreach14Days | Predicted P99 > 0.1s within 14 days | 30m | info | platform-team | Advisory only — plan scaling or object reduction | **Low (R²=0.59)** — advisory only |
| CrossplaneObjectCount50kIn14Days | Projected object count > 50k within 14 days | 30m | warning | platform-team | Review growth rate, plan cleanup | N/A (linear extrapolation) |
| CrossplaneObjectCount100kIn14Days | Projected object count > 100k within 14 days | 30m | warning | platform-team | Initiate scaling plan | N/A (linear extrapolation) |

> **Note on latency forecasts**: etcd and API server latency models have low R² (0.59 and 0.48 respectively). The overnight test showed healthy latencies even at 106k objects, so these metrics don't strongly correlate with object count in the tested range. These forecasts are marked **advisory-only** and downgraded to `info` severity. Symptom alerts (threshold-based, no model) remain fully active.

### Drift Alerts

Trigger when **actual metrics diverge from model predictions** beyond tolerance for a sustained period. Indicates the model needs refitting.

| Alert Name | Condition | `for` | Severity | Owner | Action |
|------------|-----------|-------|----------|-------|--------|
| CrossplaneMemoryModelDrift | > 30% deviation from predicted | 30m | warning | platform-team | Refit capacity models |
| CrossplaneEtcdLatencyModelDrift | > 50% deviation from predicted | 30m | warning | platform-team | Refit capacity models |

## Forecast Valid Range

| Parameter | Value |
|-----------|-------|
| Minimum tested object count | 18,047 |
| Maximum tested object count | 115,856 |
| Extrapolation limit (1.5x max) | 174,000 |
| Hard extrapolation ceiling (2x max) | 232,000 |
| Confidence degrades beyond | 115,856 |
| Predictions above ceiling | Advisory only, not for paging |

## Confidence Classification

| Class | Criteria | Use |
|-------|----------|-----|
| High | Holdout MAPE < 10%, R² > 0.90, RMSE within 1 stddev of data | Safe for forecast alerts |
| Medium | Holdout MAPE < 25%, R² > 0.70 | Warning-level alerts, dashboard display |
| Low | MAPE ≥ 25% or R² < 0.70 | Advisory display only, not for paging |

### Current Model Confidence (as of 2026-03-03)

| Metric | Model | R² | Confidence | Status |
|--------|-------|----|------------|--------|
| Controller Memory | power_law | 0.9389 | High | Active — safe for forecast alerts |
| Controller CPU | power_law | 0.9425 | High | Active — safe for forecast alerts |
| etcd P99 Latency | power_law | 0.5872 | Low | **Advisory only** — healthy at 106k objects |
| API Server P99 Latency | power_law | 0.4779 | Low | **Advisory only** — healthy at 106k objects |
| etcd DB Size | power_law | 0.9302 | Low | Active — monotonic increase tracked |
| WAL Fsync P99 | power_law | 0.5071 | Low | **Advisory only** — healthy at 106k objects |

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

## Routing Policy

### Critical Symptom Alerts
1. Route to PagerDuty immediately
2. Notify Slack #alerts-critical
3. On-call SRE acknowledges within 15 minutes
4. Escalate to platform-team lead if unacknowledged after 30 minutes

### Warning Symptom Alerts
1. Notify Slack #alerts-warning
2. Review within 1 hour during business hours
3. Create tracking ticket if sustained > 2 hours

### Forecast Alerts (warning)
1. Create Jira ticket automatically
2. Notify Slack #capacity-planning
3. Review in next capacity planning session
4. Platform-team plans response for next maintenance window

### Forecast Alerts (info)
1. Log for weekly review
2. No immediate notification required

### Drift Alerts
1. Notify Slack #capacity-planning
2. Create ticket to refit models
3. Review model scorecard and recent data
4. Schedule refit in next maintenance window

## Silencing Guidelines

| Scenario | Duration | Approval |
|----------|----------|----------|
| Planned load test | Test duration + 1h | Platform-team lead |
| Known maintenance | Maintenance window | On-call SRE |
| Model refit in progress | Until refit completes | Platform-team |
| False positive investigation | 4h max | On-call SRE |
