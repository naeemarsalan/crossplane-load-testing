"""Capacity model fitting for Crossplane etcd metrics.

Fits linear, power-law, piecewise-linear, and log-linear models to correlate
etcd object count with observed metrics (latency, memory, request rate).

Supports holdout evaluation (MAPE, RMSE, R²), prediction intervals,
and confidence classification for operational decision-making.
"""

import numpy as np
from scipy.optimize import curve_fit
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class FitResult:
    """Result of a curve fit."""
    model_name: str
    params: np.ndarray
    r_squared: float
    equation: str
    predict: callable
    # Holdout evaluation metrics (populated by evaluate_holdout)
    holdout_mape: Optional[float] = None
    holdout_rmse: Optional[float] = None
    holdout_r2: Optional[float] = None
    # Prediction interval multiplier (populated by compute_prediction_intervals)
    residual_std: Optional[float] = None
    # Metadata
    confidence: Optional[str] = None
    fit_date: Optional[str] = None
    valid_range: Optional[tuple] = None

    def __repr__(self) -> str:
        conf = f", confidence={self.confidence}" if self.confidence else ""
        return f"FitResult(model={self.model_name}, R²={self.r_squared:.4f}, eq={self.equation}{conf})"

    def predict_interval(self, x_new, confidence_level: float = 0.95) -> tuple:
        """Return (predicted, lower_bound, upper_bound) at x_new.

        Uses residual_std with a z-multiplier for the interval.
        Falls back to point prediction if residual_std is not set.
        """
        y_pred = self.predict(np.asarray(x_new))
        if self.residual_std is None or self.residual_std == 0:
            return y_pred, y_pred, y_pred
        # z-multiplier: 1.96 for 95%, 2.576 for 99%
        z = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}.get(confidence_level, 1.96)
        margin = z * self.residual_std
        return y_pred, y_pred - margin, y_pred + margin


@dataclass
class ModelScorecard:
    """Summary of model selection and validation for one metric."""
    metric_name: str
    best_model: Optional[FitResult]
    all_candidates: list
    confidence: str
    valid_range: tuple
    fit_date: str


def _r_squared(y_actual: np.ndarray, y_predicted: np.ndarray) -> float:
    """Calculate R² (coefficient of determination)."""
    ss_res = np.sum((y_actual - y_predicted) ** 2)
    ss_tot = np.sum((y_actual - np.mean(y_actual)) ** 2)
    if ss_tot == 0:
        return 0.0
    return 1.0 - (ss_res / ss_tot)


def _mape(y_actual: np.ndarray, y_predicted: np.ndarray) -> float:
    """Mean Absolute Percentage Error. Excludes zero actuals."""
    mask = y_actual != 0
    if mask.sum() == 0:
        return float('inf')
    return float(np.mean(np.abs((y_actual[mask] - y_predicted[mask]) / y_actual[mask])) * 100)


def _rmse(y_actual: np.ndarray, y_predicted: np.ndarray) -> float:
    """Root Mean Square Error."""
    return float(np.sqrt(np.mean((y_actual - y_predicted) ** 2)))


# --- Model functions ---

def _linear(x, a, b):
    return a * x + b


def _quadratic(x, a, b, c):
    return a * x**2 + b * x + c


def _power_law(x, a, b):
    return a * np.power(x, b)


def _log_linear(x, a, b):
    """Log-linear: y = a * ln(x) + b"""
    return a * np.log(x) + b


def _piecewise_linear(x, x_break, a1, b1, a2):
    """Piecewise linear with one breakpoint.

    y = a1*x + b1           for x <= x_break
    y = a2*x + (a1-a2)*x_break + b1  for x > x_break
    (continuous at x_break)
    """
    b2 = (a1 - a2) * x_break + b1
    return np.where(x <= x_break, a1 * x + b1, a2 * x + b2)


def _saturating_exp(x, a, b, c):
    """Saturating exponential: y = a * (1 - e^(-b*x)) + c
    Captures plateau behavior — always positive, always bounded.
    """
    return a * (1.0 - np.exp(-b * x)) + c


