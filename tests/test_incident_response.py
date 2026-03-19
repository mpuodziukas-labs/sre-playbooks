"""
Tests for incident_response.py — 15 test cases covering:
- MTTD/MTTR/MTTA/MTTI/MTTM calculations
- Severity classification
- Escalation chain generation
- Postmortem generation
- Edge cases
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "runbooks"))

from incident_response import (
    DEFAULT_ESCALATION_CHAIN,
    SEVERITY_CONFIG,
    IncidentMetrics,
    Severity,
    build_escalation_timeline,
    classify_severity,
    generate_postmortem,
    parse_events,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    {"ts": "2026-03-18T14:00:00Z", "type": "incident_start", "severity": "P0",
     "msg": "Payment failures spiking", "actor": "prometheus"},
    {"ts": "2026-03-18T14:02:00Z", "type": "alert_fired",
     "msg": "PagerDuty P0 alert", "actor": "pagerduty"},
    {"ts": "2026-03-18T14:06:00Z", "type": "acknowledged",
     "msg": "On-call acked", "actor": "alice"},
    {"ts": "2026-03-18T14:14:00Z", "type": "identified",
     "msg": "Root cause: bad deploy", "actor": "alice"},
    {"ts": "2026-03-18T14:19:00Z", "type": "mitigated",
     "msg": "Rollback complete", "actor": "alice"},
    {"ts": "2026-03-18T14:47:00Z", "type": "resolved",
     "msg": "Incident closed", "actor": "alice"},
]


@pytest.fixture
def sample_metrics() -> IncidentMetrics:
    return parse_events(SAMPLE_EVENTS, "INC-TEST-001")


# ---------------------------------------------------------------------------
# Tests: MTTD/MTTA/MTTI/MTTM/MTTR calculations
# ---------------------------------------------------------------------------

class TestMetricsCalculations:

    def test_mttd_is_2_minutes(self, sample_metrics: IncidentMetrics) -> None:
        """Alert fired 2 minutes after incident start."""
        assert sample_metrics.mttd_minutes == pytest.approx(2.0)

    def test_mtta_is_4_minutes(self, sample_metrics: IncidentMetrics) -> None:
        """Acknowledged 4 minutes after alert fired."""
        assert sample_metrics.mtta_minutes == pytest.approx(4.0)

    def test_mtti_is_8_minutes(self, sample_metrics: IncidentMetrics) -> None:
        """Identified 8 minutes after acknowledged."""
        assert sample_metrics.mtti_minutes == pytest.approx(8.0)

    def test_mttm_is_19_minutes(self, sample_metrics: IncidentMetrics) -> None:
        """Mitigated 19 minutes after incident start."""
        assert sample_metrics.mttm_minutes == pytest.approx(19.0)

    def test_mttr_is_47_minutes(self, sample_metrics: IncidentMetrics) -> None:
        """Resolved 47 minutes after incident start."""
        assert sample_metrics.mttr_minutes == pytest.approx(47.0)

    def test_none_when_alert_missing(self) -> None:
        """MTTD is None when no alert_fired event exists."""
        events = [
            {"ts": "2026-03-18T14:00:00Z", "type": "incident_start", "severity": "P1",
             "msg": "Degradation", "actor": "human"},
            {"ts": "2026-03-18T14:30:00Z", "type": "resolved",
             "msg": "Fixed", "actor": "human"},
        ]
        metrics = parse_events(events)
        assert metrics.mttd_minutes is None
        assert metrics.mtta_minutes is None

    def test_mttr_is_none_when_unresolved(self) -> None:
        """MTTR is None when incident has no resolved event."""
        events = [
            {"ts": "2026-03-18T14:00:00Z", "type": "incident_start", "severity": "P2",
             "msg": "Degradation", "actor": "prometheus"},
        ]
        metrics = parse_events(events)
        assert metrics.mttr_minutes is None


# ---------------------------------------------------------------------------
# Tests: severity classification
# ---------------------------------------------------------------------------

class TestSeverityClassification:

    def test_high_error_rate_is_p0(self) -> None:
        sev = classify_severity(
            error_rate_percent=6.0,
            latency_p99_ms=200.0,
            availability_percent=99.5,
            active_user_impact_percent=5.0,
        )
        assert sev == Severity.P0

    def test_majority_user_impact_is_p0(self) -> None:
        sev = classify_severity(
            error_rate_percent=0.5,
            latency_p99_ms=300.0,
            availability_percent=99.9,
            active_user_impact_percent=60.0,
        )
        assert sev == Severity.P0

    def test_low_availability_is_p0(self) -> None:
        sev = classify_severity(
            error_rate_percent=0.5,
            latency_p99_ms=300.0,
            availability_percent=98.5,
            active_user_impact_percent=2.0,
        )
        assert sev == Severity.P0

    def test_moderate_error_rate_is_p1(self) -> None:
        sev = classify_severity(
            error_rate_percent=2.0,
            latency_p99_ms=500.0,
            availability_percent=99.6,
            active_user_impact_percent=5.0,
        )
        assert sev == Severity.P1

    def test_no_impact_is_p4(self) -> None:
        sev = classify_severity(
            error_rate_percent=0.0,
            latency_p99_ms=100.0,
            availability_percent=100.0,
            active_user_impact_percent=0.0,
        )
        assert sev == Severity.P4


# ---------------------------------------------------------------------------
# Tests: severity config
# ---------------------------------------------------------------------------

class TestSeverityConfig:

    def test_p0_response_target_is_5_minutes(self) -> None:
        assert SEVERITY_CONFIG[Severity.P0]["response_time_minutes"] == 5

    def test_p0_requires_war_room(self) -> None:
        assert SEVERITY_CONFIG[Severity.P0]["requires_war_room"] is True

    def test_p4_does_not_require_war_room(self) -> None:
        assert SEVERITY_CONFIG[Severity.P4]["requires_war_room"] is False

    def test_p0_burn_rate_multiplier(self) -> None:
        """P0 uses the canonical 14.4× fast burn threshold."""
        assert SEVERITY_CONFIG[Severity.P0]["error_budget_multiplier"] == pytest.approx(14.4)


# ---------------------------------------------------------------------------
# Tests: escalation chain
# ---------------------------------------------------------------------------

class TestEscalationChain:

    def test_p0_pages_all_5_tiers(self) -> None:
        now = datetime(2026, 3, 18, 14, 0, 0, tzinfo=timezone.utc)
        chain = build_escalation_timeline(now, Severity.P0)
        assert len(chain) == 5

    def test_p3_pages_only_1_tier(self) -> None:
        now = datetime(2026, 3, 18, 14, 0, 0, tzinfo=timezone.utc)
        chain = build_escalation_timeline(now, Severity.P3)
        assert len(chain) == 1

    def test_first_page_at_incident_start(self) -> None:
        now = datetime(2026, 3, 18, 14, 0, 0, tzinfo=timezone.utc)
        chain = build_escalation_timeline(now, Severity.P0)
        # Tier 1 is paged at incident start (offset 0)
        assert chain[0]["tier"] == "1"
        assert "14:00" in chain[0]["timestamp"]

    def test_escalation_chain_has_contact_info(self) -> None:
        now = datetime(2026, 3, 18, 14, 0, 0, tzinfo=timezone.utc)
        chain = build_escalation_timeline(now, Severity.P1)
        for ev in chain:
            assert ev["contacts"]  # non-empty


# ---------------------------------------------------------------------------
# Tests: postmortem generation
# ---------------------------------------------------------------------------

class TestPostmortem:

    def test_postmortem_contains_incident_id(self, sample_metrics: IncidentMetrics) -> None:
        pm = generate_postmortem(sample_metrics, "Test Incident")
        assert "INC-TEST-001" in pm

    def test_postmortem_contains_all_metric_rows(self, sample_metrics: IncidentMetrics) -> None:
        pm = generate_postmortem(sample_metrics, "Test Incident")
        assert "MTTD" in pm
        assert "MTTA" in pm
        assert "MTTM" in pm
        assert "MTTR" in pm

    def test_postmortem_is_valid_markdown(self, sample_metrics: IncidentMetrics) -> None:
        pm = generate_postmortem(sample_metrics, "Test Incident")
        # Should have h1 title and h2 sections
        assert pm.startswith("# Postmortem")
        assert "## Executive Summary" in pm
        assert "## Timeline" in pm
        assert "## Action Items" in pm

    def test_postmortem_marks_met_response_target(self, sample_metrics: IncidentMetrics) -> None:
        """MTTA was 4 min; P0 target is 5 min — should be ✅."""
        pm = generate_postmortem(sample_metrics, "Test Incident")
        # Response met: 4 min ≤ 5 min target → ✅
        assert "✅" in pm
