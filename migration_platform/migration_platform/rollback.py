"""
Rollback stage.

Produces a rollback plan for anything that failed execution or failed
validation, plus a generic "how to roll back a healthy-but-unwanted
wave" guide for resources that succeeded. Rollback order is the reverse
of migration order, and for each resource we also report its
"dependents" (per the dependency graph) so the operator knows the blast
radius of tearing it back out -- rolling back a database that three
services now point at is a bigger deal than rolling back an isolated
file share.

Two outputs:
- A structured RollbackPlan (for tooling/automation)
- A human-readable Markdown runbook + a shell script skeleton (for the
  on-call engineer at 2am)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .dependency_analysis import DependencyGraph
from .models import ExecutionRecord, Inventory, MigrationPlan, StepStatus, ValidationResult

STRATEGY_ROLLBACK_ACTION = {
    "rehost": "Terminate the migrated instance/resource; source system was left "
              "running (non-destructive rehost) so traffic can be pointed back "
              "immediately via DNS/config revert.",
    "replatform": "Deprovision the managed target service; restore traffic to the "
                  "original self-managed resource. If data was written only to the "
                  "target post-cutover, replicate it back before decommissioning "
                  "the target.",
    "refactor": "Roll back to the previous release/image of the refactored "
                "workload, or fall back to the original (pre-refactor) resource "
                "if still available.",
    "repurchase": "Cancel/disable the SaaS integration; re-enable the legacy "
                  "system and resync any data captured only in the SaaS product.",
    "retain": "No action -- resource was not migrated.",
    "retire": "No action -- resource was intentionally decommissioned, not migrated.",
}


@dataclass
class RollbackStep:
    resource_id: str
    reason: str
    action: str
    dependents_at_risk: list[str] = field(default_factory=list)


@dataclass
class RollbackPlan:
    steps: list[RollbackStep]

    def to_markdown(self) -> str:
        lines = ["# Rollback Runbook", ""]
        if not self.steps:
            lines.append("No rollback actions required -- nothing failed and nothing "
                          "was flagged for reversal.")
            return "\n".join(lines)
        lines.append(
            f"{len(self.steps)} resource(s) require rollback. Execute in the order "
            f"listed below (reverse of migration order) so dependents are detached "
            f"before their dependencies are torn down."
        )
        lines.append("")
        for i, step in enumerate(self.steps, start=1):
            lines.append(f"## {i}. `{step.resource_id}`")
            lines.append(f"- **Reason:** {step.reason}")
            lines.append(f"- **Action:** {step.action}")
            if step.dependents_at_risk:
                lines.append(
                    f"- **⚠ Dependents affected:** {', '.join(step.dependents_at_risk)} "
                    f"-- confirm these are already reverted or can tolerate this "
                    f"resource disappearing before proceeding."
                )
            lines.append("")
        return "\n".join(lines)

    def to_shell_script(self) -> str:
        lines = [
            "#!/usr/bin/env bash",
            "# Auto-generated rollback skeleton. Review every command before running --",
            "# this is a checklist made executable, not a fire-and-forget script.",
            "set -euo pipefail",
            "",
        ]
        for step in self.steps:
            tf_name = step.resource_id.replace("-", "_")
            lines.append(f"echo 'Rolling back {step.resource_id}: {step.reason}'")
            lines.append(f"# {step.action}")
            lines.append(
                f"# terraform destroy -auto-approve -target=<resource_type>.{tf_name}"
            )
            lines.append("")
        return "\n".join(lines)


def build_rollback_plan(
    inventory: Inventory,
    plan: MigrationPlan,
    execution_records: list[ExecutionRecord],
    validation_results: list[ValidationResult],
    graph: DependencyGraph,
) -> RollbackPlan:
    steps: list[RollbackStep] = []

    failed_exec = {r.resource_id: r for r in execution_records if r.status == StepStatus.FAILED}
    failed_valid = {r.resource_id: r for r in validation_results if not r.passed}

    # Reverse migration order: later waves roll back first.
    ordered_ids = [rid for wave in reversed(plan.waves) for rid in wave]

    for rid in ordered_ids:
        rp = plan.resource_plans[rid]
        if rid in failed_exec:
            reason = f"Execution failed: {failed_exec[rid].detail}"
        elif rid in failed_valid:
            reason = f"Validation failed: {failed_valid[rid].detail}"
        else:
            continue
        action = STRATEGY_ROLLBACK_ACTION.get(rp.strategy.value, "Manual review required.")
        steps.append(
            RollbackStep(
                resource_id=rid,
                reason=reason,
                action=action,
                dependents_at_risk=graph.dependents_of(rid),
            )
        )

    return RollbackPlan(steps=steps)


def write_rollback_artifacts(rollback_plan: RollbackPlan, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "ROLLBACK.md")
    sh_path = os.path.join(out_dir, "rollback.sh")
    with open(md_path, "w") as f:
        f.write(rollback_plan.to_markdown())
    with open(sh_path, "w") as f:
        f.write(rollback_plan.to_shell_script())
    os.chmod(sh_path, 0o755)
    return md_path, sh_path
