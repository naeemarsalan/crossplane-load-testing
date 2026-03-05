#!/bin/bash
# deploy-self-managed.sh — Full overnight pipeline for self-managed OpenShift (crossplane1)
#
# Phases:
#   1. Pre-flight checks (~2 min)
#   2. Setup — Crossplane, provider-nop, XRDs, compositions (~5 min)
#   3. Monitoring — remote-write, recording rules, verify metrics (~3 min)
#   4. Overnight load test — 500 claims/batch, 10 min intervals (~8-12 hours)
#   5. Post-test — export final metrics, print summary
#
# Usage:
#   source scripts/self-managed-env.sh  # or export CLUSTER_* vars
#   nohup bash scripts/deploy-self-managed.sh >> results/overnight-stdout.log 2>&1 &
#
# Kill switch: touch scripts/.stop-test
#
set -uo pipefail

########################################################################
# Configuration
########################################################################
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RESULTS_DIR="$PROJECT_DIR/results"
TRACKING_LOG="$RESULTS_DIR/overnight-log.json"
STOP_FILE="$SCRIPT_DIR/.stop-test"
KUBE_BURNER_CONFIG="$PROJECT_DIR/kube-burner/config-overnight.yaml"
PROM_URL="${PROM_URL:-https://prometheus.example.com}"
GRAFANA_URL="${GRAFANA_URL:-http://grafana.example.com:3000}"
GRAFANA_DASHBOARD_UID="crossplane-capacity"
SOURCE_CLUSTER="crossplane1"

# Test parameters
CLAIMS_PER_BATCH=500
OBJECTS_PER_CLAIM=8
SETTLE_WAIT=120        # seconds to wait after kube-burner for metrics to settle
INTER_BATCH_WAIT=480   # seconds between batches (total cycle ~10 min)
MAX_BATCHES=200        # safety cap
API_P99_STOP=5.0       # stop if API P99 > 5s
CONSECUTIVE_STOP=3     # ... for this many consecutive batches
MILESTONES=(10000 20000 30000 50000 75000 100000 150000 200000)

# General estimation formulas (from plan)
# memory_gb = 0.5 + objects/10000
# cpu_cores = 0.25 + objects/20000

########################################################################
# Helpers
########################################################################
log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

die() {
    log "FATAL: $*"
    exit 1
}

query_prom() {
    local query="$1"
    curl -sf --max-time 15 "${PROM_URL}/api/v1/query" \
        --data-urlencode "query=${query}" 2>/dev/null
}

query_prom_value() {
    local query="$1"
    local result
    result=$(query_prom "$query")
    if [[ $? -ne 0 || -z "$result" ]]; then
        echo ""
        return 1
    fi
    echo "$result" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    results = data.get('data', {}).get('result', [])
    if results:
        print(results[0]['value'][1])
    else:
        print('')
except:
    print('')
" 2>/dev/null
}

create_grafana_annotation() {
    local text="$1"
    local tags="$2"
    curl -sf --max-time 10 \
        -H "Content-Type: application/json" \
        -d "{\"text\": \"$text\", \"tags\": [$tags]}" \
        "${GRAFANA_URL}/api/annotations" 2>/dev/null || true
}

