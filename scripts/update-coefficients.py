#!/usr/bin/env python3
"""Update model coefficients across all locations from refit output.

Reads results/overnight-model-coefficients.json and updates:
1. analysis/capacity_calculator.py — DEFAULT_MODELS
2. monitoring/prometheus-rules.yaml — PrometheusRule CRD
3. monitoring/crossplane-rules-self-managed.yml — prediction rules, fixed-point, node-sizing
4. monitoring/capacity-contract.md — threshold table, valid range, confidence

Does NOT update archived files (monitoring/crossplane-rules-external.yml).

Usage:
    python3 scripts/update-coefficients.py --dry-run   # Show what would change
    python3 scripts/update-coefficients.py --apply      # Apply changes
"""

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COEFF_FILE = PROJECT_ROOT / "results" / "overnight-model-coefficients.json"

# Old coefficients (ROSA-fitted) for search-and-replace
OLD = {
    "memory":   {"a": "2.410377201e+06", "b": "0.675841", "r2": "0.9335"},
    "cpu":      {"a": "2.409721870e-03", "b": "0.589598", "r2": "0.7608"},
    "etcd_p99": {"a": "8.138691113e-04", "b": "0.544690", "r2": "0.3278"},
    "api_p99":  {"a": "7.494035261e-03", "b": "0.542813", "r2": "0.5184"},
}
OLD_VALID_RANGE = "6514-48035"
OLD_FIT_DATE = "2026-02-28"
OLD_FIT_CLUSTER = "rosa"

# Fixed-point values to recompute
FIXED_POINTS = [50000, 100000]


def load_coefficients() -> dict:
    with open(COEFF_FILE) as f:
        return json.load(f)


def format_coeff(val: float, metric: str) -> str:
    """Format coefficient in scientific notation matching the original style."""
    if metric == "memory":
        return f"{val:.6e}"
    elif metric == "cpu":
        return f"{val:.6e}"
    else:
        return f"{val:.6e}"


def update_file(filepath: Path, replacements: list[tuple[str, str]], dry_run: bool) -> int:
    """Apply a list of (old, new) replacements to a file. Returns count."""
    text = filepath.read_text()
    count = 0
    for old, new in replacements:
        if old in text:
            text = text.replace(old, new)
            count += 1
    if not dry_run and count > 0:
        filepath.write_text(text)
    return count


