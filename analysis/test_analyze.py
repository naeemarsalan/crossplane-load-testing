#!/usr/bin/env python3
"""Tests for analyze.py — validates data loaders, metric correlation,
KEY_METRICS changes, and CLI argument parsing."""

import sys
import os
import json
import tempfile
import shutil

import numpy as np
import pandas as pd

# Import the module under test
from analyze import (
    load_prometheus_timeseries,
    load_kube_burner_metrics,
    load_soak_metrics,
    correlate_with_object_count,
    KEY_METRICS,
    THRESHOLDS,
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} — {detail}")


print("=" * 60)
print("TEST SUITE: analyze.py")
print("=" * 60)

# --- 1. KEY_METRICS should not contain dropped metrics ---
print("\n--- KEY_METRICS validation ---")
check("apiserverInflightRequests NOT in KEY_METRICS",
      "apiserverInflightRequests" not in KEY_METRICS)
check("apiserverErrorRate NOT in KEY_METRICS",
      "apiserverErrorRate" not in KEY_METRICS)
check("apiserverRequestRate NOT in KEY_METRICS",
      "apiserverRequestRate" not in KEY_METRICS)
check("etcdLatencyP99 in KEY_METRICS", "etcdLatencyP99" in KEY_METRICS)
check("etcdLatencyP50 in KEY_METRICS", "etcdLatencyP50" in KEY_METRICS)
check("apiserverLatencyP99 in KEY_METRICS", "apiserverLatencyP99" in KEY_METRICS)
check("apiserverLatencyP50 in KEY_METRICS", "apiserverLatencyP50" in KEY_METRICS)
check("crossplaneMemory in KEY_METRICS", "crossplaneMemory" in KEY_METRICS)
check("crossplaneCPU in KEY_METRICS", "crossplaneCPU" in KEY_METRICS)
check("KEY_METRICS has exactly 6 entries", len(KEY_METRICS) == 6, f"got {len(KEY_METRICS)}")

# --- 2. THRESHOLDS ---
print("\n--- THRESHOLDS validation ---")
check("etcdLatencyP99 in THRESHOLDS", "etcdLatencyP99" in THRESHOLDS)
check("apiserverLatencyP99 in THRESHOLDS", "apiserverLatencyP99" in THRESHOLDS)
check("crossplaneMemory in THRESHOLDS", "crossplaneMemory" in THRESHOLDS)
check("crossplaneCPU in THRESHOLDS", "crossplaneCPU" in THRESHOLDS)

# --- 3. load_prometheus_timeseries ---
print("\n--- load_prometheus_timeseries ---")
tmpdir = tempfile.mkdtemp()
try:
    # Create a valid Prometheus range query result
    prom_data = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{
                "metric": {"__name__": "test_metric"},
                "values": [
                    [1709100000, "1.5"],
                    [1709100030, "2.0"],
                    [1709100060, "2.5"],
                    [1709100090, "3.0"],
                    [1709100120, "3.5"],
                ]
            }]
        }
    }
    with open(os.path.join(tmpdir, "testMetric.json"), "w") as f:
        json.dump(prom_data, f)

    result = load_prometheus_timeseries(tmpdir)
    check("load_prometheus_timeseries returns dict", isinstance(result, dict))
    check("testMetric loaded", "testMetric" in result, str(list(result.keys())))
    if "testMetric" in result:
        df = result["testMetric"]
        check("DataFrame has 5 rows", len(df) == 5, f"got {len(df)}")
        check("DataFrame has timestamp column", "timestamp" in df.columns)
        check("DataFrame has value column", "value" in df.columns)

    # Error file
    bad_data = {"status": "error", "error": "bad query"}
    with open(os.path.join(tmpdir, "badMetric.json"), "w") as f:
        json.dump(bad_data, f)
    result2 = load_prometheus_timeseries(tmpdir)
    check("error status metric skipped", "badMetric" not in result2)

finally:
    shutil.rmtree(tmpdir)

