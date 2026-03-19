# RB-042: Memory Leak Investigation and Mitigation

**Severity**: SEV2 (potential SEV1 if service is unavailable)
**Category**: Resource Exhaustion
**Last Reviewed**: 2026-03-18
**Owner**: SRE On-Call

---

## Overview

This runbook covers detection, triage, mitigation, and prevention of memory leaks in production services. Memory leaks left unaddressed cause OOMKill events, cascading failures, and degraded user experience.

**SLO Impact**: Memory leaks degrade latency (GC pressure) before causing outages. A service consuming >90% memory budget triggers a SEV2 before the kill occurs.

---

## 1. Detection

### Automated Alerts

```
# Prometheus alert: RSS growing >500MB/hr sustained for 15 minutes
- alert: MemoryLeakSuspected
  expr: |
    rate(process_resident_memory_bytes[1h]) > 524288000
  for: 15m
  labels:
    severity: warning
  annotations:
    summary: "{{ $labels.job }} RSS growing >500MB/hr"
    description: "Growth rate: {{ $value | humanize }}B/s"

# OOM kill imminent: >85% container memory limit
- alert: MemoryPressureCritical
  expr: |
    container_memory_working_set_bytes / container_spec_memory_limit_bytes > 0.85
  for: 5m
  labels:
    severity: page
```

### Manual Detection

```bash
# Check RSS trend for a process over time
ps -o pid,rss,vsz,comm -p <PID>

# Watch live memory growth
watch -n 5 'ps -o pid,rss,%mem,cmd -p <PID>'

# Check for container nearing limits in Kubernetes
kubectl top pods -n <namespace> --sort-by=memory

# Check OOMKilled events
kubectl get events -n <namespace> --field-selector reason=OOMKilling

# Identify process by memory consumption
ps aux --sort=-%mem | head -20
```

### Heap Profiler Output (Python)

```bash
# Attach py-spy to running process
py-spy record --pid <PID> --format speedscope -o profile.json --duration 30

# Memory snapshot with memray
python3 -m memray run --live --live-port 1234 -m your_service
# or attach to running process
memray attach <PID>

# For Node.js: take heap snapshot
kill -USR2 <PID>  # writes heapsnapshot to CWD

# For Go: pprof endpoint (if exposed)
curl http://localhost:6060/debug/pprof/heap > heap.prof
go tool pprof -http=:8080 heap.prof
```

### Key Indicators

| Metric | Warning Threshold | Critical Threshold |
|--------|-----------------|-------------------|
| RSS growth rate | >100MB/hr | >500MB/hr |
| Container memory % | >75% | >85% |
| GC pause duration | >100ms p99 | >500ms p99 |
| Heap fragmentation | >30% | >50% |

---

## 2. Triage

### Step 1: Identify the Leaking Process

```bash
# In Kubernetes: find the pod with highest memory and rising trend
kubectl top pods -n <namespace> --sort-by=memory

# Check recent OOMKill history
kubectl get events -n <namespace> -o json | \
  jq '.items[] | select(.reason=="OOMKilling") | {time: .lastTimestamp, pod: .involvedObject.name, message: .message}'

# Compare current vs 1-hour-ago memory for all pods
# (requires Prometheus)
promtool query instant 'topk(10, container_memory_working_set_bytes{namespace="production"})'
```

### Step 2: Check for Retention Patterns

```bash
# Python: find objects growing unboundedly
python3 - <<'EOF'
import tracemalloc, time
tracemalloc.start()
# ... trigger suspected code path ...
snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics('lineno')
for stat in top_stats[:15]:
    print(stat)
EOF

# Check for common leak patterns:
# 1. Unbounded caches (no TTL/max size)
grep -rn "cache\|dict\|list\|queue" src/ | grep -v "maxsize\|ttl\|maxlen" | head -20

# 2. Event listeners never removed
grep -rn "addEventListener\|on\b\|subscribe" src/ | grep -v "removeEventListener\|off\b\|unsubscribe" | head -20

# 3. Long-lived closures capturing large objects
# Review code paths that create closures in request handlers
```

### Step 3: Correlate with Traffic or Deployments

```bash
# Check if growth correlates with deploy time
git log --oneline --since="4 hours ago"

# Check if growth correlates with specific request types
kubectl logs -n <namespace> <pod> --since=1h | \
  grep -E "POST|PUT|PATCH" | \
  awk '{print $1, $2, $NF}' | \
  sort | uniq -c | sort -rn | head -20

# Check if growth is per-worker or per-process (indicates request vs startup leak)
kubectl exec -n <namespace> <pod> -- ps aux --sort=-%mem
```

### Triage Decision Tree

