.PHONY: setup test-small test-full monitor analyze clean all help \
       deploy-self-managed monitor-self-managed clean-self-managed status-self-managed \
       refit-models update-coefficients deploy-prom-config \
       etcd-baseline etcd-tune etcd-verify etcd-defrag etcd-status compare-results

SHELL := /bin/bash
PROJECT_DIR := $(shell pwd)
KUBE_BURNER := kube-burner
KUBE_BURNER_OCP := kube-burner-ocp
REMOTE_PROM_URL ?= http://prometheus.example.com:9090
PROMETHEUS_URL ?= $(shell kubectl get route -n openshift-monitoring thanos-querier -o jsonpath='{.spec.host}' 2>/dev/null || echo "")
PROMETHEUS_TOKEN ?= $(shell kubectl create token prometheus-k8s -n openshift-monitoring 2>/dev/null || echo "")
METRICS_DIR := $(PROJECT_DIR)/kube-burner/collected-metrics
REPORT_DIR := $(PROJECT_DIR)/report

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Setup ───────────────────────────────────────────────────────────

setup: ## Install Crossplane, provider-nop, functions, XRDs, compositions
	@echo "=== Setting up Crossplane capacity planning ==="
	bash setup/install.sh

# ─── Monitoring ──────────────────────────────────────────────────────

monitor: ## Enable user workload monitoring, remote write, and deploy PrometheusRule
	@echo "=== Deploying monitoring ==="
	@echo "--- Enabling user workload monitoring ---"
	kubectl apply -f monitoring/01-cluster-monitoring-config.yaml
	@echo "Waiting for user-workload-monitoring namespace..."
	@for i in $$(seq 1 60); do \
		kubectl get namespace openshift-user-workload-monitoring &>/dev/null && break; \
		sleep 5; \
	done
	kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=prometheus -n openshift-user-workload-monitoring --timeout=300s 2>/dev/null || true
	@echo ""
	@echo "--- Configuring remote write to $(REMOTE_PROM_URL) ---"
	kubectl apply -f monitoring/02-user-workload-monitoring-config.yaml
	@echo ""
	@echo "--- Deploying PrometheusRule to crossplane-loadtest namespace ---"
	kubectl apply -f monitoring/prometheus-rules.yaml
	@echo ""
	@echo "Monitoring deployed."
	@echo "  - User workload monitoring: enabled"
	@echo "  - Remote write: $(REMOTE_PROM_URL)/api/v1/write"
	@echo "  - PrometheusRule: crossplane-loadtest/crossplane-capacity-planning"
	@echo "  - Dashboard JSON: import monitoring/grafana-dashboard.json into your remote Grafana"

# ─── Load Tests ──────────────────────────────────────────────────────

test-small: ## Smoke test: 100 VM claims (~800 etcd objects)
	@echo "=== Running smoke test (100 VM claims) ==="
	@mkdir -p $(METRICS_DIR)
	$(KUBE_BURNER) init \
		--config kube-burner/config.yaml \
		--metrics-profile kube-burner/metrics-profile.yaml \
		--alert-profile kube-burner/alerts-profile.yaml \
		--prometheus-url "https://$(PROMETHEUS_URL)" \
		--token "$(PROMETHEUS_TOKEN)" \
		--log-level info \
		--skip-tls-verify \
		--job-name crossplane-ramp-100
	@echo ""
	@echo "Smoke test complete. Check $(METRICS_DIR) for results."
	@echo "Run 'make analyze' to generate capacity report."

test-full: ## Full load test: ramp to ~12,500 claims (~100k etcd objects)
	@echo "=== Running full load test ==="
	@echo "WARNING: This will create ~100k etcd objects. Ensure cluster can handle the load."
	@echo "Press Ctrl+C within 10 seconds to cancel..."
	@sleep 10
	@mkdir -p $(METRICS_DIR)
	$(KUBE_BURNER) init \
		--config kube-burner/config.yaml \
		--metrics-profile kube-burner/metrics-profile.yaml \
		--alert-profile kube-burner/alerts-profile.yaml \
		--prometheus-url "https://$(PROMETHEUS_URL)" \
		--token "$(PROMETHEUS_TOKEN)" \
		--log-level info \
		--skip-tls-verify
	@echo ""
	@echo "Full load test complete. Check $(METRICS_DIR) for results."
	@echo "Run 'make analyze' to generate capacity report."

