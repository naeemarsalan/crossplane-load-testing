#!/usr/bin/env python3
"""
analyze-overnight-results.py — Post-mortem analysis of overnight load test results.

Reads results/overnight-log.json (JSONL) and per-batch metrics snapshots to produce:
  - Object count growth curve
  - Metric progression table (memory, CPU, latency at each batch)
  - Direct etcd metrics (DB size, WAL fsync, leader changes)
  - Failure point analysis
  - Actual vs predicted comparison
  - Optional comparison against ROSA baseline (results/cron-log.json)

Usage:
  python3 scripts/analyze-overnight-results.py [--rosa-baseline] [--help]
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
TRACKING_LOG = PROJECT_DIR / "results" / "overnight-log.json"
ROSA_LOG = PROJECT_DIR / "results" / "cron-log.json"
RESULTS_DIR = PROJECT_DIR / "results"
REPORT_FILE = PROJECT_DIR / "results" / "overnight-analysis-report.md"


def load_jsonl(path):
    """Load JSONL tracking log into a list of dicts."""
    entries = []
    if not path.exists():
        print(f"ERROR: Tracking log not found: {path}")
        sys.exit(1)

    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"WARNING: Skipping malformed line {lineno}: {e}")

    if not entries:
        print(f"ERROR: No entries found in {path}.")
        sys.exit(1)

    return entries


def load_batch_metrics(batch_num):
    """Load per-batch metrics snapshot if available."""
    batch_dir = RESULTS_DIR / f"batch-{batch_num:03d}"
    metrics_file = batch_dir / "metrics.json"
    if metrics_file.exists():
        with open(metrics_file) as f:
            return json.load(f)
    return {}


def fmt_bytes(val):
    """Format bytes as human-readable."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
        if v >= 1e9:
            return f"{v/1e9:.2f} GB"
        elif v >= 1e6:
            return f"{v/1e6:.1f} MB"
        else:
            return f"{v:.0f} B"
    except (ValueError, TypeError):
        return str(val)


def fmt_latency(val):
    """Format latency in seconds as ms."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val)*1000:.1f}ms"
    except (ValueError, TypeError):
        return str(val)


def fmt_num(val):
    """Format number with commas."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val):,.0f}"
    except (ValueError, TypeError):
        return str(val)


