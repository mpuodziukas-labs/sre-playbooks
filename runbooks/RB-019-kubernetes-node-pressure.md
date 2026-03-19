# RB-019: Kubernetes Node Pressure

**Severity**: SEV2 (node NotReady) → SEV1 (multiple nodes, services degraded)
**Category**: Infrastructure / Kubernetes
**Last Reviewed**: 2026-03-18
**Owner**: SRE On-Call

---

## Overview

Node pressure events (memory, disk, PID, CPU throttling) are among the most operationally complex Kubernetes incidents. This runbook covers the full response: detecting which pressure condition is active, safely draining without disrupting workloads, and recovering the node or replacing it.

---

## 1. Detection

### Node NotReady Alert

```yaml
# Prometheus alert
- alert: KubernetesNodeNotReady
  expr: kube_node_status_condition{condition="Ready",status="true"} == 0
  for: 2m
  labels:
    severity: page
  annotations:
    summary: "Node {{ $labels.node }} NotReady"

- alert: KubernetesNodeMemoryPressure
  expr: kube_node_status_condition{condition="MemoryPressure",status="true"} == 1
  for: 1m
  labels:
    severity: warning

- alert: KubernetesNodeDiskPressure
  expr: kube_node_status_condition{condition="DiskPressure",status="true"} == 1
  for: 1m
  labels:
    severity: warning

- alert: KubernetesPodOOMKilled
  expr: kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1
  labels:
    severity: warning
  annotations:
    summary: "OOMKilled: {{ $labels.namespace }}/{{ $labels.pod }}"
```

### Manual Detection

```bash
# Check all node conditions
kubectl get nodes -o wide

# Detailed node conditions for a specific node
kubectl describe node <node-name> | grep -A 20 "Conditions:"

# Check which nodes have pressure conditions
kubectl get nodes -o json | jq -r '
  .items[] |
  .metadata.name as $name |
  .status.conditions[] |
  select(.type != "Ready" and .status == "True") |
  [$name, .type, .message] | @tsv
'

# Check node resource consumption
kubectl top nodes

# Check node events
kubectl get events --all-namespaces --field-selector reason=NodeNotReady
kubectl get events --all-namespaces | grep -iE "oom|evict|pressure|failed"
```

---

## 2. OOMKilled Pods

### Identify Affected Pods

```bash
# List all OOMKilled pods
kubectl get pods --all-namespaces -o json | jq -r '
  .items[] |
  select(.status.containerStatuses != null) |
  .metadata.namespace as $ns |
  .metadata.name as $pod |
  .status.containerStatuses[] |
  select(.lastState.terminated.reason == "OOMKilled") |
  [$ns, $pod, .name, (.lastState.terminated.finishedAt)] | @tsv
' | sort -k4 -r

# Check restart counts (high count = repeated OOMKills)
kubectl get pods --all-namespaces --sort-by='.status.containerStatuses[0].restartCount' | \
  awk '$5 > 3 {print}' | head -20
```

### Diagnose: Requests vs Limits Ratio

```bash
# Check memory requests vs limits for the affected pod
kubectl get pod <pod-name> -n <namespace> -o json | jq '
  .spec.containers[] | {
    name: .name,
    requests: .resources.requests,
    limits: .resources.limits
  }
'

# Common issue: limit set too low, requests unset (defaults to 0)
# Healthy ratio: limit = 1.5-2x request (headroom for GC, spikes)
# Dangerous: limit == request (no headroom) or limit << actual usage
```

### Immediate Fix for Repeated OOMKills

```bash
# Increase memory limit (stop-gap — investigate root cause in parallel)
kubectl patch deployment <deployment-name> -n <namespace> \
  --type=json \
  -p='[{
    "op": "replace",
    "path": "/spec/template/spec/containers/0/resources/limits/memory",
    "value": "1Gi"
  }, {
    "op": "replace",
    "path": "/spec/template/spec/containers/0/resources/requests/memory",
    "value": "512Mi"
  }]'

# Trigger rolling update to apply new limits
kubectl rollout restart deployment/<deployment-name> -n <namespace>
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

---

## 3. Node NotReady: Cordon and Drain

### Step 1: Cordon the Node

```bash
# Cordon: marks node unschedulable, no new pods land here
# Existing pods continue running (drain handles eviction)
kubectl cordon <node-name>

# Verify cordon
kubectl get node <node-name>  # STATUS should show SchedulingDisabled
```

### Step 2: Describe Node to Understand Reason

```bash
kubectl describe node <node-name>

# Key sections to read:
# - Conditions: look for MemoryPressure, DiskPressure, PIDPressure, NetworkUnavailable
# - Events: shows what kubelet has been doing
# - Allocated resources: CPU/memory requests vs allocatable
# - Non-terminated Pods: what is running on this node
```

### Step 3: Drain the Node

```bash
# Drain: evicts all pods with grace period, respects PodDisruptionBudgets
# --ignore-daemonsets: DaemonSet pods are rescheduled by their controller
# --delete-emptydir-data: evicts pods using emptyDir (data is lost — confirm this is ok)

kubectl drain <node-name> \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --timeout=300s

# If drain hangs (pod violating PDB or stuck in Terminating):
kubectl drain <node-name> \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --force \           # Force evicts pods not managed by a controller
  --timeout=300s

# Monitor drain progress
watch kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name>
```

### PodDisruptionBudget Issues

```bash
# If drain is blocked by PDB violation
kubectl get pdb --all-namespaces

# Check which PDB is blocking
kubectl describe pdb <pdb-name> -n <namespace>