```
RSS growing >500MB/hr?
├── Yes → Is it isolated to one pod/instance?
│   ├── Yes → Single bad deployment or data pattern → proceed to mitigation
│   └── No → Systemic issue → escalate to SEV1, pull all affected instances
└── No → Monitor, check after 30 min
    └── Still growing? → Escalate to above
```

---

## 3. Mitigation

### Option A: Controlled Restart (preferred for stateless services)

```bash
# 1. Cordon the leaking pod's node (prevents new pods landing there)
kubectl cordon <node-name>

# 2. Enable traffic draining: reduce pod weight in load balancer
# For Kubernetes services with Readiness gates:
kubectl patch pod <pod-name> -n <namespace> -p '{"metadata":{"labels":{"draining":"true"}}}'

# 3. Wait for in-flight requests to complete
# Monitor active connections (if exposed via metrics)
watch kubectl exec -n <namespace> <pod> -- ss -s

# 4. Delete the pod (deployment will reschedule on healthy node)
kubectl delete pod <pod-name> -n <namespace>

# 5. Verify replacement pod is healthy
kubectl rollout status deployment/<deployment-name> -n <namespace>

# 6. Uncordon the node after root cause is identified
kubectl uncordon <node-name>
```

### Option B: Rolling Restart (for fleet-wide leak)

```bash
# Rolling restart with zero downtime (maxUnavailable=0 in strategy)
kubectl rollout restart deployment/<deployment-name> -n <namespace>

# Watch progress
kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=300s

# Verify no memory spike after restart
kubectl top pods -n <namespace> -l app=<app-label> --sort-by=memory
```

### Option C: Memory Limit Bump (emergency stop-gap only)

```bash
# ONLY if restart is not possible and OOMKill is imminent
# This buys time — root cause MUST be fixed within 24h
kubectl patch deployment <deployment-name> -n <namespace> \
  --patch '{"spec":{"template":{"spec":{"containers":[{"name":"<container>","resources":{"limits":{"memory":"2Gi"}}}]}}}}'

# Create P1 ticket immediately: "Memory limit bumped as stop-gap — root cause TBD"
```

### Traffic Draining Procedure

```bash
# For services behind a load balancer:
# 1. Mark pod as not-ready via readiness probe failure
kubectl exec -n <namespace> <pod> -- touch /tmp/unhealthy  # if probe checks file

# 2. Monitor connection drain in access logs
kubectl logs -n <namespace> <pod> -f | grep -v "200 OK" | tail -20

# 3. Wait for active connections to drop to <10
# 4. Restart is now safe
```

---

## 4. Prevention

### CI: Heap Snapshots

```yaml
# .github/workflows/memory-profile.yml
- name: Memory baseline test
  run: |
    python3 -m pytest tests/ -k "memory" --memray \
      --memray-output-file=.memray/result.bin
    python3 -m memray stats .memray/result.bin \
      --peak-memory > memory_report.txt

    # Fail if peak exceeds budget
    PEAK=$(grep "Peak memory" memory_report.txt | awk '{print $3}')
    if [ "$PEAK" -gt "524288000" ]; then
      echo "FAIL: Peak memory $PEAK exceeds 500MB budget"
      exit 1
    fi

- name: Upload memory profile
  uses: actions/upload-artifact@v4
  with:
    name: memory-profile
    path: .memray/
```

### Memory Budgets Per Service

Define in `service-config.yaml`:

```yaml
# Example memory budget definitions
services:
  api-gateway:
    memory_budget_bytes: 512_000_000  # 512MB
    growth_alert_bytes_per_hour: 52_428_800  # 50MB/hr
    oom_kill_limit: "768Mi"

  worker:
    memory_budget_bytes: 1_073_741_824  # 1GB
    growth_alert_bytes_per_hour: 104_857_600  # 100MB/hr
    oom_kill_limit: "1.5Gi"
```

### Code Review Checklist for Memory Safety

- [ ] All caches have `maxsize` and/or `ttl` parameters
- [ ] All event listeners are removed on cleanup/shutdown
- [ ] Background tasks are tracked and cancelled on service stop
- [ ] Large payloads are streamed, not buffered in memory
- [ ] Connection pools have explicit limits
- [ ] Circular references avoided or broken with `weakref`
- [ ] File handles closed in `finally` blocks or context managers

---

## 5. Escalation

| Condition | Action |
|-----------|--------|
| Single pod, stateless service | Restart, file root-cause ticket |
| Multiple pods affected | SEV2 incident, full team page |
| Memory exhaustion causing user-visible errors | SEV1, engage incident commander |
| Root cause not found within 2 hours | Engage senior engineer + memory profiling session |

---

## Related Runbooks

- [RB-019: Kubernetes Node Pressure](RB-019-kubernetes-node-pressure.md)
- [RB-055: Deploy Rollback](RB-055-deploy-rollback.md)
- [RB-031: SLO Burn Alert Response](RB-031-slo-burn-alert.md)
