#!/bin/bash
# cron-status.sh — Show current progress of the cron growth test.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="$SCRIPT_DIR/cron-state.json"
TRACKING_LOG="$PROJECT_DIR/results/cron-log.json"
PROM_URL="https://prom.arsalan.io"

query_prom_value() {
    local query="$1"
    curl -sf --max-time 5 "${PROM_URL}/api/v1/query" \
        --data-urlencode "query=${query}" 2>/dev/null | \
    python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    results = data.get('data', {}).get('result', [])
    if results:
        print(results[0]['value'][1])
    else:
        print('N/A')
except:
    print('N/A')
" 2>/dev/null || echo "N/A"
}

echo ""
echo "=== Crossplane Cron Growth Status ==="
echo ""

# --- State file ---
if [[ ! -f "$STATE_FILE" ]]; then
    echo "No state file found. Run: bash scripts/install-cron.sh"
    exit 1
fi

STATE=$(cat "$STATE_FILE")
BATCH_NUM=$(echo "$STATE" | python3 -c "import sys,json; print(json.load(sys.stdin)['batch_num'])" 2>/dev/null)
STARTED_AT=$(echo "$STATE" | python3 -c "import sys,json; print(json.load(sys.stdin)['started_at'])" 2>/dev/null)
STATUS=$(echo "$STATE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null)
TOTAL_ADDED=$(echo "$STATE" | python3 -c "import sys,json; print(json.load(sys.stdin)['total_objects_added'])" 2>/dev/null)
LAST_COUNT=$(echo "$STATE" | python3 -c "import sys,json; print(json.load(sys.stdin)['last_object_count'])" 2>/dev/null)

# Calculate uptime
if [[ -n "$STARTED_AT" && "$STARTED_AT" != "null" ]]; then
    START_EPOCH=$(date -d "$STARTED_AT" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    UPTIME_SEC=$((NOW_EPOCH - START_EPOCH))
    UPTIME_HOURS=$((UPTIME_SEC / 3600))
    UPTIME_MINS=$(((UPTIME_SEC % 3600) / 60))
    UPTIME="${UPTIME_HOURS}h ${UPTIME_MINS}m"
else
    UPTIME="unknown"
fi

echo "Status:        $STATUS"
echo "Running since: $STARTED_AT ($UPTIME ago)"
echo "Current batch: $((BATCH_NUM - 1)) completed (next: #${BATCH_NUM})"
echo ""

# --- Live metrics from Prometheus ---
echo "--- Live Metrics ---"
OBJ_COUNT=$(query_prom_value 'crossplane:etcd_object_count:total')
MEMORY=$(query_prom_value 'crossplane:controller_memory_bytes')
CPU=$(query_prom_value 'crossplane:controller_cpu_cores')
ETCD_P99=$(query_prom_value 'crossplane:etcd_request_latency:p99')
API_P99=$(query_prom_value 'crossplane:apiserver_request_latency:p99')
CAP_STATUS=$(query_prom_value 'crossplane:capacity_status')
DATA_AGE=$(query_prom_value 'time() - timestamp(crossplane:etcd_object_count:total)')

# Format memory as GB
if [[ "$MEMORY" != "N/A" ]]; then
    MEM_GB=$(python3 -c "print(f'{float(\"$MEMORY\")/1e9:.2f} GB')" 2>/dev/null || echo "$MEMORY")
else
    MEM_GB="N/A"
fi

# Format latencies as ms
format_ms() {
    local val="$1"
    if [[ "$val" != "N/A" ]]; then
        python3 -c "print(f'{float(\"$val\")*1000:.1f}ms')" 2>/dev/null || echo "$val"
    else
        echo "N/A"
    fi
}

# Map capacity status
CAP_LABEL="UNKNOWN"
case "$CAP_STATUS" in
    0) CAP_LABEL="GREEN" ;;
    1) CAP_LABEL="WARNING (DEGRADED)" ;;
    2) CAP_LABEL="CRITICAL" ;;
esac

printf "Object count:  %s / 100,000\n" "$OBJ_COUNT"
printf "Memory:        %s\n" "$MEM_GB"
printf "CPU:           %s cores\n" "$CPU"
printf "etcd P99:      %s\n" "$(format_ms "$ETCD_P99")"
printf "API P99:       %s\n" "$(format_ms "$API_P99")"
printf "Capacity:      %s (status=%s)\n" "$CAP_LABEL" "$CAP_STATUS"