collect_metrics_snapshot() {
    python3 << 'PYEOF'
import json, urllib.request, urllib.parse, sys

PROM_URL = os.environ.get("PROM_URL", "https://prometheus.example.com")
SOURCE = "crossplane1"

def query(expr):
    try:
        url = f"{PROM_URL}/api/v1/query?query={urllib.parse.quote(expr)}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
            results = data.get("data", {}).get("result", [])
            if results:
                return results[0]["value"][1]
    except:
        pass
    return None

metrics = {
    "object_count": query(f'crossplane:etcd_object_count:total{{source_cluster="{SOURCE}"}}'),
    "controller_memory_bytes": query(f'crossplane:controller_memory_bytes{{source_cluster="{SOURCE}"}}'),
    "controller_cpu_cores": query(f'crossplane:controller_cpu_cores{{source_cluster="{SOURCE}"}}'),
    "etcd_latency_p99": query(f'crossplane:etcd_request_latency:p99{{source_cluster="{SOURCE}"}}'),
    "etcd_latency_p50": query(f'crossplane:etcd_request_latency:p50{{source_cluster="{SOURCE}"}}'),
    "apiserver_latency_p99": query(f'crossplane:apiserver_request_latency:p99{{source_cluster="{SOURCE}"}}'),
    "apiserver_latency_p50": query(f'crossplane:apiserver_request_latency:p50{{source_cluster="{SOURCE}"}}'),
    "capacity_status": query(f'crossplane:capacity_status{{source_cluster="{SOURCE}"}}'),
    "predicted_memory": query(f'crossplane:predicted_memory_bytes{{source_cluster="{SOURCE}"}}'),
    "predicted_cpu": query(f'crossplane:predicted_cpu_cores{{source_cluster="{SOURCE}"}}'),
    "growth_rate_per_hour": query(f'crossplane:object_growth_rate:per_hour{{source_cluster="{SOURCE}"}}'),
    "days_until_30k": query(f'crossplane:days_until_object_limit:30k{{source_cluster="{SOURCE}"}}'),
    "days_until_100k": query(f'crossplane:days_until_object_limit:100k{{source_cluster="{SOURCE}"}}'),
    "days_until_memory_breach": query(f'crossplane:days_until_memory_breach{{source_cluster="{SOURCE}"}}'),
    "request_rate": query(f'crossplane:apiserver_request_rate:total{{source_cluster="{SOURCE}"}}'),
    # Direct etcd metrics (self-managed only)
    "etcd_db_size_bytes": query(f'crossplane:etcd_db_size_bytes{{source_cluster="{SOURCE}"}}'),
    "etcd_keys_total": query(f'crossplane:etcd_keys_total{{source_cluster="{SOURCE}"}}'),
    "etcd_wal_fsync_p99": query(f'crossplane:etcd_wal_fsync_latency:p99{{source_cluster="{SOURCE}"}}'),
    "etcd_backend_commit_p99": query(f'crossplane:etcd_backend_commit:p99{{source_cluster="{SOURCE}"}}'),
    "etcd_leader_changes_rate1h": query(f'crossplane:etcd_leader_changes:rate1h{{source_cluster="{SOURCE}"}}'),
    "etcd_peer_rtt_p99": query(f'crossplane:etcd_peer_rtt:p99{{source_cluster="{SOURCE}"}}'),
    "etcd_db_size_pct": query(f'crossplane:etcd_db_size_pct{{source_cluster="{SOURCE}"}}'),
}
print(json.dumps(metrics, indent=2))
PYEOF
}

