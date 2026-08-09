"""
Validation stage.

For every resource that reported SUCCEEDED in execution, runs a set of
checks appropriate to its type (existence, connectivity/health,
data-integrity spot-check, dependency reachability) and produces a
ValidationResult. This is what decides whether a wave is genuinely safe
to build on, or whether rollback should be triggered.

Checks here are simulated (no live cloud calls, no network access in
this environment) but the shape -- a dict of named boolean checks plus
free-text detail -- is what a real health-check integration (CloudWatch,
a synthetic transaction, a `SELECT 1` query, an HTTP probe) would
populate.
"""
from __future__ import annotations

from .dependency_analysis import DependencyGraph
from .models import (
    ExecutionRecord,
    Inventory,
    MigrationPlan,
    ResourceType,
    StepStatus,
    ValidationResult,
)


def _checks_for(r_type: ResourceType) -> list[str]:
    common = ["resource_exists", "tags_present"]
    extra = {
        ResourceType.COMPUTE: ["boots_successfully", "app_health_endpoint_200"],
        ResourceType.DATABASE: ["accepts_connections", "row_count_matches_source"],
        ResourceType.OBJECT_STORAGE: ["bucket_reachable", "object_count_matches_source"],
        ResourceType.LOAD_BALANCER: ["targets_healthy"],
        ResourceType.NETWORK: ["routes_reachable"],
        ResourceType.FUNCTION: ["invokes_successfully"],
        ResourceType.DNS: ["resolves_correctly"],
    }.get(r_type, [])
    return common + extra


def validate(
    inventory: Inventory,
    plan: MigrationPlan,
    execution_records: list[ExecutionRecord],
    graph: DependencyGraph,
    fail_checks_on: dict[str, list[str]] | None = None,
) -> list[ValidationResult]:
    """
    fail_checks_on: optional {resource_id: [check_name, ...]} to force
    specific checks to fail -- used to demo/test the rollback path.
    """
    fail_checks_on = fail_checks_on or {}
    succeeded_ids = {rec.resource_id for rec in execution_records if rec.status == StepStatus.SUCCEEDED}

    # Validate in dependency order (wave order) so that by the time we check
    # "are my dependencies healthy?" for a resource, its dependencies have
    # already been validated -- iterating succeeded_ids directly (a set) would
    # give an arbitrary order and produce false negatives.
    ordered_ids = [rid for wave in graph.compute_waves() for rid in wave if rid in succeeded_ids]

    results: list[ValidationResult] = []

    for rid in ordered_ids:
        r = inventory.by_id(rid)
        checks = _checks_for(r.type)
        forced_failures = set(fail_checks_on.get(rid, []))
        check_results = {c: (c not in forced_failures) for c in checks}

        # A dependency that failed validation should flag its dependents too --
        # no point declaring a web server "healthy" if its database didn't validate.
        dep_ok = all(
            any(res.resource_id == dep and res.passed for res in results) or dep not in succeeded_ids
            for dep in graph.edges.get(rid, set())
        )
        if not dep_ok:
            check_results["dependencies_healthy"] = False
        else:
            check_results["dependencies_healthy"] = True

        passed = all(check_results.values())
        detail = "All checks passed." if passed else (
            f"Failed checks: {[k for k, v in check_results.items() if not v]}"
        )
        results.append(
            ValidationResult(resource_id=rid, passed=passed, checks=check_results, detail=detail)
        )

    return results


def summarize(results: list[ValidationResult]) -> str:
    passed = sum(1 for r in results if r.passed)
    lines = [f"Validation: {passed}/{len(results)} resources passed."]
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"  [{status}] {r.resource_id} -- {r.detail}")
    return "\n".join(lines)
