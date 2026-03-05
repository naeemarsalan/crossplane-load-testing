# Crossplane etcd Capacity Planning

A framework for capacity planning Crossplane workloads on Kubernetes. Uses load testing with mock resources (provider-nop) to build predictive models for memory, CPU, and API latency as a function of etcd object count — then deploys those models as Prometheus recording rules and a Grafana dashboard for continuous monitoring.

![Crossplane Memory vs Object Count](report/charts/crossplaneMemory.png)

## Key Findings

- **1 Crossplane VMDeployment claim = 8 etcd objects** (6 NopResources + 1 XR + 1 Claim)
- **106k+ objects sustained for 9+ hours** on a 3-master + 3-worker self-managed OpenShift cluster (crossplane1)
- **Memory is the binding constraint** — controller hits 5 GiB critical at ~66,048 objects (~8,256 VM claims)
- **Memory and CPU models are highly predictable** (R²=0.94); latency models are advisory-only
- **etcd and API latency remained healthy** throughout — 32ms etcd P99, 74ms API P99 at 106k objects
- **Pipeline fully automated**: `make setup && make monitor && make deploy-self-managed`

## Documentation

| Document | Audience | Description |
|----------|----------|-------------|
| [etcd Scaling Guide](docs/crossplane-etcd-scaling-guide.md) | SREs, platform engineers | Full technical reference: why etcd is the bottleneck, monitoring, predictive models, scaling strategies, node sizing |
| [etcd Capacity Planning Guide](docs/etcd-capacity-planning-guide.md) | Platform engineers | Vendor-neutral etcd capacity planning for Crossplane on any Kubernetes platform |
| [etcd Thresholds](docs/etcd-thresholds.md) | SREs | How alert thresholds were derived from load test data, guidance for setting your own |
| [Monitoring Guide](docs/monitoring-guide.md) | Platform engineers | Full monitoring stack walkthrough: remote-write pipeline, 8 Prometheus rule groups, Grafana dashboard, operations |
| [Capacity Contract](monitoring/capacity-contract.md) | SREs, on-call | Alert definitions, thresholds, routing policy, silencing guidelines, response procedures |
| [Model Scorecard](monitoring/capacity-model-scorecard.md) | Data/platform engineers | Per-metric model accuracy tracking |
| [Capacity Report](report/capacity-report.md) | All | Generated analysis output with charts |

## Quick Start

```bash
# Install Crossplane, provider-nop, XRDs, compositions
make setup

# Deploy monitoring (remote-write, recording rules, dashboard)
make monitor

# Run overnight load test on self-managed cluster
make deploy-self-managed

# Refit models from test data and update all files
make refit-models && make update-coefficients

# Deploy updated config to external Prometheus
make deploy-prom-config
```

See `make help` for all available targets.

### Prerequisites

- OpenShift cluster with 2+ worker nodes (self-managed or managed)
- `oc` / `kubectl` CLI, authenticated
- [kube-burner](https://kube-burner.io/) installed
- Python 3.10+ with pip
- External Prometheus instance (remote-write receiver)
- Grafana instance (for dashboards)

Copy `.env.example` to `.env` and fill in `CLUSTER_API_URL`, `CLUSTER_USERNAME`, `CLUSTER_PASSWORD`, `PROM_URL`, and `GRAFANA_URL`.

## Project Structure

```
crossplabn/
├── setup/           # Crossplane install (idempotent)
├── xrds/            # 4 XRDs: VMDeployment, Disk, DNSZone, FirewallRuleSet
├── compositions/    # 4 Compositions (Pipeline mode)
├── kube-burner/     # Load test configs, metrics profile, claim templates
├── monitoring/      # Grafana dashboard, Prometheus rules, capacity contract
├── analysis/        # Python: capacity_model.py, capacity_calculator.py, report_generator.py
├── scripts/         # deploy-self-managed.sh, refit-models.py, update-coefficients.py
├── results/         # Overnight test data (67 data points, 18k–116k objects)
├── docs/            # Technical guides and threshold documentation
├── report/          # Generated capacity report + charts
└── archive/         # Historical data
    └── rosa-baseline/
```

## Previous Baseline (ROSA)

Initial models were fitted on a 2-node m5.xlarge ROSA cluster (Mar 2026). ROSA showed API latency as the bottleneck at ~29,500 objects — significantly lower than self-managed due to the shared control plane. The self-managed cluster's dedicated masters handle 2x+ the capacity. ROSA data is preserved in [`archive/rosa-baseline/`](archive/rosa-baseline/) for reference.

---

This project is provided as-is for educational and capacity planning purposes.
