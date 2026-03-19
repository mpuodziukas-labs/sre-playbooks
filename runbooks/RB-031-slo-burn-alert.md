# RB-031: SLO Burn Alert Response

**Severity**: SEV2 (fast burn) → SEV1 (budget <10%)
**Category**: SLO / Error Budget
**Last Reviewed**: 2026-03-18
**Owner**: SRE On-Call

---

## Overview

Multi-window burn rate alerts are the most reliable early warning system for SLO violations. This runbook explains the math, response procedures, and escalation criteria. A page here means the service is consuming error budget faster than sustainable.

**Key principle**: Alert on burn rate, not raw error rate. A 1% error rate is fine if your SLO is 95%, catastrophic if it is 99.99%.

---

## 1. The Mathematics

### Burn Rate Formula

```
burn_rate = (1 - success_rate) / (1 - SLO_target)

Example for SLO = 99.9% (1 - 0.999 = 0.001 error budget):
  If current error rate = 1%:
    burn_rate = 0.01 / 0.001 = 10x
  Interpretation: consuming error budget 10x faster than the SLO allows
```

### Error Budget Remaining

```
# Available error budget for a 30-day window
budget_total_minutes = 30 * 24 * 60 * (1 - SLO_target)

# Example: SLO = 99.9%, 30-day window
budget_total_minutes = 43200 * 0.001 = 43.2 minutes

# Remaining budget
budget_remaining_minutes = budget_total_minutes - consumed_minutes
budget_remaining_percent = budget_remaining_minutes / budget_total_minutes * 100

# Time to exhaustion at current burn rate
time_to_exhaustion_hours = budget_remaining_minutes / (burn_rate * (1 - SLO_target) * 60)
```

### Multi-Window Alert Logic

Google's recommended multi-window alert prevents both alert storms (1h window) and slow-burn misses (6h window):

```
# SEV2 page trigger:
burn_rate_1h > 14.4 AND burn_rate_6h > 6.0

# Rationale:
# 14.4x burn for 1h = 1/14.4 of 30-day budget consumed in 1 hour
# 6.0x burn for 6h  = 1/6.0 of budget consumed per normalized window
# Both required = reduces false positives from traffic spikes

# SEV1 trigger:
budget_remaining_percent < 10%
OR
burn_rate_1h > 50  # consuming budget so fast that SEV2 response is too slow
```

### Prometheus Alert Definitions

```yaml
groups:
  - name: slo_burn_rates
    interval: 1m
    rules:
      # Pre-compute burn rates for efficiency
      - record: slo:error_rate:1h
        expr: |
          1 - (
            sum(rate(http_requests_total{status!~"5.."}[1h]))
            / sum(rate(http_requests_total[1h]))
          )

      - record: slo:burn_rate:1h
        expr: slo:error_rate:1h / (1 - 0.999)  # adjust SLO target

      - record: slo:burn_rate:6h
        expr: |
          (1 - sum(rate(http_requests_total{status!~"5.."}[6h])) /
               sum(rate(http_requests_total[6h]))) / (1 - 0.999)

      # Multi-window alert
      - alert: SloBurnRateFast
        expr: |
          slo:burn_rate:1h > 14.4 AND slo:burn_rate:6h > 6.0
        for: 1m
        labels:
          severity: page
          team: sre
        annotations:
          summary: "Fast SLO burn: {{ $labels.service }}"
          description: |
            1h burn rate: {{ $value | humanize }}x
            Time to exhaustion: approx {{ printf "%.1f" (div 1.0 $value) }} hours at current rate

      - alert: SloErrorBudgetLow
        expr: |
          slo:error_budget_remaining_percent < 10
        labels:
          severity: page
          team: sre
        annotations:
          summary: "Error budget <10%: {{ $labels.service }}"
```

---

## 2. Response by Burn Rate Level

### Burn Rate 2-5x: Watch and Investigate

```bash
# Not a page — but check during business hours
# Acceptable action: investigate and file ticket if trend is increasing

# Check what's causing elevated errors
kubectl logs -n production -l app=<service> --since=1h | \
  grep -E "ERROR|500|502|503|504" | \
  awk '{print $NF}' | sort | uniq -c | sort -rn | head -20

# Check recent deploys
kubectl rollout history deployment/<service> -n production
```

### Burn Rate 5-14x: SEV2 — Investigate Urgently

```bash
# Pager fires only if both 1h AND 6h thresholds crossed
# Response window: 30 minutes

# STEP 1: Calculate time to exhaustion
python3 ~/sre-playbooks/scripts/slo_calculator.py \
  --slo 99.9 --window 30 --burn-rate <current_burn_rate>

# STEP 2: Identify error pattern
kubectl logs -n production -l app=<service> --since=30m | \
  grep -E "ERROR|5[0-9]{2}" | \
  python3 -c "
import sys, collections
lines = sys.stdin.readlines()
errors = [l.split()[-1] for l in lines if 'ERROR' in l or '50' in l]
for k, v in collections.Counter(errors).most_common(10):
    print(f'{v:6d} {k}')
"

# STEP 3: Check if error rate is improving or worsening
# (compare 15m window burn rate vs 1h window burn rate)
# If 15m >> 1h: worsening → escalate faster
# If 15m << 1h: improving → observe
```

