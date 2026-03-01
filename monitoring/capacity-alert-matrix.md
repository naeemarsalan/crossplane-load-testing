# Capacity Alert Matrix

> Auto-generated with defaults. Review and adjust routing, owners, and actions for your environment.

## Alert Classification

| Class | Purpose | Response Time | Notification Channel |
|-------|---------|--------------|---------------------|
| **Symptom** (page) | Current state breaches threshold | Immediate (< 15 min) | PagerDuty / Slack #alerts-critical |
| **Forecast** (warn/ticket) | Projected breach within lead time | 1 business day | Jira ticket / Slack #capacity-planning |
| **Drift** (warn) | Model diverges from reality | 1 business day | Slack #capacity-planning |

## Symptom Alerts (Page)

These fire on **current observed state**. Require immediate human investigation.

| Alert Name | Metric | Threshold | `for` | Severity | Owner | Immediate Action |
|------------|--------|-----------|-------|----------|-------|-----------------|
| CrossplaneControllerMemoryCritical | `crossplane:controller_memory_bytes` | > 4 GiB | 5m | critical | platform-team | Scale controller resources; investigate memory leak |
| CrossplaneControllerMemoryWarning | `crossplane:controller_memory_bytes` | > 2 GiB | 10m | warning | platform-team | Review resource limits, check object growth |
| CrossplaneEtcdLatencyCritical | `crossplane:etcd_request_latency:p99` | > 500ms | 5m | critical | platform-team | Investigate etcd health, reduce write rate |
| CrossplaneEtcdLatencyWarning | `crossplane:etcd_request_latency:p99` | > 100ms | 10m | warning | platform-team | Monitor, check object count growth |
| CrossplaneApiServerLatencyCritical | `crossplane:apiserver_request_latency:p99` | > 2s | 5m | critical | platform-team | Check inflight requests, throttle workload |
| CrossplaneApiServerLatencyWarning | `crossplane:apiserver_request_latency:p99` | > 1s | 10m | warning | platform-team | Monitor, review request patterns |
| CrossplaneObjectCountWarning | `crossplane:etcd_object_count:total` | > 30,000 | 5m | warning | platform-team | Monitor API latency for degradation |
| CrossplaneObjectCountCritical | `crossplane:etcd_object_count:total` | > 80,000 | 5m | critical | platform-team | etcd approaching capacity limits |
| CrossplaneRapidObjectGrowth | `crossplane:object_growth_rate:per_day` | > 24,000/day | 15m | warning | platform-team | Verify growth is expected |

## Forecast Alerts (Warn / Ticket)

These fire on **projected future breach**. Drive proactive capacity planning, not incident response.

| Alert Name | Condition | `for` | Severity | Owner | Action |
|------------|-----------|-------|----------|-------|--------|
| CrossplaneObjectCount30kIn14Days | 30k threshold in < 14 days | 30m | warning | platform-team | Plan cleanup or scaling |
| CrossplaneObjectCount30kIn3Days | 30k threshold in < 3 days | 15m | critical | platform-team | Immediate scaling action |
| CrossplaneObjectCount100kIn14Days | 100k threshold in < 14 days | 30m | warning | platform-team | Initiate scaling plan |
| CrossplaneMemoryBreach14Days | Predicted memory > 4 GiB in 14 days | 30m | warning | platform-team | Plan capacity increase |
| CrossplaneMemoryBreach30Days | Predicted memory > 4 GiB in 30 days | 1h | info | platform-team | Add to sprint planning |
| CrossplaneEtcdLatencyBreach14Days | Predicted P99 > 100ms in 14 days | 30m | warning | platform-team | Plan scaling or object reduction |

## Drift Alerts

These fire when **actual metrics diverge from model predictions** beyond tolerance for a sustained period. Indicates the model needs refitting.

| Alert Name | Condition | `for` | Severity | Owner | Action |
|------------|-----------|-------|----------|-------|--------|
| CrossplaneMemoryModelDrift | > 30% deviation from predicted | 30m | warning | platform-team | Refit capacity models |
| CrossplaneEtcdLatencyModelDrift | > 50% deviation from predicted | 30m | warning | platform-team | Refit capacity models |

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
