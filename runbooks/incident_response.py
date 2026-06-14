#!/usr/bin/env python3
"""
Incident Response Lifecycle Simulator

Implements production-grade incident management:
- MTTD/MTTR/MTTM calculations
- Severity classification (P0-P4) with SLO impact modeling
- On-call escalation chain with configurable tiers
- Postmortem template generator (Google/Stripe format)

Usage:
    python3 incident_response.py --demo
    python3 incident_response.py --severity P0 --duration 47
    python3 incident_response.py --postmortem incident_events.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Final


# ---------------------------------------------------------------------------
# Severity model
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    P0 = "P0"  # Total outage, revenue impact, all hands
    P1 = "P1"  # Major degradation, SLO breach imminent
    P2 = "P2"  # Partial degradation, SLO at risk
    P3 = "P3"  # Minor issue, SLO not at risk
    P4 = "P4"  # Cosmetic / informational


SEVERITY_CONFIG: Final[dict[Severity, dict[str, object]]] = {
    Severity.P0: {
        "response_time_minutes": 5,
        "resolution_target_minutes": 60,
        "slo_impact": "critical",  # full SLO breach
        "error_budget_multiplier": 14.4,  # 14.4× burn rate alert threshold
        "requires_war_room": True,
        "requires_exec_notification": True,
        "escalation_delay_minutes": 5,
    },
    Severity.P1: {
        "response_time_minutes": 15,
        "resolution_target_minutes": 240,
        "slo_impact": "high",
        "error_budget_multiplier": 6.0,
        "requires_war_room": True,
        "requires_exec_notification": False,
        "escalation_delay_minutes": 15,
    },
    Severity.P2: {
        "response_time_minutes": 30,
        "resolution_target_minutes": 480,
        "slo_impact": "medium",
        "error_budget_multiplier": 1.0,
        "requires_war_room": False,
        "requires_exec_notification": False,
        "escalation_delay_minutes": 30,
    },
    Severity.P3: {
        "response_time_minutes": 120,
        "resolution_target_minutes": 2880,
        "slo_impact": "low",
        "error_budget_multiplier": 0.1,
        "requires_war_room": False,
        "requires_exec_notification": False,
        "escalation_delay_minutes": 120,
    },
    Severity.P4: {
        "response_time_minutes": 480,
        "resolution_target_minutes": 10080,
        "slo_impact": "none",
        "error_budget_multiplier": 0.0,
        "requires_war_room": False,
        "requires_exec_notification": False,
        "escalation_delay_minutes": 480,
    },
}


# ---------------------------------------------------------------------------
# On-call escalation chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OnCallTier:
    tier: int
    role: str
    contacts: list[str]
    escalation_after_minutes: int


DEFAULT_ESCALATION_CHAIN: Final[list[OnCallTier]] = [
    OnCallTier(
        tier=1,
        role="Primary On-Call Engineer",
        contacts=["oncall-primary@company.com", "+1-555-0100"],
        escalation_after_minutes=5,
    ),
    OnCallTier(
        tier=2,
        role="Secondary On-Call / TL",
        contacts=["oncall-secondary@company.com", "+1-555-0101"],
        escalation_after_minutes=10,
    ),
    OnCallTier(
        tier=3,
        role="Engineering Manager",
        contacts=["eng-manager@company.com", "+1-555-0200"],
        escalation_after_minutes=15,
    ),
    OnCallTier(
        tier=4,
        role="VP Engineering",
        contacts=["vp-eng@company.com", "+1-555-0300"],
        escalation_after_minutes=20,
    ),
    OnCallTier(
        tier=5,
        role="CTO / Incident Commander",
        contacts=["cto@company.com", "+1-555-0400"],
        escalation_after_minutes=30,
    ),
]


def build_escalation_timeline(
    incident_start: datetime,
    severity: Severity,
    chain: list[OnCallTier] | None = None,
) -> list[dict[str, str]]:
    """Return ordered list of escalation events with timestamps."""
    if chain is None:
        chain = DEFAULT_ESCALATION_CHAIN

    config = SEVERITY_CONFIG[severity]
    escalation_delay: int = int(config["escalation_delay_minutes"])  # type: ignore[arg-type]

    # For P3/P4 only page tier 1 by default
    max_tiers = (
        5
        if severity in (Severity.P0, Severity.P1)
        else (3 if severity == Severity.P2 else 1)
    )

    events: list[dict[str, str]] = []
    current_time = incident_start

    for tier in chain[:max_tiers]:
        notify_time = current_time + timedelta(
            minutes=tier.escalation_after_minutes if tier.tier > 1 else 0
        )
        events.append(
            {
                "timestamp": notify_time.isoformat(),
                "action": "PAGE",
                "tier": str(tier.tier),
                "role": tier.role,
                "contacts": ", ".join(tier.contacts),
                "expected_response_by": (
                    notify_time
                    + timedelta(minutes=int(config["response_time_minutes"]))  # type: ignore[arg-type]
                ).isoformat(),
            }
        )
        current_time = notify_time + timedelta(minutes=escalation_delay)

    return events


# ---------------------------------------------------------------------------
# Incident metrics
# ---------------------------------------------------------------------------


@dataclass
class IncidentEvent:
    timestamp: datetime
    event_type: str  # incident_start | alert_fired | acknowledged | identified | mitigated | resolved
    message: str
    actor: str


@dataclass
class IncidentMetrics:
    incident_id: str
    severity: Severity
    started_at: datetime
    alert_fired_at: datetime | None
    acknowledged_at: datetime | None
    identified_at: datetime | None
    mitigated_at: datetime | None
    resolved_at: datetime | None
    events: list[IncidentEvent] = field(default_factory=list)

    # Computed properties ------------------------------------------------

    @property
    def mttd_minutes(self) -> float | None:
        """Mean Time to Detect: incident_start → alert_fired."""
        if self.alert_fired_at is None:
            return None
        delta = self.alert_fired_at - self.started_at
        return delta.total_seconds() / 60.0

    @property
    def mtta_minutes(self) -> float | None:
        """Mean Time to Acknowledge: alert_fired → acknowledged."""
        if self.alert_fired_at is None or self.acknowledged_at is None:
            return None
        delta = self.acknowledged_at - self.alert_fired_at
        return delta.total_seconds() / 60.0

    @property
    def mtti_minutes(self) -> float | None:
        """Mean Time to Identify root cause: acknowledged → identified."""
        if self.acknowledged_at is None or self.identified_at is None:
            return None
        delta = self.identified_at - self.acknowledged_at
        return delta.total_seconds() / 60.0

    @property
    def mttm_minutes(self) -> float | None:
        """Mean Time to Mitigate: incident_start → mitigated."""
        if self.mitigated_at is None:
            return None
        delta = self.mitigated_at - self.started_at
        return delta.total_seconds() / 60.0

    @property
    def mttr_minutes(self) -> float | None:
        """Mean Time to Resolve: incident_start → resolved."""
        if self.resolved_at is None:
            return None
        delta = self.resolved_at - self.started_at
        return delta.total_seconds() / 60.0

    @property
    def slo_minutes_consumed(self) -> float | None:
        """Downtime minutes charged against SLO error budget."""
        return self.mttr_minutes  # simplified: full outage for the duration

    @property
    def met_response_target(self) -> bool | None:
        """Whether the team met the severity response time target."""
        if self.mtta_minutes is None:
            return None
        target = int(SEVERITY_CONFIG[self.severity]["response_time_minutes"])  # type: ignore[arg-type]
        return self.mtta_minutes <= target

    @property
    def met_resolution_target(self) -> bool | None:
        """Whether the team met the severity resolution target."""
        if self.mttr_minutes is None:
            return None
        target = int(SEVERITY_CONFIG[self.severity]["resolution_target_minutes"])  # type: ignore[arg-type]
        return self.mttr_minutes <= target


def parse_events(
    raw_events: list[dict[str, str]], incident_id: str = "INC-UNKNOWN"
) -> IncidentMetrics:
    """Parse a list of raw event dicts into an IncidentMetrics object."""
    parsed: list[IncidentEvent] = []
    for ev in raw_events:
        parsed.append(
            IncidentEvent(
                timestamp=datetime.fromisoformat(ev["ts"].replace("Z", "+00:00")),
                event_type=ev["type"],
                message=ev.get("msg", ""),
                actor=ev.get("actor", "unknown"),
            )
        )

    parsed.sort(key=lambda e: e.timestamp)

    def _find(event_type: str) -> datetime | None:
        for e in parsed:
            if e.event_type == event_type:
                return e.timestamp
        return None

    severity_str = next(
        (ev.get("severity", "P2") for ev in raw_events if "severity" in ev),
        "P2",
    )
    severity = Severity(severity_str)

    started_at = _find("incident_start") or _find("alert_fired") or parsed[0].timestamp

    return IncidentMetrics(
        incident_id=incident_id,
        severity=severity,
        started_at=started_at,
        alert_fired_at=_find("alert_fired"),
        acknowledged_at=_find("acknowledged"),
        identified_at=_find("identified"),
        mitigated_at=_find("mitigated"),
        resolved_at=_find("resolved"),
        events=parsed,
    )


# ---------------------------------------------------------------------------
# Postmortem template
# ---------------------------------------------------------------------------


def generate_postmortem(
    metrics: IncidentMetrics, title: str = "Incident Postmortem"
) -> str:
    """Generate a Stripe/Google-style postmortem in Markdown."""
    config = SEVERITY_CONFIG[metrics.severity]
    sev_impact = config["slo_impact"]

    def fmt_minutes(m: float | None) -> str:
        if m is None:
            return "N/A"
        if m < 1:
            return f"{m * 60:.0f}s"
        h = int(m // 60)
        rem = int(m % 60)
        if h:
            return f"{h}h {rem}m"
        return f"{rem}m"

    def fmt_ts(dt: datetime | None) -> str:
        if dt is None:
            return "N/A"
        return dt.strftime("%Y-%m-%d %H:%M UTC")

    timeline_lines = "\n".join(
        f"| {fmt_ts(e.timestamp)} | {e.event_type} | {e.message} | {e.actor} |"
        for e in metrics.events
    )

    met_response = metrics.met_response_target
    met_resolution = metrics.met_resolution_target
    response_emoji = "✅" if met_response else ("❌" if met_response is False else "—")
    resolution_emoji = (
        "✅" if met_resolution else ("❌" if met_resolution is False else "—")
    )

    return f"""# Postmortem: {title}