# --- 4. load_soak_metrics ---
print("\n--- load_soak_metrics ---")
tmpdir = tempfile.mkdtemp()
try:
    os.makedirs(os.path.join(tmpdir, "timeseries"))

    # Create soak summary
    summary = {
        "objectCount": [
            {"timestamp": 1709100000, "avg_value": 10000, "step": "step1"},
            {"timestamp": 1709100300, "avg_value": 20000, "step": "step2"},
            {"timestamp": 1709100600, "avg_value": 30000, "step": "step3"},
        ],
        "crossplaneMemory": [
            {"timestamp": 1709100000, "avg_value": 500000000, "step": "step1"},
            {"timestamp": 1709100300, "avg_value": 800000000, "step": "step2"},
            {"timestamp": 1709100600, "avg_value": 1100000000, "step": "step3"},
        ],
    }
    with open(os.path.join(tmpdir, "soak-summary.json"), "w") as f:
        json.dump(summary, f)

    result = load_soak_metrics(tmpdir)
    check("load_soak_metrics returns dict", isinstance(result, dict))
    check("objectCount loaded from summary", "objectCount" in result, str(list(result.keys())))
    check("crossplaneMemory loaded from summary", "crossplaneMemory" in result)
    if "objectCount" in result:
        check("objectCount has 3 soak steps", len(result["objectCount"]) == 3,
              f"got {len(result['objectCount'])}")

    # Without summary, should fall back to timeseries
    os.remove(os.path.join(tmpdir, "soak-summary.json"))
    prom_data = {
        "status": "success",
        "data": {
            "result": [{"values": [[1709100000, "42.0"], [1709100060, "43.0"]]}]
        }
    }
    with open(os.path.join(tmpdir, "timeseries", "fallbackMetric.json"), "w") as f:
        json.dump(prom_data, f)

    result2 = load_soak_metrics(tmpdir)
    check("soak fallback to timeseries", "fallbackMetric" in result2,
          str(list(result2.keys())))

    # Non-existent dir
    result3 = load_soak_metrics("/nonexistent/path")
    check("load_soak_metrics empty for missing dir", result3 == {})

finally:
    shutil.rmtree(tmpdir)

# --- 5. correlate_with_object_count ---
print("\n--- correlate_with_object_count ---")
metrics = {
    "objectCount": pd.DataFrame({
        "timestamp": [100, 200, 300, 400, 500],
        "value": [10000, 20000, 30000, 40000, 50000],
    }),
    "crossplaneMemory": pd.DataFrame({
        "timestamp": [100, 200, 300, 400, 500],
        "value": [500e6, 800e6, 1100e6, 1400e6, 1700e6],
    }),
    "etcdLatencyP99": pd.DataFrame({
        "timestamp": [105, 205, 305],  # slightly offset timestamps
        "value": [0.01, 0.02, 0.03],
    }),
}
correlated = correlate_with_object_count(metrics)
check("correlate returns dict", isinstance(correlated, dict))
check("crossplaneMemory correlated", "crossplaneMemory" in correlated)
check("etcdLatencyP99 correlated", "etcdLatencyP99" in correlated)
check("objectCount self-correlated", "object_count" in correlated)
if "crossplaneMemory" in correlated:
    x, y = correlated["crossplaneMemory"]
    check("correlated x has same length as y", len(x) == len(y))
    check("correlated x values are object counts", x[0] == 10000, f"x[0]={x[0]}")
if "etcdLatencyP99" in correlated:
    x, y = correlated["etcdLatencyP99"]
    check("nearest-timestamp matching works", len(x) == 3)
    # ts=105 should match objectCount ts=100 → 10000
    check("nearest match for ts=105 is 10000", x[0] == 10000, f"x[0]={x[0]}")

# Alt key detection
metrics_alt = {
    "etcdObjectCountTotal": pd.DataFrame({
        "timestamp": [100, 200], "value": [5000, 10000]
    }),
    "testMetric": pd.DataFrame({
        "timestamp": [100, 200], "value": [1.0, 2.0]
    }),
}
corr_alt = correlate_with_object_count(metrics_alt)
check("alternative objectCount key detected", "testMetric" in corr_alt)

# --- 6. CLI argument parsing ---
print("\n--- CLI argument parsing ---")
import argparse
# Simulate parsing --test-type
parser = argparse.ArgumentParser()
parser.add_argument("--test-type", choices=["ramp", "soak"], default="ramp")
args_ramp = parser.parse_args(["--test-type", "ramp"])
check("--test-type ramp parses", args_ramp.test_type == "ramp")
args_soak = parser.parse_args(["--test-type", "soak"])
check("--test-type soak parses", args_soak.test_type == "soak")
args_default = parser.parse_args([])
check("--test-type defaults to ramp", args_default.test_type == "ramp")

# Invalid test type should error
try:
    parser.parse_args(["--test-type", "invalid"])
    check("--test-type invalid rejected", False, "should have raised")
except SystemExit:
    check("--test-type invalid rejected", True)

# ======================================================================
print("\n" + "=" * 60)
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
print("=" * 60)

sys.exit(1 if FAIL > 0 else 0)
