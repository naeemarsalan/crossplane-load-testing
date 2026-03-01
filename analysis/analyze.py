#!/usr/bin/env python3
"""Main entry point for Crossplane capacity planning analysis.

Reads kube-burner metric output (JSON), fits capacity models,
evaluates on holdout data, and generates a Markdown report with charts.

Usage:
    python analyze.py [--metrics-dir DIR] [--output-dir DIR] [--holdout-dir DIR]

The metrics directory should contain kube-burner's indexed output JSON files.
These are typically found in kube-burner/collected-metrics/ after a test run.
"""

import argparse
import glob
import json
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

from capacity_model import (
    best_fit,
    select_best_model,
    FitResult,
    ModelScorecard,
    generate_scorecard_md,
    find_threshold,
)
from report_generator import generate_report


def load_prometheus_timeseries(timeseries_dir: str) -> dict:
    """Load Prometheus range query results from JSON files.

    Each file is a Prometheus API /query_range response with structure:
    {"status": "success", "data": {"result": [{"values": [[ts, val], ...]}]}}

    Returns:
        Dict of {metric_name: DataFrame with columns [timestamp, value]}
    """
    result = {}
    json_files = glob.glob(os.path.join(timeseries_dir, "*.json"))
    for filepath in sorted(json_files):
        name = os.path.splitext(os.path.basename(filepath))[0]
        try:
            with open(filepath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  Skipping {filepath}: {e}")
            continue

        if data.get("status") != "success":
            print(f"  Skipping {name}: query error: {data.get('error', 'unknown')}")
            continue

        results = data.get("data", {}).get("result", [])
        if not results:
            print(f"  Skipping {name}: no data")
            continue

        # Take first result series
        values = results[0].get("values", [])
        points = []
        for ts, val in values:
            try:
                points.append({"timestamp": float(ts), "value": float(val)})
            except (ValueError, TypeError):
                continue

        if points:
            result[name] = pd.DataFrame(points)
            print(f"  {name}: {len(points)} data points")

    return result


def load_kube_burner_metrics(metrics_dir: str) -> dict:
    """Load metrics from kube-burner output or Prometheus time series.

    Prefers Prometheus time-series data in timeseries/ subdirectory (collected
    via direct Prometheus range queries during the test window). Falls back to
    kube-burner indexed JSON files.

    Returns:
        Dict of {metric_name: DataFrame with columns [timestamp, value]}
    """
    # Prefer Prometheus time-series data
    ts_dir = os.path.join(metrics_dir, "timeseries")
    if os.path.isdir(ts_dir):
        print(f"Found Prometheus time-series data in {ts_dir}")
        result = load_prometheus_timeseries(ts_dir)
        if result:
            return result

    json_files = glob.glob(os.path.join(metrics_dir, "*.json"), recursive=False)
    if not json_files:
        print(f"WARNING: No JSON files found in {metrics_dir}")
        return {}

    all_metrics = {}

    for filepath in sorted(json_files):
        try:
            with open(filepath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  Skipping {filepath}: {e}")
            continue

        if not isinstance(data, list):
            continue

        for entry in data:
            metric_name = entry.get("metricName", "")
            if not metric_name or metric_name in ("jobSummary", "alert"):
                continue

            value = entry.get("value")
            timestamp = entry.get("timestamp", 0)
            job_name = entry.get("jobName", "")
            if value is not None:
                if metric_name not in all_metrics:
                    all_metrics[metric_name] = []
                all_metrics[metric_name].append({
                    "timestamp": timestamp,
                    "value": float(value),
                    "jobName": job_name,
                })

    result = {}
    for name, points in all_metrics.items():
        if points:
            df = pd.DataFrame(points)
            df = df.sort_values("timestamp").reset_index(drop=True)
            result[name] = df

    return result


def load_csv_fallback(metrics_dir: str) -> dict:
    """Load metrics from CSV files as fallback."""
    csv_files = glob.glob(os.path.join(metrics_dir, "**", "*.csv"), recursive=True)
    result = {}
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath)
            name = os.path.splitext(os.path.basename(filepath))[0]
            result[name] = df
        except Exception as e:
            print(f"  Skipping {filepath}: {e}")
    return result


def load_soak_metrics(soak_dir: str) -> dict:
    """Load stepped-soak test metrics (steady-state averages per step).

    Each JSON file in soak_dir/timeseries/ contains one metric's data.
    For soak tests, each data point is a 5-minute average at a known
    object-count plateau — much lower noise than ramp-based snapshots.

    The data format is the same Prometheus range query JSON, but each
    "step" label in the file name (e.g. etcdLatencyP99_step3.json)
    corresponds to a soak window. If no step files exist, falls back
    to the standard loader with a preference for the last 60s of each
    soak window (lowest-noise portion).

    Returns:
        Dict of {metric_name: DataFrame with columns [timestamp, value]}
    """
    ts_dir = os.path.join(soak_dir, "timeseries")
    if not os.path.isdir(ts_dir):
        print(f"  Soak timeseries dir not found: {ts_dir}")
        return {}

    # Check for step-averaged summary files first
    summary_file = os.path.join(soak_dir, "soak-summary.json")
    if os.path.isfile(summary_file):
        print(f"  Loading soak summary from {summary_file}")
        try:
            with open(summary_file) as f:
                summary = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  Error reading soak summary: {e}")
            summary = {}

        result = {}
        for metric_name, steps in summary.items():
            points = []
            for step in steps:
                points.append({
                    "timestamp": float(step["timestamp"]),
                    "value": float(step["avg_value"]),
                })
            if points:
                result[metric_name] = pd.DataFrame(points)
                print(f"  {metric_name}: {len(points)} soak steps")
        if result:
            return result

    # Fall back to standard timeseries loader
    print(f"  No soak summary found, using raw timeseries from {ts_dir}")
    return load_prometheus_timeseries(ts_dir)


def correlate_with_object_count(
    metrics: dict,
    object_count_key: str = "objectCount",
) -> dict:
    """Correlate each metric with the object count at the same timestamp.

    Returns:
        Dict of {metric_name: (object_counts_array, values_array)}
    """
    if object_count_key not in metrics:
        for alt in ["etcdObjectCountTotal", "etcdObjectCountTimeSeries", "object_count"]:
            if alt in metrics:
                object_count_key = alt
                break
        else:
            print(f"WARNING: Object count metric not found.")
            print(f"  Available metrics: {list(metrics.keys())}")
            return {}

    obj_df = metrics[object_count_key]
    obj_timestamps = obj_df["timestamp"].values
    obj_values = obj_df["value"].values

    correlated = {}
    for metric_name, df in metrics.items():
        if metric_name == object_count_key:
            continue

        # For each metric data point, find the nearest object count
        x_vals = []
        y_vals = []
        for _, row in df.iterrows():
            ts = row["timestamp"]
            idx = np.argmin(np.abs(obj_timestamps - ts))
            x_vals.append(obj_values[idx])
            y_vals.append(row["value"])

        if x_vals:
            correlated[metric_name] = (np.array(x_vals), np.array(y_vals))

    correlated["object_count"] = (obj_values, obj_values)

    return correlated


# Key metrics to analyze (names match Prometheus timeseries file names)
# Dropped from projections (low R², no correlation):
#   apiserverInflightRequests (R²=0.12) — essentially random
#   apiserverErrorRate (R²=0.29) — flat zero with one outlier
#   apiserverRequestRate — kept as recording rule input for composite
#     latency models, but not published as standalone forecast
KEY_METRICS = [
    "etcdLatencyP99",
    "etcdLatencyP50",
    "apiserverLatencyP99",
    "apiserverLatencyP50",
    "crossplaneMemory",
    "crossplaneCPU",
]

# Thresholds from the capacity contract
THRESHOLDS = {
    "etcdLatencyP99": {
        "100ms warning": 0.1,
        "500ms critical": 0.5,
    },
    "apiserverLatencyP99": {
        "1s warning": 1.0,
        "2s critical": 2.0,
    },
    "crossplaneMemory": {
        "2GB warning": 2 * 1024**3,
        "4GB critical": 4 * 1024**3,
    },
    "crossplaneCPU": {
        "1.5 cores warning": 1.5,
        "3 cores critical": 3.0,
    },
}


def run_analysis(
    metrics_dir: str,
    output_dir: str,
    holdout_dir: Optional[str] = None,
    scorecard_path: Optional[str] = None,
    test_type: str = "ramp",
) -> None:
    """Main analysis pipeline with optional holdout validation.

    Args:
        test_type: "ramp" for continuous ramp data (existing pipeline),
                   "soak" for stepped soak data (steady-state averages).
    """
    print(f"Loading training metrics from: {metrics_dir} (test_type={test_type})")
    if test_type == "soak":
        raw_metrics = load_soak_metrics(metrics_dir)
        if not raw_metrics:
            print("  Soak loader returned empty, falling back to standard loader")
            raw_metrics = load_kube_burner_metrics(metrics_dir)
    else:
        raw_metrics = load_kube_burner_metrics(metrics_dir)

    if not raw_metrics:
        print("ERROR: No metrics data found. Run kube-burner first:")
        print("  make test-small  # smoke test")
        print("  make test-full   # full load test")
        sys.exit(1)

    print(f"Loaded {len(raw_metrics)} metric types:")
    for name, df in raw_metrics.items():
        print(f"  {name}: {len(df)} data points")

    # Load holdout data if provided
    holdout_correlated = {}
    if holdout_dir and os.path.isdir(holdout_dir):
        print(f"\nLoading holdout metrics from: {holdout_dir}")
        holdout_metrics = load_kube_burner_metrics(holdout_dir)
        if holdout_metrics:
            print(f"Loaded {len(holdout_metrics)} holdout metric types")
            holdout_correlated = correlate_with_object_count(holdout_metrics)
            print(f"Correlated {len(holdout_correlated)} holdout metrics with object count")
        else:
            print("WARNING: No holdout data found, proceeding without holdout validation")
    else:
        if holdout_dir:
            print(f"WARNING: Holdout directory not found: {holdout_dir}")
        print("Proceeding without holdout validation (training-only evaluation)")

    # Correlate training metrics with object count
    print("\nCorrelating training metrics with object count...")
    correlated = correlate_with_object_count(raw_metrics)

    if not correlated:
        print("ERROR: Could not correlate metrics with object count.")
        sys.exit(1)

    print(f"Correlated {len(correlated)} metrics with object count")

    # Fit models and evaluate
    print("\nFitting capacity models...")
    fits = {}
    metrics_data = {}
    scorecards = {}

    for metric_name in KEY_METRICS:
        if metric_name not in correlated:
            print(f"  {metric_name}: SKIPPED (no data)")
            continue

        x, y = correlated[metric_name]

        # Remove NaN/Inf
        mask = np.isfinite(x) & np.isfinite(y)
        x, y = x[mask], y[mask]

        if len(x) < 3:
            print(f"  {metric_name}: SKIPPED (too few data points: {len(x)})")
            continue

        # Get holdout data for this metric
        x_holdout, y_holdout = None, None
        if metric_name in holdout_correlated:
            xh, yh = holdout_correlated[metric_name]
            hm = np.isfinite(xh) & np.isfinite(yh)
            if hm.sum() >= 3:
                x_holdout, y_holdout = xh[hm], yh[hm]

        # Use select_best_model for full evaluation
        scorecard = select_best_model(x, y, x_holdout, y_holdout)
        scorecard.metric_name = metric_name
        scorecards[metric_name] = scorecard

        fit = scorecard.best_model
        fits[metric_name] = fit
        metrics_data[metric_name] = (x, y)

        if fit:
            conf_str = f", confidence={fit.confidence}" if fit.confidence else ""
            holdout_str = ""
            if fit.holdout_mape is not None:
                holdout_str = f", holdout_MAPE={fit.holdout_mape:.1f}%"
            print(f"  {metric_name}: {fit.model_name} (R²={fit.r_squared:.4f}{holdout_str}{conf_str})")
        else:
            print(f"  {metric_name}: No model could be fitted")

    # Also include any correlated metrics not in key list
    for metric_name, (x, y) in correlated.items():
        if metric_name in metrics_data or metric_name == "object_count":
            continue
        mask = np.isfinite(x) & np.isfinite(y)
        x, y = x[mask], y[mask]
        if len(x) >= 3:
            fit = best_fit(x, y)
            if fit and fit.r_squared > 0.5:
                fits[metric_name] = fit
                metrics_data[metric_name] = (x, y)

    # Generate scorecard
    if scorecard_path:
        print(f"\nGenerating model scorecard: {scorecard_path}")
        generate_scorecard_md(scorecards, scorecard_path)

    # Generate report
    print(f"\nGenerating report in: {output_dir}")
    report_path = generate_report(metrics_data, fits, THRESHOLDS, output_dir)
    print(f"\nAnalysis complete!")
    print(f"  Report: {report_path}")
    print(f"  Charts: {output_dir}/charts/")
    if scorecard_path:
        print(f"  Scorecard: {scorecard_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("CAPACITY PLANNING SUMMARY")
    print("=" * 60)

    for metric_name in KEY_METRICS:
        if metric_name not in fits or fits[metric_name] is None:
            continue
        fit = fits[metric_name]
        print(f"\n{metric_name}:")
        print(f"  Model: {fit.equation}")
        print(f"  R²: {fit.r_squared:.4f}")
        if fit.confidence:
            print(f"  Confidence: {fit.confidence}")
        if fit.holdout_mape is not None:
            print(f"  Holdout MAPE: {fit.holdout_mape:.1f}%")
        if fit.holdout_rmse is not None:
            print(f"  Holdout RMSE: {fit.holdout_rmse:.4g}")
        for count in [30000, 50000, 100000]:
            predicted, lower, upper = fit.predict_interval(count)
            if isinstance(predicted, np.ndarray):
                predicted = predicted.item()
            if isinstance(lower, np.ndarray):
                lower = lower.item()
            if isinstance(upper, np.ndarray):
                upper = upper.item()
            print(f"  At {count:>7,} objects: {predicted:.4g} [{lower:.4g} – {upper:.4g}]")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Crossplane capacity planning metrics from kube-burner output"
    )
    parser.add_argument(
        "--metrics-dir",
        default="kube-burner/collected-metrics",
        help="Directory containing kube-burner metric output (default: kube-burner/collected-metrics)",
    )
    parser.add_argument(
        "--output-dir",
        default="report",
        help="Directory for output report and charts (default: report)",
    )
    parser.add_argument(
        "--holdout-dir",
        default=None,
        help="Directory containing holdout/validation test metrics (optional)",
    )
    parser.add_argument(
        "--scorecard-path",
        default=None,
        help="Path for model scorecard output (default: monitoring/capacity-model-scorecard.md)",
    )
    parser.add_argument(
        "--test-type",
        choices=["ramp", "soak"],
        default="ramp",
        help="Test type: 'ramp' for continuous ramp data, 'soak' for stepped soak steady-state data (default: ramp)",
    )
    args = parser.parse_args()

    # Resolve relative to project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)

    metrics_dir = args.metrics_dir
    if not os.path.isabs(metrics_dir):
        metrics_dir = os.path.join(project_dir, metrics_dir)

    output_dir = args.output_dir
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(project_dir, output_dir)

    holdout_dir = args.holdout_dir
    if holdout_dir and not os.path.isabs(holdout_dir):
        holdout_dir = os.path.join(project_dir, holdout_dir)

    scorecard_path = args.scorecard_path
    if scorecard_path is None:
        scorecard_path = os.path.join(project_dir, "monitoring", "capacity-model-scorecard.md")
    elif not os.path.isabs(scorecard_path):
        scorecard_path = os.path.join(project_dir, scorecard_path)

    run_analysis(metrics_dir, output_dir, holdout_dir, scorecard_path, args.test_type)


if __name__ == "__main__":
    main()