**Incident ID:** {metrics.incident_id}
**Severity:** {metrics.severity.value}
**Date:** {fmt_ts(metrics.started_at)}
**Authors:** [List authors here]
**Status:** Draft

---

## Executive Summary

> _One paragraph. What happened, customer impact, how long, root cause in one sentence._

---

## Impact

| Dimension | Value |
|-----------|-------|
| Severity | {metrics.severity.value} |
| SLO Impact | {sev_impact} |
| Duration (MTTR) | {fmt_minutes(metrics.mttr_minutes)} |
| SLO minutes consumed | {fmt_minutes(metrics.slo_minutes_consumed)} |
| Customers affected | [quantify] |
| Revenue impact | [quantify or N/A] |

---

## Timeline

| Timestamp (UTC) | Event | Description | Actor |
|-----------------|-------|-------------|-------|
{timeline_lines}

---

## Key Metrics

| Metric | Value | Target ({metrics.severity.value}) | Met? |
|--------|-------|----------------------------------|------|
| MTTD (Time to Detect) | {fmt_minutes(metrics.mttd_minutes)} | < {fmt_minutes(float(config["response_time_minutes"]))} | — |
| MTTA (Time to Acknowledge) | {fmt_minutes(metrics.mtta_minutes)} | < {fmt_minutes(float(config["response_time_minutes"]))} | {response_emoji} |
| MTTI (Time to Identify) | {fmt_minutes(metrics.mtti_minutes)} | — | — |
| MTTM (Time to Mitigate) | {fmt_minutes(metrics.mttm_minutes)} | — | — |
| MTTR (Time to Resolve) | {fmt_minutes(metrics.mttr_minutes)} | < {fmt_minutes(float(config["resolution_target_minutes"]))} | {resolution_emoji} |

