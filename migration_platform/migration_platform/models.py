"""
Core data models shared across every stage of the migration pipeline:
Discovery -> Dependency Analysis -> Planning -> IaC Generation ->
Execution -> Validation -> Rollback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class ResourceType(str, Enum):
    COMPUTE = "compute"          # VM / bare-metal server
    DATABASE = "database"
    OBJECT_STORAGE = "object_storage"
    BLOCK_STORAGE = "block_storage"
    NETWORK = "network"          # VPC / subnet / firewall
    LOAD_BALANCER = "load_balancer"
    FUNCTION = "function"        # serverless workload
    QUEUE = "queue"
    DNS = "dns"


class Strategy(str, Enum):
    """The classic '6 R's' of cloud migration."""
    REHOST = "rehost"                # lift-and-shift
    REPLATFORM = "replatform"        # lift-tinker-and-shift (e.g. VM -> managed DB)
    REPURCHASE = "repurchase"        # move to SaaS
    REFACTOR = "refactor"            # re-architect (e.g. VM -> containers/serverless)
    RETAIN = "retain"                # keep on-prem for now
    RETIRE = "retire"                # decommission, not migrated


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class Resource:
    id: str
    name: str
    type: ResourceType
    # Free-form attributes captured during discovery (os, cpu, ram_gb,
    # engine, size_gb, ip, url, runtime, etc.)
    attributes: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)  # resource ids

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "Resource":
        return Resource(
            id=d["id"],
            name=d["name"],
            type=ResourceType(d["type"]),
            attributes=d.get("attributes", {}),
            tags=d.get("tags", {}),
            depends_on=d.get("depends_on", []),
        )


@dataclass
class Inventory:
    resources: list[Resource] = field(default_factory=list)
    source_environment: str = "on-prem"

    def by_id(self, rid: str) -> Optional[Resource]:
        return next((r for r in self.resources if r.id == rid), None)

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(
                {
                    "source_environment": self.source_environment,
                    "resources": [r.to_dict() for r in self.resources],
                },
                f,
                indent=2,
            )

    @staticmethod
    def load(path: str) -> "Inventory":
        with open(path) as f:
            d = json.load(f)
        return Inventory(
            resources=[Resource.from_dict(r) for r in d["resources"]],
            source_environment=d.get("source_environment", "on-prem"),
        )


@dataclass
class ResourcePlan:
    resource_id: str
    strategy: Strategy
    target_service: str          # e.g. "aws_instance", "aws_db_instance"
    wave: int                    # execution order group
    risk: RiskLevel
    rationale: str
    estimated_downtime_minutes: int


@dataclass
class MigrationPlan:
    target_cloud: str
    waves: list[list[str]]                     # list of resource-id groups, in order
    resource_plans: dict[str, ResourcePlan]     # resource_id -> plan
    notes: list[str] = field(default_factory=list)

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(
                {
                    "target_cloud": self.target_cloud,
                    "waves": self.waves,
                    "resource_plans": {
                        rid: {
                            **asdict(rp),
                            "strategy": rp.strategy.value,
                            "risk": rp.risk.value,
                        }
                        for rid, rp in self.resource_plans.items()
                    },
                    "notes": self.notes,
                },
                f,
                indent=2,
            )

    @staticmethod
    def load(path: str) -> "MigrationPlan":
        with open(path) as f:
            d = json.load(f)
        resource_plans = {
            rid: ResourcePlan(
                resource_id=rp["resource_id"],
                strategy=Strategy(rp["strategy"]),
                target_service=rp["target_service"],
                wave=rp["wave"],
                risk=RiskLevel(rp["risk"]),
                rationale=rp["rationale"],
                estimated_downtime_minutes=rp["estimated_downtime_minutes"],
            )
            for rid, rp in d["resource_plans"].items()
        }
        return MigrationPlan(
            target_cloud=d["target_cloud"],
            waves=d["waves"],
            resource_plans=resource_plans,
            notes=d.get("notes", []),
        )


@dataclass
class ExecutionRecord:
    resource_id: str
    wave: int
    status: StepStatus
    detail: str = ""


@dataclass
class ValidationResult:
    resource_id: str
    passed: bool
    checks: dict[str, bool]
    detail: str = ""
