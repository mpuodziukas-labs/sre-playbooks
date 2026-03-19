# RB-008: CDN Cache Purge

**Severity**: SEV3 (stale content) → SEV2 (incorrect/harmful content)
**Category**: Content Delivery
**Last Reviewed**: 2026-03-18
**Owner**: SRE On-Call

---

## Overview

This runbook covers CDN cache purge procedures for Cloudflare, Fastly, and AWS CloudFront. An incorrect purge can spike origin traffic significantly. A missed purge can serve stale or harmful content to users.

**Cost Awareness**: Full cache purge = cold cache = origin spike. For large-scale purges, pre-warm the origin or notify infrastructure team before purging.

---

## 1. When to Purge

### Purge Required

| Scenario | Purge Scope | Urgency |
|----------|-------------|---------|
| Security incident: sensitive data in cached response | Full purge | Immediate |
| Deploy rollback: new assets not served | Tag/path purge | Within 5 min |
| Content update not propagating | URL-targeted purge | Within 15 min |
| A/B test contamination | Segment/tag purge | Within 30 min |
| Pricing/product data stale | URL-targeted purge | Within 5 min |

### Do NOT Purge When

- Cache-Control headers have short TTLs (<60s) — wait for natural expiry
- Issue is origin-side (purging helps nothing, diagnosis first)
- Traffic is already low (purge cost may exceed benefit)
- Only affecting single user (likely client-side cache — advise hard refresh)

---

## 2. Purge Scope Decision

### Scope Selection Framework

```
Is the stale content a security risk?
├── Yes → Full purge + immediate page SRE lead
└── No → Can you identify specific URLs?
    ├── Yes → URL-targeted purge (cheapest, safest)
    └── No → Can you identify by tag/path?
        ├── Yes → Tag or wildcard purge
        └── No → Full purge (last resort — see cost section)
```

---

## 3. Cloudflare Purge Procedures

### URL-Targeted Purge

```bash
# Single URL
curl -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/purge_cache" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{
    "files": ["https://example.com/api/prices", "https://example.com/api/products"]
  }'

# Verify response: "result":{"id":"..."} and "success":true
```

### Cache Tag Purge (Cloudflare Enterprise)

```bash
# Requires CF-Cache-Tag response header on origin responses
# Tags are set in origin e.g.: CF-Cache-Tag: product-123,category-shoes

curl -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/purge_cache" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"tags": ["product-123", "deploy-v2.4.1"]}'
```

### Wildcard / Path Purge (Enterprise)

```bash
# Purge all URLs matching a prefix
curl -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/purge_cache" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"prefixes": ["https://example.com/api/v1/", "https://example.com/static/js/"]}'
```

### Full Purge (Cloudflare)

```bash
# WARNING: Purges ALL cached content. Notify infra team first.
# Expect origin traffic to spike 5-20x for 30-120 seconds.
curl -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/purge_cache" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"purge_everything": true}'
```

---

## 4. AWS CloudFront Purge Procedures

### Path-Based Invalidation

```bash
# Targeted path invalidation
aws cloudfront create-invalidation \
  --distribution-id "${CF_DISTRIBUTION_ID}" \
  --paths "/api/prices" "/api/products" "/static/js/main-*.js"

# Wildcard path (expensive — costs as one invalidation per path, wildcard counts as 1)
aws cloudfront create-invalidation \
  --distribution-id "${CF_DISTRIBUTION_ID}" \
  --paths "/api/*"

# Full invalidation (/* = single "path" for billing but purges everything)
aws cloudfront create-invalidation \
  --distribution-id "${CF_DISTRIBUTION_ID}" \
  --paths "/*"

# Check invalidation status
aws cloudfront get-invalidation \
  --distribution-id "${CF_DISTRIBUTION_ID}" \
  --id "${INVALIDATION_ID}"
# Status: InProgress → Completed (typically 30-120 seconds)
```

### CloudFront Cost Note

CloudFront pricing: first 1,000 path invalidations/month free, then $0.005/path. Wildcards count as one path regardless of files matched.

---

## 5. Fastly Purge Procedures

### Surrogate-Key (Tag) Purge

