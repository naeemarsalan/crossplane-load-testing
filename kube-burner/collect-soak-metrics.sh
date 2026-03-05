#!/usr/bin/env bash
# collect-soak-metrics.sh — Collect steady-state metrics from soak test windows
#
# After running config-stepped-soak.yml, this script queries Prometheus for
# the average metric values during each 5-minute soak window (the last 4 min
# of each pause, skipping the first minute for settling).
#
# Usage:
#   ./collect-soak-metrics.sh [PROMETHEUS_URL] [TEST_START_RFC3339]
#
# Example:
#   ./collect-soak-metrics.sh https://prometheus.example.com 2026-02-28T14:00:00Z
#
# Output:
#   collected-metrics-soak/soak-summary.json    — step-averaged data for analyze.py
#   collected-metrics-soak/timeseries/*.json     — raw range query results per metric

set -euo pipefail

PROM_URL="${1:-${PROM_URL:-https://prometheus.example.com}}"
TEST_START="${2:-}"
OUTPUT_DIR="collected-metrics-soak"
TS_DIR="${OUTPUT_DIR}/timeseries"

# --- Soak step definitions ---
# Each step: [step_name, approx_object_count, minutes_after_test_start_when_soak_begins, soak_duration_minutes]
# These are estimates — adjust based on actual kube-burner timing output.
# The ramp durations are approximate and depend on QPS and object count.
STEPS=(
    "baseline:6500:0:5"
    "step1_10k:10500:8:5"
    "step2_20k:20500:18:5"
    "step3_30k:30500:28:5"
    "step4_40k:40500:38:5"
    "step5_50k:50500:48:5"
    "step6_75k:75500:62:5"
    "step7_100k:100500:78:5"
)

# Metrics to collect
METRICS=(
    "etcdLatencyP99:crossplane:etcd_request_latency:p99"
    "etcdLatencyP50:crossplane:etcd_request_latency:p50"
    "apiserverLatencyP99:crossplane:apiserver_request_latency:p99"
    "apiserverLatencyP50:crossplane:apiserver_request_latency:p50"
    "crossplaneMemory:crossplane:controller_memory_bytes"
    "crossplaneCPU:crossplane:controller_cpu_cores"
    "objectCount:crossplane:etcd_object_count:total"
    "apiserverRequestRate:crossplane:apiserver_request_rate:total"
)

# --- Helpers ---

die() { echo "ERROR: $*" >&2; exit 1; }

epoch_from_rfc3339() {
    date -d "$1" +%s 2>/dev/null || date -jf "%Y-%m-%dT%H:%M:%SZ" "$1" +%s 2>/dev/null
}

query_prometheus_avg() {
    local metric_expr="$1"
    local start_epoch="$2"
    local end_epoch="$3"

    # Use avg_over_time on the metric for the soak window
    local query="avg_over_time(${metric_expr}[${end_epoch}s])"
    # Actually, use a range query with step=60s and take the average of results
    local url="${PROM_URL}/api/v1/query_range"

    local response
    response=$(curl -sf --max-time 30 \
        --data-urlencode "query=${metric_expr}" \
        --data-urlencode "start=${start_epoch}" \
        --data-urlencode "end=${end_epoch}" \
        --data-urlencode "step=30" \
        "${url}" 2>/dev/null) || return 1

    echo "${response}"
}

compute_avg_from_range() {
    # Extract average value from Prometheus range query result
    local json="$1"
    python3 -c "
import json, sys
data = json.loads('''${json}''')
if data.get('status') != 'success':
    print('NaN')
    sys.exit(0)
results = data.get('data', {}).get('result', [])
if not results:
    print('NaN')
    sys.exit(0)
values = [float(v[1]) for v in results[0].get('values', []) if v[1] != 'NaN']
if not values:
    print('NaN')
else:
    print(sum(values) / len(values))
" 2>/dev/null || echo "NaN"
}

# --- Main ---

if [[ -z "${TEST_START}" ]]; then
    echo "Usage: $0 [PROMETHEUS_URL] TEST_START_RFC3339"
    echo ""
    echo "  TEST_START_RFC3339: When the kube-burner soak test started (e.g. 2026-02-28T14:00:00Z)"
    echo "  PROMETHEUS_URL: Prometheus base URL (default: ${PROM_URL})"
    echo ""
    echo "Tip: check kube-burner log output for exact job start/end times and adjust STEPS array."
    exit 1
fi

TEST_START_EPOCH=$(epoch_from_rfc3339 "${TEST_START}")
if [[ -z "${TEST_START_EPOCH}" ]]; then
    die "Could not parse TEST_START: ${TEST_START}"
fi

echo "Soak metrics collection"
echo "  Prometheus: ${PROM_URL}"
echo "  Test start: ${TEST_START} (epoch: ${TEST_START_EPOCH})"
echo "  Output:     ${OUTPUT_DIR}/"
echo ""

mkdir -p "${TS_DIR}"

# Build soak summary JSON
SUMMARY="{}"

for step_def in "${STEPS[@]}"; do
    IFS=':' read -r step_name approx_objects soak_start_offset soak_duration <<< "${step_def}"

    # Calculate soak window: skip first 60s for settling
    soak_begin=$((TEST_START_EPOCH + soak_start_offset * 60 + 60))
    soak_end=$((TEST_START_EPOCH + (soak_start_offset + soak_duration) * 60))

    echo "--- ${step_name} (≈${approx_objects} objects) ---"
    echo "  Soak window: $(date -d @${soak_begin} -Iseconds 2>/dev/null || echo ${soak_begin}) → $(date -d @${soak_end} -Iseconds 2>/dev/null || echo ${soak_end})"

    for metric_def in "${METRICS[@]}"; do
        IFS=':' read -r metric_name metric_expr <<< "${metric_def}"
        # Rejoin the metric expression (it contains colons)
        metric_expr="${metric_def#*:}"

        echo -n "  ${metric_name}: "

        # Query Prometheus for range data during soak window
        response=$(query_prometheus_avg "${metric_expr}" "${soak_begin}" "${soak_end}" 2>/dev/null || echo "")

        if [[ -z "${response}" ]]; then
            echo "FAILED (no response)"
            continue
        fi

        # Save raw timeseries
        echo "${response}" > "${TS_DIR}/${metric_name}_${step_name}.json"

        # Compute average
        avg=$(compute_avg_from_range "${response}")
        echo "${avg}"

        # Add to summary JSON
        SUMMARY=$(python3 -c "
import json
summary = json.loads('${SUMMARY}')
metric = '${metric_name}'
if metric not in summary:
    summary[metric] = []
summary[metric].append({
    'step': '${step_name}',
    'approx_objects': ${approx_objects},
    'timestamp': ${soak_begin},
    'avg_value': float('${avg}') if '${avg}' != 'NaN' else None,
    'soak_start': ${soak_begin},
    'soak_end': ${soak_end}
})
print(json.dumps(summary))
" 2>/dev/null || echo "${SUMMARY}")
    done
    echo ""
done

# Write summary
echo "${SUMMARY}" | python3 -m json.tool > "${OUTPUT_DIR}/soak-summary.json" 2>/dev/null \
    || echo "${SUMMARY}" > "${OUTPUT_DIR}/soak-summary.json"

echo "Done! Results saved to:"
echo "  ${OUTPUT_DIR}/soak-summary.json        — step-averaged data (for analyze.py --test-type soak)"
echo "  ${TS_DIR}/                             — raw Prometheus range queries"
echo ""
echo "Next steps:"
echo "  cd ../analysis && python analyze.py --metrics-dir ../kube-burner/collected-metrics-soak --test-type soak"
