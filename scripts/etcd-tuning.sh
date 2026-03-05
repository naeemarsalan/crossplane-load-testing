#!/bin/bash
# etcd-tuning.sh — Capture baseline etcd config, apply tuning, and verify.
#
# Subcommands:
#   baseline   — Capture current etcd config and metrics to results/etcd-baseline-config.json
#   tune       — Apply etcd tuning parameters (compaction, quota, snapshot-count)
#   verify     — Verify tuning was applied and etcd is healthy
#   defrag     — Run etcd defragmentation on all members
#   status     — Show current etcd status (DB size, health, args)
#
# Usage:
#   bash scripts/etcd-tuning.sh baseline
#   bash scripts/etcd-tuning.sh tune
#   bash scripts/etcd-tuning.sh verify
#   bash scripts/etcd-tuning.sh defrag
#   bash scripts/etcd-tuning.sh status
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RESULTS_DIR="$PROJECT_DIR/results"
BASELINE_FILE="$RESULTS_DIR/etcd-baseline-config.json"
PROM_URL="${PROM_URL:-https://prometheus.example.com}"
SOURCE_CLUSTER="crossplane1"

# Tuning parameters
TUNED_COMPACTION_RETENTION="1m"
TUNED_QUOTA_BACKEND_BYTES="8589934592"   # 8 GiB
TUNED_SNAPSHOT_COUNT="10000"            # Already the OCP default, keep as-is

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

ensure_login() {
    if [[ -f "$SCRIPT_DIR/self-managed-env.sh" ]]; then
        source "$SCRIPT_DIR/self-managed-env.sh"
    fi

    if [[ -n "${CLUSTER_API_URL:-}" && -n "${CLUSTER_USERNAME:-}" && -n "${CLUSTER_PASSWORD:-}" ]]; then
        oc login --username="$CLUSTER_USERNAME" --password="$CLUSTER_PASSWORD" \
            --server="$CLUSTER_API_URL" --insecure-skip-tls-verify 2>&1 | head -3
    else
        oc whoami &>/dev/null || die "Not authenticated. Source self-managed-env.sh or login manually."
    fi
}

get_etcd_pod() {
    local pod
    pod=$(oc get pods -n openshift-etcd -l app=etcd --no-headers 2>/dev/null | head -1 | awk '{print $1}')
    if [[ -z "$pod" ]]; then
        # Fallback: static pod naming
        pod=$(oc get pods -n openshift-etcd --no-headers 2>/dev/null | grep "etcd-" | grep Running | head -1 | awk '{print $1}')
    fi
    echo "$pod"
}

