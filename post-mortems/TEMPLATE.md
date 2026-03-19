# Post-Mortem: [Incident Title]

> **Blameless principle**: This document identifies systemic and process failures,
> not individual fault. The goal is to make the system more resilient, not to
> assign blame to people who made decisions under uncertainty with incomplete information.

---

## Incident Summary

| Field | Value |
|-------|-------|
| **Incident ID** | INC-XXXX |
| **Date** | YYYY-MM-DD |
| **Duration** | X hours Y minutes |
| **Severity** | SEV1 / SEV2 / SEV3 |
| **Incident Commander** | @name |
| **Scribe** | @name |
| **Services Affected** | service-a, service-b |
| **Status** | Resolved / Monitoring |
| **Post-Mortem Date** | YYYY-MM-DD |
| **Review Meeting** | YYYY-MM-DD HH:MM UTC |

---

## Impact

### Users Affected

- **Total users impacted**: [number or percentage]
- **Peak error rate**: [X.X%]
- **Affected regions**: [us-east-1, eu-west-1]
- **Affected feature/surface**: [describe what users experienced]

### Business Impact

- **Revenue impact**: $[X,XXX] estimated (based on [methodology])
- **SLO budget consumed**: [X.X] minutes ([X.X%] of 30-day budget)
- **Requests failed**: ~[N] requests
- **Longest user-facing downtime**: [X minutes]

### SLO Budget Accounting

```
SLO target:          99.X%
30-day budget:       X.X minutes
Consumed (incident): X.X minutes
Budget remaining:    X.X minutes (XX.X%)
```

---

## Timeline

*5-minute resolution. All times UTC.*

| Time | Event |
|------|-------|
| HH:MM | Incident start (first user-facing impact) |
| HH:MM | Alert fired: [alert name] |
| HH:MM | On-call acknowledged page |
| HH:MM | Initial triage: [what was checked first] |
| HH:MM | Root cause identified: [brief description] |
| HH:MM | Mitigation started: [action taken] |
| HH:MM | Mitigation complete: [confirmation signal] |
| HH:MM | Error rate returned to baseline |
| HH:MM | Incident resolved / monitoring started |

### Key Metrics

| Metric | Value | Target |
|--------|-------|--------|
| Time to Detect | X min | < 5 min |
| Time to Acknowledge | X min | < 5 min |
| Time to Identify Root Cause | X min | < 30 min |
| Time to Mitigate | X min | < 30 min |
| MTTR | X min | < 60 min |

---

## Root Cause Analysis

### Direct Cause

> *What immediately caused the user-facing impact?*

[One sentence describing the technical trigger, e.g.: "A memory leak in the session
handler caused all worker processes on Node A to be OOMKilled simultaneously."]

### 5 Whys

1. **Why** did users experience errors?
   → [First-level symptom]

2. **Why** did [first-level symptom] occur?
   → [Second-level cause]

3. **Why** did [second-level cause] happen?
   → [Third-level cause]

4. **Why** was [third-level cause] present?
   → [Fourth-level cause]

5. **Why** was [fourth-level cause] not caught earlier?
   → [Systemic root cause — this is where action items should focus]

### Root Cause Statement

> *The systemic root cause, in one or two sentences. This should point to a
> process, tooling, or architectural gap — not a person.*

[Example: "Our deployment pipeline lacked a memory regression test, allowing a
change that introduced an unbounded cache to reach production without detection.
Additionally, our memory limit was set too close to the working set with no
headroom for the cache growth pattern."]

---

## Contributing Factors

*Not root causes, but conditions that made the incident worse or harder to resolve.*

- [ ] **Monitoring gap**: [e.g., "No alert for memory growth rate; only OOMKill events"]
- [ ] **On-call context**: [e.g., "Incident occurred during handoff with reduced context"]
- [ ] **Documentation gap**: [e.g., "No runbook for this failure mode"]
- [ ] **Technical debt**: [e.g., "Old cache code had no size limits by design"]
- [ ] **Process gap**: [e.g., "Code review checklist didn't include memory safety checks"]
- [ ] **Tooling gap**: [e.g., "No easy way to check memory trend without Prometheus access"]

---

## What Went Well

*Honest list of things that worked. This reinforces good practices and morale.*

- [ ] Alert fired within X minutes of impact (met <5min target)
- [ ] On-call had runbook access and followed it correctly
- [ ] Rollback was completed in under 3 minutes
- [ ] Communication to stakeholders was timely and accurate
- [ ] [Other positive items]

---

## What We Got Lucky About

*Near-misses and conditions that could have made this worse but didn't.*

- [ ] [e.g., "Incident occurred off-peak — traffic was 40% of normal, limiting user impact"]
- [ ] [e.g., "The failing node happened to be the one with no stateful workloads"]
- [ ] [e.g., "The replica was up-to-date; a 5-minute lag would have caused RPO violation"]

---

## Action Items

*Specific, ownable, time-bound. No action item without an owner and due date.*

| Priority | Action | Owner | Due Date | Status |
|----------|--------|-------|----------|--------|
| P1 | [Immediate fix] | @owner | YYYY-MM-DD | Open |
| P1 | [Monitoring/alerting gap] | @owner | YYYY-MM-DD | Open |
| P2 | [Process improvement] | @owner | YYYY-MM-DD | Open |
| P2 | [Documentation update] | @owner | YYYY-MM-DD | Open |
| P3 | [Long-term architectural fix] | @owner | YYYY-MM-DD | Open |

### Action Item Detail

#### P1: [Title of critical action]

**Problem**: [What gap this addresses]
**Solution**: [Specific technical or process change]
**Success criteria**: [How we'll know it's done and working]
**Rollback**: [Can it be reverted if it causes issues?]

#### P2: [Title of process action]

[Repeat format for each action item]

---

## Lessons Learned

> *2-5 durable lessons that apply beyond this specific incident.*

1. **[Lesson title]**: [One to two sentences describing the transferable learning]
2. **[Lesson title]**: [One to two sentences]
3. **[Lesson title]**: [One to two sentences]

---

## Detection Improvement

*Specifically, what monitoring change would have caught this earlier?*

```yaml
# Proposed alert (add to alerts/memory.yml or equivalent)
- alert: [AlertName]
  expr: |
    [PromQL expression]
  for: [duration]
  labels:
    severity: [warning|page]
  annotations:
    summary: "[Description]"
    runbook: "https://runbooks.company.com/RB-XXX"
```

---

## Appendix

### Relevant Metrics

*Include screenshots or links to dashboards from during the incident.*

- [Grafana dashboard link (permalink with time range)]
- [Prometheus query results]
- [Log excerpts]

### Related Incidents

- [INC-XXXX: Related incident title] — [brief note on relationship]

### Runbooks Referenced

- [RB-XXX: Runbook title](../runbooks/RB-XXX.md)

### External References

- [Link to relevant ticket, PR, or documentation]

---

*Post-mortem authored by @[author]. Reviewed by @[reviewer1], @[reviewer2].*
*Next review scheduled: [date] (if action items are still open).*
