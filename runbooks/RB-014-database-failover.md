# RB-014: Database Primary Failover

**Severity**: SEV1 (data path down) → SEV2 post-failover
**Category**: Database Availability
**Last Reviewed**: 2026-03-18
**Owner**: SRE On-Call + DBA On-Call
**RTO Target**: < 5 minutes
**RPO Target**: < 30 seconds

---

## Overview

This runbook covers failover procedures for PostgreSQL primary/replica topologies. Applies to RDS Multi-AZ, Patroni clusters, and manually managed streaming replication setups. The goal is to restore writes within 5 minutes and guarantee no more than 30 seconds of data loss.

**Critical**: Never promote a replica without verifying replication lag. Promoting a lagged replica risks data loss beyond RPO.

---

## 1. Detection

### Automated Alerts

```yaml
# Prometheus alerts
- alert: DatabasePrimaryDown
  expr: pg_up{role="primary"} == 0
  for: 30s
  labels:
    severity: page
  annotations:
    summary: "PostgreSQL primary unreachable"

- alert: ReplicationLagHigh
  expr: |
    pg_replication_lag_seconds > 30
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "Replica {{ $labels.instance }} lag {{ $value }}s"

- alert: ReplicationSlotsInactive
  expr: |
    pg_replication_slots_active == 0 AND pg_replication_slots_pg_size_bytes > 1073741824
  labels:
    severity: critical
  annotations:
    summary: "Inactive replication slot consuming >1GB WAL"
```

### Manual Detection

```bash
# Check primary health
psql -h primary.db.internal -U postgres -c "SELECT 1" 2>&1

# Check replication lag from replica side
psql -h replica.db.internal -U postgres -c "
  SELECT
    now() - pg_last_xact_replay_timestamp() AS replication_lag,
    pg_is_in_recovery() AS is_replica,
    pg_last_wal_receive_lsn() AS received_lsn,
    pg_last_wal_replay_lsn() AS replayed_lsn;
"

# Check from primary: replication slot status
psql -h primary.db.internal -U postgres -c "
  SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal
  FROM pg_replication_slots;
"

# Check WAL accumulation
psql -h primary.db.internal -U postgres -c "SELECT pg_size_pretty(sum(size)) FROM pg_ls_waldir();"
```

### Replication Slot Bloat Detection

```bash
# Replication slot bloat = inactive slot retaining WAL = disk full risk
psql -U postgres -c "
  SELECT
    slot_name,
    active,
    pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS wal_retained,
    pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS wal_bytes
  FROM pg_replication_slots
  WHERE NOT active
  ORDER BY wal_bytes DESC;
"

# DANGER ZONE: if wal_retained > 50% of disk capacity, drop the slot
# This will break the consumer — ensure consumer is confirmed dead first
# pg_drop_replication_slot('slot_name');
```

---

## 2. Triage

### Step 1: Is the Primary Actually Down or Just Unreachable?

```bash
# From application server: can we connect?
pg_isready -h primary.db.internal -p 5432 -U postgres

# From DBA bastion: direct connection test
psql -h primary.db.internal -U postgres -c "SELECT version();"

# From another AZ/region: rule out network partition
ssh bastion-us-east-2 "pg_isready -h primary.db.internal -p 5432"

# Check if primary thinks it is primary
psql -h primary.db.internal -U postgres -c "SELECT pg_is_in_recovery();"
# Returns: f = primary | t = replica (split-brain risk if you also see t on another node)
```

### Step 2: Assess Current Replica State

```bash
# For each replica, check:
for REPLICA in replica-1.db.internal replica-2.db.internal; do
  echo "=== $REPLICA ==="
  psql -h "$REPLICA" -U postgres -c "
    SELECT
      pg_is_in_recovery() AS is_replica,
      now() - pg_last_xact_replay_timestamp() AS lag,
      pg_last_wal_receive_lsn() AS recv_lsn,
      pg_last_wal_replay_lsn() AS replay_lsn;
  "
done

# Identify the most caught-up replica (highest replay LSN = least lag)
```

### Step 3: Verify WAL Accumulation on Disk

```bash
# Check disk on primary (if accessible)
ssh primary.db.internal "df -h /var/lib/postgresql/"

# Check WAL directory size
ssh primary.db.internal "du -sh /var/lib/postgresql/*/pg_wal/"

# If primary is unreachable: check replica for signs of slot bloat
psql -h replica.db.internal -U postgres -c "
  SELECT pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), pg_last_wal_replay_lsn())) AS wal_gap;
"
```

### Go/No-Go Criteria for Promotion

| Condition | Decision |
|-----------|----------|
| Primary confirmed unreachable, lag <30s | Proceed with promotion |
| Primary confirmed unreachable, lag >30s | Page DBA lead, assess data loss tolerance |
| Primary reachable but degraded | Do NOT promote — fix primary instead |
| Split-brain suspected (two nodes claim primary) | STOP — page DBA lead immediately |

---

## 3. Mitigation: Failover Procedure

### Automatic Failover (Patroni)