```bash
# Purge by surrogate key (equivalent to CF cache tags)
# Origin sets: Surrogate-Key: product-123 category-shoes deploy-v2.4.1

curl -X POST "https://api.fastly.com/service/${FASTLY_SERVICE_ID}/purge/product-123" \
  -H "Fastly-Key: ${FASTLY_API_TOKEN}"

# Soft purge (marks stale, serves stale while revalidating — gentler on origin)
curl -X POST "https://api.fastly.com/service/${FASTLY_SERVICE_ID}/purge/product-123" \
  -H "Fastly-Key: ${FASTLY_API_TOKEN}" \
  -H "Fastly-Soft-Purge: 1"
```

### Single URL Purge

```bash
curl -X PURGE "https://example.com/api/prices" \
  -H "Fastly-Key: ${FASTLY_API_TOKEN}"
```

### Full Service Purge

```bash
# WARNING: Same caution as Cloudflare full purge
curl -X POST "https://api.fastly.com/service/${FASTLY_SERVICE_ID}/purge_all" \
  -H "Fastly-Key: ${FASTLY_API_TOKEN}"
```

---

## 6. Validation

### Verify Purge Took Effect

```bash
# Check cache status headers immediately after purge
# Cloudflare: CF-Cache-Status: MISS (first hit) → HIT (subsequent)
# AWS: x-cache: Miss from cloudfront → Hit from cloudfront
# Fastly: X-Cache: MISS → HIT

TARGET_URL="https://example.com/api/prices"

# Check 3 times: first should be MISS, subsequent should be HIT
for i in 1 2 3; do
  echo "--- Request $i ---"
  curl -sI "$TARGET_URL" | grep -iE "cf-cache-status|x-cache|age:|last-modified:|cache-control:"
  sleep 1
done

# Verify content is updated (not just cache-busted)
ACTUAL_PRICE=$(curl -s "$TARGET_URL" | jq '.price')
EXPECTED_PRICE="29.99"
if [ "$ACTUAL_PRICE" != "$EXPECTED_PRICE" ]; then
  echo "FAIL: Price is $ACTUAL_PRICE, expected $EXPECTED_PRICE"
else
  echo "OK: Price verified as $EXPECTED_PRICE"
fi
```

### Monitor Origin Traffic Spike

```bash
# Watch origin request rate during and after purge
# Expected: spike for 30-120s, then return to baseline as CDN repopulates
watch -n 5 'curl -s "http://prometheus:9090/api/v1/query?query=rate(nginx_requests_total{upstream=\"origin\"}[1m])"'

# Alert if origin spike exceeds capacity (>80% of origin request rate limit)
```

### Cache-Control Header Audit

```bash
# Verify origin is sending correct headers for cache population
curl -sI "https://example.com/api/prices" | grep -iE "cache-control|surrogate-control|cdn-cache-control"

# Expected for API responses (short TTL, public):
# Cache-Control: public, max-age=60, stale-while-revalidate=300

# Expected for static assets (long TTL, content-addressed):
# Cache-Control: public, max-age=31536000, immutable
```

---

## 7. Cost Awareness and Pre-Warm Procedure

### Estimating Origin Impact

```bash
# Estimate cached object count before full purge
TOTAL_REQUESTS=$(curl -s "http://prometheus:9090/api/v1/query?query=sum(cloudflare_zone_requests_cached_total)" | jq -r '.data.result[0].value[1]')
echo "Approximate cached objects: $TOTAL_REQUESTS"
echo "Expect origin traffic to spike ~$(echo "$TOTAL_REQUESTS * 0.1" | bc) req/s for ~60 seconds"
```

### Pre-Warm Procedure (for large-scale purges)

```bash
# 1. Notify infra team: "Purging CDN cache at HH:MM — expect origin spike"
# 2. Scale up origin instances before purge
kubectl scale deployment origin-api -n production --replicas=20  # scale up from 5

# 3. Execute purge
# (run purge command here)

# 4. Monitor origin health for 5 minutes
watch -n 10 'kubectl top pods -n production -l app=origin-api'

# 5. Scale back down after cache repopulates
sleep 300
kubectl scale deployment origin-api -n production --replicas=5
```

---

## 8. Post-Purge Checklist

- [ ] Stale content confirmed no longer served (curl validation above)
- [ ] New content confirmed correct (spot-check 3+ URLs)
- [ ] Origin error rate returned to baseline
- [ ] Cache hit rate recovering (check CDN analytics)
- [ ] Incident ticket updated with purge time and scope
- [ ] If security-related: confirm audit log entry with timestamp and operator

---

## Related Runbooks

- [RB-055: Deploy Rollback](RB-055-deploy-rollback.md)
- [RB-031: SLO Burn Alert Response](RB-031-slo-burn-alert.md)