def _sqrt_model(x, a, b):
    """Square root model: y = a * sqrt(x) + b
    Sublinear growth, simpler than power_law with exponent locked at 0.5.
    """
    return a * np.sqrt(x) + b


# --- Fitters ---

def fit_linear(x: np.ndarray, y: np.ndarray) -> Optional[FitResult]:
    """Fit linear model: y = a*x + b"""
    try:
        params, _ = curve_fit(_linear, x, y)
        y_pred = _linear(x, *params)
        r2 = _r_squared(y, y_pred)
        a, b = params
        return FitResult(
            model_name="linear",
            params=params,
            r_squared=r2,
            equation=f"y = {a:.6e} * x + {b:.6e}",
            predict=lambda x_new, p=params: _linear(np.asarray(x_new), *p),
        )
    except (RuntimeError, ValueError):
        return None


def fit_quadratic(x: np.ndarray, y: np.ndarray) -> Optional[FitResult]:
    """Fit quadratic model: y = a*x² + b*x + c"""
    if len(x) < 4:
        return None
    try:
        params, _ = curve_fit(_quadratic, x, y)
        y_pred = _quadratic(x, *params)
        r2 = _r_squared(y, y_pred)
        a, b, c = params
        return FitResult(
            model_name="quadratic",
            params=params,
            r_squared=r2,
            equation=f"y = {a:.6e} * x² + {b:.6e} * x + {c:.6e}",
            predict=lambda x_new, p=params: _quadratic(np.asarray(x_new), *p),
        )
    except (RuntimeError, ValueError):
        return None


def fit_power_law(x: np.ndarray, y: np.ndarray) -> Optional[FitResult]:
    """Fit power-law model: y = a * x^b"""
    mask = (x > 0) & (y > 0)
    if mask.sum() < 3:
        return None
    x_pos, y_pos = x[mask], y[mask]
    try:
        params, _ = curve_fit(_power_law, x_pos, y_pos, p0=[1.0, 1.0], maxfev=10000)
        y_pred = _power_law(x, *params)
        r2 = _r_squared(y, y_pred)
        a, b = params
        return FitResult(
            model_name="power_law",
            params=params,
            r_squared=r2,
            equation=f"y = {a:.6e} * x^{b:.4f}",
            predict=lambda x_new, p=params: _power_law(np.asarray(x_new), *p),
        )
    except (RuntimeError, ValueError):
        return None


def fit_log_linear(x: np.ndarray, y: np.ndarray) -> Optional[FitResult]:
    """Fit log-linear model: y = a * ln(x) + b"""
    mask = x > 0
    if mask.sum() < 3:
        return None
    x_pos, y_pos = x[mask], y[mask]
    try:
        params, _ = curve_fit(_log_linear, x_pos, y_pos, maxfev=10000)
        y_pred = _log_linear(x_pos, *params)
        r2 = _r_squared(y_pos, y_pred)
        a, b = params
        return FitResult(
            model_name="log_linear",
            params=params,
            r_squared=r2,
            equation=f"y = {a:.6e} * ln(x) + {b:.6e}",
            predict=lambda x_new, p=params: _log_linear(np.asarray(x_new), *p),
        )
    except (RuntimeError, ValueError):
        return None


def fit_saturating_exp(x: np.ndarray, y: np.ndarray) -> Optional[FitResult]:
    """Fit saturating exponential: y = a * (1 - e^(-b*x)) + c

    Captures plateau behavior (e.g. etcd P50, CPU flattening at high object counts).
    Always bounded — no extrapolation-goes-negative problem when a > 0 and c >= 0.
    """
    if len(x) < 4:
        return None
    try:
        y_range = y.max() - y.min()
        if y_range == 0:
            return None
        # Initial guesses: a ~ amplitude, b ~ 1/x_midrange, c ~ y_min
        x_mid = (x.max() + x.min()) / 2
        p0 = [y_range, 1.0 / max(x_mid, 1.0), y.min()]
        bounds = ([0, 0, -np.inf], [np.inf, np.inf, np.inf])
        params, _ = curve_fit(_saturating_exp, x, y, p0=p0, bounds=bounds, maxfev=20000)
        y_pred = _saturating_exp(x, *params)
        r2 = _r_squared(y, y_pred)
        a, b, c = params
        return FitResult(
            model_name="saturating_exp",
            params=params,
            r_squared=r2,
            equation=f"y = {a:.6e} * (1 - e^(-{b:.6e}*x)) + {c:.6e}",
            predict=lambda x_new, p=params: _saturating_exp(np.asarray(x_new), *p),
        )
    except (RuntimeError, ValueError):
        return None