def compute_fixed_point(a: float, b: float, x: float) -> float:
    return a * (x ** b)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("--dry-run", "--apply"):
        print("Usage: python3 scripts/update-coefficients.py [--dry-run|--apply]")
        sys.exit(1)

    dry_run = sys.argv[1] == "--dry-run"
    mode = "DRY RUN" if dry_run else "APPLYING"
    print(f"=== Update Coefficients ({mode}) ===\n")

    coeff = load_coefficients()
    models = coeff["models"]
    new_date = coeff["fit_date"]
    new_cluster = coeff["fit_cluster"]
    vr = coeff["valid_range"]
    new_range = f"{vr[0]}-{vr[1]}"

    # Build new coefficient strings
    new = {}
    for metric in ["memory", "cpu", "etcd_p99", "api_p99"]:
        if metric not in models:
            print(f"WARNING: {metric} not in coefficients, keeping old values")
            new[metric] = OLD[metric]
            continue
        m = models[metric]
        new[metric] = {
            "a": format_coeff(m["a"], metric),
            "b": f'{m["b"]:.6f}',
            "r2": f'{m["r2"]:.4f}',
            "confidence": m["confidence"],
        }

    print("Coefficient mapping:")
    for metric in ["memory", "cpu", "etcd_p99", "api_p99"]:
        print(f"  {metric}: a={OLD[metric]['a']} → {new[metric]['a']}, "
              f"b={OLD[metric]['b']} → {new[metric]['b']}, "
              f"R²={OLD[metric]['r2']} → {new[metric]['r2']}")
    print(f"  valid_range: {OLD_VALID_RANGE} → {new_range}")
    print(f"  fit_date: {OLD_FIT_DATE} → {new_date}")
    print(f"  fit_cluster: {OLD_FIT_CLUSTER} → {new_cluster}")
    print()

    total = 0

    # --- 1. capacity_calculator.py ---
    path = PROJECT_ROOT / "analysis" / "capacity_calculator.py"
    reps = []
    for metric, key_map in [
        ("memory", {"a": "a", "b": "b", "r2": "r2"}),
        ("cpu", {"a": "a", "b": "b", "r2": "r2"}),
        ("etcd_p99", {"a": "a", "b": "b", "r2": "r2"}),
        ("api_p99", {"a": "a", "b": "b", "r2": "r2"}),
    ]:
        for field in ["a", "b", "r2"]:
            old_val = OLD[metric][field]
            new_val = new[metric][field] if isinstance(new[metric], dict) and field in new[metric] else old_val
            if old_val != new_val:
                reps.append((old_val, new_val))

    # Update valid range
    reps.append(("(6514, 48035)", f"({vr[0]}, {vr[1]})"))
    # Update confidence levels
    for metric in ["memory", "cpu", "etcd_p99", "api_p99"]:
        if isinstance(new[metric], dict) and "confidence" in new[metric]:
            pass  # Confidence strings in capacity_calculator use "high"/"medium"/"low" already

    n = update_file(path, reps, dry_run)
    print(f"  {path.name}: {n} replacements")
    total += n

    # --- 2. prometheus-rules.yaml ---
    path = PROJECT_ROOT / "monitoring" / "prometheus-rules.yaml"
    reps = []
    for metric in ["memory", "cpu", "etcd_p99", "api_p99"]:
        for field in ["a", "b"]:
            reps.append((OLD[metric][field], new[metric][field]))

    # Labels
    reps.append((f'fit_date: "{OLD_FIT_DATE}"', f'fit_date: "{new_date}"'))
    reps.append((f'valid_range: "{OLD_VALID_RANGE}"', f'valid_range: "{new_range}"'))
    for metric in ["memory", "cpu", "etcd_p99", "api_p99"]:
        reps.append((f'r2: "{OLD[metric]["r2"]}"', f'r2: "{new[metric]["r2"]}"'))

    # Recompute fixed-point expressions
    for x in FIXED_POINTS:
        x_label = f"{x//1000}k"
        for metric, record_prefix in [
            ("memory", "2.410377201e+06"),
            ("etcd_p99", "8.138691113e-04"),
            ("api_p99", "7.494035261e-03"),
            ("cpu", "2.409721870e-03"),
        ]:
            old_a = OLD[metric]["a"]
            old_b = OLD[metric]["b"]
            new_a = new[metric]["a"]
            new_b = new[metric]["b"]
            old_expr = f"{old_a} * ({x} ^ {old_b})"
            new_expr = f"{new_a} * ({x} ^ {new_b})"
            reps.append((old_expr, new_expr))

    n = update_file(path, reps, dry_run)
    print(f"  {path.name}: {n} replacements")
    total += n

    # --- 3. crossplane-rules-self-managed.yml ---
    path = PROJECT_ROOT / "monitoring" / "crossplane-rules-self-managed.yml"
    reps = []
    for metric in ["memory", "cpu", "etcd_p99", "api_p99"]:
        for field in ["a", "b"]:
            reps.append((OLD[metric][field], new[metric][field]))

    reps.append((f'fit_date: "{OLD_FIT_DATE}"', f'fit_date: "{new_date}"'))
    reps.append((f'valid_range: "{OLD_VALID_RANGE}"', f'valid_range: "{new_range}"'))
    reps.append((f'fit_cluster: "{OLD_FIT_CLUSTER}"', f'fit_cluster: "{new_cluster}"'))
    for metric in ["memory", "cpu", "etcd_p99", "api_p99"]:
        reps.append((f'r2: "{OLD[metric]["r2"]}"', f'r2: "{new[metric]["r2"]}"'))

    # Fixed-point expressions (same as above)
    for x in FIXED_POINTS:
        for metric in ["memory", "etcd_p99", "api_p99", "cpu"]:
            old_expr = f"{OLD[metric]['a']} * ({x} ^ {OLD[metric]['b']})"
            new_expr = f"{new[metric]['a']} * ({x} ^ {new[metric]['b']})"
            reps.append((old_expr, new_expr))

    # Node-sizing PromQL (uses memory and cpu coefficients inlined)
    # The node-sizing rules embed coefficients directly in PromQL expressions
    # These are caught by the a/b replacements above

    # Update max_objects and max_claims from reverse capacity calc
    # For now, compute using memory critical threshold (5 GiB = 5368709120)
    # and etcd latency critical (500ms)
    mem_a = models["memory"]["a"]
    mem_b = models["memory"]["b"]
    # max_objects = (threshold / a) ^ (1/b)
    mem_crit = 5 * 1024**3  # 5 GiB (new threshold from Phase 4)
    max_by_memory = int((mem_crit / mem_a) ** (1 / mem_b))

    # Use 150000 as a practical upper bound since overnight proved 106k
    max_objects = min(max_by_memory, 200000)
    max_claims = max_objects // 8

    reps.append(("29505 * vector(1)", f"{max_objects} * vector(1)"))
    reps.append(("3688 * vector(1)", f"{max_claims} * vector(1)"))

    n = update_file(path, reps, dry_run)
    print(f"  {path.name}: {n} replacements")
    total += n

    # --- 4. prometheus-self-managed.yml ---
    path = PROJECT_ROOT / "monitoring" / "prometheus-self-managed.yml"
    text = path.read_text()

    # Remove crossplane-rules.yml from rule_files (ROSA rules archived)
    new_text = text.replace(
        '  - "crossplane-rules.yml"\n  - "crossplane-rules-self-managed.yml"',
        '  - "crossplane-rules-self-managed.yml"'
    )

    # Remove rosa-federation scrape job (everything from "# Pull metrics from ROSA" to end)
    rosa_start = new_text.find("# Pull metrics from ROSA")
    if rosa_start > 0:
        new_text = new_text[:rosa_start].rstrip() + "\n"

    if not dry_run and new_text != text:
        path.write_text(new_text)
        print(f"  {path.name}: removed ROSA federation job and rules ref")
    else:
        print(f"  {path.name}: {'would remove' if dry_run else 'no changes'} ROSA federation job")
    total += 1

    print(f"\nTotal: {total} updates across 4 files")
    if dry_run:
        print("\nRun with --apply to make changes.")
    else:
        print("\nDone. Verify with: grep -rn '2026-02-28\\|6514' monitoring/ analysis/")


if __name__ == "__main__":
    main()
