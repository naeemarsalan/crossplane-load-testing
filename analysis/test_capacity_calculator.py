"""Tests for capacity_calculator.py — forward and reverse capacity analysis."""

import math
import pytest
import numpy as np

from capacity_calculator import (
    ClusterSupply,
    CapacityResult,
    ModelSet,
    ThresholdSet,
    forward_capacity,
    reverse_capacity,
    predict_resource_at_count,
    make_power_law_model,
    DEFAULT_MODELS,
    DEFAULT_THRESHOLDS,
    CR_MULTIPLIERS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def standard_supply():
    """ROSA m5.xlarge: 2 workers, 3.5 cores, 14.5 GiB per node."""
    return ClusterSupply(
        worker_count=2,
        allocatable_cpu_per_node=3.5,
        allocatable_mem_per_node=14.5 * 1024**3,
        overhead_cpu=0.5,
        overhead_mem=2 * 1024**3,
    )


@pytest.fixture
def large_supply():
    """4 workers with same per-node specs."""
    return ClusterSupply(
        worker_count=4,
        allocatable_cpu_per_node=3.5,
        allocatable_mem_per_node=14.5 * 1024**3,
        overhead_cpu=0.5,
        overhead_mem=2 * 1024**3,
    )


@pytest.fixture
def models():
    return DEFAULT_MODELS


@pytest.fixture
def thresholds():
    return DEFAULT_THRESHOLDS


# ---------------------------------------------------------------------------
# predict_resource_at_count
# ---------------------------------------------------------------------------

class TestPredictResource:
    def test_returns_all_dimensions(self, models):
        result = predict_resource_at_count(models, 10000)
        assert "memory" in result
        assert "cpu" in result
        assert "etcd_p99" in result
        assert "api_p99" in result

    def test_positive_predictions(self, models):
        result = predict_resource_at_count(models, 10000)
        for key, val in result.items():
            assert val is not None
            assert val > 0, f"{key} should be positive"

    def test_monotonically_increasing(self, models):
        """More objects should predict more resource usage."""
        r1 = predict_resource_at_count(models, 10000)
        r2 = predict_resource_at_count(models, 50000)
        for key in ["memory", "cpu", "etcd_p99", "api_p99"]:
            assert r2[key] > r1[key], f"{key} should increase with object count"

    def test_missing_model(self):
        partial = ModelSet(memory=DEFAULT_MODELS.memory)
        result = predict_resource_at_count(partial, 10000)
        assert result["memory"] is not None
        assert result["cpu"] is None
        assert result["etcd_p99"] is None


# ---------------------------------------------------------------------------
# Forward capacity
# ---------------------------------------------------------------------------

class TestForwardCapacity:
    def test_basic_result(self, standard_supply, models, thresholds):
        result = forward_capacity(standard_supply, 10000, 500, models, thresholds)
        assert result.nodes_required_now >= 1
        assert result.nodes_required_14d >= result.nodes_required_now
        assert result.nodes_required_30d >= result.nodes_required_14d

    def test_monotonicity_more_objects(self, standard_supply, models, thresholds):
        """More current objects should require >= nodes."""
        r1 = forward_capacity(standard_supply, 10000, 0, models, thresholds)
        r2 = forward_capacity(standard_supply, 50000, 0, models, thresholds)
        assert r2.nodes_required_now >= r1.nodes_required_now

    def test_monotonicity_higher_growth(self, standard_supply, models, thresholds):
        """Higher growth rate should require more nodes at 14d/30d."""
        r1 = forward_capacity(standard_supply, 10000, 100, models, thresholds)
        r2 = forward_capacity(standard_supply, 10000, 5000, models, thresholds)
        assert r2.nodes_required_30d >= r1.nodes_required_30d

    def test_zero_growth(self, standard_supply, models, thresholds):
        """With zero growth, all horizons should match 'now'."""
        result = forward_capacity(standard_supply, 10000, 0, models, thresholds)
        assert result.nodes_required_now == result.nodes_required_14d
        assert result.nodes_required_now == result.nodes_required_30d

    def test_negative_growth_treated_as_zero(self, standard_supply, models, thresholds):
        """Negative growth should not reduce future projections."""
        result = forward_capacity(standard_supply, 10000, -500, models, thresholds)
        assert result.nodes_required_14d == result.nodes_required_now

    def test_zero_workers(self, models, thresholds):
        supply = ClusterSupply(0, 3.5, 14.5 * 1024**3, 0.5, 2 * 1024**3)
        result = forward_capacity(supply, 10000, 500, models, thresholds)
        assert result.nodes_required_now == 0
        assert result.bottleneck == "no_workers"

    def test_bottleneck_reported(self, standard_supply, models, thresholds):
        result = forward_capacity(standard_supply, 10000, 500, models, thresholds)
        assert result.bottleneck in ("memory", "cpu", "etcd_latency", "api_latency")

    def test_has_details(self, standard_supply, models, thresholds):
        result = forward_capacity(standard_supply, 10000, 500, models, thresholds)
        assert "now" in result.details
        assert "14d" in result.details
        assert "30d" in result.details
        assert "predicted" in result.details["now"]


# ---------------------------------------------------------------------------
# Reverse capacity
# ---------------------------------------------------------------------------

class TestReverseCapacity:
    def test_basic_result(self, standard_supply, models, thresholds):
        result = reverse_capacity(standard_supply, models, thresholds)
        assert result.max_objects_supported is not None
        assert result.max_objects_supported > 0
        assert result.max_claims_supported is not None
        assert result.max_claims_supported > 0

    def test_claims_conversion(self, standard_supply, models, thresholds):
        """max_claims = max_objects // 8 (VMDeployment multiplier)."""
        result = reverse_capacity(standard_supply, models, thresholds)
        assert result.max_claims_supported == result.max_objects_supported // 8

    def test_claims_custom_multiplier(self, standard_supply, models, thresholds):
        """Custom multiplier should change claims calculation."""
        result = reverse_capacity(standard_supply, models, thresholds, claims_multiplier=4)
        assert result.max_claims_supported == result.max_objects_supported // 4

    def test_more_workers_more_objects(self, models, thresholds):
        """More workers should support more objects."""
        small = ClusterSupply(2, 3.5, 14.5 * 1024**3, 0.5, 2 * 1024**3)
        large = ClusterSupply(4, 3.5, 14.5 * 1024**3, 0.5, 2 * 1024**3)
        r_small = reverse_capacity(small, models, thresholds)
        r_large = reverse_capacity(large, models, thresholds)
        # More workers should support at least as many objects
        # (latency-bound dimensions don't scale with workers, but mem/cpu do)
        assert r_large.max_objects_supported >= r_small.max_objects_supported

    def test_more_overhead_fewer_objects(self, models, thresholds):
        """More overhead should reduce max supported objects."""
        low_overhead = ClusterSupply(2, 3.5, 14.5 * 1024**3, 0.5, 1 * 1024**3)
        high_overhead = ClusterSupply(2, 3.5, 14.5 * 1024**3, 2.0, 6 * 1024**3)
        r_low = reverse_capacity(low_overhead, models, thresholds)
        r_high = reverse_capacity(high_overhead, models, thresholds)
        assert r_low.max_objects_supported >= r_high.max_objects_supported

    def test_bottleneck_is_valid(self, standard_supply, models, thresholds):
        result = reverse_capacity(standard_supply, models, thresholds)
        assert result.bottleneck in ("memory", "cpu", "etcd_latency", "api_latency", "unknown")

    def test_zero_workers(self, models, thresholds):
        supply = ClusterSupply(0, 3.5, 14.5 * 1024**3, 0.5, 2 * 1024**3)
        result = reverse_capacity(supply, models, thresholds)
        assert result.max_objects_supported == 0
        assert result.bottleneck == "no_workers"


# ---------------------------------------------------------------------------
# Bottleneck selection
# ---------------------------------------------------------------------------

class TestBottleneck:
    def test_memory_bottleneck(self, thresholds):
        """When memory is most constrained, it should be the bottleneck."""
        # Very small memory, plenty of CPU
        supply = ClusterSupply(
            worker_count=1,
            allocatable_cpu_per_node=100.0,
            allocatable_mem_per_node=2 * 1024**3,  # very small
            overhead_cpu=0.0,
            overhead_mem=0.0,
        )
        models = ModelSet(
            memory=DEFAULT_MODELS.memory,
            cpu=DEFAULT_MODELS.cpu,
        )
        result = reverse_capacity(supply, models, thresholds)
        assert result.bottleneck == "memory"

    def test_cpu_bottleneck(self, thresholds):
        """When CPU is most constrained, it should be the bottleneck."""
        supply = ClusterSupply(
            worker_count=1,
            allocatable_cpu_per_node=0.5,  # very small
            allocatable_mem_per_node=1000 * 1024**3,  # huge
            overhead_cpu=0.0,
            overhead_mem=0.0,
        )
        models = ModelSet(
            memory=DEFAULT_MODELS.memory,
            cpu=DEFAULT_MODELS.cpu,
        )
        result = reverse_capacity(supply, models, thresholds)
        assert result.bottleneck == "cpu"

    def test_etcd_bottleneck(self, thresholds):
        """When only etcd model is provided, it should be the bottleneck."""
        supply = ClusterSupply(
            worker_count=10,
            allocatable_cpu_per_node=100.0,
            allocatable_mem_per_node=1000 * 1024**3,
            overhead_cpu=0.0,
            overhead_mem=0.0,
        )
        # Only etcd model — no mem/cpu competition
        models = ModelSet(etcd_p99=DEFAULT_MODELS.etcd_p99)
        result = reverse_capacity(supply, models, thresholds)
        assert result.bottleneck == "etcd_latency"


# ---------------------------------------------------------------------------
# Confidence / extrapolation
# ---------------------------------------------------------------------------

class TestConfidence:
    def test_within_range_confidence(self, standard_supply, models, thresholds):
        """Within valid range, confidence should reflect model quality."""
        result = forward_capacity(standard_supply, 20000, 500, models, thresholds)
        # Memory model is high confidence, but overall is min across all
        assert result.confidence in ("high", "medium", "low")

    def test_extrapolation_lowers_confidence(self, standard_supply, thresholds):
        """Far beyond valid range should lower confidence."""
        # Valid range is 18047-115856, querying at 200000
        result = forward_capacity(standard_supply, 200000, 0, DEFAULT_MODELS, thresholds)
        assert result.confidence == "low"


# ---------------------------------------------------------------------------
# make_power_law_model
# ---------------------------------------------------------------------------

class TestMakePowerLawModel:
    def test_basic_creation(self):
        model = make_power_law_model(2.0, 0.5)
        assert model.model_name == "power_law"
        result = model.predict(np.array([100]))
        expected = 2.0 * 100**0.5
        assert abs(float(result[0]) - expected) < 0.01

    def test_with_metadata(self):
        model = make_power_law_model(1.0, 1.0, r2=0.99, confidence="high", valid_range=(0, 50000))
        assert model.r_squared == 0.99
        assert model.confidence == "high"
        assert model.valid_range == (0, 50000)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_zero_objects(self, standard_supply, models, thresholds):
        """Zero objects should still return a valid result."""
        result = forward_capacity(standard_supply, 0, 500, models, thresholds)
        assert result.nodes_required_now >= 0

    def test_very_large_object_count(self, standard_supply, models, thresholds):
        """Very large counts should not crash."""
        result = forward_capacity(standard_supply, 1000000, 0, models, thresholds)
        assert result.nodes_required_now >= 1

    def test_all_cr_multipliers(self, standard_supply, models, thresholds):
        """Each CR multiplier should produce valid claims."""
        for cr_type, mult in CR_MULTIPLIERS.items():
            result = reverse_capacity(standard_supply, models, thresholds, claims_multiplier=mult)
            assert result.max_claims_supported == result.max_objects_supported // mult, \
                f"Failed for {cr_type} (mult={mult})"
