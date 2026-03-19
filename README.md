# sre-playbooks

Production-grade runbooks and SRE automation. SLO math included.

Six battle-tested runbooks, three Python tools (SLO calculator, incident timeline parser, toil tracker), and a blameless post-mortem template — all designed for Stripe/Coinbase/Zoom-scale reliability engineering.

---

## Runbooks

| ID | Title | Category |
|----|-------|----------|
| [RB-008](runbooks/RB-008-cdn-cache-purge.md) | CDN Cache Purge | Content Delivery |
| [RB-014](runbooks/RB-014-database-failover.md) | Database Primary Failover | Database / RTO <5min |
| [RB-019](runbooks/RB-019-kubernetes-node-pressure.md) | Kubernetes Node Pressure | Infrastructure |
| [RB-031](runbooks/RB-031-slo-burn-alert.md) | SLO Burn Alert Response | SLO / Error Budget |
| [RB-042](runbooks/RB-042-memory-leak.md) | Memory Leak Investigation | Resource Exhaustion |
| [RB-055](runbooks/RB-055-deploy-rollback.md) | Deploy Rollback | Deployment Safety |

Each runbook covers: detection (alert definitions + manual checks), triage (decision trees), mitigation (step-by-step commands), and prevention (code review checklists, CI gates).

---

## Scripts

### `scripts/slo_calculator.py` — Error Budget Math

Calculates SLO error budget remaining, burn rate, and time to exhaustion.

```bash
# Check budget after an incident consumed 8.2 minutes
python3 scripts/slo_calculator.py --slo 99.95 --window 30 --consumed-minutes 8.2

# What is time to exhaustion at 14.4x burn rate?
python3 scripts/slo_calculator.py --slo 99.9 --window 30 --burn-rate 14.4

# JSON output for dashboards / alerting
python3 scripts/slo_calculator.py --slo 99.99 --window 7 --consumed-minutes 0.5 --json
```

Sample output:

```
-------------------------------------------------------
  SLO Error Budget Report
-------------------------------------------------------
  SLO Target:             99.9500%
  Window:                 30 days (43,200 minutes)
  Error budget total:     21.60 min (0.36 hr)
  Budget (seconds):       1296.0 seconds
-------------------------------------------------------
  Consumed:               8.20 min
  Remaining:              13.40 min
  Remaining:              62.04%
-------------------------------------------------------
  Current burn rate:      19.02x
  Time to exhaustion:     N/A (burn rate <= 1.0x)
-------------------------------------------------------
  Status:                 HEALTHY
  Deploy policy:          UNRESTRICTED: Feature deploys permitted
```

Multi-window alert thresholds (from Google SRE Workbook):

| Threshold | Meaning | Response |
|-----------|---------|----------|
| 1h burn >14.4x AND 6h burn >6.0x | Fast burn | SEV2 page |
| 1h burn >50x | Critical fast burn | SEV1 page |
| Budget remaining <10% | Near exhaustion | SEV1 + deploy freeze |

### `scripts/incident_timeline.py` — Post-Mortem Generator

Parses structured incident logs (JSON lines) and produces MTTR, TTD, TTM metrics plus a markdown timeline.

```bash
# Parse an incident log
python3 scripts/incident_timeline.py incident.jsonl --incident-id INC-0042

# Generate markdown timeline for post-mortem
python3 scripts/incident_timeline.py incident.jsonl \
    --incident-id INC-0042 \
    --output post-mortems/INC-0042-timeline.md \
    --markdown
```

Input format (one JSON object per line):

```jsonl
{"ts": "2026-03-18T14:00:00Z", "type": "alert_fired", "msg": "SLO burn rate 14.4x", "actor": "prometheus"}
{"ts": "2026-03-18T14:05:00Z", "type": "acknowledged", "msg": "On-call acknowledged", "actor": "alice"}
{"ts": "2026-03-18T14:18:00Z", "type": "mitigated", "msg": "Rollback to v2.3.0 complete", "actor": "alice"}
{"ts": "2026-03-18T14:45:00Z", "type": "resolved", "msg": "Error rate nominal", "actor": "alice"}
```

Supported event types: `incident_start`, `alert_fired`, `acknowledged`, `identified`, `mitigated`, `resolved`, `note`

### `scripts/toil_tracker.py` — Toil Elimination

Log, measure, and rank automation opportunities. Target: <50% toil ratio.

```bash
# Log a toil event
python3 scripts/toil_tracker.py log \
    --type manual-deploy \
    --duration 45 \
    --automatable yes \
    --description "Manually pushed config to 12 servers"

# Weekly summary
python3 scripts/toil_tracker.py summary --weeks 1

# Rank automation opportunities (sorted by weekly time savings)
python3 scripts/toil_tracker.py opportunities --weeks 4

# Export all data
python3 scripts/toil_tracker.py export --format csv > toil_report.csv
```

Toil types tracked: `manual-deploy`, `manual-restart`, `manual-scaling`, `ticket-routing`, `log-digging`, `cert-rotation`, `config-sync`, `password-rotation`, `backup-verification`, `capacity-planning`, `oncall-interrupt`, `report-generation`

---

## Post-Mortem Template

[`post-mortems/TEMPLATE.md`](post-mortems/TEMPLATE.md) — Blameless post-mortem structure covering:

- **Impact**: users affected, revenue impact, SLO budget consumed
- **Timeline**: 5-minute resolution event log with SRE metrics (MTTR, TTD, TTM)
- **Root cause analysis**: 5 Whys methodology
- **Contributing factors**: distinguishes root causes from exacerbating conditions
- **Action items**: owner + due date + priority + success criteria
- **What went well**: reinforces positive practices
- **What we got lucky about**: near-misses that could have been worse

---

## Tests

```bash
pip install pytest pytest-cov
python3 -m pytest tests/ -v --cov=scripts
```

10 tests covering: budget math, edge cases (exhausted budget, burn rate thresholds), CLI output, JSON serialization, policy tier logic, and input validation.

---

## CI

GitHub Actions runs on every push:

- **test**: Runs pytest on Python 3.11 and 3.12
- **lint**: ruff + pyflakes
- **validate-runbooks**: Checks every runbook has required sections
- **smoke tests**: Exercises all three CLI scripts end-to-end

---

## Philosophy

**Blameless**: Post-mortems identify systemic failures, not people. Humans make reasonable decisions under uncertainty — the system should make bad outcomes impossible.

**Data-driven**: SLO burn rates, error budgets, and toil percentages are the only meaningful reliability metrics. Vanity metrics (uptime percentage) hide more than they reveal.

**Toil elimination**: Every manual runbook step is a candidate for automation. Track it, measure it, eliminate it. Target: <50% toil ratio. Automate what scales linearly with growth.

**Fast rollback**: A deploy that takes 3 minutes to roll back is safer than one that takes 30. Optimize for reversibility, not just correctness.

---

## Usage in Incidents

1. Alert fires → open the relevant runbook (linked in alert annotation `runbook:` field)
2. Follow triage → mitigation sequence
3. During incident: run `incident_timeline.py` events to a `.jsonl` file for auto-generated timeline
4. Post-incident: generate timeline markdown → paste into post-mortem template
5. After post-mortem: log toil events discovered during incident → prioritize automation

---

*Built for Stripe/Coinbase/Zoom-scale reliability engineering.*