### Burn Rate >14.4x (1h) AND >6x (6h): SEV2 Page

**Response time target**: acknowledge within 5 minutes, mitigate within 30 minutes.

```bash
# Immediate triage checklist (first 5 minutes):
# 1. Is there a recent deploy?
kubectl rollout history deployment/<service> -n production | tail -5

# 2. Is it one endpoint or all endpoints?
kubectl logs -n production -l app=<service> --since=15m | \
  grep -E "ERROR|5[0-9]{2}" | \
  awk '{print $7}' | sort | uniq -c | sort -rn | head -10  # field 7 = URL path

# 3. Is it one instance or all instances?
kubectl top pods -n production -l app=<service>
for POD in $(kubectl get pods -n production -l app=<service> -o name); do
  ERROR_COUNT=$(kubectl logs -n production "$POD" --since=5m | grep -c "ERROR" || true)
  echo "$POD: $ERROR_COUNT errors"
done

# 4. Is there a dependency that's failing?
kubectl logs -n production -l app=<service> --since=15m | \
  grep -iE "timeout|connection refused|ECONNREFUSED|upstream" | \
  head -20
```

### Burn Rate >50x OR Budget <10%: SEV1

```bash
# Immediate escalation — this is a major incident
# Incident commander should be paged

# While waiting for IC: consider immediate rollback
kubectl rollout undo deployment/<service> -n production

# Or: redirect traffic to stable version
kubectl patch service <service> -n production \
  --patch '{"spec":{"selector":{"version":"stable"}}}'
```

---

## 3. Error Budget Accounting

### Calculate Budget for Current Window

```bash
# Using the SLO calculator script
python3 ~/sre-playbooks/scripts/slo_calculator.py \
  --slo 99.95 \
  --window 30 \
  --consumed-minutes 8.2

# Expected output:
# SLO Target:          99.950%
# Window:              30 days (43200 minutes)
# Error budget total:  21.6 minutes
# Consumed:            8.2 minutes (37.96%)
# Remaining:           13.4 minutes (62.04%)
# At current burn rate: exhaustion in N hours
```

### Budget Policy

| Budget Remaining | Policy |
|-----------------|--------|
| >50% | Feature deploys: unrestricted |
| 25-50% | Feature deploys: require staged rollout |
| 10-25% | Feature deploys: require SRE approval |
| <10% | Feature freeze: bug fixes and rollbacks only |
| Exhausted | Incident review required before any deploy |

---

## 4. Post-Incident: Error Budget Review

After resolving a burn alert, update the error budget tracker:

```bash
# Calculate how much budget was consumed during the incident
INCIDENT_START="2026-03-18T14:00:00Z"
INCIDENT_END="2026-03-18T14:45:00Z"
AFFECTED_REQUEST_RATE=1000  # req/s during incident
INCIDENT_ERROR_RATE=0.15    # 15% error rate

python3 - << EOF
from datetime import datetime

start = datetime.fromisoformat("${INCIDENT_START}".replace("Z", "+00:00"))
end   = datetime.fromisoformat("${INCIDENT_END}".replace("Z", "+00:00"))
duration_min = (end - start).total_seconds() / 60

slo_target = 0.9995
allowed_error_rate = 1 - slo_target
actual_error_rate = ${INCIDENT_ERROR_RATE}

consumed_budget_min = duration_min * (actual_error_rate - allowed_error_rate) / allowed_error_rate
print(f"Incident duration: {duration_min:.1f} minutes")
print(f"Budget consumed: {consumed_budget_min:.2f} minutes")
print(f"Budget consumed: {consumed_budget_min / (30*24*60*(1-slo_target))*100:.1f}% of 30-day budget")
EOF
```

---

## 5. SLO Dashboard Quick Reference

```bash
# One-liner: current burn rates and budget status
curl -s "http://prometheus:9090/api/v1/query_range" \
  --data-urlencode 'query=slo:burn_rate:1h' \
  --data-urlencode "start=$(date -u -v-1H +%s)" \
  --data-urlencode "end=$(date -u +%s)" \
  --data-urlencode 'step=300' | \
  jq -r '.data.result[0].values[-1][1]' | \
  awk '{printf "Current 1h burn rate: %.2fx\n", $1}'
```

---

## Related Runbooks

- [RB-042: Memory Leak](RB-042-memory-leak.md)
- [RB-055: Deploy Rollback](RB-055-deploy-rollback.md)
- [RB-014: Database Failover](RB-014-database-failover.md)

## Further Reading

- [Google SRE Book: Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)
- Multi-window burn rate derivation: Chapter 5, SRE Workbook
