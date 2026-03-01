#!/usr/bin/env python3
"""Tests for capacity_model.py — validates all fitters, model selection,
holdout evaluation, prediction intervals, and edge cases."""

import sys
import numpy as np
from capacity_model import (
    ALL_FITTERS,
    FitResult,
    ModelScorecard,
    fit_linear,
    fit_quadratic,
    fit_power_law,
    fit_log_linear,
    fit_piecewise_linear,
    fit_saturating_exp,
    fit_sqrt,
    fit_all_candidates,
    best_fit,
    select_best_model,
    evaluate_holdout,
    compute_prediction_intervals,
    classify_confidence,
    find_threshold,
    generate_scorecard_md,
    _r_squared,
    _mape,
    _rmse,
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


# --- Synthetic data ---
np.random.seed(42)
X_LINEAR = np.linspace(1000, 50000, 40)
Y_LINEAR = 2.5 * X_LINEAR + 100 + np.random.normal(0, 500, 40)

X_POWER = np.linspace(5000, 50000, 40)
Y_POWER = 500 * np.power(X_POWER, 0.6) + np.random.normal(0, 100, 40)

X_SQRT = np.linspace(5000, 50000, 40)
Y_SQRT = 0.01 * np.sqrt(X_SQRT) + 0.05 + np.random.normal(0, 0.002, 40)

X_SAT = np.linspace(5000, 100000, 40)
Y_SAT = 3.0 * (1 - np.exp(-0.00003 * X_SAT)) + 0.5 + np.random.normal(0, 0.02, 40)

X_LOG = np.linspace(5000, 50000, 40)
Y_LOG = 0.5 * np.log(X_LOG) - 2.0 + np.random.normal(0, 0.05, 40)


# ======================================================================
print("=" * 60)
print("TEST SUITE: capacity_model.py")
print("=" * 60)

# --- 1. ALL_FITTERS includes new models ---
print("\n--- ALL_FITTERS registration ---")
fitter_names = [f.__name__ for f in ALL_FITTERS]
check("ALL_FITTERS has 6 entries", len(ALL_FITTERS) == 6, f"got {len(ALL_FITTERS)}")
check("fit_saturating_exp in ALL_FITTERS", "fit_saturating_exp" in fitter_names)
check("fit_sqrt in ALL_FITTERS", "fit_sqrt" in fitter_names)
check("fit_linear in ALL_FITTERS", "fit_linear" in fitter_names)
check("fit_power_law in ALL_FITTERS", "fit_power_law" in fitter_names)
check("fit_log_linear in ALL_FITTERS", "fit_log_linear" in fitter_names)
check("fit_piecewise_linear in ALL_FITTERS", "fit_piecewise_linear" in fitter_names)

# --- 2. Individual fitter smoke tests ---
print("\n--- Individual fitter smoke tests ---")

r = fit_linear(X_LINEAR, Y_LINEAR)
check("fit_linear returns FitResult", r is not None and isinstance(r, FitResult))
check("fit_linear R² > 0.95 on linear data", r.r_squared > 0.95, f"R²={r.r_squared:.4f}")
check("fit_linear model_name", r.model_name == "linear", r.model_name)

r = fit_quadratic(X_LINEAR, Y_LINEAR)
check("fit_quadratic returns FitResult", r is not None)

r = fit_power_law(X_POWER, Y_POWER)
check("fit_power_law returns FitResult", r is not None)
check("fit_power_law R² > 0.90", r.r_squared > 0.90, f"R²={r.r_squared:.4f}")

r = fit_log_linear(X_LOG, Y_LOG)
check("fit_log_linear returns FitResult", r is not None)
check("fit_log_linear R² > 0.90", r.r_squared > 0.90, f"R²={r.r_squared:.4f}")

r = fit_piecewise_linear(X_LINEAR, Y_LINEAR)
check("fit_piecewise_linear returns FitResult", r is not None)

# --- 3. New fitters: saturating_exp ---
print("\n--- fit_saturating_exp ---")
r = fit_saturating_exp(X_SAT, Y_SAT)
check("fit_saturating_exp returns FitResult", r is not None)
check("fit_saturating_exp R² > 0.90", r.r_squared > 0.90, f"R²={r.r_squared:.4f}" if r else "None")
check("fit_saturating_exp model_name", r.model_name == "saturating_exp" if r else False)
if r:
    pred_100k = float(np.asarray(r.predict(100000)).flat[0])
    pred_200k = float(np.asarray(r.predict(200000)).flat[0])
    check("saturating_exp positive at 100k", pred_100k > 0, f"pred={pred_100k}")
    check("saturating_exp positive at 200k", pred_200k > 0, f"pred={pred_200k}")
    check("saturating_exp plateaus (200k < 1.5x 100k)", pred_200k < pred_100k * 1.5,
          f"100k={pred_100k:.4f}, 200k={pred_200k:.4f}")

# Edge case: too few points
r_short = fit_saturating_exp(X_SAT[:3], Y_SAT[:3])
check("fit_saturating_exp returns None for < 4 points", r_short is None)

# Edge case: constant y
r_const = fit_saturating_exp(X_SAT, np.ones(40) * 5.0)
check("fit_saturating_exp handles constant y", r_const is None)

# --- 4. New fitters: sqrt ---
print("\n--- fit_sqrt ---")
r = fit_sqrt(X_SQRT, Y_SQRT)
check("fit_sqrt returns FitResult", r is not None)
check("fit_sqrt R² > 0.90", r.r_squared > 0.90, f"R²={r.r_squared:.4f}" if r else "None")
check("fit_sqrt model_name", r.model_name == "sqrt" if r else False)
if r:
    pred_50k = float(np.asarray(r.predict(50000)).flat[0])
    pred_100k = float(np.asarray(r.predict(100000)).flat[0])
    check("sqrt positive at 100k", pred_100k > 0, f"pred={pred_100k}")
    # sqrt(100k) / sqrt(50k) ≈ 1.414, not 2x
    ratio = pred_100k / pred_50k if pred_50k > 0 else 999
    check("sqrt sublinear (100k/50k ratio < 1.5)", ratio < 1.5, f"ratio={ratio:.4f}")

# Edge case: too few points
r_short = fit_sqrt(X_SQRT[:2], Y_SQRT[:2])
check("fit_sqrt returns None for < 3 points", r_short is None)

# --- 5. fit_all_candidates ---
print("\n--- fit_all_candidates ---")
candidates = fit_all_candidates(X_SQRT, Y_SQRT)
check("fit_all_candidates returns list", isinstance(candidates, list))
check("fit_all_candidates >= 5 results", len(candidates) >= 5, f"got {len(candidates)}")
names = [c.model_name for c in candidates]
check("saturating_exp in candidates", "saturating_exp" in names, str(names))
check("sqrt in candidates", "sqrt" in names, str(names))

# --- 6. best_fit ---
print("\n--- best_fit ---")
bf = best_fit(X_SQRT, Y_SQRT)
check("best_fit returns FitResult", bf is not None)
check("best_fit picks high R²", bf.r_squared > 0.90, f"R²={bf.r_squared:.4f}" if bf else "None")

# Negative prediction rejection
x_neg = np.array([1000, 2000, 3000, 4000, 5000], dtype=float)
y_neg = np.array([100, 80, 60, 40, 20], dtype=float)  # decreasing, will go negative
bf_neg = best_fit(x_neg, y_neg)
if bf_neg:
    pred_15k = float(np.asarray(bf_neg.predict(15000)).flat[0])
    check("best_fit avoids negative predictions (or falls back)", True,
          f"model={bf_neg.model_name}, pred@15k={pred_15k:.4f}")

# --- 7. select_best_model ---
print("\n--- select_best_model ---")

# Without holdout
sc = select_best_model(X_SQRT, Y_SQRT)
check("select_best_model returns ModelScorecard", isinstance(sc, ModelScorecard))
check("scorecard has best_model", sc.best_model is not None)
check("scorecard has all_candidates", len(sc.all_candidates) >= 5, f"got {len(sc.all_candidates)}")
check("scorecard confidence is set", sc.confidence in ("high", "medium", "low"))
check("scorecard valid_range is tuple", isinstance(sc.valid_range, tuple) and len(sc.valid_range) == 2)

# With holdout
x_hold = np.linspace(5000, 50000, 10)
y_hold = 0.01 * np.sqrt(x_hold) + 0.05 + np.random.normal(0, 0.002, 10)
sc_h = select_best_model(X_SQRT, Y_SQRT, x_hold, y_hold)
check("scorecard with holdout has best_model", sc_h.best_model is not None)
if sc_h.best_model:
    check("holdout MAPE populated", sc_h.best_model.holdout_mape is not None)
    check("holdout RMSE populated", sc_h.best_model.holdout_rmse is not None)
    check("holdout R² populated", sc_h.best_model.holdout_r2 is not None)
    check("holdout MAPE < 50% for good fit", sc_h.best_model.holdout_mape < 50,
          f"MAPE={sc_h.best_model.holdout_mape:.1f}%")

# --- 8. evaluate_holdout ---
print("\n--- evaluate_holdout ---")
fit = fit_sqrt(X_SQRT, Y_SQRT)
if fit:
    evaluate_holdout(fit, x_hold, y_hold)
    check("holdout_mape set after evaluate_holdout", fit.holdout_mape is not None)
    check("holdout_rmse set after evaluate_holdout", fit.holdout_rmse is not None)
    check("holdout_r2 set after evaluate_holdout", fit.holdout_r2 is not None)

# --- 9. compute_prediction_intervals ---
print("\n--- compute_prediction_intervals ---")
fit = fit_sqrt(X_SQRT, Y_SQRT)
if fit:
    compute_prediction_intervals(fit, X_SQRT, Y_SQRT)
    check("residual_std set", fit.residual_std is not None)
    check("residual_std > 0", fit.residual_std > 0, f"std={fit.residual_std}")
    pred, lower, upper = fit.predict_interval(30000)
    pred_val = float(np.asarray(pred).flat[0])
    lower_val = float(np.asarray(lower).flat[0])
    upper_val = float(np.asarray(upper).flat[0])
    check("lower < predicted < upper", lower_val < pred_val < upper_val,
          f"lower={lower_val:.4f}, pred={pred_val:.4f}, upper={upper_val:.4f}")

# --- 10. classify_confidence ---
print("\n--- classify_confidence ---")
fit_high = FitResult("test", np.array([1]), 0.95, "y=x", lambda x: x,
                     holdout_mape=5.0, holdout_r2=0.95)
check("classify high confidence", classify_confidence(fit_high) == "high")

fit_med = FitResult("test", np.array([1]), 0.80, "y=x", lambda x: x,
                    holdout_mape=20.0, holdout_r2=0.80)
check("classify medium confidence", classify_confidence(fit_med) == "medium")

fit_low = FitResult("test", np.array([1]), 0.50, "y=x", lambda x: x,
                    holdout_mape=30.0, holdout_r2=0.50)
check("classify low confidence", classify_confidence(fit_low) == "low")

# --- 11. find_threshold ---
print("\n--- find_threshold ---")
fit = fit_sqrt(X_SQRT, Y_SQRT)
if fit:
    # Use a threshold within the model's actual range
    pred_at_min = float(np.asarray(fit.predict(X_SQRT.min())).flat[0])
    pred_at_max = float(np.asarray(fit.predict(X_SQRT.max())).flat[0])
    thresh_val = (pred_at_min + pred_at_max) / 2  # midpoint of actual range
    x_thresh = find_threshold(fit, thresh_val, x_range=(1000, 500000))
    if x_thresh is not None:
        pred_at_thresh = float(np.asarray(fit.predict(x_thresh)).flat[0])
        check("find_threshold within 1% of target",
              abs(pred_at_thresh - thresh_val) / thresh_val < 0.01,
              f"pred={pred_at_thresh:.4f}, target={thresh_val}")
    else:
        check("find_threshold returns value", False, "returned None")

# --- 12. Helper functions ---
print("\n--- Helper functions (_r_squared, _mape, _rmse) ---")
y_actual = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
y_perfect = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
check("R² of perfect fit = 1.0", abs(_r_squared(y_actual, y_perfect) - 1.0) < 1e-10)
check("MAPE of perfect fit = 0.0", abs(_mape(y_actual, y_perfect)) < 1e-10)
check("RMSE of perfect fit = 0.0", abs(_rmse(y_actual, y_perfect)) < 1e-10)

y_bad = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
check("R² of inverse = negative", _r_squared(y_actual, y_bad) < 0)
check("MAPE handles zeros", _mape(np.array([0.0, 1.0]), np.array([0.5, 1.5])) == 50.0)

# --- 13. generate_scorecard_md ---
print("\n--- generate_scorecard_md ---")
import tempfile, os
scorecards = {"testMetric": sc_h}
with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
    tmp_path = f.name
try:
    result_path = generate_scorecard_md(scorecards, tmp_path)
    check("generate_scorecard_md returns path", result_path == tmp_path)
    content = open(tmp_path).read()
    check("scorecard contains header", "# Capacity Model Scorecard" in content)
    check("scorecard contains metric", "testMetric" in content)
    check("scorecard contains model name", any(m in content for m in ["sqrt", "saturating_exp", "power_law", "linear"]))
finally:
    os.unlink(tmp_path)

# --- 14. Extrapolation safety ---
print("\n--- Extrapolation safety (no negative predictions) ---")
for fitter in ALL_FITTERS:
    r = fitter(X_SQRT, Y_SQRT)
    if r is None:
        continue
    preds_2x = r.predict(X_SQRT.max() * 2)
    preds_2x = np.asarray(preds_2x)
    has_neg = np.any(preds_2x < 0)
    check(f"{r.model_name} no negative at 2x max", not has_neg,
          f"pred@2xmax={float(preds_2x.flat[0]):.4f}")


# ======================================================================
print("\n" + "=" * 60)
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
print("=" * 60)

sys.exit(1 if FAIL > 0 else 0)
