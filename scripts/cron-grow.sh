#!/bin/bash
# cron-grow.sh — Called by cron every 15 minutes.
# Each invocation adds 500 VMDeployment claims (~4,000 etcd objects),
# runs spot checks on all recording rules, tracks results, and
# auto-disables on failure.
set -uo pipefail

# Ensure PATH includes common binary locations (cron has minimal PATH)
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

########################################################################
# Configuration
########################################################################
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="$SCRIPT_DIR/cron-state.json"
TRACKING_LOG="$PROJECT_DIR/results/cron-log.json"
DISABLE_MARKER="$SCRIPT_DIR/.cron-disabled"
KUBE_BURNER_CONFIG="$PROJECT_DIR/kube-burner/config-cron-batch.yaml"
PROM_URL="https://prom.arsalan.io"
GRAFANA_URL="http://172.16.2.252:3000"
GRAFANA_DASHBOARD_UID="crossplane-capacity"

# Milestone thresholds for Grafana annotations
MILESTONES=(10000 20000 30000 50000 75000 100000)

########################################################################
# Helpers
########################################################################
log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

query_prom() {
    local query="$1"
    curl -sf --max-time 10 "${PROM_URL}/api/v1/query" \
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

query_prom_count() {
    local query="$1"
    local result
    result=$(query_prom "$query")
    if [[ $? -ne 0 || -z "$result" ]]; then
        echo "0"
        return 1
    fi
    echo "$result" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    results = data.get('data', {}).get('result', [])
    print(len(results))
except:
    print('0')
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

read_state() {
    python3 -c "
import json, sys
with open('$STATE_FILE') as f:
    state = json.load(f)
print(json.dumps(state))
" 2>/dev/null
}

update_state() {
    local key="$1"
    local value="$2"
    python3 -c "
import json
with open('$STATE_FILE', 'r') as f:
    state = json.load(f)
state['$key'] = $value
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
}

########################################################################
# Pre-flight checks
########################################################################
log "=== Cron batch starting ==="

# Check for disable marker
if [[ -f "$DISABLE_MARKER" ]]; then
    log "DISABLED: Found $DISABLE_MARKER — exiting. Remove marker and re-install cron to resume."
    exit 0
fi

# Ensure state file exists
if [[ ! -f "$STATE_FILE" ]]; then
    log "ERROR: State file not found. Run install-cron.sh first."
    exit 1
fi

# Read current state
STATE_JSON=$(read_state)
BATCH_NUM=$(echo "$STATE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['batch_num'])")
STATUS=$(echo "$STATE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")

if [[ "$STATUS" == "failed" || "$STATUS" == "completed" ]]; then
    log "Status is '$STATUS' — not running. Reset state to 'running' to resume."
    exit 0
fi

log "Batch #${BATCH_NUM} starting"

# Ensure results dir for this batch
BATCH_DIR="$PROJECT_DIR/results/batch-$(printf '%03d' "$BATCH_NUM")"
mkdir -p "$BATCH_DIR"

########################################################################
# Step 1: Authenticate to ROSA cluster
########################################################################
log "Authenticating to ROSA cluster..."
source "$SCRIPT_DIR/cron-env.sh" 2>/dev/null || true
if [[ -n "${ROSA_API_URL:-}" && -n "${ROSA_USERNAME:-}" && -n "${ROSA_PASSWORD:-}" ]]; then
    oc login --username="$ROSA_USERNAME" --password="$ROSA_PASSWORD" \
        --server="$ROSA_API_URL" --insecure-skip-tls-verify 2>&1 | head -5 || log "WARNING: oc login failed"
else
    log "WARNING: ROSA credentials not set — source scripts/cron-env.sh or export ROSA_API_URL, ROSA_USERNAME, ROSA_PASSWORD"
fi

########################################################################
# Step 2: Record pre-batch object count
########################################################################
PRE_OBJECTS=$(query_prom_value 'crossplane:etcd_object_count:total' || echo "")
if [[ -z "$PRE_OBJECTS" ]]; then
    PRE_OBJECTS=$(query_prom_value 'sum(apiserver_storage_objects{source_cluster="rosa"})' || echo "")
fi
if [[ -z "$PRE_OBJECTS" ]]; then
    log "WARNING: Could not query pre-batch object count from Prometheus"
    PRE_OBJECTS="unknown"
fi
log "Pre-batch object count: $PRE_OBJECTS"

########################################################################
# Step 3: Generate temp config with BATCH_NUM substituted
########################################################################
TEMP_CONFIG=$(mktemp /tmp/cron-batch-XXXXXX.yaml)
sed "s/BATCH_NUM/${BATCH_NUM}/g" "$KUBE_BURNER_CONFIG" > "$TEMP_CONFIG"
log "Config generated: $TEMP_CONFIG (job name: cron-batch-${BATCH_NUM})"

########################################################################
# Step 4: Run kube-burner
########################################################################
BATCH_START=$(date +%s)
log "Running kube-burner init..."

KB_EXIT=0
# kube-burner resolves template paths relative to CWD, so cd into kube-burner/
(cd "$PROJECT_DIR/kube-burner" && kube-burner init \
    -c "$TEMP_CONFIG" \
    --uuid "cron-batch-${BATCH_NUM}" \
    2>&1) | tee "$BATCH_DIR/kube-burner.log" || KB_EXIT=$?

BATCH_END=$(date +%s)
DURATION=$((BATCH_END - BATCH_START))
rm -f "$TEMP_CONFIG"

log "kube-burner finished: exit=$KB_EXIT, duration=${DURATION}s"

########################################################################
# Step 5: Record post-batch object count
########################################################################
# Wait a moment for metrics to propagate
sleep 15

POST_OBJECTS=$(query_prom_value 'crossplane:etcd_object_count:total' || echo "")
if [[ -z "$POST_OBJECTS" ]]; then
    POST_OBJECTS=$(query_prom_value 'sum(apiserver_storage_objects{source_cluster="rosa"})' || echo "")
fi
if [[ -z "$POST_OBJECTS" ]]; then
    log "WARNING: Could not query post-batch object count"
    POST_OBJECTS="unknown"
fi
log "Post-batch object count: $POST_OBJECTS"

########################################################################
# Step 6: Spot checks — query all recording rules
########################################################################
log "Running spot checks..."

SPOT_PASSED=0
SPOT_FAILED=0
SPOT_DETAILS="[]"

run_spot_checks() {
    python3 << 'PYEOF'
import json, sys, urllib.request, urllib.parse

PROM_URL = "https://prom.arsalan.io"

def query(expr):
    """Query Prometheus, return (value_or_None, result_count)."""
    try:
        url = f"{PROM_URL}/api/v1/query?query={urllib.parse.quote(expr)}"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            results = data.get("data", {}).get("result", [])
            if not results:
                return None, 0
            val = results[0]["value"][1]
            return val, len(results)
    except Exception as e:
        return None, -1

pre_objects = sys.argv[1] if len(sys.argv) > 1 else "0"

checks = []
passed = 0
failed = 0

# --- Tier 1: Must-Have ---
# 1. Object count increased
val, cnt = query("crossplane:etcd_object_count:total")
ok = val is not None and pre_objects != "unknown" and float(val) > float(pre_objects)
checks.append({"tier": 1, "metric": "crossplane:etcd_object_count:total", "value": val, "pass": ok, "criteria": f"> {pre_objects}"})
if ok: passed += 1
else: failed += 1

# 2-5: Non-empty checks
for metric in ["crossplane:controller_memory_bytes", "crossplane:controller_cpu_cores",
               "crossplane:etcd_request_latency:p99", "crossplane:apiserver_request_latency:p99"]:
    val, cnt = query(metric)
    ok = val is not None and val != ""
    checks.append({"tier": 1, "metric": metric, "value": val, "pass": ok, "criteria": "non-empty"})
    if ok: passed += 1
    else: failed += 1

# 6. Data freshness — check that object count metric was updated within last 5 min
val, cnt = query('time() - crossplane:etcd_object_count:total offset 0s')
# Alternative: just check the metric exists and has a recent timestamp
val2, cnt2 = query('crossplane:etcd_object_count:total')
ok = val2 is not None and val2 != ""
checks.append({"tier": 1, "metric": "data_freshness (object count exists)", "value": val2, "pass": ok, "criteria": "non-empty (remote-write or federation delivering data)"})
if ok: passed += 1
else: failed += 1

# 7. Crossplane rule count
val, cnt = query('count({__name__=~"crossplane:.*"})')
rule_count = val
ok = val is not None and float(val) >= 20
checks.append({"tier": 1, "metric": 'count({__name__=~"crossplane:.*"})', "value": val, "pass": ok, "criteria": ">= 20"})
if ok: passed += 1
else: failed += 1

# --- Tier 2: Model Validation ---
for metric in ["crossplane:predicted_memory_bytes", "crossplane:predicted_cpu_cores",
               "crossplane:predicted_memory_bytes:at_50k", "crossplane:predicted_memory_bytes:at_100k",
               "crossplane:predicted_etcd_latency_p99_seconds", "crossplane:predicted_apiserver_latency_p99_seconds"]:
    val, cnt = query(metric)
    ok = val is not None and val != ""
    checks.append({"tier": 2, "metric": metric, "value": val, "pass": ok, "criteria": "non-empty"})
    if ok: passed += 1
    else: failed += 1

# capacity_status: must be 0, 1, or 2
val, cnt = query("crossplane:capacity_status")
ok = val is not None and val in ("0", "1", "2")
checks.append({"tier": 2, "metric": "crossplane:capacity_status", "value": val, "pass": ok, "criteria": "0, 1, or 2"})
if ok: passed += 1
else: failed += 1

# --- Tier 3: Growth & Capacity (informational, always pass) ---
tier3_metrics = [
    "crossplane:object_growth_rate:per_day",
    "crossplane:object_growth_rate:per_hour",
    "crossplane:days_until_object_limit:30k",
    "crossplane:days_until_object_limit:100k",
    "crossplane:days_until_memory_breach",
    "crossplane:days_until_api_latency_breach",
    "crossplane:apiserver_request_rate:total",
    "crossplane:etcd_request_latency:p50",
    "crossplane:apiserver_request_latency:p50",
]
for metric in tier3_metrics:
    val, cnt = query(metric)
    checks.append({"tier": 3, "metric": metric, "value": val, "pass": True, "criteria": "informational"})
    passed += 1

output = {"passed": passed, "failed": failed, "details": checks}
print(json.dumps(output))
PYEOF
}

SPOT_RESULT=$(run_spot_checks "$PRE_OBJECTS" 2>/dev/null || echo '{"passed":0,"failed":0,"details":[]}')
SPOT_PASSED=$(echo "$SPOT_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['passed'])" 2>/dev/null || echo 0)
SPOT_FAILED=$(echo "$SPOT_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['failed'])" 2>/dev/null || echo 0)

log "Spot checks: ${SPOT_PASSED} passed, ${SPOT_FAILED} failed"

# Save spot checks
echo "$SPOT_RESULT" | python3 -m json.tool > "$BATCH_DIR/spot-checks.json" 2>/dev/null || \
    echo "$SPOT_RESULT" > "$BATCH_DIR/spot-checks.json"

########################################################################
# Step 7: Save per-batch metrics snapshot
########################################################################
log "Saving metrics snapshot..."
python3 << PYEOF > "$BATCH_DIR/metrics.json" 2>/dev/null || echo '{}' > "$BATCH_DIR/metrics.json"
import json, urllib.request, urllib.parse

PROM_URL = "https://prom.arsalan.io"

def query(expr):
    try:
        url = f"{PROM_URL}/api/v1/query?query={urllib.parse.quote(expr)}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            results = data.get("data", {}).get("result", [])
            if results:
                return results[0]["value"][1]
    except:
        pass
    return None

metrics = {
    "object_count": query("crossplane:etcd_object_count:total"),
    "controller_memory_bytes": query("crossplane:controller_memory_bytes"),
    "controller_cpu_cores": query("crossplane:controller_cpu_cores"),
    "etcd_latency_p99": query("crossplane:etcd_request_latency:p99"),
    "etcd_latency_p50": query("crossplane:etcd_request_latency:p50"),
    "apiserver_latency_p99": query("crossplane:apiserver_request_latency:p99"),
    "apiserver_latency_p50": query("crossplane:apiserver_request_latency:p50"),
    "capacity_status": query("crossplane:capacity_status"),
    "predicted_memory": query("crossplane:predicted_memory_bytes"),
    "predicted_cpu": query("crossplane:predicted_cpu_cores"),
    "growth_rate_per_hour": query("crossplane:object_growth_rate:per_hour"),
    "days_until_30k": query("crossplane:days_until_object_limit:30k"),
    "days_until_100k": query("crossplane:days_until_object_limit:100k"),
    "days_until_memory_breach": query("crossplane:days_until_memory_breach"),
    "request_rate": query("crossplane:apiserver_request_rate:total"),
}
print(json.dumps(metrics, indent=2))
PYEOF

########################################################################
# Step 8: Grafana annotations at milestones
########################################################################
if [[ "$POST_OBJECTS" != "unknown" ]]; then
    POST_INT=$(printf '%.0f' "$POST_OBJECTS" 2>/dev/null || echo 0)
    PRE_INT=0
    if [[ "$PRE_OBJECTS" != "unknown" ]]; then
        PRE_INT=$(printf '%.0f' "$PRE_OBJECTS" 2>/dev/null || echo 0)
    fi

    for MILESTONE in "${MILESTONES[@]}"; do
        if (( PRE_INT < MILESTONE && POST_INT >= MILESTONE )); then
            log "MILESTONE: Crossed ${MILESTONE} objects!"
            create_grafana_annotation \
                "Milestone: ${MILESTONE} objects reached (batch #${BATCH_NUM})" \
                '"load-test-milestone", "cron-growth"'
        fi
    done
fi

# Annotate on kube-burner failure
if [[ $KB_EXIT -ne 0 ]]; then
    create_grafana_annotation \
        "FAILURE: kube-burner exit=$KB_EXIT at batch #${BATCH_NUM}, objects=${POST_OBJECTS}" \
        '"load-test-failure", "cron-growth"'
fi

########################################################################
# Step 9: Append to tracking log
########################################################################
log "Appending to tracking log..."

# Query for any firing alerts
ALERTS_FIRING=$(python3 -c "
import json, urllib.request
try:
    url = 'https://prom.arsalan.io/api/v1/alerts'
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
        alerts = data.get('data', {}).get('alerts', [])
        firing = [a['labels'].get('alertname', 'unknown') for a in alerts if a.get('state') == 'firing']
        print(json.dumps(firing))
except:
    print('[]')
" 2>/dev/null || echo '[]')

python3 << PYEOF >> "$TRACKING_LOG"
import json, sys
from datetime import datetime, timezone

entry = {
    "batch": ${BATCH_NUM},
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "pre_objects": "${PRE_OBJECTS}" if "${PRE_OBJECTS}" == "unknown" else float("${PRE_OBJECTS}"),
    "post_objects": "${POST_OBJECTS}" if "${POST_OBJECTS}" == "unknown" else float("${POST_OBJECTS}"),
    "duration_sec": ${DURATION},
    "exit_code": ${KB_EXIT},
    "spot_checks": {"passed": ${SPOT_PASSED}, "failed": ${SPOT_FAILED}},
    "alerts_firing": json.loads('${ALERTS_FIRING}'),
}
print(json.dumps(entry))
PYEOF

########################################################################
# Step 10: Update state
########################################################################
OBJECTS_ADDED=0
if [[ "$PRE_OBJECTS" != "unknown" && "$POST_OBJECTS" != "unknown" ]]; then
    OBJECTS_ADDED=$(python3 -c "print(int(float('${POST_OBJECTS}') - float('${PRE_OBJECTS}')))" 2>/dev/null || echo 0)
fi

python3 << PYEOF
import json
with open("$STATE_FILE", "r") as f:
    state = json.load(f)

state["batch_num"] = ${BATCH_NUM} + 1
state["last_object_count"] = "${POST_OBJECTS}" if "${POST_OBJECTS}" == "unknown" else float("${POST_OBJECTS}")
if isinstance(state.get("total_objects_added"), (int, float)):
    state["total_objects_added"] += ${OBJECTS_ADDED}
else:
    state["total_objects_added"] = ${OBJECTS_ADDED}

with open("$STATE_FILE", "w") as f:
    json.dump(state, f, indent=2)
PYEOF

log "State updated: next batch=#$((BATCH_NUM + 1)), objects_added_this_batch=${OBJECTS_ADDED}"

########################################################################
# Step 11: Handle failure
########################################################################
if [[ $KB_EXIT -ne 0 ]]; then
    log "FAILURE: kube-burner exited with code $KB_EXIT"
    log "Disabling cron (creating marker: $DISABLE_MARKER)"

    update_state "status" '"failed"'
    touch "$DISABLE_MARKER"

    # Also remove cron entry
    crontab -l 2>/dev/null | grep -v "cron-grow.sh" | crontab - 2>/dev/null || true

    log "Cron disabled. Investigate, then: rm $DISABLE_MARKER && bash scripts/install-cron.sh"
    exit 1
fi

log "=== Batch #${BATCH_NUM} complete. Next batch in ~15 min ==="