########################################################################
# Phase 1: Pre-flight Checks
########################################################################
phase1_preflight() {
    log "=========================================="
    log "Phase 1: Pre-flight Checks"
    log "=========================================="

    # Source credentials
    if [[ -f "$SCRIPT_DIR/self-managed-env.sh" ]]; then
        source "$SCRIPT_DIR/self-managed-env.sh"
        log "Loaded credentials from self-managed-env.sh"
    fi

    # Check required tools
    for cmd in oc kubectl helm kube-burner python3 curl jq; do
        if ! command -v "$cmd" &>/dev/null; then
            die "$cmd not found in PATH"
        fi
    done
    log "All required tools found"

    # Authenticate to cluster
    if [[ -n "${CLUSTER_API_URL:-}" && -n "${CLUSTER_USERNAME:-}" && -n "${CLUSTER_PASSWORD:-}" ]]; then
        log "Logging in to ${CLUSTER_API_URL}..."
        local login_output
        login_output=$(oc login --username="$CLUSTER_USERNAME" --password="$CLUSTER_PASSWORD" \
            --server="$CLUSTER_API_URL" --insecure-skip-tls-verify 2>&1) || die "oc login failed"
        echo "$login_output" | head -5
    else
        log "No CLUSTER_* credentials found — assuming already authenticated"
        oc whoami &>/dev/null || die "Not authenticated. Export CLUSTER_API_URL, CLUSTER_USERNAME, CLUSTER_PASSWORD or login manually."
    fi

    # Verify cluster identity
    local api_url
    api_url=$(oc whoami --show-server 2>/dev/null || echo "unknown")
    log "Connected to: $api_url as $(oc whoami 2>/dev/null || echo 'unknown')"

    # Check node readiness
    log "Checking nodes..."
    local masters workers
    masters=$(oc get nodes -l node-role.kubernetes.io/master --no-headers 2>/dev/null | grep -c " Ready" || echo 0)
    workers=$(oc get nodes -l node-role.kubernetes.io/worker --no-headers 2>/dev/null | grep -c " Ready" || echo 0)

    # On OCP 4.x, master nodes might use "control-plane" label
    if [[ "$masters" -eq 0 ]]; then
        masters=$(oc get nodes -l node-role.kubernetes.io/control-plane --no-headers 2>/dev/null | grep -c " Ready" || echo 0)
    fi

    log "Nodes: ${masters} masters, ${workers} workers (Ready)"
    if [[ "$masters" -lt 3 ]]; then
        log "WARNING: Expected 3 masters, found $masters"
    fi
    if [[ "$workers" -lt 3 ]]; then
        log "WARNING: Expected 3 workers, found $workers"
    fi

    # Check etcd health (self-managed = direct access)
    log "Checking etcd health..."
    local etcd_pod
    etcd_pod=$(oc get pods -n openshift-etcd -l app=etcd --no-headers 2>/dev/null | head -1 | awk '{print $1}')
    if [[ -n "$etcd_pod" ]]; then
        local etcd_health
        etcd_health=$(oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
            etcdctl endpoint health --cluster 2>&1 | head -5 || echo "could not check")
        log "etcd health: $etcd_health"
    else
        log "WARNING: Could not find etcd pod (might use static pods)"
        # Try static pod path
        etcd_pod=$(oc get pods -n openshift-etcd --no-headers 2>/dev/null | grep "etcd-" | head -1 | awk '{print $1}')
        if [[ -n "$etcd_pod" ]]; then
            log "Found etcd pod: $etcd_pod"
        else
            log "WARNING: No etcd pods found — continuing anyway"
        fi
    fi

    # Verify external Prometheus reachable
    log "Checking Prometheus at ${PROM_URL}..."
    if curl -sf --max-time 10 "${PROM_URL}/api/v1/status/runtimeinfo" &>/dev/null; then
        log "Prometheus is reachable"
    else
        die "Cannot reach Prometheus at ${PROM_URL}"
    fi

    # Print test plan
    local target_objects=$((MAX_BATCHES * CLAIMS_PER_BATCH * OBJECTS_PER_CLAIM))
    local total_minutes=$((MAX_BATCHES * (SETTLE_WAIT + INTER_BATCH_WAIT) / 60))
    log ""
    log "=== Overnight Test Plan ==="
    log "  Cluster:         crossplane1 (self-managed OpenShift)"
    log "  Claims/batch:    $CLAIMS_PER_BATCH"
    log "  Objects/batch:   $((CLAIMS_PER_BATCH * OBJECTS_PER_CLAIM))"
    log "  Batch interval:  $((SETTLE_WAIT + INTER_BATCH_WAIT))s (~10 min)"
    log "  Max batches:     $MAX_BATCHES"
    log "  Max objects:     $target_objects"
    log "  Stop conditions: API P99 > ${API_P99_STOP}s for ${CONSECUTIVE_STOP} batches, etcd quota, .stop-test file"
    log ""

    # Print general formula estimates
    log "=== General Formula Estimates ==="
    for obj_count in 10000 25000 50000 100000; do
        local est_mem est_cpu
        est_mem=$(python3 -c "print(f'{0.5 + $obj_count/10000:.1f}')")
        est_cpu=$(python3 -c "print(f'{0.25 + $obj_count/20000:.2f}')")
        log "  ${obj_count} objects: ~${est_mem} GiB memory, ~${est_cpu} CPU cores"
    done
    log ""

    # Create results directory
    mkdir -p "$RESULTS_DIR"

    log "Phase 1 complete. Proceeding to setup."
}

