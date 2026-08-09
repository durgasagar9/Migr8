"""
Execution stage.

Walks the MigrationPlan wave by wave and, for each resource, invokes a
StepRunner to actually perform the migration action (terraform apply,
data sync, DNS cutover, etc). State is persisted after every step so a
failed run can be resumed instead of restarted from scratch, and a
partial failure stops the wave (nothing downstream of a failed
resource is attempted) rather than plowing ahead.

Two runners are provided:
- DryRunRunner: simulates execution -- the default, safe for demos and
  planning reviews. Always "succeeds" except for resources explicitly
  marked to fail (useful for testing rollback).
- ShellStepRunner: a real runner stub that shells out to `terraform
  apply -target=...` for a given resource. Included as the extension
  point for a real deployment; not exercised without network access.
"""
from __future__ import annotations

import abc
import json
import os
import subprocess
import time

from .models import ExecutionRecord, Inventory, MigrationPlan, StepStatus


class StepRunner(abc.ABC):
    @abc.abstractmethod
    def run(self, resource_id: str, inventory: Inventory, plan: MigrationPlan) -> ExecutionRecord:
        ...


class DryRunRunner(StepRunner):
    """Simulates execution. Pass `fail_on` a set of resource ids to
    exercise failure/rollback handling in demos and tests."""

    def __init__(self, fail_on: set[str] | None = None, simulate_delay: bool = False):
        self.fail_on = fail_on or set()
        self.simulate_delay = simulate_delay

    def run(self, resource_id: str, inventory: Inventory, plan: MigrationPlan) -> ExecutionRecord:
        rp = plan.resource_plans[resource_id]
        if self.simulate_delay:
            time.sleep(0.05)
        if resource_id in self.fail_on:
            return ExecutionRecord(
                resource_id=resource_id,
                wave=rp.wave,
                status=StepStatus.FAILED,
                detail=f"Simulated failure applying strategy '{rp.strategy.value}'.",
            )
        return ExecutionRecord(
            resource_id=resource_id,
            wave=rp.wave,
            status=StepStatus.SUCCEEDED,
            detail=f"Applied '{rp.strategy.value}' -> {rp.target_service} (simulated).",
        )


class ShellStepRunner(StepRunner):
    """Real runner: shells out to Terraform targeting the resource's
    address in the generated wave file. Requires `terraform` on PATH
    and the tf files already generated via iac_generator."""

    def __init__(self, tf_dir: str):
        self.tf_dir = tf_dir

    def run(self, resource_id: str, inventory: Inventory, plan: MigrationPlan) -> ExecutionRecord:
        rp = plan.resource_plans[resource_id]
        r = inventory.by_id(resource_id)
        tf_type = rp.target_service
        tf_name = resource_id.replace("-", "_")
        target = f"{tf_type}.{tf_name}"
        try:
            proc = subprocess.run(
                ["terraform", "apply", "-auto-approve", f"-target={target}"],
                cwd=self.tf_dir,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            ok = proc.returncode == 0
            return ExecutionRecord(
                resource_id=resource_id,
                wave=rp.wave,
                status=StepStatus.SUCCEEDED if ok else StepStatus.FAILED,
                detail=(proc.stdout[-2000:] if ok else proc.stderr[-2000:]),
            )
        except FileNotFoundError:
            return ExecutionRecord(
                resource_id=resource_id,
                wave=rp.wave,
                status=StepStatus.FAILED,
                detail="terraform binary not found on PATH.",
            )


class MigrationExecutor:
    def __init__(self, runner: StepRunner, state_path: str):
        self.runner = runner
        self.state_path = state_path
        self.state: dict[str, ExecutionRecord] = {}
        self._load_state()

    def _load_state(self) -> None:
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                raw = json.load(f)
            self.state = {
                rid: ExecutionRecord(
                    resource_id=rid,
                    wave=v["wave"],
                    status=StepStatus(v["status"]),
                    detail=v.get("detail", ""),
                )
                for rid, v in raw.items()
            }

    def _save_state(self) -> None:
        with open(self.state_path, "w") as f:
            json.dump(
                {
                    rid: {"wave": rec.wave, "status": rec.status.value, "detail": rec.detail}
                    for rid, rec in self.state.items()
                },
                f,
                indent=2,
            )

    def run(self, inventory: Inventory, plan: MigrationPlan) -> list[ExecutionRecord]:
        """Executes wave by wave. A resource already SUCCEEDED in prior
        state is skipped (resumability). If any resource in a wave
        fails, the wave is marked complete but subsequent waves are not
        started, since later waves may depend on the failed resource."""
        results: list[ExecutionRecord] = []
        for wave_idx, wave in enumerate(plan.waves, start=1):
            wave_failed = False
            for rid in wave:
                prior = self.state.get(rid)
                if prior and prior.status == StepStatus.SUCCEEDED:
                    results.append(prior)
                    continue
                self.state[rid] = ExecutionRecord(
                    resource_id=rid, wave=wave_idx, status=StepStatus.IN_PROGRESS
                )
                self._save_state()
                record = self.runner.run(rid, inventory, plan)
                self.state[rid] = record
                self._save_state()
                results.append(record)
                if record.status == StepStatus.FAILED:
                    wave_failed = True
            if wave_failed:
                break
        return results

    def status_summary(self) -> str:
        lines = ["Execution state:"]
        for rid, rec in sorted(self.state.items(), key=lambda kv: (kv[1].wave, kv[0])):
            lines.append(f"  [wave {rec.wave}] {rid}: {rec.status.value}")
        return "\n".join(lines)
