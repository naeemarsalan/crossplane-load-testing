"""Capacity calculator for Crossplane node sizing.

Provides forward and reverse capacity analysis:
  - Forward: given current objects + growth rate, how many worker nodes are needed?
  - Reverse: given current cluster supply, how many objects/claims can we support?

Reuses FitResult and find_threshold from capacity_model.py.
"""

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from capacity_model import FitResult, find_threshold


# Object multiplication factors per CR type
CR_MULTIPLIERS = {
    "VMDeployment": 8,   # 6 NopResources + XR + Claim
    "Disk": 4,           # 2 NopResources + XR + Claim
    "DNSZone": 6,        # 4 NopResources + XR + Claim
    "FirewallRuleSet": 7, # 5 NopResources + XR + Claim
}

DEFAULT_MULTIPLIER = CR_MULTIPLIERS["VMDeployment"]


@dataclass
class ClusterSupply:
    """Describes available cluster resources."""
    worker_count: int
    allocatable_cpu_per_node: float      # cores
    allocatable_mem_per_node: float      # bytes
    overhead_cpu: float = 0.0            # cores reserved for system
    overhead_mem: float = 0.0            # bytes reserved for system


@dataclass
class CapacityResult:
    """Result of a capacity calculation."""
    # Forward mode
    nodes_required_now: Optional[int] = None
    nodes_required_14d: Optional[int] = None
    nodes_required_30d: Optional[int] = None
    # Reverse mode
    max_objects_supported: Optional[int] = None
    max_claims_supported: Optional[int] = None
    # Diagnostics
    bottleneck: str = "unknown"
    confidence: str = "low"
    headroom_pct: float = 0.0
    details: dict = field(default_factory=dict)


@dataclass
class ModelSet:
    """Collection of fitted models for capacity dimensions."""
    memory: Optional[FitResult] = None
    cpu: Optional[FitResult] = None
    etcd_p99: Optional[FitResult] = None
    api_p99: Optional[FitResult] = None


@dataclass
class ThresholdSet:
    """Thresholds for each capacity dimension."""
    memory_critical: float = 5 * 1024**3       # 5 GB
    memory_hard: float = 6 * 1024**3           # 6 GB
    etcd_p99_critical: float = 0.5             # 500ms
    etcd_p99_hard: float = 1.0                 # 1000ms
    api_p99_critical: float = 2.0              # 2s
    api_p99_hard: float = 5.0                  # 5s


def predict_resource_at_count(models: ModelSet, object_count: float) -> dict:
    """Predict resource usage at a given object count.

    Returns dict with keys: memory, cpu, etcd_p99, api_p99 (None if model missing).
    """
    result = {}
    x = np.array([object_count])
    for name, model in [("memory", models.memory), ("cpu", models.cpu),
                        ("etcd_p99", models.etcd_p99), ("api_p99", models.api_p99)]:
        if model is not None:
            val = model.predict(x)
            result[name] = float(val) if np.isscalar(val) else float(val[0])
        else:
            result[name] = None
    return result


def _confidence_for_models(models: ModelSet, object_count: float) -> str:
    """Determine overall confidence based on model quality and extrapolation."""
    confidences = []
    for model in [models.memory, models.cpu, models.etcd_p99, models.api_p99]:
        if model is None:
            continue
        conf = model.confidence or "low"
        # Downgrade if extrapolating beyond valid range
        if model.valid_range:
            _, x_max = model.valid_range
            if object_count > x_max * 2:
                conf = "low"
            elif object_count > x_max * 1.5 and conf == "high":
                conf = "medium"
        confidences.append(conf)

    if not confidences:
        return "low"
    # Overall confidence is the lowest individual confidence
    priority = {"high": 2, "medium": 1, "low": 0}
    worst = min(confidences, key=lambda c: priority.get(c, 0))
    return worst


