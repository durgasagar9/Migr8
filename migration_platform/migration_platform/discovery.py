"""
Discovery stage.

A Discoverer's job is to answer one question: "what exists in the source
environment, and what does each thing look like?" It returns an Inventory
of Resources with raw attributes attached. Nothing here makes migration
decisions -- that happens in the planner.

Two discoverers are provided:

- FileDiscoverer: reads a JSON inventory export (agentless mode). Useful
  when you already have a CMDB export, or for the on-prem side of a
  migration where no live API exists.
- AWSDiscoverer: a real, pluggable discoverer stub that would call boto3
  to enumerate EC2/RDS/S3/etc. Included to show the extension point; it
  raises a clear error if boto3 isn't installed rather than failing silently.

Add new discoverers (Azure, GCP, VMware vCenter, ServiceNow CMDB, ...) by
subclassing Discoverer and implementing discover().
"""
from __future__ import annotations

import abc

from .models import Inventory, Resource, ResourceType


class Discoverer(abc.ABC):
    @abc.abstractmethod
    def discover(self) -> Inventory:
        ...


class FileDiscoverer(Discoverer):
    """Loads a pre-exported inventory (JSON) -- e.g. from a CMDB, a
    vCenter export, or manual documentation of an on-prem estate."""

    def __init__(self, path: str):
        self.path = path

    def discover(self) -> Inventory:
        inv = Inventory.load(self.path)
        return inv


class AWSDiscoverer(Discoverer):
    """
    Live discovery against an existing AWS account -- useful for
    cloud-to-cloud or account-to-account migrations. Requires boto3 and
    credentials configured in the environment (env vars, profile, or IAM
    role). Not exercised in the demo pipeline (no network access here),
    but wired up so it's a drop-in replacement for FileDiscoverer.
    """

    def __init__(self, region: str = "us-east-1", profile: str | None = None):
        self.region = region
        self.profile = profile

    def discover(self) -> Inventory:
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(
                "AWSDiscoverer requires boto3 (`pip install boto3`) and "
                "valid AWS credentials."
            ) from e

        session = boto3.Session(profile_name=self.profile, region_name=self.region)
        resources: list[Resource] = []

        ec2 = session.client("ec2")
        for reservation in ec2.describe_instances().get("Reservations", []):
            for inst in reservation.get("Instances", []):
                resources.append(
                    Resource(
                        id=inst["InstanceId"],
                        name=next(
                            (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                            inst["InstanceId"],
                        ),
                        type=ResourceType.COMPUTE,
                        attributes={
                            "instance_type": inst.get("InstanceType"),
                            "state": inst.get("State", {}).get("Name"),
                            "az": inst.get("Placement", {}).get("AvailabilityZone"),
                        },
                        tags={t["Key"]: t["Value"] for t in inst.get("Tags", [])},
                    )
                )

        rds = session.client("rds")
        for db in rds.describe_db_instances().get("DBInstances", []):
            resources.append(
                Resource(
                    id=db["DBInstanceIdentifier"],
                    name=db["DBInstanceIdentifier"],
                    type=ResourceType.DATABASE,
                    attributes={
                        "engine": db.get("Engine"),
                        "size_gb": db.get("AllocatedStorage"),
                        "multi_az": db.get("MultiAZ"),
                    },
                )
            )

        return Inventory(resources=resources, source_environment=f"aws:{self.region}")


def summarize(inventory: Inventory) -> str:
    counts: dict[str, int] = {}
    for r in inventory.resources:
        counts[r.type.value] = counts.get(r.type.value, 0) + 1
    lines = [f"Discovered {len(inventory.resources)} resources in '{inventory.source_environment}':"]
    for t, c in sorted(counts.items()):
        lines.append(f"  - {t}: {c}")
    return "\n".join(lines)
