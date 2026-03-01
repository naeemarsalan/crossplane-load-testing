"""Report generator for Crossplane capacity planning analysis.

Generates Markdown reports with embedded charts (saved as PNG).
"""

import os
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from capacity_model import FitResult


def plot_metric_vs_objects(
    x: np.ndarray,
    y: np.ndarray,
    fit: Optional[FitResult],
    metric_name: str,
    x_label: str = "etcd Object Count",
    y_label: str = "",
    output_path: str = "plot.png",
    thresholds: Optional[dict] = None,
) -> str:
    """Create a scatter plot with fitted curve overlay.

    Args:
        x: Object counts (x-axis).
        y: Metric values (y-axis).
        fit: Fitted model result.
        metric_name: Name for the chart title.
        x_label: X-axis label.
        y_label: Y-axis label.
        output_path: Path to save the PNG.
        thresholds: Dict of {label: value} for horizontal threshold lines.

    Returns:
        Path to the saved PNG file.
    """
    fig, ax = plt.subplots(figsize=(10, 6))

    # Scatter plot of raw data
    ax.scatter(x, y, alpha=0.6, s=20, color="steelblue", label="Observed")

    # Fitted curve
    if fit is not None:
        x_smooth = np.linspace(x.min(), x.max() * 1.2, 500)
        y_smooth = fit.predict(x_smooth)
        ax.plot(x_smooth, y_smooth, color="red", linewidth=2,
                label=f"{fit.model_name} (R²={fit.r_squared:.4f})")

    # Threshold lines
    if thresholds:
        for label, value in thresholds.items():
            ax.axhline(y=value, color="orange", linestyle="--", alpha=0.7, label=label)

    ax.set_xlabel(x_label, fontsize=12)
    ax.set_ylabel(y_label or metric_name, fontsize=12)
    ax.set_title(f"{metric_name} vs Object Count", fontsize=14)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()

    return output_path