---

## Root Cause Analysis

### What happened?

> _Technical root cause. Be specific. What code/config/system failed?_

### Why did it happen?

> _Five Whys:_
> 1. Why?
> 2. Why?
> 3. Why?
> 4. Why?
> 5. Why? ← actual root cause

### Why didn't we catch it earlier?

> _What monitoring, testing, or process gap allowed this?_

---

## What Went Well

- [ ] Item 1
- [ ] Item 2

## What Went Poorly

- [ ] Item 1
- [ ] Item 2

## Where We Got Lucky

- [ ] Item 1

---

## Action Items

| Action | Owner | Priority | Due Date | Status |
|--------|-------|----------|----------|--------|
| Add alert for [X] | @oncall | P0 | [date] | TODO |
| Improve runbook for [Y] | @team | P1 | [date] | TODO |
| Fix underlying bug | @engineer | P0 | [date] | TODO |
| Postmortem review meeting | @manager | P1 | [date] | TODO |

---

## Appendix

### Graphs / Screenshots

> _Link dashboards, flamegraphs, error rate spikes here._

### Relevant Logs

```
# Paste key log lines here
```

---

_Generated by `incident_response.py` — exo SRE Playbooks_
"""


# ---------------------------------------------------------------------------
# Severity classifier
# ---------------------------------------------------------------------------


def classify_severity(
    error_rate_percent: float,
    latency_p99_ms: float,
    availability_percent: float,
    active_user_impact_percent: float,
) -> Severity:
    """
    Classify incident severity from real-time signals.

    Mirrors Stripe/Google SRE severity rubric:
    - P0: >5% error rate OR >50% users impacted OR availability <99%
    - P1: >1% error rate OR >10% users impacted OR availability <99.5%
    - P2: >0.1% error rate OR >1% users impacted OR p99 latency >2×SLO
    - P3: >0.01% error rate OR any noticeable degradation
    - P4: No user impact
    """
    if (
        error_rate_percent > 5.0
        or active_user_impact_percent > 50.0
        or availability_percent < 99.0
    ):
        return Severity.P0

    if (
        error_rate_percent > 1.0
        or active_user_impact_percent > 10.0
        or availability_percent < 99.5
    ):
        return Severity.P1

    if (
        error_rate_percent > 0.1
        or active_user_impact_percent > 1.0
        or latency_p99_ms > 5000
    ):
        return Severity.P2

    if error_rate_percent > 0.01 or latency_p99_ms > 2000:
        return Severity.P3

    return Severity.P4


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

DEMO_EVENTS: Final[list[dict[str, str]]] = [
    {
        "ts": "2026-03-18T14:00:00Z",
        "type": "incident_start",
        "severity": "P0",
        "msg": "SLO burn rate 14.4x — payment failures spiking",
        "actor": "prometheus",
    },
    {
        "ts": "2026-03-18T14:02:00Z",
        "type": "alert_fired",
        "msg": "PagerDuty: SLO burn rate CRITICAL",
        "actor": "pagerduty",
    },
    {
        "ts": "2026-03-18T14:06:00Z",
        "type": "acknowledged",
        "msg": "On-call acknowledged — starting investigation",
        "actor": "alice@stripe.com",
    },
    {
        "ts": "2026-03-18T14:14:00Z",
        "type": "identified",
        "msg": "Root cause: DB connection pool exhausted after deploy v4.2.1",
        "actor": "alice@stripe.com",
    },
    {
        "ts": "2026-03-18T14:19:00Z",
        "type": "mitigated",
        "msg": "Rolled back to v4.2.0, connection pool recovering",
        "actor": "alice@stripe.com",
    },
    {
        "ts": "2026-03-18T14:47:00Z",
        "type": "resolved",
        "msg": "Error rate nominal, SLO burn rate <1x",
        "actor": "alice@stripe.com",
    },
]


def run_demo() -> None:
    """Print a full incident lifecycle demo."""
    metrics = parse_events(DEMO_EVENTS, "INC-2026-0318-001")

    print("=" * 70)
    print("INCIDENT LIFECYCLE DEMO")
    print("=" * 70)
    print(f"Incident ID  : {metrics.incident_id}")
    print(f"Severity     : {metrics.severity.value}")
    print(f"MTTD         : {metrics.mttd_minutes:.1f} min")
    print(f"MTTA         : {metrics.mtta_minutes:.1f} min")
    print(f"MTTI         : {metrics.mtti_minutes:.1f} min")
    print(f"MTTM         : {metrics.mttm_minutes:.1f} min")
    print(f"MTTR         : {metrics.mttr_minutes:.1f} min")
    print(f"Met response target? {metrics.met_response_target}")
    print(f"Met resolution target? {metrics.met_resolution_target}")
    print()

    print("ESCALATION CHAIN:")
    for ev in build_escalation_timeline(metrics.started_at, metrics.severity):
        print(
            f"  T{ev['tier']} [{ev['timestamp'][:16]}] PAGE {ev['role']} → {ev['contacts']}"
        )
    print()

    print("POSTMORTEM PREVIEW (first 30 lines):")
    pm = generate_postmortem(metrics, "Payment API Outage — DB Pool Exhaustion")
    print("\n".join(pm.splitlines()[:30]))
    print("...")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Incident Response Lifecycle Simulator"
    )
    parser.add_argument("--demo", action="store_true", help="Run demo incident")
    parser.add_argument("--severity", choices=[s.value for s in Severity], default="P1")
    parser.add_argument(
        "--duration", type=float, default=30.0, help="Incident duration in minutes"
    )
    parser.add_argument(
        "--postmortem", help="Path to JSON events file for postmortem generation"
    )
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return 0

    if args.postmortem:
        with open(args.postmortem) as f:
            events = json.load(f)
        metrics = parse_events(events)
        print(generate_postmortem(metrics))
        return 0

    # Quick severity + escalation preview
    sev = Severity(args.severity)
    now = datetime.now(timezone.utc)
    chain = build_escalation_timeline(now, sev)
    print(f"Severity: {sev.value}")
    print(f"Response target: {SEVERITY_CONFIG[sev]['response_time_minutes']} min")
    print(f"Resolution target: {SEVERITY_CONFIG[sev]['resolution_target_minutes']} min")
    print(f"War room required: {SEVERITY_CONFIG[sev]['requires_war_room']}")
    print(f"Exec notification: {SEVERITY_CONFIG[sev]['requires_exec_notification']}")
    print()
    print("Escalation chain:")
    for ev in chain:
        print(f"  T{ev['tier']} {ev['role']}: {ev['contacts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