# ─── Analysis ────────────────────────────────────────────────────────

analyze: ## Run Python analysis on kube-burner results
	@echo "=== Running capacity analysis ==="
	@if [ ! -d "$(METRICS_DIR)" ] || [ -z "$$(ls -A $(METRICS_DIR) 2>/dev/null)" ]; then \
		echo "ERROR: No metrics data found in $(METRICS_DIR)"; \
		echo "Run 'make test-small' or 'make test-full' first."; \
		exit 1; \
	fi
	cd analysis && python3 analyze.py \
		--metrics-dir "$(METRICS_DIR)" \
		--output-dir "$(REPORT_DIR)"
	@echo ""
	@echo "Report: $(REPORT_DIR)/capacity-report.md"

venv: ## Create Python virtual environment for analysis
	python3 -m venv .venv
	.venv/bin/pip install -r analysis/requirements.txt
	@echo "Virtual environment created. Activate with: source .venv/bin/activate"

# ─── Cleanup ─────────────────────────────────────────────────────────

clean: ## Delete all test resources (claims, XRs, NopResources)
	@echo "=== Cleaning up test resources ==="
	@echo "Deleting all claims in crossplane-loadtest namespace..."
	-kubectl delete vmdeployments.capacity.crossplane.io --all -n crossplane-loadtest --timeout=120s 2>/dev/null
	-kubectl delete disks.capacity.crossplane.io --all -n crossplane-loadtest --timeout=120s 2>/dev/null
	-kubectl delete dnszones.capacity.crossplane.io --all -n crossplane-loadtest --timeout=120s 2>/dev/null
	-kubectl delete firewallrulesets.capacity.crossplane.io --all -n crossplane-loadtest --timeout=120s 2>/dev/null
	@echo "Waiting for NopResources to be garbage collected..."
	@sleep 10
	-kubectl delete nopresources.nop.crossplane.io --all -n crossplane-loadtest --timeout=120s 2>/dev/null
	@echo "Cleanup complete."

clean-all: clean ## Full cleanup: remove Crossplane, monitoring, namespace
	@echo "=== Full cleanup ==="
	-kubectl delete -f monitoring/prometheus-rules.yaml 2>/dev/null
	-kubectl delete -f monitoring/02-user-workload-monitoring-config.yaml 2>/dev/null
	-kubectl delete -f compositions/ 2>/dev/null
	-kubectl delete -f xrds/ 2>/dev/null
	-kubectl delete -f setup/02-provider-config.yaml 2>/dev/null
	-kubectl delete -f setup/03-functions.yaml 2>/dev/null
	-kubectl delete -f setup/01-provider-nop.yaml 2>/dev/null
	-kubectl delete namespace crossplane-loadtest 2>/dev/null
	-helm uninstall crossplane -n crossplane-system 2>/dev/null
	-kubectl delete namespace crossplane-system 2>/dev/null
	@echo "Full cleanup complete."

# ─── Status ──────────────────────────────────────────────────────────

status: ## Show current state of Crossplane and test resources
	@echo "=== Cluster Status ==="
	@echo ""
	@echo "--- Crossplane ---"
	-@kubectl get deployment -n crossplane-system 2>/dev/null || echo "Crossplane not installed"
	@echo ""
	@echo "--- Providers ---"
	-@kubectl get providers 2>/dev/null || echo "No providers"
	@echo ""
	@echo "--- Functions ---"
	-@kubectl get functions 2>/dev/null || echo "No functions"
	@echo ""
	@echo "--- XRDs ---"
	-@kubectl get xrds 2>/dev/null | grep capacity || echo "No capacity XRDs"
	@echo ""
	@echo "--- Claims in crossplane-loadtest ---"
	-@kubectl get vmdeployments,disks,dnszones,firewallrulesets -n crossplane-loadtest 2>/dev/null | head -20 || echo "No claims"
	@echo ""
	@echo "--- Object Count ---"
	-@kubectl get --raw /metrics 2>/dev/null | grep etcd_object_counts || echo "Cannot read metrics directly"