def fit_sqrt(x: np.ndarray, y: np.ndarray) -> Optional[FitResult]:
    """Fit square root model: y = a * sqrt(x) + b

    Sublinear growth with exponent locked at 0.5.
    Easy to express in PromQL: a * sqrt(crossplane:etcd_object_count:total) + b
    """
    mask = x >= 0
    if mask.sum() < 3:
        return None
    x_pos, y_pos = x[mask], y[mask]
    try:
        params, _ = curve_fit(_sqrt_model, x_pos, y_pos, maxfev=10000)
        y_pred = _sqrt_model(x, *params)
        r2 = _r_squared(y, y_pred)
        a, b = params
        return FitResult(
            model_name="sqrt",
            params=params,
            r_squared=r2,
            equation=f"y = {a:.6e} * sqrt(x) + {b:.6e}",
            predict=lambda x_new, p=params: _sqrt_model(np.asarray(x_new), *p),
        )
    except (RuntimeError, ValueError):
        return None


def fit_piecewise_linear(x: np.ndarray, y: np.ndarray) -> Optional[FitResult]:
    """Fit piecewise-linear model with one breakpoint."""
    if len(x) < 6:
        return None
    try:
        x_mid = (x.min() + x.max()) / 2
        slope_est = (y[-1] - y[0]) / (x[-1] - x[0]) if x[-1] != x[0] else 0
        p0 = [x_mid, slope_est, y[0], slope_est * 1.5]
        bounds = (
            [x.min(), -np.inf, -np.inf, -np.inf],
            [x.max(), np.inf, np.inf, np.inf],
        )
        params, _ = curve_fit(_piecewise_linear, x, y, p0=p0, bounds=bounds, maxfev=20000)
        y_pred = _piecewise_linear(x, *params)
        r2 = _r_squared(y, y_pred)
        x_break, a1, b1, a2 = params
        return FitResult(
            model_name="piecewise_linear",
            params=params,
            r_squared=r2,
            equation=f"y = {a1:.6e}*x + {b1:.6e} (x<={x_break:.0f}), {a2:.6e}*x + {(a1-a2)*x_break+b1:.6e} (x>{x_break:.0f})",
            predict=lambda x_new, p=params: _piecewise_linear(np.asarray(x_new), *p),
        )
    except (RuntimeError, ValueError):
        return None


# --- Candidate evaluation ---

ALL_FITTERS = [fit_linear, fit_power_law, fit_log_linear, fit_piecewise_linear, fit_saturating_exp, fit_sqrt]


def fit_all_candidates(x: np.ndarray, y: np.ndarray) -> list:
    """Fit all candidate models and return list of FitResults (non-None)."""
    results = []
    for fit_fn in ALL_FITTERS:
        result = fit_fn(x, y)
        if result is not None:
            results.append(result)
    return results


def evaluate_holdout(fit: FitResult, x_holdout: np.ndarray, y_holdout: np.ndarray) -> FitResult:
    """Evaluate a fitted model on holdout data, populating holdout metrics."""
    y_pred = fit.predict(x_holdout)
    if isinstance(y_pred, (int, float)):
        y_pred = np.array([y_pred])
    y_pred = np.asarray(y_pred)
    fit.holdout_mape = _mape(y_holdout, y_pred)
    fit.holdout_rmse = _rmse(y_holdout, y_pred)
    fit.holdout_r2 = _r_squared(y_holdout, y_pred)
    return fit


def compute_prediction_intervals(fit: FitResult, x_train: np.ndarray, y_train: np.ndarray) -> FitResult:
    """Compute residual std on training data for prediction intervals."""
    y_pred = fit.predict(x_train)
    residuals = y_train - np.asarray(y_pred)
    fit.residual_std = float(np.std(residuals))
    return fit