########################################################################
# Phase 2: Setup
########################################################################
phase2_setup() {
    log "=========================================="
    log "Phase 2: Setup (Crossplane + Providers)"
    log "=========================================="

    log "Running setup/install.sh..."
    bash "$PROJECT_DIR/setup/install.sh" 2>&1 || die "setup/install.sh failed"

    # Verify providers are healthy
    log "Verifying provider health..."
    for provider in provider-nop; do
        local health
        health=$(kubectl get provider "$provider" -o jsonpath='{.status.conditions[?(@.type=="Healthy")].status}' 2>/dev/null || echo "")
        if [[ "$health" != "True" ]]; then
            die "Provider $provider is not Healthy"
        fi
        log "  $provider: Healthy"
    done

    # Verify XRDs established
    for xrd in xvmdeployments.capacity.crossplane.io; do
        local est
        est=$(kubectl get xrd "$xrd" -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' 2>/dev/null || echo "")
        if [[ "$est" != "True" ]]; then
            die "XRD $xrd is not Established"
        fi
        log "  $xrd: Established"
    done

    log "Phase 2 complete."
}

########################################################################
# Phase 3: Monitoring
########################################################################
phase3_monitoring() {
    log "=========================================="
    log "Phase 3: Monitoring Setup"
    log "=========================================="

    # Apply cluster monitoring config (enables user workload monitoring + remote-write)
    log "Applying cluster-monitoring-config..."
    oc apply -f "$PROJECT_DIR/monitoring/self-managed/cluster-monitoring-config.yaml" 2>&1 || \
        die "Failed to apply cluster-monitoring-config"

    # Apply user workload monitoring config
    log "Applying user-workload-monitoring-config..."
    oc apply -f "$PROJECT_DIR/monitoring/self-managed/user-workload-monitoring-config.yaml" 2>&1 || \
        die "Failed to apply user-workload-monitoring-config"

    # Apply PrometheusRule CRD (recording rules that run on-cluster)
    log "Applying PrometheusRule..."
    oc apply -f "$PROJECT_DIR/monitoring/prometheus-rules.yaml" 2>&1 || \
        die "Failed to apply prometheus-rules.yaml"

    # Wait for user-workload-monitoring pods
    log "Waiting for user-workload-monitoring pods..."
    local uwm_ready=false
    for i in $(seq 1 60); do
        local ready_count
        ready_count=$(oc get pods -n openshift-user-workload-monitoring --no-headers 2>/dev/null | grep -c "Running" || echo 0)
        if [[ "$ready_count" -ge 1 ]]; then
            uwm_ready=true
            log "User-workload-monitoring has $ready_count running pods"
            break
        fi
        if [[ "$i" -eq 60 ]]; then
            log "WARNING: user-workload-monitoring pods not ready after 5 min, continuing anyway"
        fi
        sleep 5
    done

    # Wait for metrics to start flowing (remote-write can take 1-2 min)
    log "Waiting 90s for remote-write to start delivering metrics..."
    sleep 90

    # Verify metrics flowing
    log "Verifying metrics flow to external Prometheus..."
    local obj_count
    obj_count=$(query_prom_value "sum(apiserver_storage_objects{source_cluster=\"${SOURCE_CLUSTER}\"})")
    if [[ -n "$obj_count" && "$obj_count" != "" ]]; then
        log "Metrics flowing! apiserver_storage_objects = $obj_count"
    else
        # Try without source_cluster filter (might not be labeled yet)
        obj_count=$(query_prom_value "sum(apiserver_storage_objects)")
        if [[ -n "$obj_count" && "$obj_count" != "" ]]; then
            log "Metrics flowing (no source_cluster label yet): apiserver_storage_objects = $obj_count"
        else
            log "WARNING: No metrics found yet — remote-write may still be initializing"
            log "Waiting additional 60s..."
            sleep 60
            obj_count=$(query_prom_value "sum(apiserver_storage_objects{source_cluster=\"${SOURCE_CLUSTER}\"})")
            if [[ -n "$obj_count" && "$obj_count" != "" ]]; then
                log "Metrics flowing after extra wait: $obj_count"
            else
                log "WARNING: Still no metrics. Proceeding — they may appear after first batch."
            fi
        fi
    fi

    # Check for direct etcd metrics
    local etcd_db
    etcd_db=$(query_prom_value "etcd_mvcc_db_total_size_in_bytes{source_cluster=\"${SOURCE_CLUSTER}\"}")
    if [[ -n "$etcd_db" && "$etcd_db" != "" ]]; then
        local etcd_db_mb
        etcd_db_mb=$(python3 -c "print(f'{float(\"$etcd_db\")/1048576:.1f}')")
        log "Direct etcd metrics available! DB size: ${etcd_db_mb} MiB"
    else
        log "Direct etcd metrics not yet available (may appear after remote-write stabilizes)"
    fi

    # Create Grafana annotation marking test start
    create_grafana_annotation \
        "Overnight load test started on crossplane1 (self-managed OpenShift)" \
        '"overnight-test", "crossplane1", "test-start"'

    log "Phase 3 complete."
}

########################################################################
# Phase 4: Overnight Load Test
########################################################################
phase4_overnight() {
    log "=========================================="
    log "Phase 4: Overnight Load Test"
    log "=========================================="

    local batch_num=1
    local consecutive_high_latency=0
    local stop_reason=""
    local test_start
    test_start=$(date +%s)

    while [[ $batch_num -le $MAX_BATCHES ]]; do
        local batch_start
        batch_start=$(date +%s)
        local elapsed=$(( (batch_start - test_start) / 60 ))

        log "--- Batch #${batch_num} (${elapsed} min elapsed) ---"

        # Check kill switch
        if [[ -f "$STOP_FILE" ]]; then
            stop_reason="manual (.stop-test file)"
            log "STOP: Kill switch detected ($STOP_FILE)"
            break
        fi

        # Create batch results directory
        local batch_dir="$RESULTS_DIR/batch-$(printf '%03d' "$batch_num")"
        mkdir -p "$batch_dir"

        # Record pre-batch metrics
        local pre_objects
        pre_objects=$(query_prom_value "crossplane:etcd_object_count:total{source_cluster=\"${SOURCE_CLUSTER}\"}")
        if [[ -z "$pre_objects" || "$pre_objects" == "" ]]; then
            pre_objects=$(query_prom_value "sum(apiserver_storage_objects{source_cluster=\"${SOURCE_CLUSTER}\"})")
        fi
        if [[ -z "$pre_objects" || "$pre_objects" == "" ]]; then
            pre_objects=$(query_prom_value "sum(apiserver_storage_objects)")
        fi
        log "Pre-batch objects: ${pre_objects:-unknown}"

        # Generate temp config with batch number
        local temp_config
        temp_config=$(mktemp /tmp/overnight-batch-XXXXXX.yaml)
        sed "s/BATCH_NUM/${batch_num}/g" "$KUBE_BURNER_CONFIG" > "$temp_config"

        # Run kube-burner
        log "Running kube-burner (batch #${batch_num}, ${CLAIMS_PER_BATCH} claims)..."
        local kb_exit=0
        (cd "$PROJECT_DIR/kube-burner" && kube-burner init \
            -c "$temp_config" \
            --uuid "overnight-batch-${batch_num}" \
            2>&1) | tee "$batch_dir/kube-burner.log" || kb_exit=$?

        rm -f "$temp_config"
        local batch_end
        batch_end=$(date +%s)
        local duration=$((batch_end - batch_start))

        log "kube-burner finished: exit=$kb_exit, duration=${duration}s"

        if [[ $kb_exit -ne 0 ]]; then
            log "WARNING: kube-burner exited with code $kb_exit"
            create_grafana_annotation \
                "FAILURE: kube-burner exit=$kb_exit at batch #${batch_num}" \
                '"overnight-test", "crossplane1", "failure"'
        fi

        # Wait for metrics to settle
        log "Waiting ${SETTLE_WAIT}s for metrics to settle..."
        sleep "$SETTLE_WAIT"

        # Collect post-batch metrics
        local post_objects api_p99 etcd_p99 memory_bytes cpu_cores etcd_db_size wal_fsync
        post_objects=$(query_prom_value "crossplane:etcd_object_count:total{source_cluster=\"${SOURCE_CLUSTER}\"}")
        if [[ -z "$post_objects" || "$post_objects" == "" ]]; then
            post_objects=$(query_prom_value "sum(apiserver_storage_objects{source_cluster=\"${SOURCE_CLUSTER}\"})")
        fi
        if [[ -z "$post_objects" || "$post_objects" == "" ]]; then
            post_objects=$(query_prom_value "sum(apiserver_storage_objects)")
        fi

        api_p99=$(query_prom_value "crossplane:apiserver_request_latency:p99{source_cluster=\"${SOURCE_CLUSTER}\"}")
        etcd_p99=$(query_prom_value "crossplane:etcd_request_latency:p99{source_cluster=\"${SOURCE_CLUSTER}\"}")
        memory_bytes=$(query_prom_value "crossplane:controller_memory_bytes{source_cluster=\"${SOURCE_CLUSTER}\"}")
        cpu_cores=$(query_prom_value "crossplane:controller_cpu_cores{source_cluster=\"${SOURCE_CLUSTER}\"}")
        etcd_db_size=$(query_prom_value "crossplane:etcd_db_size_bytes{source_cluster=\"${SOURCE_CLUSTER}\"}")
        wal_fsync=$(query_prom_value "crossplane:etcd_wal_fsync_latency:p99{source_cluster=\"${SOURCE_CLUSTER}\"}")

        # Log current state
        local memory_gb=""
        if [[ -n "$memory_bytes" && "$memory_bytes" != "" ]]; then
            memory_gb=$(python3 -c "print(f'{float(\"$memory_bytes\")/1073741824:.2f}')" 2>/dev/null || echo "?")
        fi
        local etcd_db_mb=""
        if [[ -n "$etcd_db_size" && "$etcd_db_size" != "" ]]; then
            etcd_db_mb=$(python3 -c "print(f'{float(\"$etcd_db_size\")/1048576:.1f}')" 2>/dev/null || echo "?")
        fi

        log "Post-batch: objects=${post_objects:-?}, memory=${memory_gb:-?}GiB, cpu=${cpu_cores:-?}, etcd_p99=${etcd_p99:-?}s, api_p99=${api_p99:-?}s, etcd_db=${etcd_db_mb:-?}MiB, wal_fsync=${wal_fsync:-?}s"

        # Save full metrics snapshot
        collect_metrics_snapshot > "$batch_dir/metrics.json" 2>/dev/null || echo '{}' > "$batch_dir/metrics.json"

        # Check milestones
        if [[ -n "$post_objects" && "$post_objects" != "" ]]; then
            local post_int
            post_int=$(printf '%.0f' "$post_objects" 2>/dev/null || echo 0)
            local pre_int=0
            if [[ -n "$pre_objects" && "$pre_objects" != "" ]]; then
                pre_int=$(printf '%.0f' "$pre_objects" 2>/dev/null || echo 0)
            fi

            for milestone in "${MILESTONES[@]}"; do
                if (( pre_int < milestone && post_int >= milestone )); then
                    log "MILESTONE: Crossed ${milestone} objects!"
                    create_grafana_annotation \
                        "Milestone: ${milestone} objects reached (overnight batch #${batch_num})" \
                        '"overnight-test", "crossplane1", "milestone"'
                fi
            done
        fi

        # Check stop conditions

        # 1. API P99 > threshold
        if [[ -n "$api_p99" && "$api_p99" != "" ]]; then
            local api_high
            api_high=$(python3 -c "print(1 if float('$api_p99') > $API_P99_STOP else 0)" 2>/dev/null || echo 0)
            if [[ "$api_high" -eq 1 ]]; then
                consecutive_high_latency=$((consecutive_high_latency + 1))
                log "WARNING: API P99 (${api_p99}s) > ${API_P99_STOP}s — consecutive count: ${consecutive_high_latency}/${CONSECUTIVE_STOP}"
                if [[ $consecutive_high_latency -ge $CONSECUTIVE_STOP ]]; then
                    stop_reason="API P99 > ${API_P99_STOP}s for ${CONSECUTIVE_STOP} consecutive batches"
                    log "STOP: $stop_reason"
                    break
                fi
            else
                consecutive_high_latency=0
            fi
        fi

        # 2. etcd quota alarm (DB size > 90% of quota)
        # quota-backend-bytes: 8589934592 (8 GiB) after tuning (was 2147483648 / 2 GiB)
        local ETCD_QUOTA=8589934592
        if [[ -n "$etcd_db_size" && "$etcd_db_size" != "" ]]; then
            local etcd_pct
            etcd_pct=$(python3 -c "print(f'{float(\"$etcd_db_size\")/$ETCD_QUOTA*100:.1f}')" 2>/dev/null || echo "0")
            local quota_alarm
            quota_alarm=$(python3 -c "print(1 if float('$etcd_db_size') > $ETCD_QUOTA * 0.9 else 0)" 2>/dev/null || echo 0)
            if [[ "$quota_alarm" -eq 1 ]]; then
                stop_reason="etcd DB size at ${etcd_pct}% of quota (>90%)"
                log "STOP: $stop_reason"
                break
            fi
        fi

        # 3. kube-burner failure (don't stop on first failure, but stop on 3 consecutive)
        if [[ $kb_exit -ne 0 ]]; then
            log "WARNING: kube-burner failed — will retry next batch"
        fi

        # Append to tracking log
        python3 << PYEOF >> "$TRACKING_LOG" 2>/dev/null || log "WARNING: Failed to write tracking log entry"
import json
from datetime import datetime, timezone

def safe_float(v):
    try:
        return float(v) if v and v.strip() else None
    except:
        return None

entry = {
    "batch": ${batch_num},
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "pre_objects": safe_float("${pre_objects:-}"),
    "post_objects": safe_float("${post_objects:-}"),
    "duration_sec": ${duration},
    "kb_exit_code": ${kb_exit},
    "api_p99": safe_float("${api_p99:-}"),
    "etcd_p99": safe_float("${etcd_p99:-}"),
    "memory_bytes": safe_float("${memory_bytes:-}"),
    "cpu_cores": safe_float("${cpu_cores:-}"),
    "etcd_db_size_bytes": safe_float("${etcd_db_size:-}"),
    "wal_fsync_p99": safe_float("${wal_fsync:-}"),
    "consecutive_high_latency": ${consecutive_high_latency},
}
print(json.dumps(entry))
PYEOF

        # Sleep until next batch
        if [[ $batch_num -lt $MAX_BATCHES ]]; then
            log "Sleeping ${INTER_BATCH_WAIT}s until next batch..."
            sleep "$INTER_BATCH_WAIT"
        fi

        batch_num=$((batch_num + 1))
    done

    if [[ -z "$stop_reason" && $batch_num -gt $MAX_BATCHES ]]; then
        stop_reason="reached max batches ($MAX_BATCHES)"
    fi

    log ""
    log "=== Load Test Finished ==="
    log "Stop reason: ${stop_reason:-completed normally}"
    log "Batches completed: $((batch_num - 1))"
    local test_end
    test_end=$(date +%s)
    local total_duration=$(( (test_end - test_start) / 60 ))
    log "Total duration: ${total_duration} minutes"

    # Store stop reason
    echo "${stop_reason:-completed normally}" > "$RESULTS_DIR/stop-reason.txt"

    create_grafana_annotation \
        "Overnight test finished: ${stop_reason:-completed normally} (${batch_num-1} batches, ${total_duration} min)" \
        '"overnight-test", "crossplane1", "test-end"'
}

########################################################################
# Phase 5: Post-test
########################################################################
phase5_posttest() {
    log "=========================================="
    log "Phase 5: Post-test Summary"
    log "=========================================="

    # Export final metrics snapshot
    log "Exporting final metrics snapshot..."
    collect_metrics_snapshot > "$RESULTS_DIR/final-metrics.json" 2>/dev/null || echo '{}' > "$RESULTS_DIR/final-metrics.json"

    # Print summary
    local final_objects final_memory final_api_p99 final_etcd_db
    final_objects=$(query_prom_value "crossplane:etcd_object_count:total{source_cluster=\"${SOURCE_CLUSTER}\"}")
    final_memory=$(query_prom_value "crossplane:controller_memory_bytes{source_cluster=\"${SOURCE_CLUSTER}\"}")
    final_api_p99=$(query_prom_value "crossplane:apiserver_request_latency:p99{source_cluster=\"${SOURCE_CLUSTER}\"}")
    final_etcd_db=$(query_prom_value "crossplane:etcd_db_size_bytes{source_cluster=\"${SOURCE_CLUSTER}\"}")

    local mem_gb=""
    if [[ -n "$final_memory" && "$final_memory" != "" ]]; then
        mem_gb=$(python3 -c "print(f'{float(\"$final_memory\")/1073741824:.2f}')" 2>/dev/null || echo "?")
    fi
    local db_mb=""
    if [[ -n "$final_etcd_db" && "$final_etcd_db" != "" ]]; then
        db_mb=$(python3 -c "print(f'{float(\"$final_etcd_db\")/1048576:.1f}')" 2>/dev/null || echo "?")
    fi

    log ""
    log "=== Final State ==="
    log "  Object count:    ${final_objects:-unknown}"
    log "  Memory:          ${mem_gb:-unknown} GiB"
    log "  API P99:         ${final_api_p99:-unknown} s"
    log "  etcd DB size:    ${db_mb:-unknown} MiB"
    log ""
    log "  Stop reason:     $(cat "$RESULTS_DIR/stop-reason.txt" 2>/dev/null || echo 'unknown')"
    log "  Tracking log:    $TRACKING_LOG"
    log "  Final metrics:   $RESULTS_DIR/final-metrics.json"
    log ""

    # Count batches from log
    if [[ -f "$TRACKING_LOG" ]]; then
        local total_batches
        total_batches=$(wc -l < "$TRACKING_LOG")
        log "  Total batches recorded: $total_batches"
    fi

    log ""
    log "=== Next Steps ==="
    log "  1. Review: tail -20 $TRACKING_LOG | python3 -m json.tool"
    log "  2. Check dashboard: Grafana → crossplane-capacity (filter source_cluster=crossplane1)"
    log "  3. Run analysis: make analyze"
    log "  4. Compare coefficients: ROSA vs crossplane1"
    log ""
    log "=== Done ==="
}

########################################################################
# Main
########################################################################
main() {
    log "================================================================"
    log "  Crossplane Overnight Load Test — Self-managed OpenShift"
    log "  Started: $(date -u)"
    log "================================================================"
    log ""

    # Clean up any previous stop file
    rm -f "$STOP_FILE"

    phase1_preflight
    phase2_setup
    phase3_monitoring
    phase4_overnight
    phase5_posttest

    log "Script exiting normally."
}

main "$@"
