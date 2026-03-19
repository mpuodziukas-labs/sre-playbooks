# RB-055: Deploy Rollback

**Severity**: SEV2 (canary abort) → SEV1 (full rollout, errors impacting users)
**Category**: Deployment Safety
**Last Reviewed**: 2026-03-18
**Owner**: SRE On-Call + Release Engineer

---

## Overview

This runbook covers safe rollback procedures for Kubernetes deployments. A rollback should be the first tool when a deploy causes elevated error rates — it is faster and safer than hotfixes under pressure. Time to rollback should be under 3 minutes.

**Principle**: When in doubt, roll back. You can always redeploy with a fix. Data loss from a bad migration is much harder to recover from than a delayed feature.

---

## 1. Canary Abort Criteria

A canary should be aborted and rolled back if ANY of the following are true within 15 minutes of deployment:

| Signal | Abort Threshold | Measurement Window |
|--------|----------------|-------------------|
| Error rate delta (canary vs baseline) | >0.5% | 5 minutes |
| SLO burn rate | >2.0x | 5 minutes |
| P99 latency increase | >20% | 10 minutes |
| Panic/crash logs | Any new pattern | 5 minutes |
| OOMKilled pods | Any | Immediate |
| Health check failures | >2 consecutive | Immediate |

### Prometheus Canary Health Queries

```promql
# Error rate delta: canary vs stable
(
  rate(http_requests_total{version="canary",status=~"5.."}[5m]) /
  rate(http_requests_total{version="canary"}[5m])
) - (
  rate(http_requests_total{version="stable",status=~"5.."}[5m]) /
  rate(http_requests_total{version="stable"}[5m])
)

# Should be < 0.005 (0.5%)

# Canary burn rate vs budget
(1 - sum(rate(http_requests_total{version="canary",status!~"5.."}[5m])) /
     sum(rate(http_requests_total{version="canary"}[5m]))) / (1 - 0.999)

# Should be < 2.0x
```

---

## 2. Rollback Command Sequence

### Kubernetes Deployment Rollback (Fastest Path)

```bash
# STEP 1: Identify the deployment and current revision
kubectl rollout history deployment/<deployment-name> -n <namespace>

# Example output:
# REVISION  CHANGE-CAUSE
# 1         Initial deploy v2.3.0
# 2         Deploy v2.4.0 (breaking change)

# STEP 2: Roll back to previous revision
kubectl rollout undo deployment/<deployment-name> -n <namespace>

# OR roll back to a specific revision
kubectl rollout undo deployment/<deployment-name> -n <namespace> --to-revision=1

# STEP 3: Watch rollback progress
kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=300s

# STEP 4: Verify the image has been reverted
kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].image}'
```

### Helm Chart Rollback

```bash
# Check release history
helm history <release-name> -n <namespace>

# Rollback to previous revision
helm rollback <release-name> -n <namespace>

# Rollback to specific revision
helm rollback <release-name> <revision-number> -n <namespace>

# Watch status
helm status <release-name> -n <namespace>
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

### ArgoCD Rollback

```bash
# Via CLI
argocd app rollback <app-name> --revision <revision-id>

# Verify
argocd app get <app-name> | grep -E "Status|Health"
```

---

## 3. Traffic Drain: Wait for In-Flight Requests

Before triggering a rollback during active traffic, drain gracefully to avoid dropped requests.

### Verify Deployment Strategy

```bash
# Check that the deployment has graceful termination configured
kubectl get deployment <deployment-name> -n <namespace> -o json | jq '
  {
    strategy: .spec.strategy,
    terminationGracePeriod: .spec.template.spec.terminationGracePeriodSeconds,
    maxUnavailable: .spec.strategy.rollingUpdate.maxUnavailable,
    maxSurge: .spec.strategy.rollingUpdate.maxSurge
  }
'

# Recommended settings for zero-downtime rollback:
# strategy: RollingUpdate
# maxUnavailable: 0      (never take down a pod before a replacement is up)
# maxSurge: 1            (allow one extra pod during transition)
# terminationGracePeriod: 60  (60s for in-flight requests to complete)
```

### Monitor Drain Progress

```bash
# Watch active connections draining from terminating pods
# (requires container to expose connection count metric)
watch -n 2 'kubectl exec -n <namespace> <pod-name> -- ss -s | grep "estab"'

# Or check access logs for the draining pod
kubectl logs -n <namespace> <old-pod-name> -f | \
  grep -v "200 OK" | \
  tail -20

# Kubernetes fires SIGTERM → app should drain → SIGKILL after terminationGracePeriodSeconds
# Pod is removed from endpoints immediately on deletion (preStop hook can add a sleep)
```

### PreStop Hook (Best Practice — add to all services)

```yaml
# In deployment spec: gives 5s for load balancer to update before SIGTERM
lifecycle:
  preStop:
    exec:
      command: ["/bin/sh", "-c", "sleep 5"]