def fmt_pct(val):
    """Format percentage of quota."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val)*100:.1f}%"
    except (ValueError, TypeError):
        return str(val)


def get_metric(entry, metrics, key, fallback_key=None):
    """Get metric value from batch metrics or entry, with optional fallback."""
    val = metrics.get(key)
    if val is None and fallback_key:
        val = entry.get(fallback_key)
    if val is None:
        val = entry.get(key)
    return val


def generate_report(entries, rosa_entries=None):
    """Generate a markdown analysis report."""
    lines = []
    lines.append("# Overnight Load Test — Post-Mortem Analysis")
    lines.append("")
    lines.append(f"**Cluster**: crossplane1 (self-managed OpenShift)")
    lines.append(f"**Entries**: {len(entries)} batches")
    lines.append("")

    # --- Summary ---
    first = entries[0]
    last = entries[-1]
    total_duration = sum(e.get("duration_sec", 0) for e in entries)
    failures = [e for e in entries if e.get("exit_code", 0) != 0]

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Start time | {first.get('timestamp', '?')} |")
    lines.append(f"| End time | {last.get('timestamp', '?')} |")
    lines.append(f"| Total batches | {len(entries)} |")
    lines.append(f"| Starting objects | {fmt_num(first.get('pre_objects'))} |")
    lines.append(f"| Ending objects | {fmt_num(last.get('post_objects'))} |")
    lines.append(f"| Total batch duration | {total_duration}s ({total_duration/3600:.1f}h) |")
    lines.append(f"| Failures | {len(failures)} |")
    lines.append(f"| Stop reason | {last.get('stop_reason', 'completed')} |")
    lines.append("")

    # --- Growth Curve ---
    lines.append("## Object Count Growth")
    lines.append("")
    lines.append("| Batch | Timestamp | Pre-Objects | Post-Objects | Delta | Duration(s) | Exit |")
    lines.append("|-------|-----------|-------------|--------------|-------|-------------|------|")
    for e in entries:
        batch = e.get("batch", "?")
        pre = e.get("pre_objects")
        post = e.get("post_objects")
        delta = "?"
        if isinstance(pre, (int, float)) and isinstance(post, (int, float)):
            delta = f"{post - pre:,.0f}"
        marker = " **FAIL**" if e.get("exit_code", 0) != 0 else ""
        lines.append(
            f"| {batch} | {e.get('timestamp', '?')} | {fmt_num(pre)} | "
            f"{fmt_num(post)} | {delta} | {e.get('duration_sec', '?')} | "
            f"{e.get('exit_code', '?')}{marker} |"
        )
    lines.append("")

    # --- Metric Progression ---
    lines.append("## Metric Progression")
    lines.append("")
    lines.append("| Batch | Objects | Memory | CPU | etcd P99 | API P99 | Status | Predicted Mem |")
    lines.append("|-------|---------|--------|-----|----------|---------|--------|---------------|")

    for e in entries:
        batch = e.get("batch", "?")
        metrics = load_batch_metrics(batch if isinstance(batch, int) else 0)
        obj = get_metric(e, metrics, "object_count", "post_objects")
        mem = get_metric(e, metrics, "controller_memory_bytes")
        cpu = get_metric(e, metrics, "controller_cpu_cores")
        etcd = get_metric(e, metrics, "etcd_latency_p99")
        api = get_metric(e, metrics, "apiserver_latency_p99")
        status = get_metric(e, metrics, "capacity_status")
        pred_mem = get_metric(e, metrics, "predicted_memory")

        status_label = {"0": "GREEN", "1": "WARN", "2": "CRIT"}.get(
            str(status), str(status) if status is not None else "N/A"
        )

        lines.append(
            f"| {batch} | {fmt_num(obj)} | {fmt_bytes(mem)} | "
            f"{fmt_num(cpu)} | {fmt_latency(etcd)} | {fmt_latency(api)} | "
            f"{status_label} | {fmt_bytes(pred_mem)} |"
        )
    lines.append("")

    # --- Direct etcd Metrics (self-managed only) ---
    lines.append("## Direct etcd Metrics (Self-Managed)")
    lines.append("")
    lines.append("These metrics are only available on self-managed clusters with direct etcd access.")
    lines.append("")
    lines.append("| Batch | Objects | DB Size | DB % Quota | WAL Fsync P99 | Leader Changes/h | etcd Keys |")
    lines.append("|-------|---------|---------|------------|---------------|------------------|-----------|")

    has_etcd_data = False
    for e in entries:
        batch = e.get("batch", "?")
        metrics = load_batch_metrics(batch if isinstance(batch, int) else 0)
        obj = get_metric(e, metrics, "object_count", "post_objects")
        db_size = get_metric(e, metrics, "etcd_db_size_bytes")
        db_pct = get_metric(e, metrics, "etcd_db_quota_pct")
        wal = get_metric(e, metrics, "wal_fsync_p99")
        leader = get_metric(e, metrics, "etcd_leader_changes:rate1h")
        keys = get_metric(e, metrics, "etcd_keys_total")

        if db_size is not None or wal is not None:
            has_etcd_data = True

        lines.append(
            f"| {batch} | {fmt_num(obj)} | {fmt_bytes(db_size)} | "
            f"{fmt_pct(db_pct)} | {fmt_latency(wal)} | "
            f"{fmt_num(leader)} | {fmt_num(keys)} |"
        )

    if not has_etcd_data:
        lines.append("")
        lines.append("*No direct etcd metrics collected — data may not be available yet.*")
    lines.append("")

    # --- Failure Analysis ---
    if failures:
        lines.append("## Failure Analysis")
        lines.append("")
        for f_entry in failures:
            batch = f_entry.get("batch", "?")
            lines.append(f"### Batch #{batch} — Exit Code {f_entry.get('exit_code', '?')}")
            lines.append(f"- Timestamp: {f_entry.get('timestamp', '?')}")
            lines.append(f"- Object count: {fmt_num(f_entry.get('post_objects'))}")
            lines.append(f"- Alerts firing: {', '.join(f_entry.get('alerts_firing', [])) or 'none'}")

            metrics = load_batch_metrics(batch if isinstance(batch, int) else 0)
            if metrics:
                lines.append(f"- Metrics at failure:")
                lines.append(f"  - Memory: {fmt_bytes(metrics.get('controller_memory_bytes'))}")
                lines.append(f"  - CPU: {metrics.get('controller_cpu_cores')} cores")
                lines.append(f"  - etcd P99: {fmt_latency(metrics.get('etcd_latency_p99'))}")
                lines.append(f"  - API P99: {fmt_latency(metrics.get('apiserver_latency_p99'))}")
                lines.append(f"  - etcd DB size: {fmt_bytes(metrics.get('etcd_db_size_bytes'))}")
                lines.append(f"  - WAL fsync P99: {fmt_latency(metrics.get('wal_fsync_p99'))}")
            lines.append("")
    else:
        lines.append("## Failure Analysis")
        lines.append("")
        lines.append("No failures recorded. The test completed all batches successfully.")
        lines.append("")

    # --- Actual vs Predicted ---
    lines.append("## Actual vs Predicted (Model Validation)")
    lines.append("")
    lines.append("Model coefficients fitted on crossplane1 data (2026-03-03). Drift indicates how well the models")
    lines.append("predict cluster behavior.")
    lines.append("")
    lines.append("| Batch | Objects | Actual Mem | Predicted Mem | Drift % |")
    lines.append("|-------|---------|------------|---------------|---------|")

    for e in entries:
        batch = e.get("batch", "?")
        metrics = load_batch_metrics(batch if isinstance(batch, int) else 0)
        obj = get_metric(e, metrics, "object_count", "post_objects")
        actual = get_metric(e, metrics, "controller_memory_bytes")
        predicted = get_metric(e, metrics, "predicted_memory")

        drift = "N/A"
        if actual and predicted:
            try:
                a, p = float(actual), float(predicted)
                if p > 0:
                    drift = f"{((a - p) / p) * 100:+.1f}%"
            except (ValueError, TypeError):
                pass

        lines.append(
            f"| {batch} | {fmt_num(obj)} | {fmt_bytes(actual)} | "
            f"{fmt_bytes(predicted)} | {drift} |"
        )
    lines.append("")

    # --- Stall Detection ---
    stalls = []
    for e in entries:
        pre, post = e.get("pre_objects"), e.get("post_objects")
        if isinstance(pre, (int, float)) and isinstance(post, (int, float)):
            if post <= pre and e.get("exit_code", 0) == 0:
                stalls.append(e)

    if stalls:
        lines.append("## Stalls Detected")
        lines.append("")
        lines.append("Batches where object count did not increase despite kube-burner success:")
        lines.append("")
        for s in stalls:
            lines.append(
                f"- Batch #{s.get('batch')}: "
                f"{fmt_num(s.get('pre_objects'))} -> {fmt_num(s.get('post_objects'))}"
            )
        lines.append("")

    # --- ROSA Baseline Comparison ---
    if rosa_entries:
        lines.append("## ROSA Baseline Comparison")
        lines.append("")
        lines.append("Comparing self-managed (crossplane1) vs ROSA at similar object counts.")
        lines.append("")
        lines.append("| Objects (approx) | SM Memory | ROSA Memory | SM etcd P99 | ROSA etcd P99 | SM API P99 | ROSA API P99 |")
        lines.append("|------------------|-----------|-------------|-------------|---------------|------------|--------------|")

        # Build lookup tables indexed by rough object count (rounded to nearest 5k)
        def build_metric_map(ents):
            m = {}
            for e in ents:
                metrics = load_batch_metrics(e.get("batch", 0) if isinstance(e.get("batch"), int) else 0)
                obj = float(get_metric(e, metrics, "object_count", "post_objects") or 0)
                if obj <= 0:
                    continue
                bucket = round(obj / 5000) * 5000
                m[bucket] = {
                    "memory": get_metric(e, metrics, "controller_memory_bytes"),
                    "etcd_p99": get_metric(e, metrics, "etcd_latency_p99"),
                    "api_p99": get_metric(e, metrics, "apiserver_latency_p99"),
                }
            return m

        sm_map = build_metric_map(entries)
        rosa_map = build_metric_map(rosa_entries)
        all_buckets = sorted(set(sm_map.keys()) | set(rosa_map.keys()))

        for bucket in all_buckets:
            sm = sm_map.get(bucket, {})
            rosa = rosa_map.get(bucket, {})
            lines.append(
                f"| {fmt_num(bucket)} | {fmt_bytes(sm.get('memory'))} | "
                f"{fmt_bytes(rosa.get('memory'))} | {fmt_latency(sm.get('etcd_p99'))} | "
                f"{fmt_latency(rosa.get('etcd_p99'))} | {fmt_latency(sm.get('api_p99'))} | "
                f"{fmt_latency(rosa.get('api_p99'))} |"
            )
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze overnight load test results from self-managed cluster."
    )
    parser.add_argument(
        "--rosa-baseline",
        action="store_true",
        help="Include ROSA baseline comparison from results/cron-log.json",
    )
    parser.add_argument(
        "--log",
        type=str,
        default=str(TRACKING_LOG),
        help=f"Path to overnight tracking log (default: {TRACKING_LOG})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(REPORT_FILE),
        help=f"Path to output report (default: {REPORT_FILE})",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Overnight Load Test — Post-Mortem Analysis")
    print("=" * 60)
    print()

    log_path = Path(args.log)
    entries = load_jsonl(log_path)
    print(f"Loaded {len(entries)} batch entries from {log_path}.")

    rosa_entries = None
    if args.rosa_baseline:
        if ROSA_LOG.exists():
            rosa_entries = load_jsonl(ROSA_LOG)
            print(f"Loaded {len(rosa_entries)} ROSA baseline entries.")
        else:
            print(f"WARNING: ROSA baseline not found at {ROSA_LOG}, skipping comparison.")

    report = generate_report(entries, rosa_entries)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(report)

    print(f"\nReport written to: {output_path}")
    print()

    # Summary to stdout
    first, last = entries[0], entries[-1]
    failures = [e for e in entries if e.get("exit_code", 0) != 0]

    print(f"  Batches:       {len(entries)}")
    print(f"  Start:         {first.get('pre_objects', '?')} objects")
    print(f"  End:           {last.get('post_objects', '?')} objects")
    print(f"  Failures:      {len(failures)}")
    print(f"  Stop reason:   {last.get('stop_reason', 'completed')}")

    if failures:
        fail = failures[0]
        print(f"  First fail:    batch #{fail.get('batch')} at {fmt_num(fail.get('post_objects'))} objects")

    # Show etcd highlights if available
    last_metrics = load_batch_metrics(last.get("batch", 0) if isinstance(last.get("batch"), int) else 0)
    db_size = last_metrics.get("etcd_db_size_bytes") or last.get("etcd_db_size_bytes")
    if db_size:
        print(f"  Final DB size: {fmt_bytes(db_size)}")

    print()
    print(f"Full report: {output_path}")


if __name__ == "__main__":
    main()