count: ## Count current etcd objects
	@echo "=== Object Counts ==="
	@echo "Total K8s objects (via API):"
	@kubectl get --all-namespaces all -o name 2>/dev/null | wc -l || echo "unknown"
	@echo ""
	@echo "Crossplane resources:"
	-@kubectl get nopresources -A 2>/dev/null | tail -1 || echo "0 NopResources"
	-@kubectl get xvmdeployments -A 2>/dev/null | tail -1 || echo "0 XVMDeployments"
	-@kubectl get xdisks -A 2>/dev/null | tail -1 || echo "0 XDisks"
	-@kubectl get xdnszones -A 2>/dev/null | tail -1 || echo "0 XDNSZones"
	-@kubectl get xfirewallrulesets -A 2>/dev/null | tail -1 || echo "0 XFirewallRuleSets"

# ─── Orchestration ───────────────────────────────────────────────────

all: setup monitor test-full analyze ## Full pipeline: setup → monitor → test → analyze
	@echo ""
	@echo "=== Full pipeline complete ==="
	@echo "Report: $(REPORT_DIR)/capacity-report.md"

# ─── Self-Managed (crossplane1) ─────────────────────────────────────

PROM_HOST ?= prometheus.example.com
PROM_CONFIG_DIR := /home/fedora/awesome-compose/prometheus-grafana/prometheus

deploy-self-managed: ## Run full overnight pipeline on self-managed cluster
	@echo "=== Deploying overnight test on self-managed cluster ==="
	@if [ ! -f scripts/self-managed-env.sh ]; then \
		echo "ERROR: scripts/self-managed-env.sh not found."; \
		echo "Copy from template and fill in credentials:"; \
		echo "  cp scripts/self-managed-env.sh.example scripts/self-managed-env.sh"; \
		exit 1; \
	fi
	@mkdir -p results
	bash scripts/deploy-self-managed.sh

monitor-self-managed: ## Apply just the monitoring configs on self-managed cluster
	@echo "=== Deploying monitoring on self-managed cluster ==="
	@if [ -f scripts/self-managed-env.sh ]; then \
		source scripts/self-managed-env.sh && \
		oc login --username="$$CLUSTER_USERNAME" --password="$$CLUSTER_PASSWORD" --server="$$CLUSTER_API_URL" --insecure-skip-tls-verify=true; \
	fi
	kubectl apply -f monitoring/self-managed/cluster-monitoring-config.yaml
	kubectl apply -f monitoring/self-managed/user-workload-monitoring-config.yaml
	kubectl apply -f monitoring/prometheus-rules.yaml
	@echo ""
	@echo "Monitoring configs applied. Waiting for user-workload-monitoring pods..."
	kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=prometheus -n openshift-user-workload-monitoring --timeout=300s 2>/dev/null || true
	@echo "Done."

clean-self-managed: ## Clean test resources on self-managed cluster
	@echo "=== Cleaning overnight test resources on self-managed cluster ==="
	@if [ -f scripts/self-managed-env.sh ]; then \
		source scripts/self-managed-env.sh && \
		oc login --username="$$CLUSTER_USERNAME" --password="$$CLUSTER_PASSWORD" --server="$$CLUSTER_API_URL" --insecure-skip-tls-verify=true; \
	fi
	-kubectl delete vmdeployments.capacity.crossplane.io --all -n crossplane-loadtest --timeout=120s 2>/dev/null
	@echo "Waiting for NopResources to be garbage collected..."
	@sleep 10
	-kubectl delete nopresources.nop.crossplane.io --all -n crossplane-loadtest --timeout=120s 2>/dev/null
	@echo "Cleanup complete."

