# Production SRE Playbooks

Four battle-proven runbooks for the failure modes that actually page you at 3 AM.
Each section follows the same structure: **Diagnose → Fix → Prevent**.

## Playbook Index

| # | Failure mode | Section |
|---|--------------|---------|
| 1 | Database connection pool exhaustion | [Database Connection Pool Exhaustion](#1-database-connection-pool-exhaustion) |
| 2 | Memory leak in a Go service | [Memory Leak in Go Service](#2-memory-leak-in-go-service) |
| 3 | Thundering herd on cache miss | [Thundering Herd on Cache Miss](#3-thundering-herd-on-cache-miss) |
| 4 | Split-brain in distributed consensus | [Split-Brain in Distributed Consensus](#4-split-brain-in-distributed-consensus) |

---

## 1. Database Connection Pool Exhaustion

### Overview

The database connection pool is full. New requests cannot acquire a connection and
either queue indefinitely, time out, or fail immediately with a connection error.
This is one of the most common P0 causes at high-traffic services.

**Symptoms**
- `too many connections` errors in application logs
- `connection_pool_exhausted_total` counter climbing in Prometheus
- Health check endpoint returns 503
- p99 latency spikes as requests queue waiting for a connection
- DB shows `max_connections` reached: `SELECT count(*) FROM pg_stat_activity`

---

### Diagnose

**Step 1 — Confirm pool exhaustion (< 2 minutes)**

```bash
# PostgreSQL: count active connections vs max_connections
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT
    max_conn,
    used_conn,
    max_conn - used_conn AS available,
    round(used_conn::numeric / max_conn * 100, 1) AS pct_used
  FROM
    (SELECT setting::int AS max_conn FROM pg_settings WHERE name='max_connections') mc,
    (SELECT count(*) AS used_conn FROM pg_stat_activity) uc;
"

# Who is holding connections?
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT pid, usename, application_name, state, wait_event_type, wait_event,
         now() - state_change AS held_for, query
  FROM pg_stat_activity
  WHERE state != 'idle'
  ORDER BY held_for DESC
  LIMIT 20;
"
```

**Step 2 — Find the leaking application**

```bash
# Group connections by application and state
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT application_name, state, count(*)
  FROM pg_stat_activity
  GROUP BY application_name, state
  ORDER BY count(*) DESC;
"

# Check if long-running transactions are blocking
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT pid, now() - pg_stat_activity.query_start AS duration, query, state
  FROM pg_stat_activity
  WHERE (now() - pg_stat_activity.query_start) > interval '30 seconds'
  ORDER BY duration DESC;
"
```

**Step 3 — Check application pool config**

```bash
# HikariCP (Java/Spring Boot)
curl -s http://localhost:8080/actuator/metrics/hikaricp.connections | jq .

# pgbouncer stats
psql -h $PGBOUNCER_HOST -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"
psql -h $PGBOUNCER_HOST -p 6432 -U pgbouncer pgbouncer -c "SHOW STATS;"

# Application Prometheus metrics
curl -s http://localhost:8080/metrics | grep -E "db_pool|connection_pool|hikari"
```

**Step 4 — Check for connection leaks**

```bash
# Find idle-in-transaction connections (common source of leaks)
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT pid, usename, application_name,
         now() - query_start AS idle_in_transaction_for,
         query
  FROM pg_stat_activity
  WHERE state = 'idle in transaction'
  ORDER BY idle_in_transaction_for DESC;
"
```

---

### Fix

**Immediate mitigation (minutes)**

```bash
# Option A: Kill idle-in-transaction connections (safe; clients will reconnect)
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state = 'idle in transaction'
    AND now() - state_change > interval '5 minutes';
"

# Option B: Kill ALL non-superuser connections for emergency relief
# WARNING: this will drop all active queries — confirm with your team first
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE pid <> pg_backend_pid()
    AND usename != 'postgres';
"

# Option C: Restart the leaking application pod/container
# (fastest fix if a single service is leaking)
kubectl rollout restart deployment/$LEAKING_SERVICE -n $NAMESPACE
```

**Verify recovery**

```bash
# Connection count should drop within 30 seconds
watch -n5 'psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c \
  "SELECT count(*) FROM pg_stat_activity;"'

# Application health check
curl -s http://localhost:8080/health | jq .database
```

**Medium-term fix (deploy within 24h)**

```sql
-- Set idle_in_transaction_session_timeout (PostgreSQL 9.6+)
ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';
SELECT pg_reload_conf();

-- Set statement_timeout to prevent runaway queries holding connections
ALTER SYSTEM SET statement_timeout = '60s';
SELECT pg_reload_conf();
```

```yaml
# HikariCP (application.yml) — correct settings
spring:
  datasource:
    hikari:
      maximum-pool-size: 20         # never more than (db_max_connections / app_replicas)
      minimum-idle: 5
      idle-timeout: 600000          # 10 minutes
      max-lifetime: 1800000         # 30 minutes (< DB wait_timeout)
      connection-timeout: 30000     # 30 seconds (not 0 = infinite!)
      leak-detection-threshold: 60000  # 60 seconds — logs stack trace of leak
```

---

### Prevent

1. **PgBouncer in front of every database** — pool multiplexing prevents N×replicas connections
2. **`idle_in_transaction_session_timeout = 30s`** — kills hung transactions automatically
3. **`leak-detection-threshold` in connection pool** — surfaces leaks in logs before production impact
4. **Alert at 80% pool utilization** — `ALERT` when `hikaricp_connections_active / hikaricp_connections_max > 0.8`
5. **Load test pool sizing formula**: `pool_size = (core_count * 2) + effective_spindle_count`
   (Hikari best practice for OLTP)
6. **Circuit breaker on DB calls** — prevents cascading failure from pool exhaustion spreading to other services

---

## 2. Memory Leak in Go Service

### Overview

A Go service is consuming memory that is never released back to the OS or GC.
The process RSS grows monotonically until OOMKill, which restarts the pod and
causes brief availability loss — repeating every N hours.

**Symptoms**
- `container_memory_working_set_bytes` climbing monotonically (no sawtooth)
- Kubernetes events: `OOMKilled` on the pod
- `go_memstats_heap_inuse_bytes` diverges from `go_memstats_heap_alloc_bytes`
- Goroutine count (`go_goroutines`) climbing — goroutine leak
- pprof heap profile shows a single allocation site growing

---

### Diagnose

**Step 1 — Confirm leak pattern (not just load)**

```bash
# Prometheus query — is heap growing regardless of traffic?
# If heap grows while requests_per_second stays flat = definite leak
promtool query instant http://localhost:9090 \
  'rate(go_memstats_heap_alloc_bytes[5m])'

# kubectl top — watch RSS over time
kubectl top pods -l app=$SERVICE -n $NAMESPACE --containers
watch -n30 'kubectl top pods -l app=$SERVICE -n $NAMESPACE'
```

**Step 2 — Capture pprof heap profile**

```bash
# Enable pprof in your Go service (add to main.go if not present):
# import _ "net/http/pprof"
# go func() { log.Fatal(http.ListenAndServe(":6060", nil)) }()

# Capture heap profile (before and after, 30 seconds apart)
curl -s http://localhost:6060/debug/pprof/heap > /tmp/heap_before.pb.gz
sleep 30
curl -s http://localhost:6060/debug/pprof/heap > /tmp/heap_after.pb.gz

# Compare to find allocation growth
go tool pprof -diff_base /tmp/heap_before.pb.gz /tmp/heap_after.pb.gz
# In pprof interactive shell:
# (pprof) top20
# (pprof) web     # requires graphviz — shows call graph
# (pprof) list <FunctionName>

# Flame graph (most readable)
curl -s 'http://localhost:6060/debug/pprof/heap?debug=1' > /tmp/heap_text.txt
go tool pprof -http=:8081 /tmp/heap_after.pb.gz
# Open http://localhost:8081 → "Flame Graph" view
```

**Step 3 — Check goroutine leak**

```bash
# Count goroutines
curl -s http://localhost:6060/debug/pprof/goroutine?debug=1 | head -50

# If count is growing, get full goroutine dump
curl -s 'http://localhost:6060/debug/pprof/goroutine?debug=2' > /tmp/goroutines.txt

# Look for goroutines blocked on:
grep -A3 "goroutine [0-9]" /tmp/goroutines.txt | grep -E "chan (receive|send)|select"

# Prometheus: go_goroutines metric
promtool query instant http://localhost:9090 'go_goroutines{job="your-service"}'
```

**Step 4 — Identify the allocation site**

```bash
# allocs profile = allocations since start (includes GC'd objects)
# heap profile = live objects only — for leaks use heap
curl -s http://localhost:6060/debug/pprof/allocs > /tmp/allocs.pb.gz

go tool pprof /tmp/allocs.pb.gz
# (pprof) top -cum 20    # show cumulative allocation by call path
```

---

### Fix

**Immediate mitigation**

```bash
# Force GC to see if it's a GC tuning issue (not a true leak)
curl -X POST http://localhost:6060/debug/pprof/gc

# Check if GOGC is tuned too aggressively (default 100 is fine for most services)
# If GOGC=off or GOGC=1000, GC almost never runs
kubectl exec -it deployment/$SERVICE -- env | grep GOGC

# Rolling restart (buys time if OOMKill is imminent)
kubectl rollout restart deployment/$SERVICE -n $NAMESPACE

# Increase memory limit temporarily (prevents OOMKill while you fix root cause)
kubectl set resources deployment/$SERVICE \
  --limits=memory=2Gi --requests=memory=1Gi -n $NAMESPACE
```

**Common leak patterns and fixes**

```go
// LEAK PATTERN 1: HTTP response body not closed
resp, err := http.Get(url)
if err != nil { return err }
// BUG: resp.Body is never closed — leaks the underlying TCP connection
defer resp.Body.Close()  // FIX: always defer close

// LEAK PATTERN 2: goroutine leak — channel never closed
func processItems(items []Item) {
    ch := make(chan result)
    for _, item := range items {
        go func(i Item) {
            ch <- process(i)  // BUG: if caller exits early, goroutine blocks forever
        }(item)
    }
    // FIX: use context cancellation + select
}

// LEAK PATTERN 3: map growing unbounded (common in caches)
var cache = make(map[string][]byte)
// FIX: use a TTL cache or LRU with bounded size
// e.g. github.com/hashicorp/golang-lru

// LEAK PATTERN 4: time.Ticker not stopped
ticker := time.NewTicker(30 * time.Second)
// BUG: if the function returns without ticker.Stop(), the ticker goroutine leaks
defer ticker.Stop()  // FIX

// LEAK PATTERN 5: sync.WaitGroup or mutex held after context cancellation
ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
defer cancel()  // FIX: always defer cancel
```

---

### Prevent

1. **`goleak` in tests** — `defer goleak.VerifyNone(t)` at the top of every test function
2. **pprof endpoint always enabled** — never disable in production (it's read-only, low overhead)
3. **Prometheus alert on goroutine growth**:
   ```yaml
   - alert: GoGoroutineLeak
     expr: rate(go_goroutines[10m]) > 10
     for: 20m
     annotations:
       summary: "Goroutine count growing — likely goroutine leak"
   ```
4. **Memory limit = 2× memory request** — gives headroom before OOMKill
5. **`GOGC=100`** — never set to off in production; tune with `GOMEMLIMIT` (Go 1.19+) instead
6. **Weekly pprof baseline** — capture heap snapshot every week; diff against previous to catch slow leaks

---

## 3. Thundering Herd on Cache Miss

### Overview

A popular cache entry expires (or the cache restarts). Every request that was being
served from cache now hits the database simultaneously. The database is overwhelmed,
latency spikes, connections exhaust, and the cache never has time to warm because
the DB is too slow to respond.

**Symptoms**
- Cache hit rate drops from ~99% to ~0% in Prometheus
- DB CPU spikes to 100% immediately after cache restart or TTL expiry
- `SELECT` query rate on DB increases 10–100×
- Application p99 latency goes from ~50ms to ~5000ms
- Error rate increases as DB connections exhaust (see Playbook 1)

---

### Diagnose

**Step 1 — Confirm the thundering herd**

```bash
# Redis: check hit/miss ratio
redis-cli INFO stats | grep -E "keyspace_hits|keyspace_misses"
# Or via Prometheus
promtool query instant http://localhost:9090 \
  'rate(redis_keyspace_hits_total[1m]) / (rate(redis_keyspace_hits_total[1m]) + rate(redis_keyspace_misses_total[1m]))'

# DB: is query rate abnormally high?
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT calls, mean_exec_time, query
  FROM pg_stat_statements
  WHERE calls > 100
  ORDER BY total_exec_time DESC
  LIMIT 10;
"
```

**Step 2 — Identify the hot key**

```bash
# Redis: find hottest keys (requires redis-cli --hotkeys, Redis 4.0+)
redis-cli --hotkeys

# Or use MONITOR for a brief window (WARNING: impacts performance)
timeout 5 redis-cli MONITOR | awk '{print $4}' | sort | uniq -c | sort -rn | head -20

# Check TTL of suspected hot key
redis-cli TTL <suspected-hot-key>
redis-cli DEBUG OBJECT <suspected-hot-key>
```

**Step 3 — Measure DB saturation**

```bash
# PostgreSQL: are we hitting max connections?
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "
  SELECT count(*), state FROM pg_stat_activity GROUP BY state;
"

# Is DB CPU at 100%?
ssh $DB_HOST 'top -bn1 | grep postgres | head -5'

# Check DB slow query log
tail -100 /var/log/postgresql/postgresql-*.log | grep 'duration:'
```

---

### Fix

**Immediate mitigation**

```bash
# Option A: Force cache repopulation from a single writer
# Run a "cache warmer" script that populates the key before TTL expires
python3 scripts/cache_warmer.py --key "$HOT_KEY" --service $SERVICE

# Option B: Extend TTL of existing (stale) cache entries temporarily
# "Better serve stale data than crash the DB"
redis-cli EXPIRE "$HOT_KEY" 3600  # 1 hour

# Option C: Rate-limit DB requests while cache warms
# Activate circuit breaker to shed load
kubectl exec -it deployment/$SERVICE -- curl -X POST \
  http://localhost:8080/admin/circuit-breaker/db/half-open
```

**Implement cache stampede prevention (deploy within 24h)**

```python
import threading
import time
import redis

_refill_locks: dict[str, threading.Lock] = {}

def get_with_mutex_fill(key: str, cache: redis.Redis, db_fetch_fn) -> bytes:
    """
    Probabilistic early expiry + mutex to prevent stampede.
    Only one goroutine/thread recomputes the value; others wait.
    """
    value = cache.get(key)
    if value is not None:
        return value

    # Cache miss — acquire per-key lock so only one thread hits DB
    lock = _refill_locks.setdefault(key, threading.Lock())
    with lock:
        # Double-check: another thread may have filled it while we waited
        value = cache.get(key)
        if value is not None:
            return value

        # We are the one thread responsible for refilling
        value = db_fetch_fn(key)
        # Add jitter to TTL to prevent simultaneous expiry of many keys
        ttl = 300 + int(hash(key) % 60)  # 5–6 minutes, deterministic per key
        cache.setex(key, ttl, value)
        return value
```

```go
// Go: singleflight prevents duplicate DB calls during cache miss
import "golang.org/x/sync/singleflight"

var group singleflight.Group

func GetUser(id string) (*User, error) {
    v, err, _ := group.Do("user:"+id, func() (interface{}, error) {
        // Only ONE goroutine executes this block, regardless of concurrent callers
        user, err := db.QueryUser(id)
        if err == nil {
            cache.Set("user:"+id, user, 5*time.Minute)
        }
        return user, err
    })
    if err != nil { return nil, err }
    return v.(*User), nil
}
```

---

### Prevent

1. **`singleflight` / mutex-fill pattern** — only one request rebuilds the cache per key
2. **Staggered TTLs** — add `random.randint(0, 60)` seconds to TTLs; prevents synchronized expiry
3. **Probabilistic early expiration** (XFetch algorithm):
   ```python
   # Recompute the cache slightly before expiry, in background
   if ttl_remaining < beta * delta * math.log(random.random()):
       asyncio.create_task(background_refresh(key))
   ```
4. **Cache aside with background refresh** — serve stale while refreshing asynchronously
5. **DB read replica** — thundering herd hits replica, not primary
6. **Alert on cache hit rate drop** — `ALERT IF cache_hit_rate < 0.90 for 5m` → P1

---

## 4. Split-Brain in Distributed Consensus

### Overview

The distributed system has formed two partitions, each of which believes it is
the legitimate primary/leader. Both are accepting writes. This leads to data
divergence, conflicting state, and potential data loss on partition healing.

**Classic causes**: network partition, clock skew breaking lease expiry,
misconfigured quorum size, or etcd/Consul/ZooKeeper losing quorum.

**Symptoms**
- Two Kubernetes pods both show `LEADER=true` in `/health`
- etcd: `etcdctl endpoint health` shows split quorum
- Consul: `consul operator raft list-peers` shows two leaders
- Write conflicts appear in logs: `duplicate key violates unique constraint`
- Prometheus: `etcd_server_is_leader` gauge is 1 on more than one pod

---

### Diagnose

**Step 1 — Identify the split**

```bash
# Kubernetes/etcd split
etcdctl --endpoints=https://$ETCD1:2379,https://$ETCD2:2379,https://$ETCD3:2379 \
  endpoint health --cluster

# Check leader count (should be exactly 1)
etcdctl --endpoints=... endpoint status --cluster \
  | awk -F',' '{print $5}' | grep -c "true"

# Consul
consul operator raft list-peers
# Look for: two nodes with "Leader: true"

# ZooKeeper
echo ruok | nc $ZK_HOST1 2181
echo stat | nc $ZK_HOST1 2181 | grep "Mode:"
echo stat | nc $ZK_HOST2 2181 | grep "Mode:"
# Should be: "Mode: follower" or "Mode: leader" (never two leaders)

# Custom Raft/Paxos: check your leader-election metric
promtool query instant http://localhost:9090 \
  'sum(is_leader{job="your-service"})'
# Should be exactly 1; anything else = split-brain or no leader
```

**Step 2 — Assess data divergence**

```bash
# Compare data between the two "primaries"
# PostgreSQL streaming replication split
psql -h $PRIMARY1 -U $DB_USER -c "SELECT pg_current_wal_lsn();"
psql -h $PRIMARY2 -U $DB_USER -c "SELECT pg_current_wal_lsn();"
# If both return values and they differ = split-brain confirmed

# Check for conflicting transactions
psql -h $PRIMARY1 -U $DB_USER -d $DB_NAME -c "
  SELECT schemaname, tablename,
         n_tup_ins, n_tup_upd, n_tup_del
  FROM pg_stat_user_tables
  ORDER BY n_tup_upd DESC LIMIT 10;
"

# etcd: check revision divergence
etcdctl --endpoints=https://$ETCD1:2379 endpoint status
etcdctl --endpoints=https://$ETCD2:2379 endpoint status
# Compare the "RAFT INDEX" and "RAFT TERM" columns
```

**Step 3 — Determine which partition has correct state**

```bash
# Find the partition with the highest commit index (has the most up-to-date log)
etcdctl --endpoints=https://$ETCD1:2379 endpoint status --write-out=table
# Look at "RAFT INDEX" — higher = more up-to-date

# For PostgreSQL: which primary has higher LSN?
# Higher LSN = more writes applied = more recent state
psql -h $PRIMARY1 -c "SELECT pg_current_wal_lsn();" -t
psql -h $PRIMARY2 -c "SELECT pg_current_wal_lsn();" -t
```

---

### Fix

**CRITICAL: DO NOT proceed without on-call lead and data team sign-off for production.**

```bash
# Step 1: STOP WRITES to both partitions immediately
# This prevents further divergence while you resolve the split.

# Kubernetes: scale down application tier to 0 (stops writes)
kubectl scale deployment/$APP -n $NAMESPACE --replicas=0

# OR: enable maintenance mode at load balancer level
kubectl annotate ingress/$INGRESS nginx.ingress.kubernetes.io/server-snippet='return 503;'
```

```bash
# Step 2: For etcd split-brain — remove the minority partition

# Identify minority member (lower raft index = behind)
etcdctl --endpoints=https://$ETCD1:2379,https://$ETCD2:2379,https://$ETCD3:2379 \
  member list

# Remove the lagging member and re-add as learner
etcdctl --endpoints=<surviving_cluster_endpoints> member remove <lagging_member_id>
etcdctl --endpoints=<surviving_cluster_endpoints> member add $ETCD_MINORITY \
  --peer-urls=https://$ETCD_MINORITY:2380 --learner

# Wait for the learner to catch up, then promote
etcdctl --endpoints=<surviving_cluster_endpoints> member promote <learner_id>
```

```bash
# Step 3: PostgreSQL split-brain — promote one primary, demote the other to replica

# Identify canonical primary (higher LSN)
CANONICAL_PRIMARY=$PRIMARY1  # whichever has higher LSN

# Demote the other to standby
# On $PRIMARY2 (the one being demoted):
touch /var/lib/postgresql/data/standby.signal
pg_ctl -D /var/lib/postgresql/data reload

# Configure recovery on demoted node
cat >> /var/lib/postgresql/data/postgresql.conf <<EOF
primary_conninfo = 'host=$CANONICAL_PRIMARY port=5432 user=replicator'
restore_command = 'cp /var/lib/postgresql/wal_archive/%f %p'
EOF

pg_ctl -D /var/lib/postgresql/data restart
```

```bash
# Step 4: Verify consensus restored
etcdctl endpoint health --cluster  # all healthy
consul operator raft list-peers    # exactly one leader

# Step 5: Resolve write conflicts (application-specific)
# Use your conflict resolution strategy:
# - Last-write-wins (timestamp-based)
# - Operational transform
# - Manual reconciliation (for financial data — always manual)

# Step 6: Restore application traffic
kubectl scale deployment/$APP -n $NAMESPACE --replicas=$ORIGINAL_REPLICAS
kubectl annotate ingress/$INGRESS nginx.ingress.kubernetes.io/server-snippet-
```

---

### Prevent

1. **Always use odd-numbered quorum** — 3, 5, or 7 nodes; never 2 or 4
2. **Fencing tokens** — every write to storage must include a monotonically increasing token;
   storage rejects writes with stale tokens even if the writer believes it is leader
3. **Leader lease expiry < network timeout** — if your network can partition for 30s,
   your leader lease must expire in <30s; otherwise stale leaders hold leases
4. **`STONITH`** — Shoot The Other Node In The Head; fence the minority partition at the
   hypervisor/network level before allowing the majority to accept writes
5. **Alert on leader count**:
   ```yaml
   - alert: SplitBrainDetected
     expr: sum(is_leader) != 1
     for: 30s
     labels:
       severity: P0
     annotations:
       summary: "Split-brain: {{ $value }} leaders detected (expected exactly 1)"
   ```
6. **Chaos test quarterly** — deliberately partition the network and verify your fencing
   implementation actually prevents writes on the minority side

---

*Last updated: 2026-03-18 | Maintained by: SRE Platform Team*
*Questions: #sre-oncall | Runbook issues: open a PR*
