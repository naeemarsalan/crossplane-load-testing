#!/usr/bin/env python3
"""
analyze-cron-results.py — Post-mortem analysis of cron growth test results.

Reads results/cron-log.json (JSONL) and per-batch metrics snapshots to produce:
  - Object count growth curve
  - Metric progression table (memory, CPU, latency at each batch)
  - Spot check pass rate over time
  - Failure point analysis
  - Actual vs predicted comparison
"""

import json
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
TRACKING_LOG = PROJECT_DIR / "results" / "cron-log.json"
RESULTS_DIR = PROJECT_DIR / "results"
REPORT_FILE = PROJECT_DIR / "results" / "cron-analysis-report.md"


def load_tracking_log():
    """Load JSONL tracking log into a list of dicts."""
    entries = []
    if not TRACKING_LOG.exists():
        print(f"ERROR: Tracking log not found: {TRACKING_LOG}")
        sys.exit(1)

    with open(TRACKING_LOG) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"WARNING: Skipping malformed line {lineno}: {e}")

    if not entries:
        print("ERROR: No entries found in tracking log.")
        sys.exit(1)

    print(f"Loaded {len(entries)} batch entries.")
    return entries


def load_batch_metrics(batch_num):
    """Load per-batch metrics snapshot if available."""
    batch_dir = RESULTS_DIR / f"batch-{batch_num:03d}"
    metrics_file = batch_dir / "metrics.json"
    if metrics_file.exists():
        with open(metrics_file) as f:
            return json.load(f)
    return {}


def load_batch_spot_checks(batch_num):
    """Load per-batch spot checks if available."""
    batch_dir = RESULTS_DIR / f"batch-{batch_num:03d}"
    spot_file = batch_dir / "spot-checks.json"
    if spot_file.exists():
        with open(spot_file) as f:
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