status-self-managed: ## Show overnight test status on self-managed cluster
	@echo "=== Self-Managed Overnight Test Status ==="
	@echo ""
	@echo "--- Tracking Log (last 5 entries) ---"
	@if [ -f results/overnight-log.json ]; then \
		tail -5 results/overnight-log.json | python3 -m json.tool --no-ensure-ascii 2>/dev/null || tail -5 results/overnight-log.json; \
	else \
		echo "No tracking log yet (results/overnight-log.json)."; \
	fi
	@echo ""
	@echo "--- External Prometheus (crossplane1 metrics) ---"
	@curl -s "$(REMOTE_PROM_URL)/api/v1/query?query=crossplane:etcd_object_count:total{source_cluster=\"crossplane1\"}" 2>/dev/null \
		| python3 -c "import sys,json; r=json.load(sys.stdin); d=r.get('data',{}).get('result',[]); print(f'  Object count: {d[0][\"value\"][1]}') if d else print('  No data yet.')" \
		2>/dev/null || echo "  Cannot reach external Prometheus."
	@curl -s "$(REMOTE_PROM_URL)/api/v1/query?query=crossplane:controller_memory_bytes{source_cluster=\"crossplane1\"}" 2>/dev/null \
		| python3 -c "import sys,json; r=json.load(sys.stdin); d=r.get('data',{}).get('result',[]); v=float(d[0]['value'][1]) if d else 0; print(f'  Controller memory: {v/1e6:.1f} MB') if d else print('  No memory data yet.')" \
		2>/dev/null || echo "  Cannot reach external Prometheus."
	@curl -s "$(REMOTE_PROM_URL)/api/v1/query?query=crossplane:etcd_db_size_bytes{source_cluster=\"crossplane1\"}" 2>/dev/null \
		| python3 -c "import sys,json; r=json.load(sys.stdin); d=r.get('data',{}).get('result',[]); v=float(d[0]['value'][1]) if d else 0; print(f'  etcd DB size: {v/1e6:.1f} MB') if d else print('  No etcd DB size data yet.')" \
		2>/dev/null || echo "  Cannot reach external Prometheus."
	@echo ""
	@echo "--- Kill Switch ---"
	@if [ -f scripts/.stop-test ]; then \
		echo "  ACTIVE — test will stop after current batch"; \
	else \
		echo "  Not set (touch scripts/.stop-test to stop)"; \
	fi

refit-models: ## Refit capacity models from overnight data
	python3 scripts/refit-models.py

update-coefficients: ## Update model coefficients from refit output
	python3 scripts/update-coefficients.py --apply

deploy-prom-config: ## Deploy Prometheus config files to external Prometheus (PROM_HOST)
	@echo "=== Deploying Prometheus config to $(PROM_HOST) ==="
	scp monitoring/crossplane-rules-self-managed.yml fedora@$(PROM_HOST):$(PROM_CONFIG_DIR)/crossplane-rules-self-managed.yml
	scp monitoring/prometheus-self-managed.yml fedora@$(PROM_HOST):$(PROM_CONFIG_DIR)/prometheus.yml
	@echo "Reloading Prometheus..."
	curl -s -X POST http://$(PROM_HOST):9090/-/reload || ssh fedora@$(PROM_HOST) 'docker kill --signal=SIGHUP $$(docker ps -q --filter name=prometheus)'
	@echo ""
	@echo "Config deployed. Verify at http://$(PROM_HOST):9090/config"

# ─── etcd Tuning ────────────────────────────────────────────────────

etcd-baseline: ## Capture baseline etcd config and metrics
	bash scripts/etcd-tuning.sh baseline

etcd-tune: ## Apply etcd tuning (compaction=1m, quota=8GiB, snapshot-count=25k)
	bash scripts/etcd-tuning.sh tune

etcd-verify: ## Verify etcd tuning was applied
	bash scripts/etcd-tuning.sh verify

etcd-defrag: ## Run etcd defragmentation on all members
	bash scripts/etcd-tuning.sh defrag

etcd-status: ## Show current etcd status
	bash scripts/etcd-tuning.sh status

compare-results: ## Compare baseline vs tuned overnight results
	python3 scripts/compare-overnight-results.py