```bash
# Check current Patroni cluster state
patronictl -c /etc/patroni/config.yml list

# Patroni auto-promotes within ~30s of primary failure
# If it hasn't promoted after 60s:
patronictl -c /etc/patroni/config.yml failover <cluster-name> --master <primary-name> --candidate <replica-name> --force

# Verify new primary
patronictl -c /etc/patroni/config.yml list
```

### Manual Promotion (streaming replication without Patroni)

```bash
# STEP 1: Confirm primary is truly down (not a network partition)
ping -c 3 primary.db.internal || echo "UNREACHABLE"

# STEP 2: Choose the most up-to-date replica
# (from triage: highest pg_last_wal_replay_lsn())
TARGET_REPLICA="replica-1.db.internal"

# STEP 3: Promote the replica
ssh "$TARGET_REPLICA" "pg_ctl promote -D /var/lib/postgresql/data"

# STEP 4: Verify promotion succeeded
psql -h "$TARGET_REPLICA" -U postgres -c "SELECT pg_is_in_recovery();"
# Must return: f

# STEP 5: Record the LSN at promotion time for other replicas
PROMOTION_LSN=$(psql -h "$TARGET_REPLICA" -U postgres -Atc "SELECT pg_current_wal_lsn();")
echo "Promotion LSN: $PROMOTION_LSN"
```

### Update Connection Strings

```bash
# STEP 6: Update application configuration
# Option A: DNS failover (preferred — applications reconnect automatically)
# Update Route53 / internal DNS CNAME for db.internal → new primary IP
aws route53 change-resource-record-sets --hosted-zone-id <zone-id> \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "primary.db.internal",
        "Type": "CNAME",
        "TTL": 10,
        "ResourceRecords": [{"Value": "replica-1.db.internal"}]
      }
    }]
  }'

# Option B: Environment variable update + rolling restart
kubectl set env deployment/<app> DATABASE_URL=postgresql://replica-1.db.internal/appdb -n production

# STEP 7: Force application reconnect (connection pools hold stale connections)
kubectl rollout restart deployment/<app> -n production
```

### Repoint Remaining Replicas

```bash
# STEP 8: Point old replicas to new primary
for REPLICA in replica-2.db.internal replica-3.db.internal; do
  ssh "$REPLICA" "psql -U postgres -c \"
    SELECT pg_promote();  -- PostgreSQL 12+
  \""
  # Or for older versions: pg_ctl promote -D /var/lib/postgresql/data

  # Update recovery.conf / primary_conninfo (PostgreSQL 11 and below)
  ssh "$REPLICA" "cat > /var/lib/postgresql/data/recovery.conf << EOF
standby_mode = on
primary_conninfo = 'host=replica-1.db.internal port=5432 user=replication password=<pass>'
recovery_target_timeline = 'latest'
EOF
  "
  ssh "$REPLICA" "pg_ctl reload -D /var/lib/postgresql/data"
done
```

### Verification Checklist

```bash
# Verify new primary is accepting writes
psql -h primary.db.internal -U postgres -c "
  INSERT INTO healthcheck (ts) VALUES (now()) RETURNING ts;
"

# Verify replicas are replicating from new primary
psql -h primary.db.internal -U postgres -c "
  SELECT client_addr, state, sent_lsn, replay_lsn,
         pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) AS lag
  FROM pg_stat_replication;
"

# Verify application is writing to new primary
kubectl logs -n production -l app=<app> --since=5m | grep -i "database\|postgres\|write"

# Verify replication lag is converging to 0
watch -n 5 'psql -h replica.db.internal -U postgres -c "
  SELECT now() - pg_last_xact_replay_timestamp() AS lag;"'
```

---

## 4. RTO/RPO Verification

```bash
# Calculate actual RPO: time between last confirmed write and first successful write after promotion
# Check application logs for last successful write before incident
grep -E "INSERT|UPDATE|DELETE" /var/log/app/app.log | tail -50

# Calculate RTO: time from first alert to first successful write after failover
# Should be < 5 minutes total:
# - Alert fire: 30s
# - Triage: 60s
# - Promotion: 30s
# - DNS propagation: 30s (TTL=10s recommended for db DNS)
# - Application reconnect: 60s
# Total typical: ~3.5 minutes
```

---

## 5. Prevention

### Replication Monitoring

```bash
# Add to cron: daily replication slot audit
# Alert if any slot retains > 1GB of WAL
psql -U postgres -c "
  SELECT slot_name, active,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))
  FROM pg_replication_slots
  WHERE pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 1073741824
" | mail -s "WAL slot bloat alert" sre-oncall@company.com
```

### Runbook Drill Schedule

| Drill Type | Frequency | Owner |
|------------|-----------|-------|
| Read replica health check | Daily (automated) | SRE platform |
| Failover simulation (staging) | Monthly | SRE + DBA |
| Full production failover drill | Quarterly | SRE lead + DBA lead |
| RPO verification | After every failover drill | DBA |

---

## Related Runbooks

- [RB-042: Memory Leak](RB-042-memory-leak.md)
- [RB-055: Deploy Rollback](RB-055-deploy-rollback.md)
- [RB-031: SLO Burn Alert Response](RB-031-slo-burn-alert.md)