if [[ "$DATA_AGE" != "N/A" ]]; then
    AGE_MIN=$(python3 -c "print(f'{float(\"$DATA_AGE\")/60:.1f}')" 2>/dev/null || echo "$DATA_AGE")
    if python3 -c "exit(0 if float('$DATA_AGE') < 300 else 1)" 2>/dev/null; then
        echo "Data freshness: FRESH (${AGE_MIN}m old, remote-write active)"
    else
        echo "Data freshness: STALE (${AGE_MIN}m old — check remote-write pipeline)"
    fi
else
    echo "Data freshness: UNKNOWN (metric not found)"
fi
echo ""

# --- Tracking log summary ---
echo "--- Batch History ---"
if [[ -f "$TRACKING_LOG" && -s "$TRACKING_LOG" ]]; then
    TOTAL_BATCHES=$(wc -l < "$TRACKING_LOG")
    FAILURES=$(grep -c '"exit_code": [^0]' "$TRACKING_LOG" 2>/dev/null || echo 0)
    # Fix: count non-zero exit codes properly
    FAILURES=$(python3 -c "
import json
count = 0
with open('$TRACKING_LOG') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get('exit_code', 0) != 0:
                count += 1
        except:
            pass
print(count)
" 2>/dev/null || echo "?")

    echo "Total batches: $TOTAL_BATCHES"
    echo "Failures:      $FAILURES"

    # Last batch info
    LAST_BATCH=$(tail -1 "$TRACKING_LOG")
    if [[ -n "$LAST_BATCH" ]]; then
        python3 -c "
import json
entry = json.loads('$LAST_BATCH'.replace(\"'\", '\"'))
ts = entry.get('timestamp', 'unknown')
batch = entry.get('batch', '?')
dur = entry.get('duration_sec', '?')
exit_code = entry.get('exit_code', '?')
spot = entry.get('spot_checks', {})
passed = spot.get('passed', '?')
failed = spot.get('failed', '?')
pre = entry.get('pre_objects', '?')
post = entry.get('post_objects', '?')
print(f'Last batch:    #{batch} at {ts}')
print(f'               Duration: {dur}s, exit: {exit_code}')
print(f'               Objects: {pre} -> {post}')
print(f'               Spot checks: {passed}/{int(passed)+int(failed)} passing')
" 2>/dev/null || echo "Last batch: (parse error)"
    fi

    # Estimate time to 100k
    if [[ "$OBJ_COUNT" != "N/A" ]]; then
        python3 -c "
import json
obj = float('$OBJ_COUNT')
remaining = 100000 - obj
if remaining <= 0:
    print('Est. to 100k:  REACHED!')
else:
    entries = []
    with open('$TRACKING_LOG') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                entries.append(json.loads(line))
            except:
                pass
    if len(entries) >= 2:
        first_post = entries[0].get('post_objects', 0)
        last_post = entries[-1].get('post_objects', 0)
        if isinstance(first_post, (int, float)) and isinstance(last_post, (int, float)):
            added = last_post - first_post
            if added > 0:
                batches_needed = remaining / (added / len(entries))
                hours = batches_needed * 0.25  # 15 min per batch
                print(f'Est. to 100k:  ~{hours:.1f}h remaining (~{int(batches_needed)} batches)')
            else:
                print('Est. to 100k:  cannot estimate (no growth)')
        else:
            print('Est. to 100k:  cannot estimate')
    else:
        print('Est. to 100k:  need more data')
" 2>/dev/null || true
    fi
else
    echo "No batches completed yet."
fi

echo ""

# --- Cron status ---
echo "--- Cron Job ---"
CRON_ENTRY=$(crontab -l 2>/dev/null | grep "cron-grow.sh" || echo "")
if [[ -n "$CRON_ENTRY" ]]; then
    echo "Cron:          ACTIVE"
    echo "Schedule:      $CRON_ENTRY"
else
    echo "Cron:          NOT INSTALLED"
fi

DISABLE_MARKER="$SCRIPT_DIR/.cron-disabled"
if [[ -f "$DISABLE_MARKER" ]]; then
    echo "Disable marker: PRESENT (remove to re-enable)"
fi

echo ""