def generate_report(
    metrics_data: dict,
    fits: dict,
    thresholds: dict,
    output_dir: str = "report",
) -> str:
    """Generate a Markdown capacity planning report.

    Args:
        metrics_data: Dict of {metric_name: (x_array, y_array)}.
        fits: Dict of {metric_name: FitResult}.
        thresholds: Dict of {metric_name: {threshold_label: threshold_value}}.
        output_dir: Directory for output files.

    Returns:
        Path to the generated Markdown report.
    """
    os.makedirs(output_dir, exist_ok=True)
    charts_dir = os.path.join(output_dir, "charts")
    os.makedirs(charts_dir, exist_ok=True)

    report_lines = []
    report_lines.append("# Crossplane etcd Capacity Planning Report")
    report_lines.append("")
    report_lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append("")

    # Executive Summary
    report_lines.append("## Executive Summary")
    report_lines.append("")

    summary_items = []
    for metric_name, fit in fits.items():
        if fit is None:
            continue
        summary_items.append(f"- **{metric_name}**: Best fit is **{fit.model_name}** "
                            f"(R²={fit.r_squared:.4f}): `{fit.equation}`")

        # Find threshold crossings
        if metric_name in thresholds:
            from capacity_model import find_threshold
            for t_label, t_value in thresholds[metric_name].items():
                crossing = find_threshold(fit, t_value)
                if crossing is not None:
                    summary_items.append(
                        f"  - {t_label} reached at **{crossing:,.0f}** objects"
                    )

    report_lines.extend(summary_items)
    report_lines.append("")

    # Object count summary
    if "object_count" in metrics_data:
        x_obj, _ = metrics_data["object_count"]
        report_lines.append(f"**Object count range tested**: {x_obj.min():,.0f} — {x_obj.max():,.0f}")
        report_lines.append("")

    # Detailed Analysis per metric
    report_lines.append("## Detailed Analysis")
    report_lines.append("")

    y_labels = {
        "etcd_request_latency_p99": "Latency (seconds)",
        "apiserver_request_latency_p99": "Latency (seconds)",
        "controller_memory_bytes": "Memory (bytes)",
        "apiserver_request_rate": "Requests/sec",
        "object_count": "Object Count",
    }

    for metric_name in sorted(metrics_data.keys()):
        x, y = metrics_data[metric_name]
        fit = fits.get(metric_name)

        report_lines.append(f"### {metric_name}")
        report_lines.append("")

        # Stats
        report_lines.append(f"- **Data points**: {len(x)}")
        report_lines.append(f"- **Range**: {y.min():.4g} — {y.max():.4g}")
        report_lines.append(f"- **Mean**: {y.mean():.4g}")

        if fit:
            report_lines.append(f"- **Best fit**: {fit.model_name} (R²={fit.r_squared:.4f})")
            report_lines.append(f"- **Equation**: `{fit.equation}`")

            # Predictions at key object counts
            for count in [10000, 30000, 50000, 100000, 200000]:
                predicted = fit.predict(count)
                if isinstance(predicted, np.ndarray):
                    predicted = predicted.item()
                report_lines.append(f"- **Predicted at {count:,} objects**: {predicted:.4g}")
        else:
            report_lines.append("- **Best fit**: No model could be fitted")

        report_lines.append("")

        # Generate chart
        chart_path = os.path.join(charts_dir, f"{metric_name}.png")
        plot_metric_vs_objects(
            x, y, fit, metric_name,
            y_label=y_labels.get(metric_name, metric_name),
            output_path=chart_path,
            thresholds=thresholds.get(metric_name),
        )
        rel_chart = os.path.relpath(chart_path, output_dir)
        report_lines.append(f"![{metric_name}]({rel_chart})")
        report_lines.append("")

    # Recommendations
    report_lines.append("## Recommendations")
    report_lines.append("")
    report_lines.append("Based on the analysis:")
    report_lines.append("")

    # Generate recommendations based on fits
    if "etcdLatencyP99" in fits and fits["etcdLatencyP99"]:
        fit = fits["etcdLatencyP99"]
        latency_at_100k = fit.predict(100000)
        if isinstance(latency_at_100k, np.ndarray):
            latency_at_100k = latency_at_100k.item()
        if latency_at_100k > 0.1:
            report_lines.append(
                f"1. **Scale before 100k objects**: Projected etcd P99 latency at 100k objects "
                f"is {latency_at_100k*1000:.0f}ms (exceeds 100ms threshold). "
                f"Consider scaling etcd or reducing object count before reaching this point."
            )
        else:
            report_lines.append(
                f"1. **etcd latency nominal at 100k**: Projected P99 is {latency_at_100k*1000:.0f}ms "
                f"at 100k objects. etcd should handle this load."
            )

    if "crossplaneMemory" in fits and fits["crossplaneMemory"]:
        fit = fits["crossplaneMemory"]
        mem_at_100k = fit.predict(100000)
        if isinstance(mem_at_100k, np.ndarray):
            mem_at_100k = mem_at_100k.item()
        mem_gb = mem_at_100k / (1024**3)
        report_lines.append(
            f"2. **Controller memory at 100k objects**: ~{mem_gb:.1f}GB. "
            f"{'Increase resource limits.' if mem_gb > 2 else 'Current limits should suffice.'}"
        )

    report_lines.append(
        "3. **Monitor `crossplane:days_until_object_limit:30k`** alert for proactive scaling."
    )
    report_lines.append(
        "4. **Re-run this analysis** after any significant change in workload patterns."
    )
    report_lines.append("")

    # Methodology
    report_lines.append("## Methodology")
    report_lines.append("")
    report_lines.append("- **Load generator**: kube-burner with Crossplane claims (VMDeployment, Disk, DNSZone, FirewallRuleSet)")
    report_lines.append("- **Mock resources**: provider-nop NopResources (real etcd objects, no cloud resources)")
    report_lines.append("- **Curve fitting**: scipy.optimize.curve_fit with linear, quadratic, and power-law models")
    report_lines.append("- **Best model selection**: Highest R² (coefficient of determination)")
    report_lines.append("- **Metrics source**: Prometheus via kube-burner metric collection")
    report_lines.append("- **ROSA note**: Direct etcd_mvcc metrics not available; using etcd_request_duration and apiserver metrics as proxies")
    report_lines.append("")

    # Write report
    report_path = os.path.join(output_dir, "capacity-report.md")
    with open(report_path, "w") as f:
        f.write("\n".join(report_lines))

    print(f"Report written to {report_path}")
    print(f"Charts saved in {charts_dir}/")
    return report_path
