#!/usr/bin/env python3
"""Compare baseline vs tuned overnight load-test results.

Reads results/overnight-log-baseline.json (untuned) and results/overnight-log.json (tuned),
generates a comparison table, overlay charts, and outputs results/tuning-comparison.md.

Usage:
    python3 scripts/compare-overnight-results.py
    python3 scripts/compare-overnight-results.py --baseline results/overnight-log-baseline.json --tuned results/overnight-log.json
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = PROJECT_ROOT / "results" / "overnight-log-baseline.json"
DEFAULT_TUNED = PROJECT_ROOT / "results" / "overnight-log.json"
CHARTS_DIR = PROJECT_ROOT / "report" / "charts"
OUTPUT_MD = PROJECT_ROOT / "results" / "tuning-comparison.md"

# Metrics to compare
METRICS = {
    "memory_bytes": {
        "label": "Controller Memory",
        "unit": "bytes",
        "display_unit": "GiB",
        "scale": 1 / (1024**3),
        "format": ".2f",
        "higher_is_worse": True,
    },
    "cpu_cores": {
        "label": "Controller CPU",
        "unit": "cores",
        "display_unit": "cores",
        "scale": 1,
        "format": ".2f",
        "higher_is_worse": True,
    },
    "etcd_p99": {
        "label": "etcd P99 Latency",
        "unit": "seconds",
        "display_unit": "ms",
        "scale": 1000,
        "format": ".1f",
        "higher_is_worse": True,
    },
    "api_p99": {
        "label": "API P99 Latency",
        "unit": "seconds",
        "display_unit": "ms",
        "scale": 1000,
        "format": ".1f",
        "higher_is_worse": True,
    },
    "etcd_db_size_bytes": {
        "label": "etcd DB Size",
        "unit": "bytes",
        "display_unit": "MiB",
        "scale": 1 / (1024**2),
        "format": ".1f",
        "higher_is_worse": True,
    },
    "wal_fsync_p99": {
        "label": "WAL Fsync P99",
        "unit": "seconds",
        "display_unit": "ms",
        "scale": 1000,
        "format": ".2f",
        "higher_is_worse": True,
    },
}


def load_jsonl(path: Path) -> list[dict]:
    """Load JSONL file."""
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def get_value_at_object_count(entries: list[dict], field: str, target_objects: int,
                               tolerance: float = 0.1) -> float | None:
    """Get metric value at a specific object count, interpolating if needed."""
    pairs = []
    for e in entries:
        x = e.get("post_objects")
        y = e.get(field)
        if x is not None and y is not None and x > 0 and y > 0:
            pairs.append((float(x), float(y)))

    if not pairs:
        return None

    pairs.sort(key=lambda p: p[0])
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    # Exact or near match
    for x, y in pairs:
        if abs(x - target_objects) / target_objects < tolerance:
            return y

    # Interpolate
    for i in range(len(xs) - 1):
        if xs[i] <= target_objects <= xs[i + 1]:
            frac = (target_objects - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + frac * (ys[i + 1] - ys[i])

    return None


def get_peak_value(entries: list[dict], field: str) -> tuple[float | None, float | None]:
    """Get peak value and the object count where it occurred."""
    peak_val = None
    peak_obj = None
    for e in entries:
        y = e.get(field)
        x = e.get("post_objects")
        if y is not None and x is not None and y > 0:
            if peak_val is None or float(y) > peak_val:
                peak_val = float(y)
                peak_obj = float(x)
    return peak_val, peak_obj


def get_max_objects(entries: list[dict]) -> float:
    """Get the maximum object count reached."""
    return max((float(e.get("post_objects", 0)) for e in entries), default=0)


def fit_power_law(entries: list[dict], field: str) -> tuple[float, float, float] | None:
    """Fit y = a * x^b, return (a, b, r2)."""
    xs, ys = [], []
    for e in entries:
        x = e.get("post_objects")
        y = e.get(field)
        if x is not None and y is not None and x > 0 and y > 0:
            xs.append(float(x))
            ys.append(float(y))

    if len(xs) < 3:
        return None

    xs, ys = np.array(xs), np.array(ys)
    try:
        log_x = np.log(xs)
        log_y = np.log(ys)
        b, log_a = np.polyfit(log_x, log_y, 1)
        a = np.exp(log_a)
        y_pred = a * xs**b
        ss_res = np.sum((ys - y_pred) ** 2)
        ss_tot = np.sum((ys - np.mean(ys)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return a, b, r2
    except Exception:
        return None


def generate_comparison_table(baseline: list[dict], tuned: list[dict]) -> str:
    """Generate markdown comparison table."""
    max_baseline = get_max_objects(baseline)
    max_tuned = get_max_objects(tuned)

    # Find common object counts for comparison
    compare_points = [50000, 75000, 100000]
    # Add the max baseline count as a comparison point
    if max_baseline > 0:
        compare_points.append(int(max_baseline))

    lines = []
    lines.append("## Metric Comparison\n")

    # Overview
    lines.append(f"| | Baseline | Tuned |")
    lines.append(f"|---|---|---|")
    lines.append(f"| Total batches | {len(baseline)} | {len(tuned)} |")
    lines.append(f"| Max objects | {max_baseline:,.0f} | {max_tuned:,.0f} |")
    lines.append(f"| Duration | {_duration(baseline)} | {_duration(tuned)} |")
    lines.append("")

    # Per-metric comparison at common object counts
    for obj_count in sorted(set(compare_points)):
        if obj_count > max_baseline and obj_count > max_tuned:
            continue

        lines.append(f"\n### At {obj_count:,} objects\n")
        lines.append(f"| Metric | Baseline | Tuned | Delta | % Change |")
        lines.append(f"|--------|----------|-------|-------|----------|")

        for field, mcfg in METRICS.items():
            base_val = get_value_at_object_count(baseline, field, obj_count)
            tuned_val = get_value_at_object_count(tuned, field, obj_count)

            if base_val is None and tuned_val is None:
                continue

            scale = mcfg["scale"]
            fmt = mcfg["format"]
            unit = mcfg["display_unit"]

            base_str = f"{base_val * scale:{fmt}} {unit}" if base_val else "N/A"
            tuned_str = f"{tuned_val * scale:{fmt}} {unit}" if tuned_val else "N/A"

            if base_val and tuned_val:
                delta = (tuned_val - base_val) * scale
                pct = ((tuned_val - base_val) / base_val) * 100
                delta_str = f"{delta:+{fmt}} {unit}"
                pct_str = f"{pct:+.1f}%"
                # Mark improvement vs regression
                if mcfg["higher_is_worse"]:
                    if pct < -5:
                        pct_str += " :white_check_mark:"
                    elif pct > 5:
                        pct_str += " :warning:"
                else:
                    if pct > 5:
                        pct_str += " :white_check_mark:"
                    elif pct < -5:
                        pct_str += " :warning:"
            else:
                delta_str = "N/A"
                pct_str = "N/A"

            lines.append(f"| {mcfg['label']} | {base_str} | {tuned_str} | {delta_str} | {pct_str} |")

    # Peak values
    lines.append("\n### Peak Values\n")
    lines.append("| Metric | Baseline Peak | Tuned Peak | Delta | % Change |")
    lines.append("|--------|--------------|------------|-------|----------|")

    for field, mcfg in METRICS.items():
        base_peak, base_obj = get_peak_value(baseline, field)
        tuned_peak, tuned_obj = get_peak_value(tuned, field)

        if base_peak is None and tuned_peak is None:
            continue

        scale = mcfg["scale"]
        fmt = mcfg["format"]
        unit = mcfg["display_unit"]

        base_str = f"{base_peak * scale:{fmt}} {unit} (@{base_obj/1000:.0f}k)" if base_peak else "N/A"
        tuned_str = f"{tuned_peak * scale:{fmt}} {unit} (@{tuned_obj/1000:.0f}k)" if tuned_peak else "N/A"

        if base_peak and tuned_peak:
            pct = ((tuned_peak - base_peak) / base_peak) * 100
            delta = (tuned_peak - base_peak) * scale
            delta_str = f"{delta:+{fmt}}"
            pct_str = f"{pct:+.1f}%"
        else:
            delta_str = "N/A"
            pct_str = "N/A"

        lines.append(f"| {mcfg['label']} | {base_str} | {tuned_str} | {delta_str} | {pct_str} |")

    # Growth rates (power-law fit)
    lines.append("\n### Growth Rates (Power-Law Exponent)\n")
    lines.append("| Metric | Baseline b | Tuned b | R² (base) | R² (tuned) | Steeper? |")
    lines.append("|--------|-----------|---------|-----------|------------|----------|")

    for field, mcfg in METRICS.items():
        base_fit = fit_power_law(baseline, field)
        tuned_fit = fit_power_law(tuned, field)

        if base_fit is None and tuned_fit is None:
            continue

        base_b = f"{base_fit[1]:.4f}" if base_fit else "N/A"
        tuned_b = f"{tuned_fit[1]:.4f}" if tuned_fit else "N/A"
        base_r2 = f"{base_fit[2]:.4f}" if base_fit else "N/A"
        tuned_r2 = f"{tuned_fit[2]:.4f}" if tuned_fit else "N/A"

        if base_fit and tuned_fit:
            if abs(tuned_fit[1]) > abs(base_fit[1]):
                steeper = "Yes (tuned grows faster)"
            elif abs(tuned_fit[1]) < abs(base_fit[1]):
                steeper = "No (tuned grows slower)"
            else:
                steeper = "Same"
        else:
            steeper = "N/A"

        lines.append(f"| {mcfg['label']} | {base_b} | {tuned_b} | {base_r2} | {tuned_r2} | {steeper} |")

    return "\n".join(lines)


def _duration(entries: list[dict]) -> str:
    """Calculate test duration from timestamps."""
    if len(entries) < 2:
        return "N/A"
    try:
        t0 = datetime.fromisoformat(entries[0]["timestamp"])
        t1 = datetime.fromisoformat(entries[-1]["timestamp"])
        delta = t1 - t0
        hours = delta.total_seconds() / 3600
        return f"{hours:.1f}h"
    except (KeyError, ValueError):
        return "N/A"


def plot_overlay(baseline: list[dict], tuned: list[dict], field: str,
                 mcfg: dict, filename: str):
    """Plot baseline vs tuned overlay chart."""
    if not HAS_MATPLOTLIB:
        return

    scale = mcfg["scale"]
    label = mcfg["label"]
    unit = mcfg["display_unit"]

    # Extract data
    base_x, base_y = [], []
    for e in baseline:
        x = e.get("post_objects")
        y = e.get(field)
        if x and y and x > 0 and y > 0:
            base_x.append(float(x))
            base_y.append(float(y) * scale)

    tuned_x, tuned_y = [], []
    for e in tuned:
        x = e.get("post_objects")
        y = e.get(field)
        if x and y and x > 0 and y > 0:
            tuned_x.append(float(x))
            tuned_y.append(float(y) * scale)

    if not base_x and not tuned_x:
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    if base_x:
        ax.scatter(base_x, base_y, alpha=0.5, s=20, color="steelblue",
                   label=f"Baseline ({len(base_x)} pts)", zorder=2)
    if tuned_x:
        ax.scatter(tuned_x, tuned_y, alpha=0.5, s=20, color="darkorange",
                   label=f"Tuned ({len(tuned_x)} pts)", zorder=3)

    # Fit curves
    for data_x, data_y, color, name in [
        (base_x, base_y, "steelblue", "Baseline"),
        (tuned_x, tuned_y, "darkorange", "Tuned"),
    ]:
        if len(data_x) < 3:
            continue
        xs, ys = np.array(data_x), np.array(data_y)
        try:
            log_x = np.log(xs)
            log_y = np.log(ys)
            b, log_a = np.polyfit(log_x, log_y, 1)
            a = np.exp(log_a)
            x_smooth = np.linspace(xs.min() * 0.9, xs.max() * 1.1, 300)
            y_smooth = a * x_smooth**b
            ax.plot(x_smooth, y_smooth, color=color, linewidth=2, linestyle="--",
                    alpha=0.7, zorder=4, label=f"{name} fit")
        except Exception:
            pass

    ax.set_xlabel("etcd Object Count", fontsize=12)
    ax.set_ylabel(f"{label} ({unit})", fontsize=12)
    ax.set_title(f"{label}: Baseline vs Tuned (crossplane1)", fontsize=14)
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x/1000:.0f}k"))

    plt.tight_layout()
    out_path = CHARTS_DIR / filename
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"  {filename}")


def main():
    parser = argparse.ArgumentParser(description="Compare baseline vs tuned overnight results")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE,
                        help="Path to baseline JSONL file")
    parser.add_argument("--tuned", type=Path, default=DEFAULT_TUNED,
                        help="Path to tuned JSONL file")
    parser.add_argument("--output", type=Path, default=OUTPUT_MD,
                        help="Output markdown file")
    args = parser.parse_args()

    if not args.baseline.exists():
        print(f"ERROR: Baseline file not found: {args.baseline}")
        print("Run the baseline overnight test first, then archive it:")
        print("  cp results/overnight-log.json results/overnight-log-baseline.json")
        sys.exit(1)

    if not args.tuned.exists():
        print(f"ERROR: Tuned results file not found: {args.tuned}")
        print("Run the tuned overnight test first.")
        sys.exit(1)

    print("=" * 60)
    print("Comparing Overnight Results: Baseline vs Tuned")
    print("=" * 60)

    baseline = load_jsonl(args.baseline)
    tuned = load_jsonl(args.tuned)
    print(f"Baseline: {len(baseline)} entries from {args.baseline.name}")
    print(f"Tuned:    {len(tuned)} entries from {args.tuned.name}")
    print()

    # Generate comparison
    comparison = generate_comparison_table(baseline, tuned)

    # Build report
    report_lines = [
        "# etcd Tuning Comparison Report",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Cluster: crossplane1 (self-managed OpenShift)",
        "",
        "## Tuning Changes Applied",
        "",
        "| Parameter | Baseline | Tuned |",
        "|-----------|----------|-------|",
        "| auto-compaction-retention | 5m (default) | 1m |",
        "| quota-backend-bytes | 2 GiB (default) | 8 GiB |",
        "| snapshot-count | 100000 (default) | 25000 |",
        "",
        comparison,
        "",
        "## Interpretation",
        "",
        "### Expected impacts:",
        "- **Quota increase (2 GiB -> 8 GiB)**: Allows more objects before quota alarm. "
        "Should see higher max objects if DB size was the bottleneck.",
        "- **Compaction retention (5m -> 1m)**: More frequent compaction keeps DB smaller, "
        "may reduce fragmentation and improve read performance.",
        "- **Snapshot count (100k -> 25k)**: More frequent snapshots for faster recovery. "
        "May show minor WAL fsync overhead.",
        "",
        "### Key questions:",
        "1. Did max object count increase? (quota effect)",
        "2. Is etcd DB size smaller at equivalent object counts? (compaction effect)",
        "3. Did WAL fsync change? (snapshot-count effect)",
        "4. Are memory/CPU unchanged? (should be, not etcd-dependent)",
    ]

    report = "\n".join(report_lines) + "\n"

    # Write report
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        f.write(report)
    print(f"Wrote comparison report to {args.output}")

    # Generate overlay charts
    if HAS_MATPLOTLIB:
        print("\nGenerating overlay charts...")
        CHARTS_DIR.mkdir(parents=True, exist_ok=True)

        chart_configs = [
            ("memory_bytes", "compare_memory.png"),
            ("cpu_cores", "compare_cpu.png"),
            ("etcd_p99", "compare_etcd_p99.png"),
            ("api_p99", "compare_api_p99.png"),
            ("etcd_db_size_bytes", "compare_etcd_db_size.png"),
            ("wal_fsync_p99", "compare_wal_fsync.png"),
        ]

        for field, filename in chart_configs:
            plot_overlay(baseline, tuned, field, METRICS[field], filename)

        print(f"Charts saved to {CHARTS_DIR}/")
    else:
        print("\nWARNING: matplotlib not available, skipping charts.")

    # Print summary to stdout
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    max_base = get_max_objects(baseline)
    max_tune = get_max_objects(tuned)
    print(f"Baseline max objects: {max_base:,.0f}")
    print(f"Tuned max objects:    {max_tune:,.0f}")
    if max_base > 0:
        pct = ((max_tune - max_base) / max_base) * 100
        print(f"Object count change:  {pct:+.1f}%")

    # Compare at 100k if both reached it
    if max_base >= 100000 and max_tune >= 100000:
        print("\nAt 100,000 objects:")
        for field, mcfg in METRICS.items():
            base_val = get_value_at_object_count(baseline, field, 100000)
            tuned_val = get_value_at_object_count(tuned, field, 100000)
            if base_val and tuned_val:
                s = mcfg["scale"]
                pct = ((tuned_val - base_val) / base_val) * 100
                print(f"  {mcfg['label']}: {base_val*s:{mcfg['format']}} -> "
                      f"{tuned_val*s:{mcfg['format']}} {mcfg['display_unit']} ({pct:+.1f}%)")


if __name__ == "__main__":
    main()