def forward_capacity(
    supply: ClusterSupply,
    current_objects: float,
    growth_rate_per_day: float,
    models: ModelSet,
    thresholds: Optional[ThresholdSet] = None,
    target_util: float = 0.80,
    claims_multiplier: int = DEFAULT_MULTIPLIER,
) -> CapacityResult:
    """Forward capacity: how many nodes are needed now and in the future?

    Args:
        supply: Current cluster resource specification per node.
        current_objects: Current etcd object count.
        growth_rate_per_day: Object growth rate (objects/day).
        models: Fitted power-law models for each dimension.
        thresholds: Resource thresholds (uses defaults if None).
        target_util: Target utilization fraction (0-1).
        claims_multiplier: etcd objects per claim (default: 8 for VMDeployment).

    Returns:
        CapacityResult with nodes_required_now/14d/30d and diagnostics.
    """
    if thresholds is None:
        thresholds = ThresholdSet()

    if supply.worker_count <= 0:
        return CapacityResult(
            nodes_required_now=0, nodes_required_14d=0, nodes_required_30d=0,
            bottleneck="no_workers", confidence="low",
        )

    # Effective capacity per node (after overhead, with utilization target)
    eff_mem_per_node = (supply.allocatable_mem_per_node - supply.overhead_mem / max(supply.worker_count, 1)) * target_util
    eff_cpu_per_node = (supply.allocatable_cpu_per_node - supply.overhead_cpu / max(supply.worker_count, 1)) * target_util

    if eff_mem_per_node <= 0 or eff_cpu_per_node <= 0:
        return CapacityResult(
            nodes_required_now=0, nodes_required_14d=0, nodes_required_30d=0,
            bottleneck="overhead_exceeds_capacity", confidence="low",
        )

    result = CapacityResult()
    details = {}

    # Compute nodes needed at three time horizons
    horizons = {"now": 0, "14d": 14, "30d": 30}
    for label, days in horizons.items():
        obj_count = current_objects + max(growth_rate_per_day, 0) * days
        predicted = predict_resource_at_count(models, obj_count)

        nodes_by_mem = math.ceil(predicted["memory"] / eff_mem_per_node) if predicted["memory"] else 1
        nodes_by_cpu = math.ceil(predicted["cpu"] / eff_cpu_per_node) if predicted["cpu"] else 1
        nodes_needed = max(nodes_by_mem, nodes_by_cpu, 1)

        details[label] = {
            "object_count": obj_count,
            "predicted": predicted,
            "nodes_by_mem": nodes_by_mem,
            "nodes_by_cpu": nodes_by_cpu,
            "nodes_needed": nodes_needed,
        }

        if label == "now":
            result.nodes_required_now = nodes_needed
        elif label == "14d":
            result.nodes_required_14d = nodes_needed
        elif label == "30d":
            result.nodes_required_30d = nodes_needed

    # Determine bottleneck from current prediction
    now_detail = details["now"]
    if now_detail["nodes_by_mem"] >= now_detail["nodes_by_cpu"]:
        result.bottleneck = "memory"
    else:
        result.bottleneck = "cpu"

    # Check latency thresholds
    pred_now = now_detail["predicted"]
    if pred_now.get("etcd_p99") and pred_now["etcd_p99"] > thresholds.etcd_p99_critical:
        result.bottleneck = "etcd_latency"
    if pred_now.get("api_p99") and pred_now["api_p99"] > thresholds.api_p99_critical:
        result.bottleneck = "api_latency"

    # Headroom
    if result.nodes_required_now and result.nodes_required_now > 0:
        result.headroom_pct = max(0, (supply.worker_count - result.nodes_required_now) / supply.worker_count * 100)

    result.confidence = _confidence_for_models(models, current_objects)
    result.details = details
    return result


