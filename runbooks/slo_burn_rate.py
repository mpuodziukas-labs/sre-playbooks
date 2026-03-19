#!/usr/bin/env python3
"""
SLO Burn Rate Calculator — Google SRE Book Chapter 5 Implementation

Implements multi-window burn rate alerting as described in
"Implementing SLOs" (Google SRE Workbook, Chapter 5).

Key concepts:
  - Error budget = 1 - SLO target
  - Burn rate = (error rate) / (1 - SLO)  →  1.0 = consuming budget at exactly the SLO rate
  - Fast burn (1h window):  14.4× threshold  → burns 2% budget in 1h (P0)
  - Slow burn (72h window): 1.0×  threshold  → burns 100% budget in 30d (P3)

Multi-window alerting prevents both false positives (short spikes) and
missed incidents (slow persistent degradation).

Usage:
    python3 slo_burn_rate.py --slo 99.9 --error-rate 0.015
    python3 slo_burn_rate.py --slo 99.95 --window-1h 14.4 --window-6h 6.0
    python3 slo_burn_rate.py --demo
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Final


# ---------------------------------------------------------------------------
# Constants from Google SRE Workbook Chapter 5
# ---------------------------------------------------------------------------

ALERT_WINDOWS: Final[dict[str, int]] = {
    "1h": 60,
    "6h": 360,
    "24h": 1440,
    "72h": 4320,
}

# (window_label, burn_rate_threshold, budget_consumed_percent, severity)
# Two-window pairs per alert tier (primary + confirmation window)
MULTIWINDOW_ALERT_TIERS: Final[list[tuple[str, str, float, float, str]]] = [
    # primary_window, confirmation_window, burn_rate, budget_consumed, severity
    ("1h",  "5m",   14.4, 2.0,  "P0"),   # 2%  budget in 1h  → page immediately
    ("6h",  "30m",   6.0, 5.0,  "P1"),   # 5%  budget in 6h  → page
    ("24h", "2h",    3.0, 10.0, "P2"),   # 10% budget in 24h → ticket
    ("72h", "6h",    1.0, 10.0, "P3"),   # 10% budget in 3d  → review
]

MINUTES_PER_30_DAYS: Final[int] = 30 * 24 * 60  # 43_200


# ---------------------------------------------------------------------------
# Error budget model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SLOConfig:
    target: float  # e.g. 0.999 for 99.9%
    window_days: int = 30

    def __post_init__(self) -> None:
        if not (0.0 < self.target < 1.0):
            raise ValueError(f"SLO target must be between 0 and 1, got {self.target}")
        if self.window_days < 1:
            raise ValueError(f"window_days must be >= 1, got {self.window_days}")

    @classmethod
    def from_percent(cls, percent: float, window_days: int = 30) -> "SLOConfig":
        """Construct from percentage string, e.g. 99.9."""
        return cls(target=percent / 100.0, window_days=window_days)

    @property
    def error_budget(self) -> float:
        """Fraction of requests/time allowed to fail."""
        return 1.0 - self.target

    @property
    def error_budget_minutes(self) -> float:
        """Total minutes of downtime allowed in the SLO window."""
        return self.error_budget * self.window_days * 24 * 60

    @property
    def error_budget_seconds(self) -> float:
        return self.error_budget_minutes * 60.0


@dataclass(frozen=True)
class BurnRateResult:
    slo: SLOConfig
    observed_error_rate: float   # fraction (0.0–1.0)
    window_minutes: int

    @property
    def burn_rate(self) -> float:
        """
        Burn rate = observed_error_rate / error_budget.

        Interpretation:
          1.0 = consuming budget at exactly SLO pace (exhausted in window_days)
          14.4 = consuming budget 14.4× faster
        """
        if self.slo.error_budget == 0:
            return float("inf")
        return self.observed_error_rate / self.slo.error_budget

    @property
    def budget_consumption_rate_per_hour(self) -> float:
        """Fraction of 30-day budget consumed per hour at current burn rate."""
        budget_minutes = self.slo.window_days * 24 * 60  # total budget window in minutes
        return self.burn_rate / (budget_minutes / 60.0)  # burn_rate / hours_in_window

    @property
    def time_to_exhaustion_hours(self) -> float:
        """Hours until error budget is fully exhausted at current burn rate."""
        if self.burn_rate <= 0:
            return float("inf")
        total_hours = self.slo.window_days * 24.0
        return total_hours / self.burn_rate

    @property
    def budget_consumed_in_window_percent(self) -> float:
        """Percent of total error budget consumed in the observed window."""
        window_hours = self.window_minutes / 60.0
        total_hours = self.slo.window_days * 24.0
        return (self.burn_rate * window_hours / total_hours) * 100.0


# ---------------------------------------------------------------------------
# Multi-window alert evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AlertWindow:
    label: str
    minutes: int
    burn_rate: float
    threshold: float
    firing: bool

    @property
    def budget_consumed_percent(self) -> float:
        window_hours = self.minutes / 60.0
        total_hours = 30 * 24.0
        return (self.burn_rate * window_hours / total_hours) * 100.0


@dataclass(frozen=True)
class MultiWindowAlert:
    slo: SLOConfig
    tier: str  # P0/P1/P2/P3
    primary_window: AlertWindow
    confirmation_window: AlertWindow
    firing: bool

    @property
    def severity(self) -> str:
        return self.tier

    @property
    def description(self) -> str:
        if not self.firing:
            return f"[{self.tier}] NOT firing — burn rate below threshold"
        return (
            f"[{self.tier}] FIRING — {self.primary_window.label} burn rate "
            f"{self.primary_window.burn_rate:.2f}× (threshold {self.primary_window.threshold}×), "
            f"consuming {self.primary_window.budget_consumed_percent:.1f}% budget/window"
        )


def evaluate_multiwindow_alerts(
    slo: SLOConfig,
    burn_rates_by_window: dict[str, float],
) -> list[MultiWindowAlert]:
    """
    Evaluate all four alert tiers against observed burn rates.

    Args:
        slo: SLO configuration
        burn_rates_by_window: mapping of window label → observed burn rate
                               e.g. {"1h": 15.2, "6h": 7.1, "5m": 16.0, "30m": 6.8, ...}

    Returns:
        List of MultiWindowAlert, one per tier.
    """
    alerts: list[MultiWindowAlert] = []

    for primary_label, confirm_label, threshold, _budget_pct, severity in MULTIWINDOW_ALERT_TIERS:
        primary_burn = burn_rates_by_window.get(primary_label, 0.0)
        confirm_burn = burn_rates_by_window.get(confirm_label, 0.0)

        primary_minutes = _label_to_minutes(primary_label)
        confirm_minutes = _label_to_minutes(confirm_label)

        primary_window = AlertWindow(
            label=primary_label,
            minutes=primary_minutes,
            burn_rate=primary_burn,
            threshold=threshold,
            firing=primary_burn >= threshold,
        )
        confirm_window = AlertWindow(
            label=confirm_label,
            minutes=confirm_minutes,
            burn_rate=confirm_burn,
            threshold=threshold,
            firing=confirm_burn >= threshold,
        )

        firing = primary_window.firing and confirm_window.firing

        alerts.append(MultiWindowAlert(
            slo=slo,
            tier=severity,
            primary_window=primary_window,
            confirmation_window=confirm_window,
            firing=firing,
        ))

    return alerts


def _label_to_minutes(label: str) -> int:
    """Convert window label like '1h', '30m', '72h' to minutes."""
    if label.endswith("h"):
        return int(label[:-1]) * 60
    if label.endswith("m"):
        return int(label[:-1])
    if label.endswith("d"):
        return int(label[:-1]) * 1440
    raise ValueError(f"Unknown window label: {label!r}")


# ---------------------------------------------------------------------------
# Prometheus alert rule generator
# ---------------------------------------------------------------------------

def generate_prometheus_rules(slo: SLOConfig, metric_name: str = "request_errors_total") -> str:
    """
    Generate Prometheus alerting rules for multi-window burn rate monitoring.

    Output is copy-paste ready YAML for a Prometheus rules file.
    """
    slo_pct = slo.target * 100
    budget = slo.error_budget

    lines: list[str] = [
        f"# SLO Burn Rate Alerts — {slo_pct}% SLO ({slo.window_days}d window)",
        f"# Error budget: {budget * 100:.4f}% = {slo.error_budget_minutes:.1f} minutes",
        "# Generated by slo_burn_rate.py",
        "",
        "groups:",
        "  - name: slo_burn_rate",
        "    rules:",
    ]

    for primary_label, confirm_label, threshold, budget_pct, severity in MULTIWINDOW_ALERT_TIERS:
        pwin = primary_label.replace("h", "h").replace("m", "m")
        cwin = confirm_label.replace("h", "h").replace("m", "m")
        lines += [
            f"",
            f"    # {severity}: {threshold}× burn rate ({budget_pct:.0f}% budget/{primary_label})",
            f"    - alert: SLOBurnRate{severity}",
            f"      expr: |",
            f"        (",
            f"          rate({metric_name}[{pwin}])",
            f"          /",
            f"          rate(requests_total[{pwin}])",
            f"        ) / {budget:.6f} > {threshold}",
            f"        and",
            f"        (",
            f"          rate({metric_name}[{cwin}])",
            f"          /",
            f"          rate(requests_total[{cwin}])",
            f"        ) / {budget:.6f} > {threshold}",
            f"      for: 2m",
            f"      labels:",
            f"        severity: {severity.lower()}",
            f"      annotations:",
            f"        summary: \"SLO burn rate {severity} — {{{{ $value | printf \\\"%.1f\\\" }}}}x\"",
            f"        description: >",
            f"          Error budget consuming {budget_pct:.0f}% per {primary_label} window.",
            f"          At this rate budget exhausts in",
            f"          {{{{ div {slo.window_days * 24.0} $value | printf \\\"%.1f\\\" }}}} hours.",
            f"          Runbook: https://wiki/SRE/SLO-Burn-Rate-{severity}",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fast/slow burn thresholds
# ---------------------------------------------------------------------------

def compute_fast_slow_thresholds(slo: SLOConfig) -> dict[str, dict[str, float]]:
    """
    Return the exact burn rate thresholds for fast and slow burn alerts.

    Fast burn: detects sudden outages (1h + 5m windows)
    Slow burn: detects slow persistent degradation (72h + 6h windows)

    Based on Google SRE Workbook formula:
      threshold = (percent_budget_consumed / 100) * (window_days * 24) / window_hours
    """
    window_days = slo.window_days
    total_hours = window_days * 24.0

    results: dict[str, dict[str, float]] = {}

    for primary_label, confirm_label, threshold, budget_pct, severity in MULTIWINDOW_ALERT_TIERS:
        primary_hours = _label_to_minutes(primary_label) / 60.0
        # Re-derive the threshold formula: burn_rate = (budget_pct/100) * total_hours / primary_hours
        derived_threshold = (budget_pct / 100.0) * total_hours / primary_hours
        results[severity] = {
            "primary_window": primary_label,
            "confirmation_window": confirm_label,
            "burn_rate_threshold": round(derived_threshold, 2),
            "budget_consumed_percent": budget_pct,
            "time_to_exhaustion_hours": round(total_hours / derived_threshold, 2),
            "canonical_threshold": threshold,  # Google SRE Workbook canonical value
        }

    return results


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def run_demo() -> None:
    """Print a full SLO burn rate analysis demo."""
    slo = SLOConfig.from_percent(99.9, window_days=30)

    print("=" * 70)
    print(f"SLO BURN RATE ANALYSIS — {slo.target * 100}% SLO ({slo.window_days}d window)")
    print("=" * 70)
    print(f"Error budget      : {slo.error_budget * 100:.3f}%")
    print(f"Error budget (min): {slo.error_budget_minutes:.1f} minutes")
    print()

    # Simulate a 1.5% error rate
    observed_error_rate = 0.015
    result = BurnRateResult(slo=slo, observed_error_rate=observed_error_rate, window_minutes=60)
    print(f"Observed error rate : {observed_error_rate * 100:.2f}%")
    print(f"Burn rate           : {result.burn_rate:.2f}×")
    print(f"Time to exhaustion  : {result.time_to_exhaustion_hours:.1f} hours")
    print(f"Budget/hour consumed: {result.budget_consumption_rate_per_hour * 100:.3f}%")
    print()

    # Multi-window evaluation
    burn_rates = {
        "1h": 15.2, "5m": 14.8,
        "6h": 7.1, "30m": 6.9,
        "24h": 3.5, "2h": 3.2,
        "72h": 1.2, "6h": 1.1,
    }
    # Note: "6h" key collision; in production these come from separate Prometheus queries
    burn_rates_full = {
        "1h": 15.2, "5m": 14.8,
        "6h": 7.1, "30m": 6.9,
        "24h": 3.5, "2h": 3.2,
        "72h": 1.2,
    }

    print("MULTI-WINDOW ALERT EVALUATION:")
    alerts = evaluate_multiwindow_alerts(slo, burn_rates_full)
    for alert in alerts:
        status = "FIRING" if alert.firing else "OK"
        print(f"  [{status}] {alert.description}")
    print()

    print("FAST/SLOW BURN THRESHOLDS:")
    thresholds = compute_fast_slow_thresholds(slo)
    for sev, data in thresholds.items():
        print(f"  {sev}: {data['burn_rate_threshold']}× ({data['primary_window']} window, "
              f"exhausts in {data['time_to_exhaustion_hours']}h)")
    print()

    print("PROMETHEUS RULES (first 20 lines):")
    rules = generate_prometheus_rules(slo)
    print("\n".join(rules.splitlines()[:20]))
    print("...")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SLO Burn Rate Calculator — Google SRE Book Chapter 5"
    )
    parser.add_argument("--demo", action="store_true", help="Run full demo")
    parser.add_argument("--slo", type=float, default=99.9,
                        help="SLO target as percentage (e.g. 99.9)")
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--error-rate", type=float,
                        help="Observed error rate as fraction (e.g. 0.015 = 1.5%%)")
    parser.add_argument("--burn-rate", type=float,
                        help="Observed burn rate (skip --error-rate)")
    parser.add_argument("--prometheus-rules", action="store_true",
                        help="Print Prometheus alerting rules")
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return 0

    slo = SLOConfig.from_percent(args.slo, args.window_days)

    if args.prometheus_rules:
        print(generate_prometheus_rules(slo))
        return 0

    if args.error_rate is not None:
        result = BurnRateResult(slo=slo, observed_error_rate=args.error_rate, window_minutes=60)
        print(f"Burn rate          : {result.burn_rate:.2f}×")
        print(f"Time to exhaustion : {result.time_to_exhaustion_hours:.1f} hours")
        return 0

    if args.burn_rate is not None:
        # Reverse: given burn rate, what error rate corresponds to it?
        error_rate = args.burn_rate * slo.error_budget
        print(f"At burn rate {args.burn_rate}×:")
        print(f"  Implied error rate  : {error_rate * 100:.4f}%")
        print(f"  Time to exhaustion  : {slo.window_days * 24.0 / args.burn_rate:.1f} hours")
        return 0

    print("Error: specify --error-rate, --burn-rate, --prometheus-rules, or --demo")
    return 1


if __name__ == "__main__":
    sys.exit(main())
