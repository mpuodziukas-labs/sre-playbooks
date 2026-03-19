#!/usr/bin/env python3
"""
Incident Timeline Parser and Post-Mortem Generator

Parses structured incident logs (JSON lines format) and produces:
  - MTTR (Mean Time To Resolve)
  - Time-to-detect (TTD)
  - Time-to-mitigate (TTM)
  - Markdown timeline for post-mortem

Input format (JSON lines, one event per line):
    {"ts": "2026-03-18T14:00:00Z", "type": "alert_fired", "msg": "SLO burn rate 14.4x", "actor": "prometheus"}
    {"ts": "2026-03-18T14:05:00Z", "type": "acknowledged", "msg": "On-call acknowledged page", "actor": "alice"}
    {"ts": "2026-03-18T14:12:00Z", "type": "identified", "msg": "Root cause: bad deploy v2.4.1", "actor": "alice"}
    {"ts": "2026-03-18T14:18:00Z", "type": "mitigated", "msg": "Rollback to v2.3.0 complete", "actor": "alice"}
    {"ts": "2026-03-18T14:45:00Z", "type": "resolved", "msg": "Error rate nominal, incident closed", "actor": "alice"}

Event types (standard):
    incident_start  - First user-impacting event (or alert_fired if earlier)
    alert_fired     - Monitoring alert triggered
    acknowledged    - On-call acknowledged
    identified      - Root cause identified
    mitigated       - Primary mitigation applied (symptoms relieved)
    resolved        - Incident fully closed
    note            - Any intermediate annotation

Usage:
    python3 incident_timeline.py incident.jsonl
    python3 incident_timeline.py incident.jsonl --output post-mortem-timeline.md
    cat incident.jsonl | python3 incident_timeline.py -
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


TIMESTAMP_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S",
]

# Ordered event type priorities for metric anchoring
EVENT_PRIORITY: dict[str, int] = {
    "incident_start": 0,
    "alert_fired": 1,
    "acknowledged": 2,
    "identified": 3,
    "mitigated": 4,
    "resolved": 5,
    "note": 99,
}


@dataclass
class IncidentEvent:
    """A single timestamped incident event."""

    ts: datetime
    event_type: str
    message: str
    actor: str
    raw: dict[str, object]

    @property
    def ts_utc_str(self) -> str:
        return self.ts.strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class IncidentMetrics:
    """Derived metrics from an incident timeline."""

    incident_id: str
    total_events: int
    first_event_ts: datetime
    last_event_ts: datetime

    # Core SRE metrics (all in minutes, None if not calculable)
    time_to_detect_minutes: float | None  # incident_start → alert_fired
    time_to_acknowledge_minutes: float | None  # alert_fired → acknowledged
    time_to_identify_minutes: float | None  # acknowledged → identified
    time_to_mitigate_minutes: float | None  # incident_start → mitigated
    mttr_minutes: float | None  # incident_start → resolved (Mean Time To Resolve)

    events: list[IncidentEvent]


def parse_timestamp(ts_str: str) -> datetime:
    """Parse a timestamp string into a timezone-aware datetime."""
    for fmt in TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts_str!r}")


def parse_event(line: str, line_number: int) -> IncidentEvent:
    """Parse a single JSON line into an IncidentEvent."""
    try:
        raw = json.loads(line.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Line {line_number}: invalid JSON: {exc}") from exc

    required = {"ts", "type", "msg"}
    missing = required - set(raw.keys())
    if missing:
        raise ValueError(f"Line {line_number}: missing required fields: {missing}")

    ts_value = raw.get("ts")
    if not isinstance(ts_value, str):
        raise ValueError(f"Line {line_number}: 'ts' must be a string")

    msg_value = raw.get("msg")
    if not isinstance(msg_value, str):
        raise ValueError(f"Line {line_number}: 'msg' must be a string")

    type_value = raw.get("type")
    if not isinstance(type_value, str):
        raise ValueError(f"Line {line_number}: 'type' must be a string")

    actor_value = raw.get("actor", "unknown")
    if not isinstance(actor_value, str):
        actor_value = str(actor_value)

    return IncidentEvent(
        ts=parse_timestamp(ts_value),
        event_type=type_value,
        message=msg_value,
        actor=actor_value,
        raw=raw,
    )


def parse_events(source: TextIO) -> list[IncidentEvent]:
    """Parse all JSON lines from a file-like source."""
    events: list[IncidentEvent] = []
    errors: list[str] = []

    for line_number, line in enumerate(source, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            events.append(parse_event(line, line_number))
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        print("Parse warnings:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)

    return sorted(events, key=lambda e: e.ts)


def find_event(events: list[IncidentEvent], *event_types: str) -> IncidentEvent | None:
    """Find the first event matching any of the given types."""
    for event_type in event_types:
        for evt in events:
            if evt.event_type == event_type:
                return evt
    return None


def delta_minutes(start: datetime, end: datetime) -> float:
    """Return the number of minutes between two datetimes."""
    return (end - start).total_seconds() / 60.0


def calculate_metrics(
    events: list[IncidentEvent],
    incident_id: str = "INC-UNKNOWN",
) -> IncidentMetrics:
    """Derive SRE metrics from a list of events."""
    if not events:
        raise ValueError("No events to analyze")

    first_event = events[0]
    last_event = events[-1]

    # Anchor points
    start_event = find_event(events, "incident_start", "alert_fired")
    alert_event = find_event(events, "alert_fired")
    ack_event = find_event(events, "acknowledged")
    identified_event = find_event(events, "identified")
    mitigated_event = find_event(events, "mitigated")
    resolved_event = find_event(events, "resolved")

    # Time to detect: gap between incident start and alert firing
    time_to_detect: float | None = None
    if start_event and alert_event and start_event.event_type == "incident_start":
        time_to_detect = delta_minutes(start_event.ts, alert_event.ts)

    # Time to acknowledge: alert_fired → acknowledged
    time_to_acknowledge: float | None = None
    if alert_event and ack_event:
        time_to_acknowledge = delta_minutes(alert_event.ts, ack_event.ts)

    # Time to identify root cause: acknowledged → identified
    time_to_identify: float | None = None
    if ack_event and identified_event:
        time_to_identify = delta_minutes(ack_event.ts, identified_event.ts)

    # Time to mitigate: incident start → mitigated
    time_to_mitigate: float | None = None
    if start_event and mitigated_event:
        time_to_mitigate = delta_minutes(start_event.ts, mitigated_event.ts)

    # MTTR: incident start → resolved
    mttr: float | None = None
    if start_event and resolved_event:
        mttr = delta_minutes(start_event.ts, resolved_event.ts)

    return IncidentMetrics(
        incident_id=incident_id,
        total_events=len(events),
        first_event_ts=first_event.ts,
        last_event_ts=last_event.ts,
        time_to_detect_minutes=time_to_detect,
        time_to_acknowledge_minutes=time_to_acknowledge,
        time_to_identify_minutes=time_to_identify,
        time_to_mitigate_minutes=time_to_mitigate,
        mttr_minutes=mttr,
        events=events,
    )


def fmt_duration(minutes: float | None) -> str:
    """Format a duration in minutes as a human-readable string."""
    if minutes is None:
        return "N/A"
    if minutes < 1.0:
        return f"{minutes * 60:.0f}s"
    if minutes < 60.0:
        return f"{minutes:.1f}m"
    hours = minutes / 60.0
    return f"{hours:.1f}h ({minutes:.0f}m)"


def generate_markdown(metrics: IncidentMetrics) -> str:
    """Generate a markdown timeline section for a post-mortem."""
    lines: list[str] = []

    lines.append(f"# Incident Timeline: {metrics.incident_id}")
    lines.append("")
    lines.append(f"**Generated**: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Total events**: {metrics.total_events}")
    lines.append("")

    # Summary metrics table
    lines.append("## SRE Metrics")
    lines.append("")
    lines.append("| Metric | Value | SLO Target |")
    lines.append("|--------|-------|------------|")
    lines.append(f"| Time to Detect (TTD) | {fmt_duration(metrics.time_to_detect_minutes)} | < 5 min |")
    lines.append(f"| Time to Acknowledge (TTA) | {fmt_duration(metrics.time_to_acknowledge_minutes)} | < 5 min |")
    lines.append(f"| Time to Identify Root Cause | {fmt_duration(metrics.time_to_identify_minutes)} | < 30 min |")
    lines.append(f"| Time to Mitigate (TTM) | {fmt_duration(metrics.time_to_mitigate_minutes)} | < 30 min |")
    lines.append(f"| MTTR (Time to Resolve) | {fmt_duration(metrics.mttr_minutes)} | < 60 min |")
    lines.append("")

    # SLA assessment
    lines.append("## SLA Assessment")
    lines.append("")
    issues: list[str] = []
    if metrics.time_to_acknowledge_minutes is not None and metrics.time_to_acknowledge_minutes > 5:
        issues.append(f"- Acknowledge time {fmt_duration(metrics.time_to_acknowledge_minutes)} exceeded 5m target")
    if metrics.time_to_mitigate_minutes is not None and metrics.time_to_mitigate_minutes > 30:
        issues.append(f"- Mitigation time {fmt_duration(metrics.time_to_mitigate_minutes)} exceeded 30m target")
    if metrics.mttr_minutes is not None and metrics.mttr_minutes > 60:
        issues.append(f"- MTTR {fmt_duration(metrics.mttr_minutes)} exceeded 60m target")

    if issues:
        lines.append("**Missed targets:**")
        lines.extend(issues)
    else:
        lines.append("All response time targets met.")
    lines.append("")

    # Chronological timeline
    lines.append("## Event Timeline (5-minute resolution)")
    lines.append("")
    lines.append("| Time (UTC) | Type | Actor | Event |")
    lines.append("|-----------|------|-------|-------|")

    # Group events by 5-minute buckets for readability
    for event in metrics.events:
        ts_str = event.ts.strftime("%H:%M")
        event_type = event.event_type.upper().replace("_", " ")
        # Escape pipe characters in message
        message = event.message.replace("|", "\\|")
        lines.append(f"| {ts_str} | `{event_type}` | {event.actor} | {message} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*Timeline generated by [incident_timeline.py](../scripts/incident_timeline.py)*")
    lines.append("")

    return "\n".join(lines)


def print_console_report(metrics: IncidentMetrics) -> None:
    """Print a concise summary to stdout."""
    sep = "-" * 55
    print(sep)
    print(f"  Incident: {metrics.incident_id}")
    print(sep)
    print(f"  Events parsed:     {metrics.total_events}")
    print(f"  Start:             {metrics.first_event_ts.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  End:               {metrics.last_event_ts.strftime('%Y-%m-%d %H:%M UTC')}")
    print(sep)
    print(f"  TTD (detect):      {fmt_duration(metrics.time_to_detect_minutes)}")
    print(f"  TTA (acknowledge): {fmt_duration(metrics.time_to_acknowledge_minutes)}")
    print(f"  TTI (identify):    {fmt_duration(metrics.time_to_identify_minutes)}")
    print(f"  TTM (mitigate):    {fmt_duration(metrics.time_to_mitigate_minutes)}")
    print(f"  MTTR (resolve):    {fmt_duration(metrics.mttr_minutes)}")
    print(sep)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse incident log and generate post-mortem timeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        help="Path to incident log (JSON lines) or '-' for stdin",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write markdown timeline to this file (default: print to stdout)",
    )
    parser.add_argument(
        "--incident-id",
        default="INC-UNKNOWN",
        dest="incident_id",
        help="Incident identifier for the report header",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Output full markdown (default: print metrics summary only)",
    )

    args = parser.parse_args(argv)

    # Read input
    try:
        if args.input == "-":
            events = parse_events(sys.stdin)
        else:
            with open(args.input) as fh:
                events = parse_events(fh)
    except OSError as exc:
        print(f"Error reading input: {exc}", file=sys.stderr)
        return 1

    if not events:
        print("Error: no valid events found in input", file=sys.stderr)
        return 1

    try:
        metrics = calculate_metrics(events, incident_id=args.incident_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print_console_report(metrics)

    if args.markdown or args.output:
        markdown = generate_markdown(metrics)
        if args.output:
            Path(args.output).write_text(markdown)
            print(f"Markdown timeline written to: {args.output}")
        else:
            print()
            print(markdown)

    return 0


if __name__ == "__main__":
    sys.exit(main())