def classify_confidence(fit: FitResult) -> str:
    """Classify model confidence based on holdout evaluation.

    Returns: 'high', 'medium', or 'low'.
    """
    mape = fit.holdout_mape if fit.holdout_mape is not None else 100.0
    r2 = fit.holdout_r2 if fit.holdout_r2 is not None else fit.r_squared

    if mape < 10 and r2 > 0.90:
        return "high"
    elif mape < 25 and r2 > 0.70:
        return "medium"
    else:
        return "low"


def best_fit(x: np.ndarray, y: np.ndarray) -> Optional[FitResult]:
    """Try all models and return the best one by training R².

    Prefers the highest R², but rejects models that predict negative values
    at reasonable extrapolation points (up to 3x the max observed x), since
    metrics like latency and memory cannot be negative.

    For holdout-validated selection, use select_best_model() instead.
    """
    results = []
    x_max = x.max()
    extrapolation_points = np.array([x_max * 1.5, x_max * 2, x_max * 3])

    for fit_fn in ALL_FITTERS:
        result = fit_fn(x, y)
        if result is None:
            continue

        # Reject models that go negative within extrapolation range
        if y.min() >= 0:
            preds = result.predict(extrapolation_points)
            if isinstance(preds, np.ndarray) and np.any(preds < 0):
                continue
            elif not isinstance(preds, np.ndarray) and preds < 0:
                continue

        results.append(result)

    if not results:
        fallback = []
        for fit_fn in ALL_FITTERS:
            result = fit_fn(x, y)
            if result is not None:
                fallback.append(result)
        if fallback:
            return max(fallback, key=lambda r: r.r_squared)
        return None

    return max(results, key=lambda r: r.r_squared)


def select_best_model(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_holdout: Optional[np.ndarray] = None,
    y_holdout: Optional[np.ndarray] = None,
) -> ModelScorecard:
    """Fit all candidates, evaluate on holdout, and select best model.

    Selection priority:
    1. If holdout data available: lowest holdout MAPE among non-negative models
    2. Fallback: highest training R²

    Returns a ModelScorecard with best model and all candidates.
    """
    candidates = fit_all_candidates(x_train, y_train)

    if not candidates:
        return ModelScorecard(
            metric_name="unknown",
            best_model=None,
            all_candidates=[],
            confidence="low",
            valid_range=(float(x_train.min()), float(x_train.max())),
            fit_date=datetime.now().strftime("%Y-%m-%d"),
        )

    x_max = x_train.max()
    extrapolation_points = np.array([x_max * 1.5, x_max * 2])

    # Filter out negative-predicting models for non-negative data
    viable = []
    for c in candidates:
        if y_train.min() >= 0:
            preds = c.predict(extrapolation_points)
            if isinstance(preds, np.ndarray) and np.any(preds < 0):
                continue
        viable.append(c)

    if not viable:
        viable = candidates

    # Evaluate on holdout if available
    if x_holdout is not None and y_holdout is not None and len(x_holdout) > 0:
        for c in viable:
            evaluate_holdout(c, x_holdout, y_holdout)
        # Select by lowest holdout MAPE
        scored = [c for c in viable if c.holdout_mape is not None and np.isfinite(c.holdout_mape)]
        if scored:
            best = min(scored, key=lambda c: c.holdout_mape)
        else:
            best = max(viable, key=lambda c: c.r_squared)
    else:
        best = max(viable, key=lambda c: c.r_squared)

    # Compute prediction intervals and confidence
    compute_prediction_intervals(best, x_train, y_train)
    best.confidence = classify_confidence(best)
    best.fit_date = datetime.now().strftime("%Y-%m-%d")
    best.valid_range = (float(x_train.min()), float(x_train.max()))

    return ModelScorecard(
        metric_name="unknown",
        best_model=best,
        all_candidates=candidates,
        confidence=best.confidence,
        valid_range=best.valid_range,
        fit_date=best.fit_date,
    )


