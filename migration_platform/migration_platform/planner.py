"""
Planning stage.

Consumes the Inventory + DependencyGraph and produces a MigrationPlan:
for every resource, which of the 6 R's to apply, what target-cloud
service it maps to, an estimated downtime, and a risk rating. Waves from
the dependency graph become the execution sequence, so nothing is
scheduled to migrate before the things it depends on.

The rule set below is intentionally simple and declarative so it's easy
to see -- and override -- the reasoning. Swap in a more sophisticated
scorer (cost-based, ML-based, whatever) without touching the rest of
the pipeline; only this module needs to change.
"""
from __future__ import annotations

from .dependency_analysis import DependencyGraph
from .models import (
    Inventory,
    MigrationPlan,
    Resource,
    ResourcePlan,
    ResourceType,
    RiskLevel,
    Strategy,
)

AWS_TARGET_SERVICE = {
    ResourceType.COMPUTE: "aws_instance",
    ResourceType.DATABASE: "aws_db_instance",
    ResourceType.OBJECT_STORAGE: "aws_s3_bucket",
    ResourceType.BLOCK_STORAGE: "aws_ebs_volume",
    ResourceType.NETWORK: "aws_vpc",
    ResourceType.LOAD_BALANCER: "aws_lb",
    ResourceType.FUNCTION: "aws_lambda_function",
    ResourceType.QUEUE: "aws_sqs_queue",
    ResourceType.DNS: "aws_route53_zone",
}


def _decide_strategy(r: Resource) -> tuple[Strategy, RiskLevel, str]:
    attrs = r.attributes

    if r.type == ResourceType.DATABASE:
        if attrs.get("eol") or attrs.get("engine") in ("oracle", "db2"):
            return (
                Strategy.REPLATFORM,
                RiskLevel.HIGH,
                "EOL/legacy engine: move to a managed equivalent (e.g. RDS) "
                "rather than a straight lift-and-shift; schema/engine "
                "compatibility needs validation.",
            )
        return (
            Strategy.REPLATFORM,
            RiskLevel.MEDIUM,
            "Standard managed-database replatform (self-managed -> RDS-class "
            "service) to pick up automated backups/HA.",
        )

    if r.type == ResourceType.COMPUTE:
        if attrs.get("eol") or "2008" in str(attrs.get("os", "")):
            return (
                Strategy.REFACTOR,
                RiskLevel.HIGH,
                "EOL OS: rehosting would carry the unsupported OS into the "
                "cloud. Recommend refactoring onto a supported "
                "runtime/container image before or during migration.",
            )
        if attrs.get("stateless"):
            return (
                Strategy.REPLATFORM,
                RiskLevel.LOW,
                "Stateless web/app tier: good candidate for replatforming "
                "onto an auto-scaled instance group behind a load balancer.",
            )
        return (
            Strategy.REHOST,
            RiskLevel.MEDIUM,
            "Stateful, non-EOL server: lift-and-shift first, optimize later.",
        )

    if r.type == ResourceType.OBJECT_STORAGE:
        return (
            Strategy.REPLATFORM,
            RiskLevel.LOW,
            "File share -> object storage; low risk, plan for a protocol "
            "change (NFS/SMB clients need to switch to S3 API or a gateway).",
        )

    if r.type == ResourceType.FUNCTION:
        return (
            Strategy.REHOST,
            RiskLevel.LOW,
            "Scheduled job maps directly onto a managed serverless function.",
        )

    if r.type == ResourceType.LOAD_BALANCER:
        return (
            Strategy.REPLATFORM,
            RiskLevel.LOW,
            "Replace with a managed load balancer service.",
        )

    if r.type == ResourceType.NETWORK:
        return (
            Strategy.REHOST,
            RiskLevel.MEDIUM,
            "Recreate network topology (VPC/subnets/routing) as the "
            "foundation other resources land on.",
        )

    if r.type == ResourceType.DNS:
        return (
            Strategy.REHOST,
            RiskLevel.LOW,
            "Recreate DNS zone in managed DNS; cutover is the last step "
            "and the easiest to roll back.",
        )

    return (Strategy.REHOST, RiskLevel.MEDIUM, "Default lift-and-shift.")


def _estimate_downtime(r: Resource, strategy: Strategy) -> int:
    base = {
        Strategy.REHOST: 30,
        Strategy.REPLATFORM: 60,
        Strategy.REFACTOR: 180,
        Strategy.REPURCHASE: 120,
        Strategy.RETAIN: 0,
        Strategy.RETIRE: 0,
    }[strategy]
    if r.type == ResourceType.DATABASE:
        size_gb = r.attributes.get("size_gb", 0)
        base += int(size_gb / 50)  # rough: bigger data = longer cutover
    return base


def build_plan(
    inventory: Inventory,
    graph: DependencyGraph,
    target_cloud: str = "aws",
) -> MigrationPlan:
    waves = graph.compute_waves()
    target_map = AWS_TARGET_SERVICE if target_cloud == "aws" else AWS_TARGET_SERVICE

    resource_plans: dict[str, ResourcePlan] = {}
    for wave_idx, wave in enumerate(waves, start=1):
        for rid in wave:
            r = inventory.by_id(rid)
            strategy, risk, rationale = _decide_strategy(r)
            resource_plans[rid] = ResourcePlan(
                resource_id=rid,
                strategy=strategy,
                target_service=target_map.get(r.type, "unknown"),
                wave=wave_idx,
                risk=risk,
                rationale=rationale,
                estimated_downtime_minutes=_estimate_downtime(r, strategy),
            )

    notes = []
    high_risk = [rp.resource_id for rp in resource_plans.values() if rp.risk == RiskLevel.HIGH]
    if high_risk:
        notes.append(
            f"{len(high_risk)} high-risk resource(s) flagged for extra review "
            f"before cutover: {', '.join(high_risk)}."
        )
    notes.append(
        f"Plan has {len(waves)} wave(s); resources within a wave have no "
        f"dependency ordering between them and can migrate in parallel."
    )

    return MigrationPlan(
        target_cloud=target_cloud,
        waves=waves,
        resource_plans=resource_plans,
        notes=notes,
    )