# Options:
# 1. Wait for replica count to increase (if autoscaler is active)
# 2. Temporarily scale up the deployment
kubectl scale deployment <deployment-name> -n <namespace> --replicas=4

# 3. Emergency: delete the PDB temporarily (document this action in incident)
kubectl delete pdb <pdb-name> -n <namespace>
# Re-create after drain: kubectl apply -f pdb-backup.yaml
```

---

## 4. Specific Pressure Conditions

### Memory Pressure (MemoryPressure=True)

```bash
# Check what's consuming memory on the node
kubectl describe node <node-name> | grep -A 30 "Non-terminated Pods"

# SSH to node if accessible (check cloud provider for method)
# AWS: aws ssm start-session --target <instance-id>
# GCP: gcloud compute ssh <node-name>

# On the node: identify top memory consumers
sudo ps aux --sort=-%mem | head -20
sudo free -h
sudo cat /proc/meminfo | grep -E "MemTotal|MemFree|MemAvailable|Cached|Buffers"

# Check for large container logs consuming disk memory
sudo journalctl --disk-usage
```

### Disk Pressure (DiskPressure=True)

```bash
# On the node: check disk usage
df -h
du -sh /var/lib/docker/ 2>/dev/null  # Docker image layers
du -sh /var/lib/containerd/ 2>/dev/null  # containerd layers

# Clean up unused container images (safe to run on cordoned node)
sudo crictl rmi --prune  # containerd
# or: sudo docker image prune -f  # Docker

# Clean up old logs
sudo journalctl --vacuum-size=500M

# Check for runaway log files in pod volumes
sudo find /var/lib/kubelet/pods -name "*.log" -size +100M -exec ls -lh {} \;
```

### PID Pressure (PIDPressure=True)

```bash
# On the node: check process count
cat /proc/sys/kernel/pid_max
ps aux | wc -l

# Find process spawning too many children
ps aux | awk '{print $1}' | sort | uniq -c | sort -rn | head -10

# Check for fork bombs or runaway init processes in containers
sudo kubectl exec -n <namespace> <suspected-pod> -- ps aux | wc -l
```

---

## 5. PVC Issues

### Diagnose PVC Binding Problems

```bash
# Check PVC status
kubectl get pvc -n <namespace>
# STATUS: Bound = ok | Pending = problem | Lost = data risk

# Describe a pending PVC
kubectl describe pvc <pvc-name> -n <namespace>
# Look for: "no persistent volumes available for this claim"

# Check available PVs
kubectl get pv | grep -v Bound

# Check storage class provisioner is running
kubectl get pods -n kube-system | grep -i provisioner

# Check if storage class exists
kubectl get storageclass
```

### Fix: Storage Class Provisioner Down

```bash
# Check provisioner logs
kubectl logs -n kube-system -l app=ebs-csi-controller  # AWS EBS
kubectl logs -n kube-system -l app=pd-csi-driver       # GCP PD

# Restart provisioner
kubectl rollout restart deployment ebs-csi-controller -n kube-system

# Verify PVC transitions to Bound after provisioner restart
watch kubectl get pvc -n <namespace>
```

### Lost PV Recovery

```bash
# PV in Released state can be reclaimed (if data is still there)
kubectl get pv <pv-name> -o yaml | grep reclaimPolicy
# If Retain: PV data is preserved even after PVC deletion

# Re-bind: remove claimRef to make PV Available again
kubectl patch pv <pv-name> --type=json \
  -p='[{"op": "remove", "path": "/spec/claimRef"}]'

# Then re-create the PVC that references the specific PV by storageClass + access modes
```

---

## 6. Recovery

### Rollout Restart After Node Recovery

```bash
# After draining and fixing node issue, or replacing node:

# 1. Uncordon the node when it's healthy
kubectl uncordon <node-name>

# 2. Verify node is Ready
kubectl get node <node-name>

# 3. Rollout restart affected deployments to rebalance pods
kubectl rollout restart deployment/<deployment-name> -n <namespace>

# 4. Scale down/up to force rescheduling if needed
CURRENT_REPLICAS=$(kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.replicas}')
kubectl scale deployment <deployment-name> -n <namespace> --replicas=$((CURRENT_REPLICAS - 1))
sleep 10
kubectl scale deployment <deployment-name> -n <namespace> --replicas=$CURRENT_REPLICAS
```

### Verify Service Health After Recovery

```bash
# Check all pods are running and ready
kubectl get pods -n <namespace> -l app=<app-label>

# Check endpoints are populated (services routing to healthy pods)
kubectl get endpoints <service-name> -n <namespace>

# Run a quick synthetic check
curl -f "https://service.example.com/healthz" && echo "OK"
```

---

## 7. Node Replacement (Managed Node Groups)

```bash
# AWS EKS: terminate instance and let ASG replace
aws ec2 terminate-instances --instance-ids <instance-id>

# GCP GKE: delete node from managed instance group
gcloud compute instance-groups managed delete-instances <group-name> \
  --instances=<instance-name> --zone=<zone>

# Azure AKS: scale node pool (removes oldest node)
az aks nodepool scale --resource-group <rg> --cluster-name <cluster> \
  --name <nodepool> --node-count <current-1>
sleep 60
az aks nodepool scale --resource-group <rg> --cluster-name <cluster> \
  --name <nodepool> --node-count <current>
```

---

## Related Runbooks

- [RB-042: Memory Leak](RB-042-memory-leak.md)
- [RB-055: Deploy Rollback](RB-055-deploy-rollback.md)
- [RB-031: SLO Burn Alert](RB-031-slo-burn-alert.md)
