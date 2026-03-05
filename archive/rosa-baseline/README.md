# ROSA Baseline Archive

Historical capacity-planning data from a managed OpenShift (ROSA) cluster, collected March 1-2, 2026.

## Cluster Specs

| Property | Value |
|----------|-------|
| Platform | ROSA (Red Hat OpenShift Service on AWS) |
| Region | us-east-2 |
| K8s Version | 1.33.6 |
| Workers | 2 x m5.xlarge (4 vCPU, 16 GiB each) |
| Allocatable/node | 3.5 cores, 14.5 GiB |
| etcd Access | None (managed control plane) |

## Data Collection

- **Method:** Cron growth test — 500 VMDeployment claims per batch, every 15 minutes
- **Duration:** ~29 hours (115 batches attempted)
- **Objects reached:** ~124,500 etcd objects (batch 114)
- **Batch 115 status:** Failed (cluster under stress)

## Model Coefficients (ROSA-fitted)

| Metric | a | b | R² | Confidence |
|--------|---|---|-----|-----------|
| Memory | 2.410e+06 | 0.6758 | 0.9335 | HIGH |
| CPU | 2.410e-03 | 0.5896 | 0.7608 | MEDIUM |
| etcd P99 | 8.139e-04 | 0.5447 | 0.3278 | LOW |
| API P99 | 7.494e-03 | 0.5428 | 0.5184 | LOW |

Valid range: 6,514 — 48,035 etcd objects (37 data points from ramp test).

## Batch Contamination Note

Batch directories 001-005 were overwritten by the crossplane1 overnight test on March 3.
Only the `spot-checks.json` files in those dirs are original ROSA data; `kube-burner.log`
and `metrics.json` were replaced by overnight (crossplane1) data. The spot-checks have been
copied here for archival purposes.

## File Inventory

```
results/
  cron-log.json           # 117 JSONL entries (full cron run)
  cron-stdout.log         # Script stdout
  cron-setup-log.md       # Setup log
  batch-001/ … batch-005/ # spot-checks.json only (ROSA data)
  batch-006/ … batch-115/ # Complete ROSA per-batch data

scripts/
  cron-grow.sh            # Main cron growth driver
  cron-status.sh          # Status checker
  install-cron.sh         # Crontab installer
  stop-cron.sh            # Crontab remover
  analyze-cron-results.py # ROSA-specific analysis script

monitoring/
  crossplane-rules-external.yml  # ROSA recording/alert rules
  prometheus-external.yml         # ROSA federation config
```

## Superseded By

The crossplane1 overnight test (March 3, 2026) on a self-managed OpenShift cluster with
direct etcd access. See the main repository README for current models and thresholds.
