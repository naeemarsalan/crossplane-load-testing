#!/usr/bin/env python3
"""Refit capacity models from overnight load-test data.

Reads results/overnight-log.json, fits power-law models for each metric,
outputs results/overnight-model-coefficients.json and regenerates the
monitoring/capacity-model-scorecard.md.

Data strategy:
  The overnight test has two phases:
    - Ramp (batches 1-10): 18k → 108k objects, metrics scale clearly
    - Steady state (batches 11-66): ~100-115k objects, high variance from
      GC cycles and idle periods

  For memory, CPU, and latencies: use ramp data (clear x-axis variation).
  For etcd_db_size: use all data (monotonically increasing, agg=max).
  For wal_fsync: use all data (benefits from larger sample).
"""

import json
import sys
from pathlib import Path
from datetime import datetime

import numpy as np

# Add analysis/ to path so we can import capacity_model
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

from capacity_model import (
    fit_power_law,
    select_best_model,
    generate_scorecard_md,
    classify_confidence,
    compute_prediction_intervals,
    evaluate_holdout,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / "results" / "overnight-log.json"
OUTPUT_JSON = PROJECT_ROOT / "results" / "overnight-model-coefficients.json"
SCORECARD_PATH = PROJECT_ROOT / "monitoring" / "capacity-model-scorecard.md"

# Thresholds for data cleaning
MEMORY_OUTLIER_BYTES = 7 * 1024**3  # 7 GiB — Go GC spikes
API_IDLE_THRESHOLD = 0.05           # API P99 < 50ms = idle apiserver
BUCKET_SIZE = 2000                  # ±2k objects for x-axis dedup
RAMP_BATCHES = 10                   # First N batches form the ramp phase

# Metric config
METRICS = {
    "memory": {
        "field": "memory_bytes", "unit": "bytes",
        "clean_outlier_max": MEMORY_OUTLIER_BYTES,
        "agg": "p75", "ramp_only": True,
    },
    "cpu": {
        "field": "cpu_cores", "unit": "cores",
        "agg": "p75", "ramp_only": True,
    },
    "etcd_p99": {
        "field": "etcd_p99", "unit": "seconds",
        "agg": "p75", "ramp_only": True,
    },
    "api_p99": {
        "field": "api_p99", "unit": "seconds",
        "clean_idle_min": API_IDLE_THRESHOLD,
        "agg": "p75", "ramp_only": True,
    },
    "etcd_db_size": {
        "field": "etcd_db_size_bytes", "unit": "bytes",
        "agg": "max", "ramp_only": False,
    },
    "wal_fsync": {
        "field": "wal_fsync_p99", "unit": "seconds",
        "agg": "p75", "ramp_only": False,
    },
}


def load_data() -> list[dict]:
    """Load JSONL overnight log."""
    entries = []
    with open(DATA_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    print(f"Loaded {len(entries)} entries from {DATA_FILE.name}")
    return entries


def extract_xy(entries: list[dict], metric_cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """Extract (post_objects, metric_value) arrays from entries."""
    xs, ys = [], []
    for e in entries:
        x = e.get("post_objects")
        y = e.get(metric_cfg["field"])
        if x is not None and y is not None and x > 0 and y > 0:
            xs.append(float(x))
            ys.append(float(y))
    return np.array(xs), np.array(ys)


def clean_data(x: np.ndarray, y: np.ndarray, metric_name: str, cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """Apply metric-specific data cleaning."""
    n_before = len(x)
    mask = np.ones(len(x), dtype=bool)

    if cfg.get("clean_outlier_max") is not None:
        mask &= y <= cfg["clean_outlier_max"]

    if cfg.get("clean_idle_min") is not None:
        mask &= y >= cfg["clean_idle_min"]

    x, y = x[mask], y[mask]
    n_after = len(x)
    if n_after < n_before:
        print(f"  cleaned {n_before - n_after} points ({n_before} → {n_after})")

    return x, y


def deduplicate_buckets(x: np.ndarray, y: np.ndarray, agg: str = "p75",
                        bucket_size: int = BUCKET_SIZE) -> tuple[np.ndarray, np.ndarray]:
    """Aggregate y-values for similar object counts (±bucket_size)."""
    if len(x) == 0:
        return x, y

    buckets = {}
    for xi, yi in zip(x, y):
        key = round(xi / bucket_size) * bucket_size
        buckets.setdefault(key, []).append(yi)

    x_out = np.array(sorted(buckets.keys()))
    if agg == "max":
        y_out = np.array([np.max(buckets[k]) for k in sorted(buckets.keys())])
    elif agg == "p75":
        y_out = np.array([np.percentile(buckets[k], 75) for k in sorted(buckets.keys())])
    else:
        y_out = np.array([np.mean(buckets[k]) for k in sorted(buckets.keys())])

    print(f"  bucketed: {len(x)} → {len(x_out)} points (±{bucket_size}, agg={agg})")
    return x_out, y_out


def fit_metric(x: np.ndarray, y: np.ndarray, metric_name: str,
               x_full: np.ndarray = None, y_full: np.ndarray = None) -> tuple:
    """Fit models for a single metric.

    x, y: training data (possibly ramp-only)
    x_full, y_full: full dataset for valid_range calculation

    Returns (coefficient_dict, scorecard) or None.
    """
    if len(x) < 5:
        print(f"  {metric_name}: insufficient data ({len(x)} points), skipping")
        return None

    # Sort by x
    order = np.argsort(x)
    x, y = x[order], y[order]

    # 80/20 holdout split
    n = len(x)
    n_train = max(int(n * 0.8), 3)
    x_train, y_train = x[:n_train], y[:n_train]
    x_holdout, y_holdout = x[n_train:], y[n_train:]

    # Use select_best_model for full evaluation
    scorecard = select_best_model(x_train, y_train, x_holdout, y_holdout)
    scorecard.metric_name = metric_name

    best = scorecard.best_model
    if best is None:
        print(f"  {metric_name}: no model converged")
        return None, scorecard

    print(f"  best={best.model_name}, R²={best.r_squared:.4f}, confidence={best.confidence}")

    # Always fit power-law for PromQL compatibility
    pl = fit_power_law(x_train, y_train)
    if pl is None:
        print(f"  {metric_name}: power-law fit failed")
        return None, scorecard

    if x_holdout is not None and len(x_holdout) > 0:
        evaluate_holdout(pl, x_holdout, y_holdout)
    compute_prediction_intervals(pl, x_train, y_train)
    pl.confidence = classify_confidence(pl)

    # Valid range: use full dataset if provided
    if x_full is not None and len(x_full) > 0:
        pl.valid_range = (float(x_full.min()), float(x_full.max()))
    else:
        pl.valid_range = (float(x.min()), float(x.max()))

    a, b = float(pl.params[0]), float(pl.params[1])
    r2 = float(pl.r_squared)
    holdout_mape = float(pl.holdout_mape) if pl.holdout_mape is not None else None

    if best.model_name != "power_law":
        print(f"  power-law for PromQL: a={a:.6e}, b={b:.4f}, R²={r2:.4f}")

    # Sanity check: for metrics that should increase with object count,
    # reject negative exponents and fall back to a constrained fit
    if b < 0 and metric_name in ("memory", "cpu", "etcd_db_size"):
        print(f"  WARNING: negative exponent b={b:.4f} for {metric_name}, "
              f"using best model params instead")
        # Use best model if it has positive correlation
        if best.model_name == "power_law":
            a, b = float(best.params[0]), float(best.params[1])
            r2 = float(best.r_squared)

    # Override confidence when holdout set is too small for reliable R².
    # Use training R² and holdout MAPE for classification.
    confidence = pl.confidence
    n_holdout = n - n_train
    if n_holdout < 5:
        if holdout_mape is not None and holdout_mape < 10 and r2 > 0.90:
            confidence = "high"
        elif holdout_mape is not None and holdout_mape < 25 and r2 > 0.70:
            confidence = "medium"
        elif r2 > 0.85:
            confidence = "medium"

    result = {
        "a": a,
        "b": b,
        "r2": r2,
        "confidence": confidence,
        "best_model": best.model_name,
        "best_r2": float(best.r_squared),
        "holdout_mape": holdout_mape,
        "data_points": int(n),
        "train_points": int(n_train),
    }
    return result, scorecard


def main():
    print("=" * 60)
    print("Refit Capacity Models from Overnight Data")
    print("=" * 60)

    entries = load_data()

    results = {
        "fit_date": datetime.now().strftime("%Y-%m-%d"),
        "fit_cluster": "crossplane1",
        "data_points": len(entries),
        "valid_range": None,
        "models": {},
    }
    scorecards = {}

    # Split ramp vs steady-state entries
    ramp_entries = entries[:RAMP_BATCHES]
    print(f"Ramp phase: {len(ramp_entries)} batches "
          f"({ramp_entries[0]['post_objects']:.0f} → {ramp_entries[-1]['post_objects']:.0f} objects)")
    print(f"Steady state: {len(entries) - RAMP_BATCHES} batches")

    # Global valid range from all entries
    all_objects = [e["post_objects"] for e in entries if e.get("post_objects")]
    if all_objects:
        results["valid_range"] = [int(min(all_objects)), int(max(all_objects))]

    print()
    for metric_name, cfg in METRICS.items():
        print(f"Fitting {metric_name}...")

        # Extract full data and ramp data
        x_full, y_full = extract_xy(entries, cfg)

        if cfg.get("ramp_only"):
            x, y = extract_xy(ramp_entries, cfg)
            phase = "ramp"
        else:
            x, y = x_full.copy(), y_full.copy()
            phase = "all"
        print(f"  using {phase} data: {len(x)} points")

        x, y = clean_data(x, y, metric_name, cfg)
        x, y = deduplicate_buckets(x, y, agg=cfg.get("agg", "p75"))

        out = fit_metric(x, y, metric_name, x_full, y_full)
        if out is None:
            continue
        coeff, sc = out
        if coeff is None:
            continue

        results["models"][metric_name] = coeff
        scorecards[metric_name] = sc
        print()

    # Write coefficients JSON
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Wrote coefficients to {OUTPUT_JSON}")

    # Regenerate scorecard
    generate_scorecard_md(scorecards, str(SCORECARD_PATH))
    print(f"Wrote scorecard to {SCORECARD_PATH}")

    # Print summary
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"{'Metric':<15} {'a':>15} {'b':>8} {'R²':>8} {'Confidence':<10}")
    print("-" * 60)
    for name, m in results["models"].items():
        print(f"{name:<15} {m['a']:>15.6e} {m['b']:>8.4f} {m['r2']:>8.4f} {m['confidence']:<10}")

    print(f"\nValid range: {results['valid_range']}")
    print(f"Data points: {results['data_points']}")

    # Spot-check predictions at key points
    print("\nSpot-check predictions (power-law):")
    for check_objs in [50000, 100000, 150000]:
        print(f"  At {check_objs:,} objects:")
        for name, m in results["models"].items():
            val = m["a"] * (check_objs ** m["b"])
            unit = METRICS[name]["unit"]
            if unit == "bytes" and val > 1e9:
                print(f"    {name}: {val/1e9:.2f} GiB")
            elif unit == "bytes" and val > 1e6:
                print(f"    {name}: {val/1e6:.1f} MiB")
            elif unit == "seconds":
                print(f"    {name}: {val*1000:.1f} ms")
            elif unit == "cores":
                print(f"    {name}: {val:.2f} cores")
            else:
                print(f"    {name}: {val:.4g}")


if __name__ == "__main__":
    main()
