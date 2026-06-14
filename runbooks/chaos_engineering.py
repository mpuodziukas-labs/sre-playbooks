#!/usr/bin/env python3
"""
Chaos Engineering Framework

Implements a structured chaos experiment workflow:
- Blast radius calculator (services × failure modes × traffic weight)
- Hypothesis + evidence tracking with pass/fail evaluation
- GameDay scenario templates (network partition, pod kill, disk fill, CPU spike)
- Recovery verification with SLO impact assessment

Philosophy (from Principles of Chaos Engineering, netflix.com/chaos):
  1. Build a hypothesis around steady state behavior
  2. Vary real-world events (failures, traffic surges)
  3. Run experiments in production (with blast radius control)
  4. Automate experiments to run continuously
  5. Minimize blast radius

Usage:
    python3 chaos_engineering.py --demo
    python3 chaos_engineering.py --scenario network-partition --services "api,db"
    python3 chaos_engineering.py --blast-radius --services "payment-api" --traffic 40
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Final


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class FailureMode(str, Enum):
    NETWORK_PARTITION = "network_partition"
    POD_KILL = "pod_kill"
    DISK_FILL = "disk_fill"
    CPU_SPIKE = "cpu_spike"
    MEMORY_PRESSURE = "memory_pressure"
    LATENCY_INJECTION = "latency_injection"
    PACKET_LOSS = "packet_loss"
    DNS_FAILURE = "dns_failure"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    CLOCK_SKEW = "clock_skew"


class ExperimentStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"


# ---------------------------------------------------------------------------
# Blast radius model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceDependency:
    name: str
    traffic_weight: float  # 0.0–1.0 fraction of total traffic this service handles
    criticality: str  # critical | high | medium | low
    has_circuit_breaker: bool
    has_fallback: bool
    slo_target: float  # e.g. 0.999


CRITICALITY_MULTIPLIER: Final[dict[str, float]] = {
    "critical": 1.0,
    "high": 0.75,
    "medium": 0.5,
    "low": 0.25,
}


@dataclass(frozen=True)
class BlastRadiusResult:
    services: list[ServiceDependency]
    failure_mode: FailureMode
    affected_traffic_fraction: float  # 0.0–1.0
    blast_radius_score: float  # 0.0–10.0
    risk_level: str  # low | medium | high | critical
    mitigations_active: int
    mitigations_total: int
    recommendation: str

    @property
    def affected_traffic_percent(self) -> float:
        return self.affected_traffic_fraction * 100.0

    @property
    def proceed_recommended(self) -> bool:
        return self.blast_radius_score <= 6.0


def calculate_blast_radius(
    services: list[ServiceDependency],
    failure_mode: FailureMode,
) -> BlastRadiusResult:
    """
    Calculate blast radius for a chaos experiment.

    Score (0–10):
      - Base: sum of (traffic_weight × criticality_multiplier) for affected services
      - Reduced by circuit breakers (−0.5 per service) and fallbacks (−1.0 per service)
      - Failure mode modifier: network partitions are more severe than pod kills
    """
    failure_mode_multiplier: Final[dict[FailureMode, float]] = {
        FailureMode.NETWORK_PARTITION: 1.4,
        FailureMode.DISK_FILL: 1.2,
        FailureMode.CPU_SPIKE: 1.0,
        FailureMode.POD_KILL: 0.8,
        FailureMode.MEMORY_PRESSURE: 0.9,
        FailureMode.LATENCY_INJECTION: 0.7,
        FailureMode.PACKET_LOSS: 1.1,
        FailureMode.DNS_FAILURE: 1.3,
        FailureMode.DEPENDENCY_TIMEOUT: 0.8,
        FailureMode.CLOCK_SKEW: 0.6,
    }

    base_score = 0.0
    affected_traffic = 0.0
    mitigations_active = 0
    mitigations_total = len(services) * 2  # circuit breaker + fallback per service

    for svc in services:
        crit_mult = CRITICALITY_MULTIPLIER.get(svc.criticality, 0.5)
        svc_score = svc.traffic_weight * crit_mult * 10.0

        if svc.has_circuit_breaker:
            svc_score -= 0.5 * 10.0 * svc.traffic_weight
            mitigations_active += 1
        if svc.has_fallback:
            svc_score -= 1.0 * 10.0 * svc.traffic_weight
            mitigations_active += 1

        base_score += max(0.0, svc_score)
        affected_traffic += svc.traffic_weight

    blast_score = min(10.0, base_score * failure_mode_multiplier[failure_mode])

    if blast_score >= 8.0:
        risk_level = "critical"
        recommendation = "DO NOT PROCEED — blast radius too high. Add circuit breakers/fallbacks first."
    elif blast_score >= 6.0:
        risk_level = "high"
        recommendation = "Proceed only with full war room staffed and rollback tested."
    elif blast_score >= 4.0:
        risk_level = "medium"
        recommendation = (
            "Proceed with on-call standing by and 5-minute abort criteria defined."
        )
    elif blast_score >= 2.0:
        risk_level = "low"
        recommendation = (
            "Safe to proceed in staging. Run in production during low-traffic window."
        )
    else:
        risk_level = "low"
        recommendation = "Safe to proceed. Mitigations in place."

    return BlastRadiusResult(
        services=services,
        failure_mode=failure_mode,
        affected_traffic_fraction=min(1.0, affected_traffic),
        blast_radius_score=round(blast_score, 2),
        risk_level=risk_level,
        mitigations_active=mitigations_active,
        mitigations_total=mitigations_total,
        recommendation=recommendation,
    )


# ---------------------------------------------------------------------------
# Hypothesis + evidence tracking
# ---------------------------------------------------------------------------


@dataclass
class Hypothesis:
    statement: str
    steady_state_metric: str  # e.g. "p99 latency < 200ms"
    steady_state_threshold: float
    metric_unit: str  # ms, %, count, etc.
    higher_is_better: bool = (
        False  # True for metrics like availability%; False for latency/error rate
    )


@dataclass
class EvidencePoint:
    timestamp: datetime
    metric_name: str
    observed_value: float
    expected_threshold: float
    passed: bool
    notes: str = ""


@dataclass
class ChaosExperiment:
    experiment_id: str
    title: str
    hypothesis: Hypothesis
    failure_mode: FailureMode
    services: list[ServiceDependency]
    status: ExperimentStatus = ExperimentStatus.DRAFT
    started_at: datetime | None = None
    completed_at: datetime | None = None
    evidence: list[EvidencePoint] = field(default_factory=list)
    abort_criteria: list[str] = field(default_factory=list)
    rollback_procedure: str = ""
    game_day_notes: str = ""

    def add_evidence(
        self,
        metric_name: str,
        observed_value: float,
        notes: str = "",
    ) -> EvidencePoint:
        """Record a measurement and evaluate against the hypothesis threshold."""
        if self.hypothesis.higher_is_better:
            passed = observed_value >= self.hypothesis.steady_state_threshold
        else:
            passed = observed_value <= self.hypothesis.steady_state_threshold
        point = EvidencePoint(
            timestamp=datetime.now(timezone.utc),
            metric_name=metric_name,
            observed_value=observed_value,
            expected_threshold=self.hypothesis.steady_state_threshold,
            passed=passed,
            notes=notes,
        )
        self.evidence.append(point)
        return point

    @property
    def hypothesis_confirmed(self) -> bool | None:
        """True if all evidence points pass; False if any fail; None if no evidence."""
        if not self.evidence:
            return None
        return all(e.passed for e in self.evidence)

    @property
    def should_abort(self) -> bool:
        """True if any evidence point fails (abort criteria met)."""
        return any(not e.passed for e in self.evidence)

    def blast_radius(self) -> BlastRadiusResult:
        return calculate_blast_radius(self.services, self.failure_mode)


# ---------------------------------------------------------------------------
# GameDay scenario templates
# ---------------------------------------------------------------------------


def gameday_network_partition(services: list[ServiceDependency]) -> ChaosExperiment:
    """
    GameDay: Network partition between service and its primary DB.

    Tests: circuit breaker activation, read-only fallback, client retry behavior.
    """
    return ChaosExperiment(
        experiment_id="CHAOS-NP-001",
        title="Network Partition: Service ↔ Primary Database",
        hypothesis=Hypothesis(
            statement=(
                "When a network partition occurs between the API service and primary DB, "
                "the service activates circuit breaker within 5 seconds, serves cached reads, "
                "and rejects writes gracefully (503 with Retry-After header). "
                "p99 latency stays below 500ms for read traffic."
            ),
            steady_state_metric="api_p99_latency_ms",
            steady_state_threshold=500.0,
            metric_unit="ms",
        ),
        failure_mode=FailureMode.NETWORK_PARTITION,
        services=services,
        abort_criteria=[
            "Error rate exceeds 10% for > 30 seconds",
            "Any payment transaction fails silently (no 503/retry)",
            "Circuit breaker does not activate within 10 seconds",
            "Data corruption detected in any service",
        ],
        rollback_procedure=(
            "1. Remove iptables DROP rule: `iptables -D INPUT -s <db-ip> -j DROP`\n"
            "2. Verify DB connectivity: `psql -h <db-host> -c 'SELECT 1'`\n"
            "3. Reset circuit breaker: POST /admin/circuit-breaker/reset\n"
            "4. Verify error rate returns to baseline within 60 seconds\n"
            "5. Check for any incomplete transactions in DB transaction log"
        ),
        game_day_notes=(
            "Setup:\n"
            "  # Inject partition (run on service host)\n"
            "  sudo iptables -I INPUT -s $DB_PRIMARY_IP -j DROP\n"
            "  sudo iptables -I OUTPUT -d $DB_PRIMARY_IP -j DROP\n\n"
            "Observe:\n"
            "  watch -n1 'curl -s http://localhost:9090/metrics | grep circuit_breaker'\n"
            "  kubectl logs -f deployment/api --tail=50\n\n"
            "Cleanup:\n"
            "  sudo iptables -D INPUT -s $DB_PRIMARY_IP -j DROP\n"
            "  sudo iptables -D OUTPUT -d $DB_PRIMARY_IP -j DROP\n"
        ),
    )


def gameday_pod_kill(services: list[ServiceDependency]) -> ChaosExperiment:
    """
    GameDay: Random pod kill in production deployment.

    Tests: Kubernetes pod restart, traffic rerouting, zero-downtime recovery.
    """
    return ChaosExperiment(
        experiment_id="CHAOS-PK-001",
        title="Pod Kill: Random Instance Termination",
        hypothesis=Hypothesis(
            statement=(
                "When a random pod is killed, Kubernetes reschedules it within 30 seconds, "
                "existing traffic is rerouted to healthy pods, and no requests are lost "
                "beyond the TCP connection teardown window. Availability stays >= 99.9%."
            ),
            steady_state_metric="availability_percent",
            steady_state_threshold=99.9,
            metric_unit="%",
            higher_is_better=True,  # availability must stay >= threshold
        ),
        failure_mode=FailureMode.POD_KILL,
        services=services,
        abort_criteria=[
            "Pod fails to reschedule within 2 minutes (CrashLoopBackOff)",
            "More than 1% of requests return 5xx for > 60 seconds",
            "OOMKill detected (memory leak trigger)",
            "PersistentVolumeClaim fails to reattach",
        ],
        rollback_procedure=(
            "1. If CrashLoopBackOff: `kubectl describe pod <pod> -n <ns>` → check events\n"
            "2. If image pull error: `kubectl rollout undo deployment/<name>`\n"
            "3. If resource pressure: `kubectl top nodes` → cordon overloaded node\n"
            "4. Scale up manually: `kubectl scale deployment/<name> --replicas=<n+2>`\n"
            "5. Verify HPA not fighting you: `kubectl get hpa`"
        ),
        game_day_notes=(
            "Setup (using chaos-mesh or manual):\n"
            "  # Manual pod kill\n"
            "  POD=$(kubectl get pods -l app=$SERVICE -o name | shuf -n1)\n"
            '  echo "Killing $POD" && kubectl delete $POD --grace-period=0 --force\n\n'
            "  # Using chaos-mesh\n"
            "  kubectl apply -f chaos-mesh/pod-kill-experiment.yaml\n\n"
            "Observe:\n"
            "  kubectl get pods -l app=$SERVICE -w\n"
            "  kubectl rollout status deployment/$SERVICE\n"
            "  watch -n1 'kubectl top pods -l app=$SERVICE'\n"
        ),
    )


def gameday_disk_fill(services: list[ServiceDependency]) -> ChaosExperiment:
    """
    GameDay: Disk fill to 95% capacity.

    Tests: disk pressure handling, log rotation, graceful write rejection.
    """
    return ChaosExperiment(
        experiment_id="CHAOS-DF-001",
        title="Disk Fill: Filesystem at 95% Capacity",
        hypothesis=Hypothesis(
            statement=(
                "When disk usage reaches 95%, the service continues serving reads, "
                "rejects new writes with a clear error (507 Insufficient Storage), "
                "alerts fire within 5 minutes, and on-call can free space using runbook RB-019."
            ),
            steady_state_metric="disk_usage_percent",
            steady_state_threshold=95.0,
            metric_unit="%",
        ),
        failure_mode=FailureMode.DISK_FILL,
        services=services,
        abort_criteria=[
            "Database crashes (not graceful rejection)",
            "Log writes fail causing service crash",
            "Disk fills to 100% (complete filesystem lock)",
            "Alert does not fire within 10 minutes at 90% usage",
        ],
        rollback_procedure=(
            "1. Remove the filler file: `rm /tmp/chaos-disk-fill-*.bin`\n"
            "2. Free log space: `journalctl --vacuum-size=500M`\n"
            "3. Check DB status: `systemctl status postgresql`\n"
            "4. If DB crashed: `systemctl start postgresql && pg_dump --schema-only`\n"
            "5. Verify disk: `df -h /var/lib/postgresql/`"
        ),
        game_day_notes=(
            "Setup:\n"
            "  # Fill disk to ~95% (calculate available first)\n"
            "  AVAILABLE=$(df / | awk 'NR==2 {print $4}')\n"
            "  FILL_KB=$((AVAILABLE * 90 / 100))\n"
            "  dd if=/dev/zero of=/tmp/chaos-disk-fill-$(date +%s).bin bs=1K count=$FILL_KB\n\n"
            "  # Alternative using fallocate (faster)\n"
            "  fallocate -l ${FILL_KB}K /tmp/chaos-disk-fill.bin\n\n"
            "Observe:\n"
            "  watch -n5 'df -h /'\n"
            "  tail -f /var/log/postgresql/postgresql-*.log\n"
            "  curl -s http://localhost:8080/health | jq .disk\n"
        ),
    )


def gameday_cpu_spike(services: list[ServiceDependency]) -> ChaosExperiment:
    """
    GameDay: CPU spike to 90%+ utilization.

    Tests: HPA scaling, request queuing behavior, timeout propagation.
    """
    return ChaosExperiment(
        experiment_id="CHAOS-CPU-001",
        title="CPU Spike: 90% Utilization for 10 Minutes",
        hypothesis=Hypothesis(
            statement=(
                "When CPU utilization reaches 90%, HPA triggers within 3 minutes, "
                "scales pods by 2×, and p99 latency stays below 2× the normal baseline. "
                "No requests time out at the load balancer level."
            ),
            steady_state_metric="p99_latency_multiple_of_baseline",
            steady_state_threshold=2.0,
            metric_unit="×",
        ),
        failure_mode=FailureMode.CPU_SPIKE,
        services=services,
        abort_criteria=[
            "HPA does not scale within 5 minutes",
            "p99 latency exceeds 10× baseline",
            "Any 504 Gateway Timeout at load balancer",
            "Cascading failure to downstream services",
        ],
        rollback_procedure=(
            "1. Kill the CPU stress process: `pkill -f stress-ng || kill <PID>`\n"
            "2. Scale down HPA if over-scaled: `kubectl patch hpa <name> --patch '{...}'`\n"
            "3. Drain request queue: check /metrics endpoint for queue depth\n"
            "4. Verify latency returns to baseline within 5 minutes\n"
            "5. Review HPA events: `kubectl describe hpa <name>`"
        ),
        game_day_notes=(
            "Setup (using stress-ng):\n"
            "  # Run in target pod\n"
            "  kubectl exec -it deployment/$SERVICE -- bash -c \\\n"
            "    'stress-ng --cpu $(nproc) --cpu-load 90 --timeout 600s &'\n\n"
            "  # Or inject via chaos-mesh CPU stressor\n"
            "  kubectl apply -f chaos-mesh/cpu-stress.yaml\n\n"
            "Observe:\n"
            "  kubectl top pods -l app=$SERVICE\n"
            "  kubectl get hpa $SERVICE -w\n"
            "  watch -n5 'kubectl get pods -l app=$SERVICE | wc -l'\n"
        ),
    )


GAMEDAY_TEMPLATES: Final[dict[str, object]] = {
    "network-partition": gameday_network_partition,
    "pod-kill": gameday_pod_kill,
    "disk-fill": gameday_disk_fill,
    "cpu-spike": gameday_cpu_spike,
}


# ---------------------------------------------------------------------------
# Recovery verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryVerificationResult:
    experiment_id: str
    hypothesis_confirmed: bool | None
    all_evidence_passed: bool
    slo_breach_occurred: bool
    recovery_time_seconds: float | None
    evidence_summary: list[str]
    verdict: str


def verify_recovery(
    experiment: ChaosExperiment,
    recovery_time_seconds: float | None = None,
) -> RecoveryVerificationResult:
    """Evaluate whether the system recovered as hypothesized."""
    all_passed = (
        all(e.passed for e in experiment.evidence) if experiment.evidence else True
    )
    any_slo_breach = any(
        not e.passed
        for e in experiment.evidence
        if "slo" in e.metric_name.lower() or "availability" in e.metric_name.lower()
    )

    evidence_summary = [
        f"{'PASS' if e.passed else 'FAIL'} [{e.metric_name}] "
        f"observed={e.observed_value:.2f} threshold={e.expected_threshold:.2f}"
        for e in experiment.evidence
    ]

    if experiment.hypothesis_confirmed is True:
        verdict = (
            "HYPOTHESIS CONFIRMED — System behaved resiliently under failure. "
            "Chaos experiment successful."
        )
    elif experiment.hypothesis_confirmed is False:
        verdict = (
            "HYPOTHESIS REFUTED — System did NOT behave as expected. "
            "File reliability backlog item before re-running in production."
        )
    else:
        verdict = "INSUFFICIENT EVIDENCE — No measurements recorded."

    return RecoveryVerificationResult(
        experiment_id=experiment.experiment_id,
        hypothesis_confirmed=experiment.hypothesis_confirmed,
        all_evidence_passed=all_passed,
        slo_breach_occurred=any_slo_breach,
        recovery_time_seconds=recovery_time_seconds,
        evidence_summary=evidence_summary,
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

DEMO_SERVICES: Final[list[ServiceDependency]] = [
    ServiceDependency(
        name="payment-api",
        traffic_weight=0.4,
        criticality="critical",
        has_circuit_breaker=True,
        has_fallback=False,
        slo_target=0.9999,
    ),
    ServiceDependency(
        name="user-service",
        traffic_weight=0.3,
        criticality="high",
        has_circuit_breaker=True,
        has_fallback=True,
        slo_target=0.999,
    ),
    ServiceDependency(
        name="analytics",
        traffic_weight=0.1,
        criticality="low",
        has_circuit_breaker=False,
        has_fallback=False,
        slo_target=0.99,
    ),
]


def run_demo() -> None:
    """Print a full chaos engineering demo."""
    print("=" * 70)
    print("CHAOS ENGINEERING FRAMEWORK DEMO")
    print("=" * 70)

    # Blast radius
    br = calculate_blast_radius(DEMO_SERVICES, FailureMode.NETWORK_PARTITION)
    print(
        f"Blast Radius Score : {br.blast_radius_score}/10.0 ({br.risk_level.upper()})"
    )
    print(f"Affected Traffic   : {br.affected_traffic_percent:.0f}%")
    print(f"Mitigations Active : {br.mitigations_active}/{br.mitigations_total}")
    print(f"Recommendation     : {br.recommendation}")
    print(f"Proceed?           : {'YES' if br.proceed_recommended else 'NO'}")
    print()

    # GameDay scenario
    exp = gameday_network_partition(DEMO_SERVICES)
    print(f"GameDay Scenario   : {exp.title}")
    print(f"Hypothesis         : {exp.hypothesis.statement[:80]}...")
    print(f"Abort Criteria     : {len(exp.abort_criteria)} defined")
    print()

    # Simulate evidence collection
    exp.add_evidence(
        "api_p99_latency_ms", 380.0, notes="Circuit breaker activated at T+4s"
    )
    exp.add_evidence("api_p99_latency_ms", 420.0, notes="Serving cached reads")
    exp.add_evidence("api_p99_latency_ms", 210.0, notes="After partition healed")

    result = verify_recovery(exp, recovery_time_seconds=47.3)
    print("RECOVERY VERIFICATION:")
    for line in result.evidence_summary:
        print(f"  {line}")
    print(f"Verdict: {result.verdict}")
    print()

    # All 4 templates
    print("AVAILABLE GAMEDAY TEMPLATES:")
    for name in GAMEDAY_TEMPLATES:
        print(f"  - {name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Chaos Engineering Framework")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--scenario",
        choices=list(GAMEDAY_TEMPLATES.keys()),
        help="Print a GameDay scenario",
    )
    parser.add_argument("--blast-radius", action="store_true")
    parser.add_argument(
        "--failure-mode",
        default="network_partition",
        choices=[m.value for m in FailureMode],
    )
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return 0

    if args.scenario:
        template_fn = GAMEDAY_TEMPLATES[args.scenario]  # type: ignore[index]
        exp = template_fn(DEMO_SERVICES)  # type: ignore[operator]
        print(f"=== {exp.title} ===")
        print(f"\nHypothesis:\n  {exp.hypothesis.statement}")
        print("\nAbort Criteria:")
        for c in exp.abort_criteria:
            print(f"  - {c}")
        print(f"\nRollback:\n{exp.rollback_procedure}")
        print(f"\nSetup:\n{exp.game_day_notes}")
        return 0

    if args.blast_radius:
        fm = FailureMode(args.failure_mode)
        br = calculate_blast_radius(DEMO_SERVICES, fm)
        print(f"Blast radius score : {br.blast_radius_score}/10.0")
        print(f"Risk level         : {br.risk_level}")
        print(f"Recommendation     : {br.recommendation}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