def generate_report(entries):
    """Generate a markdown analysis report."""
    lines = []
    lines.append("# Cron Growth Test — Post-Mortem Analysis")
    lines.append("")
    lines.append(f"Generated from {len(entries)} batch entries.")
    lines.append("")

    # --- Summary ---
    first = entries[0]
    last = entries[-1]
    total_duration = 0
    failures = [e for e in entries if e.get("exit_code", 0) != 0]

    for e in entries:
        total_duration += e.get("duration_sec", 0)

    first_ts = first.get("timestamp", "?")
    last_ts = last.get("timestamp", "?")
    first_objects = first.get("pre_objects", "?")
    last_objects = last.get("post_objects", "?")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Start time | {first_ts} |")
    lines.append(f"| End time | {last_ts} |")
    lines.append(f"| Total batches | {len(entries)} |")
    lines.append(f"| Starting objects | {fmt_num(first_objects)} |")
    lines.append(f"| Ending objects | {fmt_num(last_objects)} |")
    lines.append(f"| Total batch duration | {total_duration}s ({total_duration/60:.1f}m) |")
    lines.append(f"| Failures | {len(failures)} |")
    lines.append("")

    # --- Growth Curve ---
    lines.append("## Object Count Growth")
    lines.append("")
    lines.append("| Batch | Timestamp | Pre-Objects | Post-Objects | Delta | Duration(s) | Exit |")
    lines.append("|-------|-----------|-------------|--------------|-------|-------------|------|")
    for e in entries:
        batch = e.get("batch", "?")
        ts = e.get("timestamp", "?")
        pre = e.get("pre_objects", "?")
        post = e.get("post_objects", "?")
        dur = e.get("duration_sec", "?")
        exit_code = e.get("exit_code", "?")

        delta = "?"
        if isinstance(pre, (int, float)) and isinstance(post, (int, float)):
            delta = f"{post - pre:,.0f}"

        marker = " **FAIL**" if exit_code != 0 else ""
        lines.append(f"| {batch} | {ts} | {fmt_num(pre)} | {fmt_num(post)} | {delta} | {dur} | {exit_code}{marker} |")
    lines.append("")

    # --- Metric Progression ---
    lines.append("## Metric Progression")
    lines.append("")
    lines.append("| Batch | Objects | Memory | CPU | etcd P99 | API P99 | Status | Predicted Mem |")
    lines.append("|-------|---------|--------|-----|----------|---------|--------|---------------|")

    for e in entries:
        batch = e.get("batch", "?")
        metrics = load_batch_metrics(batch if isinstance(batch, int) else 0)
        obj = metrics.get("object_count", e.get("post_objects"))
        mem = metrics.get("controller_memory_bytes")
        cpu = metrics.get("controller_cpu_cores")
        etcd = metrics.get("etcd_latency_p99")
        api = metrics.get("apiserver_latency_p99")
        status = metrics.get("capacity_status")
        pred_mem = metrics.get("predicted_memory")

        status_label = {"0": "GREEN", "1": "WARN", "2": "CRIT"}.get(str(status), str(status))

        lines.append(
            f"| {batch} | {fmt_num(obj)} | {fmt_bytes(mem)} | "
            f"{fmt_num(cpu)} | {fmt_latency(etcd)} | {fmt_latency(api)} | "
            f"{status_label} | {fmt_bytes(pred_mem)} |"
        )
    lines.append("")

    # --- Spot Check Pass Rate ---
    lines.append("## Spot Check Pass Rate")
    lines.append("")
    lines.append("| Batch | Passed | Failed | Rate |")
    lines.append("|-------|--------|--------|------|")

    for e in entries:
        batch = e.get("batch", "?")
        spot = e.get("spot_checks", {})
        passed = spot.get("passed", 0)
        failed = spot.get("failed", 0)
        total = passed + failed
        rate = f"{passed/total*100:.0f}%" if total > 0 else "N/A"
        lines.append(f"| {batch} | {passed} | {failed} | {rate} |")
    lines.append("")

    # --- Failure Analysis ---
    if failures:
        lines.append("## Failure Analysis")
        lines.append("")
        for f_entry in failures:
            batch = f_entry.get("batch", "?")
            ts = f_entry.get("timestamp", "?")
            exit_code = f_entry.get("exit_code", "?")
            post = f_entry.get("post_objects", "?")
            alerts = f_entry.get("alerts_firing", [])

            lines.append(f"### Batch #{batch} — Exit Code {exit_code}")
            lines.append(f"- Timestamp: {ts}")
            lines.append(f"- Object count at failure: {fmt_num(post)}")
            lines.append(f"- Alerts firing: {', '.join(alerts) if alerts else 'none'}")

            # Load spot checks for failure batch
            spot = load_batch_spot_checks(batch if isinstance(batch, int) else 0)
            if spot and "details" in spot:
                failed_checks = [c for c in spot["details"] if not c.get("pass", True)]
                if failed_checks:
                    lines.append(f"- Failed spot checks:")
                    for c in failed_checks:
                        lines.append(f"  - `{c['metric']}`: value={c.get('value')}, criteria={c.get('criteria')}")

            # Load metrics at failure
            metrics = load_batch_metrics(batch if isinstance(batch, int) else 0)
            if metrics:
                lines.append(f"- Metrics at failure:")
                lines.append(f"  - Memory: {fmt_bytes(metrics.get('controller_memory_bytes'))}")
                lines.append(f"  - CPU: {metrics.get('controller_cpu_cores')} cores")
                lines.append(f"  - etcd P99: {fmt_latency(metrics.get('etcd_latency_p99'))}")
                lines.append(f"  - API P99: {fmt_latency(metrics.get('apiserver_latency_p99'))}")
            lines.append("")
    else:
        lines.append("## Failure Analysis")
        lines.append("")
        lines.append("No failures recorded. The test completed all batches successfully.")
        lines.append("")

    # --- Actual vs Predicted ---
    lines.append("## Actual vs Predicted (Model Validation)")
    lines.append("")
    lines.append("| Batch | Objects | Actual Mem | Predicted Mem | Drift % |")
    lines.append("|-------|---------|------------|---------------|---------|")

    for e in entries:
        batch = e.get("batch", "?")
        metrics = load_batch_metrics(batch if isinstance(batch, int) else 0)
        obj = metrics.get("object_count")
        actual = metrics.get("controller_memory_bytes")
        predicted = metrics.get("predicted_memory")

        drift = "N/A"
        if actual and predicted:
            try:
                a = float(actual)
                p = float(predicted)
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
        pre = e.get("pre_objects")
        post = e.get("post_objects")
        if isinstance(pre, (int, float)) and isinstance(post, (int, float)):
            if post <= pre and e.get("exit_code", 0) == 0:
                stalls.append(e)

    if stalls:
        lines.append("## Stalls Detected")
        lines.append("")
        lines.append("Batches where object count did not increase despite kube-burner success:")
        lines.append("")
        for s in stalls:
            lines.append(f"- Batch #{s.get('batch')}: {fmt_num(s.get('pre_objects'))} -> {fmt_num(s.get('post_objects'))}")
        lines.append("")

    return "\n".join(lines)


def main():
    print("=" * 60)
    print("Crossplane Cron Growth Test — Post-Mortem Analysis")
    print("=" * 60)
    print()

    entries = load_tracking_log()
    report = generate_report(entries)

    with open(REPORT_FILE, "w") as f:
        f.write(report)

    print(f"Report written to: {REPORT_FILE}")
    print()

    # Also print summary to stdout
    first = entries[0]
    last = entries[-1]
    failures = [e for e in entries if e.get("exit_code", 0) != 0]

    print(f"  Batches:    {len(entries)}")
    print(f"  Start:      {first.get('pre_objects', '?')} objects")
    print(f"  End:        {last.get('post_objects', '?')} objects")
    print(f"  Failures:   {len(failures)}")

    if failures:
        fail = failures[0]
        print(f"  First fail: batch #{fail.get('batch')} at {fmt_num(fail.get('post_objects'))} objects")

    print()
    print(f"Full report: {REPORT_FILE}")


if __name__ == "__main__":
    main()
