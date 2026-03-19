"""
Tests for chaos_engineering.py — 10 test cases covering:
- Blast radius calculator
- GameDay scenario templates
- Hypothesis/evidence tracking
- Recovery verification
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "runbooks"))

from chaos_engineering import (
    GAMEDAY_TEMPLATES,
    BlastRadiusResult,
    ChaosExperiment,
    FailureMode,
    ServiceDependency,
    calculate_blast_radius,
    gameday_cpu_spike,
    gameday_disk_fill,
    gameday_network_partition,
    gameday_pod_kill,
    verify_recovery,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def critical_service() -> ServiceDependency:
    return ServiceDependency(
        name="payment-api",
        traffic_weight=0.5,
        criticality="critical",
        has_circuit_breaker=False,
        has_fallback=False,
        slo_target=0.9999,
    )


@pytest.fixture
def resilient_service() -> ServiceDependency:
    return ServiceDependency(
        name="search-api",
        traffic_weight=0.2,
        criticality="low",
        has_circuit_breaker=True,
        has_fallback=True,
        slo_target=0.99,
    )


@pytest.fixture
def demo_services(
    critical_service: ServiceDependency,
    resilient_service: ServiceDependency,
) -> list[ServiceDependency]:
    return [critical_service, resilient_service]


# ---------------------------------------------------------------------------
# Tests: blast radius calculation
# ---------------------------------------------------------------------------

class TestBlastRadius:

    def test_critical_no_mitigations_is_high_risk(
        self, critical_service: ServiceDependency
    ) -> None:
        """Critical service with no circuit breaker or fallback = high/critical risk."""
        br = calculate_blast_radius([critical_service], FailureMode.NETWORK_PARTITION)
        assert br.risk_level in ("high", "critical")

    def test_resilient_service_is_low_risk(
        self, resilient_service: ServiceDependency
    ) -> None:
        """Low-criticality service with full mitigations = low risk."""
        br = calculate_blast_radius([resilient_service], FailureMode.POD_KILL)
        assert br.risk_level in ("low", "medium")

    def test_blast_radius_score_bounded_0_to_10(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        for mode in FailureMode:
            br = calculate_blast_radius(demo_services, mode)
            assert 0.0 <= br.blast_radius_score <= 10.0

    def test_network_partition_worse_than_pod_kill(
        self, critical_service: ServiceDependency
    ) -> None:
        """Network partitions have higher blast radius than pod kills (1.4× vs 0.8× multiplier)."""
        br_np = calculate_blast_radius([critical_service], FailureMode.NETWORK_PARTITION)
        br_pk = calculate_blast_radius([critical_service], FailureMode.POD_KILL)
        assert br_np.blast_radius_score > br_pk.blast_radius_score

    def test_mitigations_reduce_score(self) -> None:
        """Adding circuit breaker + fallback should reduce blast radius score."""
        no_mitigations = ServiceDependency(
            name="api", traffic_weight=0.3, criticality="high",
            has_circuit_breaker=False, has_fallback=False, slo_target=0.999
        )
        full_mitigations = ServiceDependency(
            name="api", traffic_weight=0.3, criticality="high",
            has_circuit_breaker=True, has_fallback=True, slo_target=0.999
        )
        br_none = calculate_blast_radius([no_mitigations], FailureMode.CPU_SPIKE)
        br_full = calculate_blast_radius([full_mitigations], FailureMode.CPU_SPIKE)
        assert br_full.blast_radius_score < br_none.blast_radius_score


# ---------------------------------------------------------------------------
# Tests: GameDay templates
# ---------------------------------------------------------------------------

class TestGameDayTemplates:

    def test_all_4_templates_registered(self) -> None:
        assert set(GAMEDAY_TEMPLATES.keys()) == {
            "network-partition", "pod-kill", "disk-fill", "cpu-spike"
        }

    def test_network_partition_has_rollback(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_network_partition(demo_services)
        assert len(exp.rollback_procedure) > 50
        assert "iptables" in exp.rollback_procedure

    def test_cpu_spike_abort_criteria_defined(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_cpu_spike(demo_services)
        assert len(exp.abort_criteria) >= 3

    def test_disk_fill_hypothesis_threshold(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_disk_fill(demo_services)
        assert exp.hypothesis.steady_state_threshold == 95.0

    def test_pod_kill_failure_mode(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_pod_kill(demo_services)
        assert exp.failure_mode == FailureMode.POD_KILL


# ---------------------------------------------------------------------------
# Tests: hypothesis and recovery verification
# ---------------------------------------------------------------------------

class TestHypothesisTracking:

    def test_hypothesis_confirmed_when_all_pass(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_network_partition(demo_services)
        exp.add_evidence("api_p99_latency_ms", 300.0)  # under 500ms threshold
        exp.add_evidence("api_p99_latency_ms", 420.0)  # under 500ms threshold
        assert exp.hypothesis_confirmed is True

    def test_hypothesis_refuted_when_any_fail(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_network_partition(demo_services)
        exp.add_evidence("api_p99_latency_ms", 300.0)
        exp.add_evidence("api_p99_latency_ms", 800.0)  # OVER 500ms threshold → fail
        assert exp.hypothesis_confirmed is False

    def test_should_abort_on_failed_evidence(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_cpu_spike(demo_services)
        exp.add_evidence("p99_latency_multiple_of_baseline", 12.0)  # WAY over 2× threshold
        assert exp.should_abort is True

    def test_verify_recovery_verdict_confirmed(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_pod_kill(demo_services)
        exp.add_evidence("availability_percent", 99.95)  # above 99.9% threshold
        result = verify_recovery(exp, recovery_time_seconds=28.0)
        assert result.hypothesis_confirmed is True
        assert "CONFIRMED" in result.verdict

    def test_verify_recovery_verdict_refuted(
        self, demo_services: list[ServiceDependency]
    ) -> None:
        exp = gameday_pod_kill(demo_services)
        exp.add_evidence("availability_percent", 98.5)  # BELOW 99.9% threshold
        result = verify_recovery(exp, recovery_time_seconds=180.0)
        assert result.hypothesis_confirmed is False
        assert "REFUTED" in result.verdict