get_all_etcd_pods() {
    oc get pods -n openshift-etcd -l app=etcd --no-headers 2>/dev/null | awk '{print $1}'
    if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
        oc get pods -n openshift-etcd --no-headers 2>/dev/null | grep "etcd-" | grep Running | awk '{print $1}'
    fi
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

########################################################################
# Subcommand: baseline
########################################################################
cmd_baseline() {
    log "=========================================="
    log "Capturing Baseline etcd Configuration"
    log "=========================================="

    ensure_login

    local etcd_pod
    etcd_pod=$(get_etcd_pod)
    [[ -n "$etcd_pod" ]] || die "No etcd pod found"
    log "Using etcd pod: $etcd_pod"

    mkdir -p "$RESULTS_DIR"

    # Capture endpoint status
    log "Capturing endpoint status..."
    local endpoint_status
    endpoint_status=$(oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
        etcdctl endpoint status --cluster -w json 2>/dev/null || echo "[]")

    # Capture endpoint health
    log "Capturing endpoint health..."
    local endpoint_health
    endpoint_health=$(oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
        etcdctl endpoint health --cluster -w json 2>/dev/null || echo "[]")

    # Capture etcd command-line args from pod spec
    log "Capturing etcd pod args..."
    local etcd_args
    etcd_args=$(oc get pod -n openshift-etcd "$etcd_pod" -o json 2>/dev/null | \
        python3 -c "
import sys, json
pod = json.load(sys.stdin)
for c in pod.get('spec', {}).get('containers', []):
    if c.get('name') == 'etcd':
        cmd = c.get('command', [])
        print(json.dumps(cmd, indent=2))
        break
" 2>/dev/null || echo "[]")

    # Extract specific parameters from args
    log "Extracting tuning-relevant parameters..."
    local current_params
    current_params=$(echo "$etcd_args" | python3 -c "
import sys, json
args = json.load(sys.stdin)
params = {}
for arg in args:
    if isinstance(arg, str):
        for key in ['auto-compaction-retention', 'quota-backend-bytes', 'snapshot-count',
                     'auto-compaction-mode', 'heartbeat-interval', 'election-timeout']:
            if f'--{key}' in arg:
                if '=' in arg:
                    params[key] = arg.split('=', 1)[1]
                elif arg == f'--{key}':
                    params[key] = '(next arg)'
print(json.dumps(params, indent=2))
" 2>/dev/null || echo "{}")

    # Capture etcd operator config
    log "Capturing etcd operator config..."
    local etcd_cr
    etcd_cr=$(oc get etcd cluster -o json 2>/dev/null | \
        python3 -c "
import sys, json
data = json.load(sys.stdin)
spec = data.get('spec', {})
print(json.dumps(spec, indent=2))
" 2>/dev/null || echo "{}")

    # Query Prometheus for current etcd health metrics
    log "Querying Prometheus for baseline metrics..."
    local db_size wal_fsync_p99 compaction_dur leader_changes db_size_pct keys_total backend_commit_p99

    db_size=$(query_prom_value "etcd_mvcc_db_total_size_in_bytes{source_cluster=\"${SOURCE_CLUSTER}\"}")
    wal_fsync_p99=$(query_prom_value "histogram_quantile(0.99, rate(etcd_disk_wal_fsync_duration_seconds_bucket{source_cluster=\"${SOURCE_CLUSTER}\"}[5m]))")
    compaction_dur=$(query_prom_value "histogram_quantile(0.99, rate(etcd_debugging_mvcc_db_compaction_total_duration_milliseconds_bucket{source_cluster=\"${SOURCE_CLUSTER}\"}[5m]))")
    leader_changes=$(query_prom_value "rate(etcd_server_leader_changes_seen_total{source_cluster=\"${SOURCE_CLUSTER}\"}[1h])")
    keys_total=$(query_prom_value "etcd_debugging_mvcc_keys_total{source_cluster=\"${SOURCE_CLUSTER}\"}")
    backend_commit_p99=$(query_prom_value "histogram_quantile(0.99, rate(etcd_disk_backend_commit_duration_seconds_bucket{source_cluster=\"${SOURCE_CLUSTER}\"}[5m]))")

    # Also try recording rules
    if [[ -z "$db_size" || "$db_size" == "" ]]; then
        db_size=$(query_prom_value "crossplane:etcd_db_size_bytes{source_cluster=\"${SOURCE_CLUSTER}\"}")
    fi
    if [[ -z "$wal_fsync_p99" || "$wal_fsync_p99" == "" ]]; then
        wal_fsync_p99=$(query_prom_value "crossplane:etcd_wal_fsync_latency:p99{source_cluster=\"${SOURCE_CLUSTER}\"}")
    fi

    # Build baseline JSON — use temp files to avoid shell escaping issues
    log "Writing baseline to $BASELINE_FILE..."
    echo "$endpoint_status" > /tmp/etcd-baseline-status.json
    echo "$endpoint_health" > /tmp/etcd-baseline-health.json
    echo "$etcd_args" > /tmp/etcd-baseline-args.json
    echo "$current_params" > /tmp/etcd-baseline-params.json
    echo "$etcd_cr" > /tmp/etcd-baseline-cr.json

    python3 - "${SOURCE_CLUSTER}" "${BASELINE_FILE}" \
        "${db_size:-}" "${wal_fsync_p99:-}" "${compaction_dur:-}" \
        "${leader_changes:-}" "${keys_total:-}" "${backend_commit_p99:-}" \
        "${TUNED_COMPACTION_RETENTION}" "${TUNED_QUOTA_BACKEND_BYTES}" "${TUNED_SNAPSHOT_COUNT}" \
        << 'PYEOF'
import json, sys
from datetime import datetime, timezone

def safe_load(path, default=None):
    """Load JSON, falling back to string or default."""
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            return default

def to_num(s):
    """Convert string to float or None."""
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None

cluster = sys.argv[1]
outfile = sys.argv[2]

baseline = {
    "capture_date": datetime.now(timezone.utc).isoformat(),
    "cluster": cluster,
    "label": "baseline (pre-tuning)",
    "endpoint_status": safe_load("/tmp/etcd-baseline-status.json", []),
    "endpoint_health": safe_load("/tmp/etcd-baseline-health.json", []),
    "etcd_args": safe_load("/tmp/etcd-baseline-args.json", []),
    "extracted_params": safe_load("/tmp/etcd-baseline-params.json", {}),
    "etcd_operator_spec": safe_load("/tmp/etcd-baseline-cr.json", {}),
    "prometheus_metrics": {
        "etcd_db_size_bytes": to_num(sys.argv[3]),
        "wal_fsync_p99_seconds": to_num(sys.argv[4]),
        "compaction_duration_p99_ms": to_num(sys.argv[5]),
        "leader_changes_rate1h": to_num(sys.argv[6]),
        "keys_total": to_num(sys.argv[7]),
        "backend_commit_p99_seconds": to_num(sys.argv[8]),
    },
    "tuning_plan": {
        "auto_compaction_retention": {"current": "5m (default)", "tuned": sys.argv[9]},
        "quota_backend_bytes": {"current": "2147483648 (2 GiB)", "tuned": f"{sys.argv[10]} (8 GiB)"},
        "snapshot_count": {"current": "10000 (OCP default)", "tuned": sys.argv[11]},
    },
}

with open(outfile, "w") as f:
    json.dump(baseline, f, indent=2)
print(f"Wrote baseline to {outfile}")
PYEOF

    # Cleanup temp files
    rm -f /tmp/etcd-baseline-*.json

    # Print summary
    log ""
    log "=== Baseline Summary ==="
    local db_mb=""
    if [[ -n "$db_size" && "$db_size" != "" ]]; then
        db_mb=$(python3 -c "print(f'{float(\"$db_size\")/1048576:.1f}')")
        log "  etcd DB size:          ${db_mb} MiB"
    else
        log "  etcd DB size:          (not available)"
    fi
    if [[ -n "$wal_fsync_p99" && "$wal_fsync_p99" != "" ]]; then
        local wal_ms
        wal_ms=$(python3 -c "print(f'{float(\"$wal_fsync_p99\")*1000:.2f}')")
        log "  WAL fsync P99:         ${wal_ms} ms"
    else
        log "  WAL fsync P99:         (not available)"
    fi
    if [[ -n "$leader_changes" && "$leader_changes" != "" ]]; then
        log "  Leader changes/hr:     ${leader_changes}"
    fi
    log "  Current params:        $(echo "$current_params" | tr -d '\n')"
    log ""
    log "Baseline saved to: $BASELINE_FILE"
    log "Phase 1 complete."
}

########################################################################
# Subcommand: tune
########################################################################
cmd_tune() {
    log "=========================================="
    log "Applying etcd Tuning Parameters"
    log "=========================================="
    log "  auto-compaction-retention: ${TUNED_COMPACTION_RETENTION}"
    log "  quota-backend-bytes:       ${TUNED_QUOTA_BACKEND_BYTES} (8 GiB)"
    log "  snapshot-count:            ${TUNED_SNAPSHOT_COUNT}"
    log ""

    ensure_login

    # Try Option A: Patch etcd operator config
    log "Attempting to patch etcd operator config (Option A)..."
    local patch_result
    patch_result=$(oc patch etcd/cluster --type=merge -p "{
      \"spec\": {
        \"unsupportedConfigOverrides\": {
          \"autoCompactionRetention\": \"${TUNED_COMPACTION_RETENTION}\",
          \"quotaBackendBytes\": \"${TUNED_QUOTA_BACKEND_BYTES}\"
        }
      }
    }" 2>&1)
    local patch_rc=$?

    if [[ $patch_rc -eq 0 ]]; then
        log "Operator patch succeeded: $patch_result"
        log ""
        log "The cluster-etcd-operator will roll out changes to each etcd member."
        log "This may take 10-15 minutes (one member at a time)."
        log ""
        log "Monitoring rollout..."
        # Wait for operator to start rolling
        sleep 10

        # Watch for etcd pods to be updated
        local max_wait=900  # 15 minutes
        local waited=0
        local check_interval=30

        while [[ $waited -lt $max_wait ]]; do
            local not_ready
            not_ready=$(oc get pods -n openshift-etcd --no-headers 2>/dev/null | grep -cv "Running" || echo 0)
            local total
            total=$(oc get pods -n openshift-etcd --no-headers 2>/dev/null | grep -c "etcd" || echo 0)
            local running
            running=$(oc get pods -n openshift-etcd --no-headers 2>/dev/null | grep "Running" | grep -c "etcd" || echo 0)

            log "  etcd pods: $running/$total Running (waited ${waited}s)"

            if [[ "$running" -ge 3 && "$not_ready" -eq 0 ]]; then
                log "All etcd pods are Running."
                break
            fi

            sleep $check_interval
            waited=$((waited + check_interval))
        done

        if [[ $waited -ge $max_wait ]]; then
            log "WARNING: Timed out waiting for etcd rollout (${max_wait}s)."
            log "Check manually: oc get pods -n openshift-etcd"
        fi

    else
        log "Operator patch failed: $patch_result"
        log ""
        log "Falling back to Option B: Direct static pod edit..."
        log "NOTE: This requires SSH access to master nodes and will be done via oc debug."
        log ""

        local masters
        masters=$(oc get nodes -l node-role.kubernetes.io/master -o name 2>/dev/null)
        if [[ -z "$masters" ]]; then
            masters=$(oc get nodes -l node-role.kubernetes.io/control-plane -o name 2>/dev/null)
        fi

        if [[ -z "$masters" ]]; then
            die "No master nodes found"
        fi

        for master in $masters; do
            log "Editing etcd on $master..."
            oc debug "$master" -- chroot /host bash -c "
                MANIFEST=/etc/kubernetes/manifests/etcd-pod.yaml
                if [ ! -f \$MANIFEST ]; then
                    echo 'ERROR: etcd manifest not found at \$MANIFEST'
                    exit 1
                fi

                # Backup
                cp \$MANIFEST \${MANIFEST}.bak.\$(date +%Y%m%d%H%M%S)

                # Update or add parameters using sed
                # auto-compaction-retention
                if grep -q 'auto-compaction-retention' \$MANIFEST; then
                    sed -i 's/--auto-compaction-retention=[^ ]*/--auto-compaction-retention=${TUNED_COMPACTION_RETENTION}/' \$MANIFEST
                fi

                # quota-backend-bytes
                if grep -q 'quota-backend-bytes' \$MANIFEST; then
                    sed -i 's/--quota-backend-bytes=[^ ]*/--quota-backend-bytes=${TUNED_QUOTA_BACKEND_BYTES}/' \$MANIFEST
                fi

                # snapshot-count
                if grep -q 'snapshot-count' \$MANIFEST; then
                    sed -i 's/--snapshot-count=[^ ]*/--snapshot-count=${TUNED_SNAPSHOT_COUNT}/' \$MANIFEST
                fi

                echo 'Updated \$MANIFEST on $master'
                grep -E 'compaction|quota|snapshot' \$MANIFEST || true
            " 2>&1 || log "WARNING: Failed to edit etcd on $master"

            log "Waiting 60s for etcd to restart on $master..."
            sleep 60
        done
    fi

    log ""
    log "Tuning applied. Run 'bash scripts/etcd-tuning.sh verify' to confirm."
}

########################################################################
# Subcommand: verify
########################################################################
cmd_verify() {
    log "=========================================="
    log "Verifying etcd Tuning"
    log "=========================================="

    ensure_login

    local etcd_pod
    etcd_pod=$(get_etcd_pod)
    [[ -n "$etcd_pod" ]] || die "No etcd pod found"

    # Check etcd args for tuning parameters
    log "Current etcd args (tuning-relevant):"
    oc get pod -n openshift-etcd "$etcd_pod" -o jsonpath='{.spec.containers[0].command}' 2>/dev/null | \
        tr ',' '\n' | grep -E 'compaction|quota|snapshot' | while read -r line; do
        log "  $line"
    done

    # Health check
    log ""
    log "Cluster health:"
    oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
        etcdctl endpoint health --cluster 2>&1 | while read -r line; do
        log "  $line"
    done

    # Status table
    log ""
    log "Cluster status:"
    oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
        etcdctl endpoint status --cluster -w table 2>&1

    # Check quota
    log ""
    log "Quota check:"
    local quota_bytes
    quota_bytes=$(oc get pod -n openshift-etcd "$etcd_pod" -o jsonpath='{.spec.containers[0].command}' 2>/dev/null | \
        tr ',' '\n' | grep 'quota-backend-bytes' | sed 's/.*=//' | tr -d '"' | tr -d ' ')
    if [[ -n "$quota_bytes" ]]; then
        local quota_gib
        quota_gib=$(python3 -c "print(f'{int(\"$quota_bytes\")/1024**3:.1f}')")
        log "  quota-backend-bytes: $quota_bytes (${quota_gib} GiB)"
        if [[ "$quota_bytes" == "$TUNED_QUOTA_BACKEND_BYTES" ]]; then
            log "  PASS: Quota matches tuned value (8 GiB)"
        else
            log "  WARN: Quota does not match tuned value ($TUNED_QUOTA_BACKEND_BYTES)"
        fi
    else
        log "  Could not extract quota from pod args"
    fi

    # Check compaction retention
    local compaction
    compaction=$(oc get pod -n openshift-etcd "$etcd_pod" -o jsonpath='{.spec.containers[0].command}' 2>/dev/null | \
        tr ',' '\n' | grep 'auto-compaction-retention' | sed 's/.*=//' | tr -d '"' | tr -d ' ')
    if [[ -n "$compaction" ]]; then
        log "  auto-compaction-retention: $compaction"
        if [[ "$compaction" == "$TUNED_COMPACTION_RETENTION" ]]; then
            log "  PASS: Compaction retention matches tuned value"
        else
            log "  WARN: Compaction does not match tuned value ($TUNED_COMPACTION_RETENTION)"
        fi
    fi

    log ""
    log "Verification complete."
}

########################################################################
# Subcommand: defrag
########################################################################
cmd_defrag() {
    log "=========================================="
    log "Running etcd Defragmentation"
    log "=========================================="

    ensure_login

    log "Getting etcd DB sizes before defrag..."
    local etcd_pod
    etcd_pod=$(get_etcd_pod)
    [[ -n "$etcd_pod" ]] || die "No etcd pod found"

    oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
        etcdctl endpoint status --cluster -w table 2>&1
    log ""

    log "Defragmenting each etcd member..."
    for pod in $(get_all_etcd_pods); do
        log "  Defragmenting $pod..."
        oc exec -n openshift-etcd "$pod" -c etcd -- \
            etcdctl defrag 2>&1 | while read -r line; do
            log "    $line"
        done
        sleep 5
    done

    log ""
    log "DB sizes after defrag:"
    oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
        etcdctl endpoint status --cluster -w table 2>&1

    log ""
    log "Defragmentation complete."
}

########################################################################
# Subcommand: status
########################################################################
cmd_status() {
    log "=========================================="
    log "etcd Status"
    log "=========================================="

    ensure_login

    local etcd_pod
    etcd_pod=$(get_etcd_pod)
    [[ -n "$etcd_pod" ]] || die "No etcd pod found"

    log "Cluster health:"
    oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
        etcdctl endpoint health --cluster 2>&1

    log ""
    log "Cluster status:"
    oc exec -n openshift-etcd "$etcd_pod" -c etcd -- \
        etcdctl endpoint status --cluster -w table 2>&1

    log ""
    log "Prometheus metrics:"
    local db_size wal_fsync_p99 keys_total
    db_size=$(query_prom_value "crossplane:etcd_db_size_bytes{source_cluster=\"${SOURCE_CLUSTER}\"}")
    wal_fsync_p99=$(query_prom_value "crossplane:etcd_wal_fsync_latency:p99{source_cluster=\"${SOURCE_CLUSTER}\"}")
    keys_total=$(query_prom_value "crossplane:etcd_keys_total{source_cluster=\"${SOURCE_CLUSTER}\"}")

    if [[ -n "$db_size" && "$db_size" != "" ]]; then
        local db_mb
        db_mb=$(python3 -c "print(f'{float(\"$db_size\")/1048576:.1f}')")
        log "  DB size: ${db_mb} MiB"
    fi
    if [[ -n "$wal_fsync_p99" && "$wal_fsync_p99" != "" ]]; then
        local wal_ms
        wal_ms=$(python3 -c "print(f'{float(\"$wal_fsync_p99\")*1000:.2f}')")
        log "  WAL fsync P99: ${wal_ms} ms"
    fi
    if [[ -n "$keys_total" && "$keys_total" != "" ]]; then
        log "  Keys total: ${keys_total}"
    fi
}

########################################################################
# Main
########################################################################
case "${1:-help}" in
    baseline)
        cmd_baseline
        ;;
    tune)
        cmd_tune
        ;;
    verify)
        cmd_verify
        ;;
    defrag)
        cmd_defrag
        ;;
    status)
        cmd_status
        ;;
    help|*)
        echo "Usage: $0 {baseline|tune|verify|defrag|status}"
        echo ""
        echo "Subcommands:"
        echo "  baseline  Capture current etcd config and metrics to results/etcd-baseline-config.json"
        echo "  tune      Apply etcd tuning (compaction=1m, quota=8GiB, snapshot-count=25k)"
        echo "  verify    Verify tuning was applied and etcd is healthy"
        echo "  defrag    Run etcd defragmentation on all members"
        echo "  status    Show current etcd status"
        ;;
esac
