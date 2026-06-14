"""
Tests for slo_burn_rate.py — 15 test cases covering:
- Error budget calculations
- Burn rate math
- Multi-window alert evaluation
- Prometheus rule generation
- Fast/slow burn thresholds
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "runbooks"))

from slo_burn_rate import (
    BurnRateResult,
    SLOConfig,
    _label_to_minutes,
    compute_fast_slow_thresholds,
    evaluate_multiwindow_alerts,
    generate_prometheus_rules,
)


# ---------------------------------------------------------------------------
# Tests: SLOConfig
# ---------------------------------------------------------------------------


class TestSLOConfig:
    def test_from_percent_999(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        assert slo.target == pytest.approx(0.999)

    def test_error_budget_999(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        assert slo.error_budget == pytest.approx(0.001)

    def test_error_budget_minutes_30d(self) -> None:
        """99.9% SLO → 43.2 minutes error budget over 30 days."""
        slo = SLOConfig.from_percent(99.9, window_days=30)
        expected = 0.001 * 30 * 24 * 60  # 43.2
        assert slo.error_budget_minutes == pytest.approx(expected, rel=1e-6)

    def test_invalid_slo_above_1(self) -> None:
        with pytest.raises(ValueError):
            SLOConfig(target=1.5)

    def test_invalid_slo_zero(self) -> None:
        with pytest.raises(ValueError):
            SLOConfig(target=0.0)

    def test_invalid_window_days(self) -> None:
        with pytest.raises(ValueError):
            SLOConfig(target=0.999, window_days=0)


# ---------------------------------------------------------------------------
# Tests: BurnRateResult
# ---------------------------------------------------------------------------


class TestBurnRateResult:
    def test_burn_rate_at_slo_pace(self) -> None:
        """Error rate == error budget → burn rate == 1.0."""
        slo = SLOConfig.from_percent(99.9)
        result = BurnRateResult(slo=slo, observed_error_rate=0.001, window_minutes=60)
        assert result.burn_rate == pytest.approx(1.0)

    def test_burn_rate_14x(self) -> None:
        """14.4× threshold (Google SRE canonical fast burn)."""
        slo = SLOConfig.from_percent(99.9)
        result = BurnRateResult(slo=slo, observed_error_rate=0.0144, window_minutes=60)
        assert result.burn_rate == pytest.approx(14.4, rel=1e-3)

    def test_time_to_exhaustion_at_1x(self) -> None:
        """At 1× burn rate, exhaustion = full window (720 hours for 30d)."""
        slo = SLOConfig.from_percent(99.9, window_days=30)
        result = BurnRateResult(slo=slo, observed_error_rate=0.001, window_minutes=60)
        assert result.time_to_exhaustion_hours == pytest.approx(30 * 24, rel=1e-3)

    def test_time_to_exhaustion_at_14x(self) -> None:
        """At 14.4× burn rate, exhaustion = 720/14.4 = 50 hours."""
        slo = SLOConfig.from_percent(99.9, window_days=30)
        result = BurnRateResult(slo=slo, observed_error_rate=0.0144, window_minutes=60)
        assert result.time_to_exhaustion_hours == pytest.approx(50.0, rel=1e-2)

    def test_zero_error_rate_gives_infinite_tte(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        result = BurnRateResult(slo=slo, observed_error_rate=0.0, window_minutes=60)
        assert result.time_to_exhaustion_hours == float("inf")


# ---------------------------------------------------------------------------
# Tests: label_to_minutes
# ---------------------------------------------------------------------------


class TestLabelToMinutes:
    def test_1h_is_60_minutes(self) -> None:
        assert _label_to_minutes("1h") == 60

    def test_72h_is_4320_minutes(self) -> None:
        assert _label_to_minutes("72h") == 4320

    def test_30m_is_30_minutes(self) -> None:
        assert _label_to_minutes("30m") == 30

    def test_5m_is_5_minutes(self) -> None:
        assert _label_to_minutes("5m") == 5

    def test_unknown_label_raises(self) -> None:
        with pytest.raises(ValueError):
            _label_to_minutes("2s")


# ---------------------------------------------------------------------------
# Tests: multi-window alert evaluation
# ---------------------------------------------------------------------------


class TestMultiWindowAlerts:
    def _make_burn_rates(
        self,
        rate_1h: float = 0.0,
        rate_5m: float = 0.0,
        rate_6h: float = 0.0,
        rate_30m: float = 0.0,
        rate_24h: float = 0.0,
        rate_2h: float = 0.0,
        rate_72h: float = 0.0,
    ) -> dict[str, float]:
        return {
            "1h": rate_1h,
            "5m": rate_5m,
            "6h": rate_6h,
            "30m": rate_30m,
            "24h": rate_24h,
            "2h": rate_2h,
            "72h": rate_72h,
        }

    def test_all_alerts_quiet_at_zero_burn(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        alerts = evaluate_multiwindow_alerts(slo, self._make_burn_rates())
        assert not any(a.firing for a in alerts)

    def test_p0_fires_when_1h_and_5m_above_threshold(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        burn_rates = self._make_burn_rates(rate_1h=15.0, rate_5m=15.0)
        alerts = evaluate_multiwindow_alerts(slo, burn_rates)
        p0_alert = next(a for a in alerts if a.tier == "P0")
        assert p0_alert.firing

    def test_p0_does_not_fire_when_only_1h_above_threshold(self) -> None:
        """Requires BOTH windows to fire — prevents false positives from spikes."""
        slo = SLOConfig.from_percent(99.9)
        burn_rates = self._make_burn_rates(rate_1h=15.0, rate_5m=0.0)
        alerts = evaluate_multiwindow_alerts(slo, burn_rates)
        p0_alert = next(a for a in alerts if a.tier == "P0")
        assert not p0_alert.firing

    def test_returns_4_alert_tiers(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        alerts = evaluate_multiwindow_alerts(slo, {})
        assert len(alerts) == 4

    def test_alert_tiers_are_p0_p1_p2_p3(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        alerts = evaluate_multiwindow_alerts(slo, {})
        tiers = [a.tier for a in alerts]
        assert tiers == ["P0", "P1", "P2", "P3"]


# ---------------------------------------------------------------------------
# Tests: fast/slow burn thresholds
# ---------------------------------------------------------------------------


class TestFastSlowThresholds:
    def test_p0_canonical_threshold_is_14_4(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        thresholds = compute_fast_slow_thresholds(slo)
        assert thresholds["P0"]["canonical_threshold"] == pytest.approx(14.4)

    def test_p3_canonical_threshold_is_1_0(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        thresholds = compute_fast_slow_thresholds(slo)
        assert thresholds["P3"]["canonical_threshold"] == pytest.approx(1.0)

    def test_p0_exhaustion_under_72_hours(self) -> None:
        """P0 burn should exhaust budget quickly — well under 72 hours."""
        slo = SLOConfig.from_percent(99.9)
        thresholds = compute_fast_slow_thresholds(slo)
        assert thresholds["P0"]["time_to_exhaustion_hours"] < 72.0


# ---------------------------------------------------------------------------
# Tests: Prometheus rule generation
# ---------------------------------------------------------------------------


class TestPrometheusRules:
    def test_contains_all_four_alert_names(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        rules = generate_prometheus_rules(slo)
        for sev in ["P0", "P1", "P2", "P3"]:
            assert f"SLOBurnRate{sev}" in rules

    def test_contains_runbook_links(self) -> None:
        slo = SLOConfig.from_percent(99.9)
        rules = generate_prometheus_rules(slo)
        assert "Runbook:" in rules

    def test_contains_slo_comment(self) -> None:
        slo = SLOConfig.from_percent(99.95)
        rules = generate_prometheus_rules(slo)
        assert "99.95" in rules