def reverse_capacity(
    supply: ClusterSupply,
    models: ModelSet,
    thresholds: Optional[ThresholdSet] = None,
    target_util: float = 0.80,
    claims_multiplier: int = DEFAULT_MULTIPLIER,
    search_range: tuple = (0, 500000),
) -> CapacityResult:
    """Reverse capacity: how many objects/claims can the current cluster support?

    Finds the minimum object count across all dimensions where a threshold
    would be breached.

    Args:
        supply: Current cluster resource specification.
        models: Fitted power-law models for each dimension.
        thresholds: Resource thresholds (uses defaults if None).
        target_util: Target utilization fraction (0-1).
        claims_multiplier: etcd objects per claim (default: 8 for VMDeployment).
        search_range: Binary search range for find_threshold.

    Returns:
        CapacityResult with max_objects_supported, max_claims_supported, bottleneck.
    """
    if thresholds is None:
        thresholds = ThresholdSet()

    if supply.worker_count <= 0:
        return CapacityResult(
            max_objects_supported=0, max_claims_supported=0,
            bottleneck="no_workers", confidence="low",
        )

    # Total effective capacity
    available_mem = (supply.worker_count * supply.allocatable_mem_per_node - supply.overhead_mem) * target_util
    available_cpu = (supply.worker_count * supply.allocatable_cpu_per_node - supply.overhead_cpu) * target_util

    limits = {}

    # Memory limit: find object count where predicted memory = available memory
    if models.memory is not None and available_mem > 0:
        max_by_mem = find_threshold(models.memory, available_mem, x_range=search_range)
        limits["memory"] = max_by_mem

    # CPU limit
    if models.cpu is not None and available_cpu > 0:
        max_by_cpu = find_threshold(models.cpu, available_cpu, x_range=search_range)
        limits["cpu"] = max_by_cpu

    # etcd latency limit
    if models.etcd_p99 is not None:
        max_by_etcd = find_threshold(models.etcd_p99, thresholds.etcd_p99_critical, x_range=search_range)
        limits["etcd_latency"] = max_by_etcd

    # API latency limit
    if models.api_p99 is not None:
        max_by_api = find_threshold(models.api_p99, thresholds.api_p99_critical, x_range=search_range)
        limits["api_latency"] = max_by_api

    # Find the binding constraint
    finite_limits = {k: v for k, v in limits.items() if v is not None}

    result = CapacityResult()
    result.details = {"limits": limits, "finite_limits": finite_limits}

    if finite_limits:
        bottleneck_key = min(finite_limits, key=finite_limits.get)
        max_objects = int(finite_limits[bottleneck_key])
        result.max_objects_supported = max_objects
        result.max_claims_supported = max_objects // claims_multiplier
        result.bottleneck = bottleneck_key

        # Headroom based on a reasonable current guess (use memory model midpoint)
        if models.memory and models.memory.valid_range:
            _, x_max = models.memory.valid_range
            current_estimate = x_max
        else:
            current_estimate = max_objects * 0.5
        result.headroom_pct = max(0, (max_objects - current_estimate) / max_objects * 100) if max_objects > 0 else 0
    else:
        result.max_objects_supported = None
        result.max_claims_supported = None
        result.bottleneck = "unknown"
        result.confidence = "low"
        result.details["status"] = "outside_modeled_bounds"
        return result

    # Overall confidence
    conf_candidates = []
    for model in [models.memory, models.cpu, models.etcd_p99, models.api_p99]:
        if model is not None:
            conf_candidates.append(model.confidence or "low")
    priority = {"high": 2, "medium": 1, "low": 0}
    result.confidence = min(conf_candidates, key=lambda c: priority.get(c, 0)) if conf_candidates else "low"

    return result


def make_power_law_model(a: float, b: float, r2: float = 0.0,
                         confidence: str = "medium",
                         valid_range: tuple = (0, 100000)) -> FitResult:
    """Create a FitResult from known power-law coefficients.

    Useful for constructing models from recording rule coefficients
    without re-fitting from data.
    """
    params = np.array([a, b])
    return FitResult(
        model_name="power_law",
        params=params,
        r_squared=r2,
        equation=f"y = {a:.6e} * x^{b:.4f}",
        predict=lambda x_new, a=a, b=b: a * np.power(np.asarray(x_new, dtype=float), b),
        confidence=confidence,
        valid_range=valid_range,
    )


# Pre-built models from crossplane-rules-external.yml coefficients
DEFAULT_MODELS = ModelSet(
    memory=make_power_law_model(2.739965e+07, 0.475558, r2=0.9389, confidence="high", valid_range=(18047, 115856)),
    cpu=make_power_law_model(3.290412e-03, 0.594721, r2=0.9425, confidence="medium", valid_range=(18047, 115856)),
    etcd_p99=make_power_law_model(6.570291e-01, -0.201361, r2=0.5872, confidence="low", valid_range=(18047, 115856)),
    api_p99=make_power_law_model(1.032016e+00, -0.004637, r2=0.4779, confidence="low", valid_range=(18047, 115856)),
)

DEFAULT_THRESHOLDS = ThresholdSet()
