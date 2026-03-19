#!/usr/bin/env python3
"""
SLO Error Budget Calculator

Calculates error budget remaining, burn rate, and time to exhaustion
for a given SLO target and consumption window.

Usage:
    python3 slo_calculator.py --slo 99.95 --window 30 --consumed-minutes 8.2
    python3 slo_calculator.py --slo 99.9  --window 30 --burn-rate 14.4
    python3 slo_calculator.py --slo 99.95 --window 7  --consumed-minutes 2.1 --json

Examples:
    # Check budget after an incident consumed 8.2 minutes
    python3 slo_calculator.py --slo 99.95 --window 30 --consumed-minutes 8.2

    # What is time to exhaustion at burn rate 14.4x?
    python3 slo_calculator.py --slo 99.9 --window 30 --burn-rate 14.4

    # Check remaining budget (nothing consumed yet)
    python3 slo_calculator.py --slo 99.99 --window 30
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass


@dataclass
class SLOBudget:
    """Immutable result of an SLO budget calculation."""

    slo_target_pct: float
    window_days: int
    window_minutes: float
    error_budget_total_minutes: float
    error_budget_total_seconds: float
    consumed_minutes: float
    remaining_minutes: float
    remaining_pct: float
    burn_rate: float
    time_to_exhaustion_hours: float | None
    policy_tier: str  # Feature release policy

    @property
    def is_exhausted(self) -> bool:
        return self.remaining_minutes <= 0.0

    @property
    def is_critical(self) -> bool:
        return self.remaining_pct < 10.0

    @property
    def is_warning(self) -> bool:
        return self.remaining_pct < 25.0


def calculate_budget(
    slo_target_pct: float,
    window_days: int,
    consumed_minutes: float = 0.0,
    current_burn_rate: float | None = None,
) -> SLOBudget:
    """
    Calculate SLO error budget status.

    Args:
        slo_target_pct: SLO target as a percentage, e.g. 99.95 for 99.95%
        window_days: Rolling window duration in days (typically 30)
        consumed_minutes: Minutes of error budget already consumed
        current_burn_rate: Current burn rate multiplier (optional; used for
                           time-to-exhaustion calculation if consumed_minutes
                           does not imply one)

    Returns:
        SLOBudget dataclass with all derived values.

    Raises:
        ValueError: If inputs are out of valid range.
    """
    if not (0.0 < slo_target_pct < 100.0):
        raise ValueError(f"slo_target_pct must be in (0, 100), got {slo_target_pct}")
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")
    if consumed_minutes < 0:
        raise ValueError(f"consumed_minutes cannot be negative, got {consumed_minutes}")

    slo_target = slo_target_pct / 100.0
    error_budget_fraction = 1.0 - slo_target

    window_minutes = window_days * 24 * 60
    error_budget_total_minutes = window_minutes * error_budget_fraction
    error_budget_total_seconds = error_budget_total_minutes * 60

    remaining_minutes = error_budget_total_minutes - consumed_minutes
    remaining_pct = max(0.0, (remaining_minutes / error_budget_total_minutes) * 100.0)

    # Burn rate: how fast budget is being consumed relative to the SLO allowance.
    # burn_rate = 1.0 means consuming exactly at the allowed rate.
    # Derived from consumed_minutes over the window; falls back to explicit arg.
    if consumed_minutes > 0 and window_minutes > 0:
        # actual error rate over window vs allowable error rate
        actual_error_rate = consumed_minutes / window_minutes
        allowable_error_rate = error_budget_fraction
        derived_burn_rate = actual_error_rate / allowable_error_rate
    elif current_burn_rate is not None:
        derived_burn_rate = current_burn_rate
    else:
        derived_burn_rate = 1.0  # nominal — no data yet

    # Override with explicitly provided burn rate if supplied
    burn_rate = current_burn_rate if current_burn_rate is not None else derived_burn_rate

    # Time to exhaustion at current burn rate
    if burn_rate > 1.0 and remaining_minutes > 0:
        # minutes remaining / (burn_rate × allowed_minutes_per_minute)
        # = remaining_minutes / (burn_rate × error_budget_fraction × 60 min/hr × ... )
        # Simpler: at burn_rate x, we consume error_budget_fraction*burn_rate per unit time.
        # exhaustion_minutes = remaining_minutes / (burn_rate - 1) ... no:
        # Actually: rate of consumption = burn_rate * (error_budget_fraction / window_minutes) minutes/minute
        rate_minutes_per_minute = burn_rate * error_budget_fraction
        if rate_minutes_per_minute > 0:
            exhaustion_minutes = remaining_minutes / rate_minutes_per_minute
            time_to_exhaustion_hours = exhaustion_minutes / 60.0
        else:
            time_to_exhaustion_hours = None
    elif burn_rate <= 1.0:
        time_to_exhaustion_hours = None  # Not burning faster than replenishment
    else:
        time_to_exhaustion_hours = 0.0  # Already exhausted

    # Policy tier based on remaining budget
    if remaining_pct > 50.0:
        policy_tier = "UNRESTRICTED: Feature deploys permitted"
    elif remaining_pct > 25.0:
        policy_tier = "STAGED: Require staged/canary rollout"
    elif remaining_pct > 10.0:
        policy_tier = "APPROVAL REQUIRED: SRE approval before any deploy"
    elif remaining_pct > 0.0:
        policy_tier = "FREEZE: Bug fixes and rollbacks only"
    else:
        policy_tier = "EXHAUSTED: Incident review required before any deploy"

    return SLOBudget(
        slo_target_pct=slo_target_pct,
        window_days=window_days,
        window_minutes=window_minutes,
        error_budget_total_minutes=error_budget_total_minutes,
        error_budget_total_seconds=error_budget_total_seconds,
        consumed_minutes=consumed_minutes,
        remaining_minutes=remaining_minutes,
        remaining_pct=remaining_pct,
        burn_rate=burn_rate,
        time_to_exhaustion_hours=time_to_exhaustion_hours,
        policy_tier=policy_tier,
    )


def format_minutes(minutes: float) -> str:
    """Format minutes into a human-readable string."""
    if minutes >= 60.0:
        hours = minutes / 60.0
        return f"{minutes:.2f} min ({hours:.2f} hr)"
    return f"{minutes:.2f} min"


def print_report(budget: SLOBudget, *, use_json: bool = False) -> None:
    """Print a formatted report to stdout."""
    if use_json:
        data = asdict(budget)
        data["is_exhausted"] = budget.is_exhausted
        data["is_critical"] = budget.is_critical
        data["is_warning"] = budget.is_warning
        print(json.dumps(data, indent=2))
        return

    # Human-readable report
    sep = "-" * 55
    print(sep)
    print(f"  SLO Error Budget Report")
    print(sep)
    print(f"  SLO Target:             {budget.slo_target_pct:.4f}%")
    print(f"  Window:                 {budget.window_days} days ({budget.window_minutes:,.0f} minutes)")
    print(f"  Error budget total:     {format_minutes(budget.error_budget_total_minutes)}")
    print(f"                          ({budget.error_budget_total_seconds:.1f} seconds)")
    print(sep)
    print(f"  Consumed:               {format_minutes(budget.consumed_minutes)}")
    print(f"  Remaining:              {format_minutes(budget.remaining_minutes)}")
    print(f"  Remaining:              {budget.remaining_pct:.2f}%")
    print(sep)
    print(f"  Current burn rate:      {budget.burn_rate:.2f}x")
    if budget.time_to_exhaustion_hours is not None:
        tte = budget.time_to_exhaustion_hours
        if tte < 1.0:
            print(f"  Time to exhaustion:     {tte * 60:.1f} minutes  [!]")
        elif tte < 24.0:
            print(f"  Time to exhaustion:     {tte:.1f} hours")
        else:
            print(f"  Time to exhaustion:     {tte / 24:.1f} days")
    else:
        print(f"  Time to exhaustion:     N/A (burn rate <= 1.0x)")
    print(sep)

    # Status indicator
    if budget.is_exhausted:
        status = "EXHAUSTED"
    elif budget.is_critical:
        status = "CRITICAL (<10%)"
    elif budget.is_warning:
        status = "WARNING (<25%)"
    else:
        status = "HEALTHY"

    print(f"  Status:                 {status}")
    print(f"  Deploy policy:          {budget.policy_tier}")
    print(sep)

    # Burn rate context
    print()
    print("  Multi-window alert thresholds (Google SRE):")
    print(f"    SEV2 page:  1h burn > 14.4x AND 6h burn > 6.0x")
    print(f"    SEV1 page:  budget remaining < 10% OR 1h burn > 50x")
    print(f"    Current:    {budget.burn_rate:.2f}x", end="")
    if budget.burn_rate > 50:
        print("  -> SEV1")
    elif budget.burn_rate > 14.4:
        print("  -> SEV2 if 6h window also >6.0x")
    elif budget.burn_rate > 6.0:
        print("  -> SEV2 watch (check 1h window)")
    elif budget.burn_rate > 2.0:
        print("  -> Investigate")
    else:
        print("  -> Normal")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SLO error budget calculator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--slo",
        type=float,
        required=True,
        help="SLO target as a percentage, e.g. 99.95",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Rolling window in days (default: 30)",
    )
    parser.add_argument(
        "--consumed-minutes",
        type=float,
        default=0.0,
        dest="consumed_minutes",
        help="Error budget already consumed, in minutes (default: 0)",
    )
    parser.add_argument(
        "--burn-rate",
        type=float,
        default=None,
        dest="burn_rate",
        help="Explicit current burn rate multiplier (overrides derived value)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )

    args = parser.parse_args(argv)

    try:
        budget = calculate_budget(
            slo_target_pct=args.slo,
            window_days=args.window,
            consumed_minutes=args.consumed_minutes,
            current_burn_rate=args.burn_rate,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print_report(budget, use_json=args.json)
    return 0 if not budget.is_exhausted else 2


if __name__ == "__main__":
    sys.exit(main())