```

---

## 4. Database Migration Rollbacks

**WARNING**: Rollback is most dangerous when a migration has already run. Follow these steps carefully.

### Check if Migration Ran

```bash
# PostgreSQL: check migration history
psql -h db.internal -U postgres -d appdb -c "
  SELECT version, applied_at FROM schema_migrations ORDER BY applied_at DESC LIMIT 10;
"

# Alembic
alembic history --indicate-current
alembic current

# Flyway
flyway info -url=jdbc:postgresql://db.internal/appdb
```

### Backward-Compatible Migration (safe to rollback)

If the migration only ADDS columns (no drops, no NOT NULL constraints on existing columns):

```bash
# Application rollback is safe — old code can run against new schema
kubectl rollout undo deployment/<deployment-name> -n <namespace>
# No migration reversal needed
```

### Destructive Migration (column drop, constraint change): Emergency Procedure

```bash
# If data has been deleted/modified — rollback requires restore from backup
# STOP: page DBA lead before proceeding

# Assess damage:
psql -h db.internal -U postgres -d appdb -c "
  SELECT schemaname, tablename, n_live_tup, n_dead_tup
  FROM pg_stat_user_tables
  ORDER BY n_dead_tup DESC LIMIT 10;
"

# If destructive migration ran < 5 minutes ago and PostgreSQL 14+:
# PITR to just before migration timestamp
# Coordinate with DBA for restore procedure
```

---

## 5. Post-Rollback Verification

```bash
# STEP 1: Confirm all pods are running the old image
kubectl get pods -n <namespace> -l app=<app-label> -o jsonpath='{.items[*].spec.containers[0].image}'

# STEP 2: Verify error rate has returned to baseline
# Wait 3-5 minutes for metrics to stabilize, then check:
curl -s "http://prometheus:9090/api/v1/query?query=rate(http_requests_total{status=~'5..'}[5m])" | \
  jq -r '.data.result[0].value[1]'

# STEP 3: Verify SLO burn rate is returning to normal
python3 ~/sre-playbooks/scripts/slo_calculator.py \
  --slo 99.9 --window 30

# STEP 4: Synthetic smoke test
curl -f "https://api.example.com/healthz" && echo "PASS: healthz"
curl -f "https://api.example.com/ready" && echo "PASS: ready"

# Run full smoke test suite if available
make smoke-test ENVIRONMENT=production

# STEP 5: Confirm in incident channel
echo "Rollback complete. Deployed version: $(kubectl get deploy <name> -n production -o jsonpath='{.spec.template.spec.containers[0].image}')"
```

---

## 6. Rollback Communication Template

Post to incident channel immediately after rollback decision:

```
[ROLLBACK] <service-name> v<bad-version> → v<good-version>
Reason: <error rate delta / burn rate / crash pattern>
Metrics at rollback: error_rate=X.X%, burn=Y.Yx, p99=ZZZms
Rollback started: HH:MM UTC
Expected recovery: ~3 minutes
Migration impact: None / DBA assessing
IC: @<on-call>
```

Post-rollback confirmation:

```
[RESOLVED] <service-name> rollback complete
Deployed version: v<good-version>
Error rate: back to baseline (X.X%)
SLO burn rate: Y.Yx (normal)
Root cause investigation: <ticket link>
```

---

## 7. Root Cause Investigation (Post-Rollback)

```bash
# Capture the diff between bad and good version
git log --oneline v<good-version>..v<bad-version>

# Check which files changed
git diff v<good-version>..v<bad-version> --stat

# Identify the first error in logs from new version
kubectl logs -n <namespace> <old-deployment-pod> --since=2h | \
  grep -E "ERROR|FATAL|panic|OOMKilled" | \
  head -50

# Create fix ticket with:
# - Rollback timestamp
# - Bad version tag
# - Error signature
# - Logs excerpt
# - Git diff link
```

---

## 8. Preventing Future Rollbacks

### Deployment Health Gate (add to CI/CD pipeline)

```bash
#!/bin/bash
# post-deploy-gate.sh: run after every deploy, fail if canary is unhealthy
NAMESPACE="${1:-production}"
DEPLOYMENT="${2}"
WAIT_SECONDS=300  # 5 minute burn-in

echo "Monitoring canary health for ${WAIT_SECONDS}s..."
sleep "$WAIT_SECONDS"

BURN_RATE=$(curl -s "http://prometheus:9090/api/v1/query?query=slo:burn_rate:1h" | \
  jq -r '.data.result[0].value[1]')

if (( $(echo "$BURN_RATE > 2.0" | bc -l) )); then
  echo "FAIL: Burn rate ${BURN_RATE}x exceeds abort threshold 2.0x"
  echo "Initiating rollback..."
  kubectl rollout undo deployment/"$DEPLOYMENT" -n "$NAMESPACE"
  exit 1
fi

echo "PASS: Burn rate ${BURN_RATE}x within bounds"
```

---

## Related Runbooks

- [RB-031: SLO Burn Alert](RB-031-slo-burn-alert.md)
- [RB-019: Kubernetes Node Pressure](RB-019-kubernetes-node-pressure.md)
- [RB-014: Database Failover](RB-014-database-failover.md)