def find_threshold(fit: FitResult, threshold: float, x_range: tuple = (0, 500000)) -> Optional[float]:
    """Find the x value where the fitted curve crosses a threshold.

    Uses binary search over x_range.
    """
    x_low, x_high = x_range
    y_low = fit.predict(x_low)
    y_high = fit.predict(x_high)

    # Check if threshold is within range
    if y_low > threshold or y_high < threshold:
        if y_low >= threshold:
            return x_low
        return None

    # Binary search
    for _ in range(100):
        x_mid = (x_low + x_high) / 2
        y_mid = fit.predict(x_mid)
        if abs(y_mid - threshold) / max(abs(threshold), 1e-10) < 0.001:
            return x_mid
        if y_mid < threshold:
            x_low = x_mid
        else:
            x_high = x_mid

    return (x_low + x_high) / 2


def generate_scorecard_md(scorecards: dict, output_path: str) -> str:
    """Generate capacity-model-scorecard.md from a dict of {metric_name: ModelScorecard}.

    Returns the path to the generated file.
    """
    lines = []
    lines.append("# Capacity Model Scorecard")
    lines.append("")
    lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Summary table
    lines.append("## Model Selection Summary")
    lines.append("")
    lines.append("| Metric | Model | Train R² | Holdout MAPE | Holdout RMSE | Holdout R² | Confidence | Valid Range |")
    lines.append("|--------|-------|----------|-------------|-------------|-----------|------------|-------------|")

    for name, sc in sorted(scorecards.items()):
        if sc.best_model is None:
            lines.append(f"| {name} | — | — | — | — | — | low | — |")
            continue
        m = sc.best_model
        mape_str = f"{m.holdout_mape:.1f}%" if m.holdout_mape is not None else "—"
        rmse_str = f"{m.holdout_rmse:.4g}" if m.holdout_rmse is not None else "—"
        h_r2_str = f"{m.holdout_r2:.4f}" if m.holdout_r2 is not None else "—"
        vr = f"{sc.valid_range[0]:,.0f} – {sc.valid_range[1]:,.0f}" if sc.valid_range else "—"
        lines.append(
            f"| {name} | {m.model_name} | {m.r_squared:.4f} | {mape_str} | {rmse_str} | {h_r2_str} | {sc.confidence} | {vr} |"
        )

    lines.append("")

    # Per-metric details
    lines.append("## Per-Metric Details")
    lines.append("")

    for name, sc in sorted(scorecards.items()):
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"- **Fit date**: {sc.fit_date}")
        lines.append(f"- **Confidence**: {sc.confidence}")
        if sc.valid_range:
            lines.append(f"- **Valid range**: {sc.valid_range[0]:,.0f} – {sc.valid_range[1]:,.0f} objects")
        lines.append("")

        if sc.best_model:
            m = sc.best_model
            lines.append(f"**Selected model**: {m.model_name}")
            lines.append(f"- Equation: `{m.equation}`")
            lines.append(f"- Training R²: {m.r_squared:.4f}")
            if m.residual_std is not None:
                lines.append(f"- Residual std: {m.residual_std:.4g}")
            lines.append("")

        if sc.all_candidates:
            lines.append("**All candidates evaluated**:")
            lines.append("")
            lines.append("| Model | Train R² | Holdout MAPE | Holdout RMSE |")
            lines.append("|-------|----------|-------------|-------------|")
            for c in sorted(sc.all_candidates, key=lambda c: c.r_squared, reverse=True):
                mape_str = f"{c.holdout_mape:.1f}%" if c.holdout_mape is not None else "—"
                rmse_str = f"{c.holdout_rmse:.4g}" if c.holdout_rmse is not None else "—"
                lines.append(f"| {c.model_name} | {c.r_squared:.4f} | {mape_str} | {rmse_str} |")
            lines.append("")

    # Change log
    lines.append("## Change Log")
    lines.append("")
    lines.append(f"| Date | Version | Change |")
    lines.append(f"|------|---------|--------|")
    lines.append(f"| {datetime.now().strftime('%Y-%m-%d')} | 1.0 | Initial model selection with holdout validation |")
    lines.append("")

    content = "\n".join(lines)
    with open(output_path, "w") as f:
        f.write(content)
    return output_path
