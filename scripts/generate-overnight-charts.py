#!/usr/bin/env python3
"""Generate capacity charts from overnight load-test data.

Reads results/overnight-log.json and results/overnight-model-coefficients.json,
produces scatter plots with power-law fit curves and threshold lines.

Usage:
    python3 scripts/generate-overnight-charts.py
"""

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / "results" / "overnight-log.json"
COEFF_FILE = PROJECT_ROOT / "results" / "overnight-model-coefficients.json"
CHARTS_DIR = PROJECT_ROOT / "report" / "charts"

RAMP_BATCHES = 10  # First N batches are ramp phase


def load_data():
    entries = []
    with open(DATA_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def load_coefficients():
    with open(COEFF_FILE) as f:
        return json.load(f)


def power_law(x, a, b):
    return a * np.power(x, b)


def plot_metric(entries, coeff, metric_key, y_field, y_label, filename,
                thresholds=None, y_scale=1.0, y_unit_label=""):
    """Plot a single metric with ramp/steady-state coloring and fit curve."""
    # Extract data
    ramp_x, ramp_y = [], []
    steady_x, steady_y = [], []
    for e in entries:
        x = e.get("post_objects")
        y = e.get(y_field)
        if x is None or y is None or x <= 0 or y <= 0:
            continue
        batch = e.get("batch", 999)
        if batch <= RAMP_BATCHES:
            ramp_x.append(float(x))
            ramp_y.append(float(y) * y_scale)
        else:
            steady_x.append(float(x))
            steady_y.append(float(y) * y_scale)

    ramp_x, ramp_y = np.array(ramp_x), np.array(ramp_y)
    steady_x, steady_y = np.array(steady_x), np.array(steady_y)

    fig, ax = plt.subplots(figsize=(10, 6))

    # Scatter: ramp vs steady-state
    if len(ramp_x) > 0:
        ax.scatter(ramp_x, ramp_y, alpha=0.8, s=40, color="steelblue",
                   label=f"Ramp ({len(ramp_x)} pts)", zorder=3)
    if len(steady_x) > 0:
        ax.scatter(steady_x, steady_y, alpha=0.4, s=20, color="gray",
                   label=f"Steady state ({len(steady_x)} pts)", zorder=2)

    # Fit curve
    if metric_key in coeff.get("models", {}):
        m = coeff["models"][metric_key]
        a, b, r2 = m["a"], m["b"], m["r2"]
        confidence = m.get("confidence", "?")
        all_x = np.concatenate([ramp_x, steady_x]) if len(steady_x) > 0 else ramp_x
        x_min = max(all_x.min() * 0.8, 1000)
        x_max = all_x.max() * 1.3
        x_smooth = np.linspace(x_min, x_max, 500)
        y_smooth = power_law(x_smooth, a, b) * y_scale
        ax.plot(x_smooth, y_smooth, color="red", linewidth=2, zorder=4,
                label=f"Power-law fit (R²={r2:.4f}, {confidence})")

    # Threshold lines
    if thresholds:
        colors = ["#e67e22", "#e74c3c", "#95a5a6"]
        for i, (label, value) in enumerate(thresholds.items()):
            color = colors[i % len(colors)]
            ax.axhline(y=value * y_scale, color=color, linestyle="--",
                       alpha=0.7, linewidth=1.5, label=label, zorder=1)

    ax.set_xlabel("etcd Object Count", fontsize=12)
    ax.set_ylabel(y_label, fontsize=12)
    ax.set_title(f"{y_label} vs Object Count (crossplane1 overnight)", fontsize=14)
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(True, alpha=0.3)

    # Format x-axis with k suffix
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x/1000:.0f}k"))

    plt.tight_layout()
    out_path = CHARTS_DIR / filename
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"  {filename}")
    return str(out_path)


def main():
    print("Generating overnight charts...")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    entries = load_data()
    coeff = load_coefficients()
    print(f"Loaded {len(entries)} entries, {len(coeff.get('models', {}))} models")
    print()

    # 1. Controller Memory (bytes → GiB)
    plot_metric(entries, coeff, "memory", "memory_bytes",
                "Controller Memory (GiB)", "crossplaneMemory.png",
                thresholds={"Warning (2 GiB)": 2 * 1024**3, "Critical (5 GiB)": 5 * 1024**3},
                y_scale=1 / (1024**3))

    # 2. Controller CPU (cores)
    plot_metric(entries, coeff, "cpu", "cpu_cores",
                "Controller CPU (cores)", "crossplaneCPU.png",
                thresholds={"Warning (1.5 cores)": 1.5, "Critical (3 cores)": 3.0})

    # 3. etcd P99 Latency (seconds → ms)
    plot_metric(entries, coeff, "etcd_p99", "etcd_p99",
                "etcd Request Latency P99 (ms)", "etcdLatencyP99.png",
                thresholds={"Warning (100ms)": 0.1, "Critical (500ms)": 0.5},
                y_scale=1000)

    # 4. API P99 Latency (seconds → ms)
    plot_metric(entries, coeff, "api_p99", "api_p99",
                "API Server Latency P99 (ms)", "apiserverLatencyP99.png",
                thresholds={"Warning (1s)": 1.0, "Critical (2s)": 2.0},
                y_scale=1000)

    # 5. etcd DB Size (bytes → MiB)
    plot_metric(entries, coeff, "etcd_db_size", "etcd_db_size_bytes",
                "etcd Database Size (MiB)", "etcdDbSize.png",
                thresholds={"50% quota (1 GiB)": 1024**3, "80% quota (1.6 GiB)": 1.6 * 1024**3},
                y_scale=1 / (1024**2))

    # 6. WAL Fsync P99 (seconds → ms)
    plot_metric(entries, coeff, "wal_fsync", "wal_fsync_p99",
                "WAL Fsync Latency P99 (ms)", "walFsyncP99.png",
                thresholds={"Warning (10ms)": 0.010, "Critical (25ms)": 0.025},
                y_scale=1000)

    print(f"\nDone. Charts saved to {CHARTS_DIR}/")


if __name__ == "__main__":
    main()
