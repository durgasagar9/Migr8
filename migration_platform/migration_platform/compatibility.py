"""
Compatibility / success-probability scoring stage.

Sits between Discovery and Planning. Answers a different question than
the planner does: not "how would we migrate this?" but "how well would
this fit on the target, and how likely is that migration to succeed?"

Input is the same Inventory used everywhere else, compared against a
TargetProfile describing what the destination environment actually
supports (engine versions, OS families, managed-service availability,
typical sizing ceiling). Three profiles ship today: AWS, Azure, and a
generic on-premises target (for cloud -> on-prem or on-prem -> on-prem
moves, e.g. datacenter consolidation).

For each resource this produces:
  - a compatibility score (does it fit at all -- engine/OS/service support)
  - a performance score (will it run well -- sizing headroom, HA posture)
  - blockers (hard incompatibilities that must be resolved pre-migration)
  - issues (soft risks worth knowing about, not migration-blocking)

Resource scores are rolled up into one project-level success-probability
percentage, weighted by each resource's "blast radius" (how many other
resources depend on it) using the same DependencyGraph the planner and
rollback stages already rely on -- a resource three other services point
at matters more to overall success than an isolated batch job.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .dependency_analysis import DependencyGraph
from .models import Inventory, Resource, ResourceType


@dataclass
class TargetProfile:
    name: str
    kind: str  # "cloud" | "on_prem"
    supported_db_engines: set[str]
    supported_os_patterns: list[str]   # substrings matched against resource os string
    max_compute_cpu: int
    max_compute_ram_gb: int
    supports_serverless: bool
    supports_managed_lb: bool
    supports_managed_object_storage: bool
    notes: str = ""


TARGET_PROFILES: dict[str, TargetProfile] = {
    "aws": TargetProfile(
        name="AWS",
        kind="cloud",
        supported_db_engines={"postgresql", "mysql", "mariadb", "sqlserver", "oracle"},
        supported_os_patterns=[
            "ubuntu", "amazon-linux", "windows-server-2016", "windows-server-2019",
            "windows-server-2022", "rhel", "centos", "debian", "suse",
        ],
        max_compute_cpu=128,
        max_compute_ram_gb=4096,
        supports_serverless=True,
        supports_managed_lb=True,
        supports_managed_object_storage=True,
        notes="Broadest managed-service coverage of the three profiles; Oracle is "
              "supported via RDS Custom/EC2 but still carries licensing overhead.",
    ),
    "azure": TargetProfile(
        name="Azure",
        kind="cloud",
        supported_db_engines={"postgresql", "mysql", "mariadb", "sqlserver"},
        supported_os_patterns=[
            "ubuntu", "windows-server-2016", "windows-server-2019", "windows-server-2022",
            "rhel", "centos", "debian", "suse",
        ],
        max_compute_cpu=96,
        max_compute_ram_gb=3892,
        supports_serverless=True,
        supports_managed_lb=True,
        supports_managed_object_storage=True,
        notes="No first-party managed Oracle service -- Oracle workloads must run "
              "self-managed on a VM, which lowers compatibility for that engine.",
    ),
    "on_prem": TargetProfile(
        name="On-Premises",
        kind="on_prem",
        supported_db_engines={"postgresql", "mysql", "mariadb", "sqlserver", "oracle"},
        supported_os_patterns=[
            "ubuntu", "windows-server-2016", "windows-server-2019", "windows-server-2022",
            "rhel", "centos", "debian", "suse", "windows-server-2012",
        ],
        max_compute_cpu=64,
        max_compute_ram_gb=512,
        supports_serverless=False,
        supports_managed_lb=False,
        supports_managed_object_storage=False,
        notes="Engine/OS support is broad since nothing is a managed service here, "
              "but there is no serverless, managed LB, or managed object storage -- "
              "everything self-hosted -- and hardware sizing is capped by whatever "
              "the target datacenter actually has racked.",
    ),
}


@dataclass
class CompatibilityResult:
    resource_id: str
    compatibility_score: int   # 0-100: does it fit at all
    performance_score: int     # 0-100: will it run well
    combined_score: int        # 0-100: per-resource rollup used for weighting
    blockers: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class ComparisonReport:
    target_name: str
    target_kind: str
    results: list[CompatibilityResult]
    overall_compatibility: int
    overall_performance: int
    success_probability: int
    top_blockers: list[str]
    top_risks: list[str]
    notes: list[str] = field(default_factory=list)

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(
                {
                    "target_name": self.target_name,
                    "target_kind": self.target_kind,
                    "overall_compatibility": self.overall_compatibility,
                    "overall_performance": self.overall_performance,
                    "success_probability": self.success_probability,
                    "top_blockers": self.top_blockers,
                    "top_risks": self.top_risks,
                    "notes": self.notes,
                    "results": [
                        {
                            "resource_id": r.resource_id,
                            "compatibility_score": r.compatibility_score,
                            "performance_score": r.performance_score,
                            "combined_score": r.combined_score,
                            "blockers": r.blockers,
                            "issues": r.issues,
                        }
                        for r in self.results
                    ],
                },
                f,
                indent=2,
            )


def _score_database(r: Resource, profile: TargetProfile) -> tuple[int, int, list[str], list[str]]:
    compat, perf = 100, 100
    blockers, issues = [], []
    engine = r.attributes.get("engine", "")

    if engine not in profile.supported_db_engines:
        blockers.append(
            f"Engine '{engine}' is not supported on {profile.name} in this profile "
            f"-- requires a database re-platform to a supported engine before/ during migration."
        )
        compat -= 55
    elif engine == "oracle" and profile.name == "AWS":
        issues.append("Oracle is supported but only via RDS Custom or self-managed EC2, "
                       "not standard RDS -- factor in licensing and operational overhead.")
        compat -= 10

    if r.attributes.get("eol"):
        issues.append("Source engine version is end-of-life; managed-service compatibility "
                       "for this exact version is not guaranteed and an in-place upgrade "
                       "may be required first.")
        compat -= 15

    if not r.attributes.get("ha") and profile.kind == "cloud":
        issues.append("Source has no HA configured; recommend enabling Multi-AZ/zone-"
                       "redundancy on the target for equivalent reliability post-migration.")
        perf -= 8

    size_gb = r.attributes.get("size_gb", 0)
    if size_gb > 4000 and profile.kind == "on_prem":
        issues.append("Large dataset relative to typical on-prem storage sizing tiers; "
                       "confirm target storage array headroom before cutover.")
        perf -= 15

    return compat, perf, blockers, issues


def _score_compute(r: Resource, profile: TargetProfile) -> tuple[int, int, list[str], list[str]]:
    compat, perf = 100, 100
    blockers, issues = [], []
    os_str = str(r.attributes.get("os", "")).lower()

    if not any(pat in os_str for pat in profile.supported_os_patterns):
        blockers.append(
            f"OS '{r.attributes.get('os')}' has no direct supported image/pattern on "
            f"{profile.name} in this profile -- requires an OS upgrade or a rebuild "
            f"onto a supported image."
        )
        compat -= 55
    elif r.attributes.get("eol"):
        issues.append("OS is end-of-life; even though a similar image exists on the "
                       "target, the unsupported OS should not simply be carried forward.")
        compat -= 20

    cpu = r.attributes.get("cpu", 0)
    ram = r.attributes.get("ram_gb", 0)
    if cpu > profile.max_compute_cpu or ram > profile.max_compute_ram_gb:
        issues.append(
            f"Requested capacity ({cpu} vCPU / {ram} GB) exceeds this profile's typical "
            f"sizing ceiling ({profile.max_compute_cpu} vCPU / {profile.max_compute_ram_gb} GB); "
            f"may require a custom/high-memory tier or won't fit at all."
        )
        perf -= 15

    return compat, perf, blockers, issues


def _score_function(r: Resource, profile: TargetProfile) -> tuple[int, int, list[str], list[str]]:
    compat, perf = 100, 100
    blockers, issues = [], []
    if not profile.supports_serverless:
        blockers.append(
            f"{profile.name} in this profile has no serverless offering -- the job must "
            f"be rehosted as a scheduled task on a VM instead of a direct lift."
        )
        compat -= 40
    return compat, perf, blockers, issues


def _score_load_balancer(r: Resource, profile: TargetProfile) -> tuple[int, int, list[str], list[str]]:
    compat, perf = 100, 100
    blockers, issues = [], []
    if not profile.supports_managed_lb:
        issues.append(
            f"{profile.name} in this profile has no managed load balancer -- requires "
            f"a self-managed LB (e.g. HAProxy/NGINX) with its own HA and patching burden."
        )
        compat -= 20
        perf -= 10
    return compat, perf, blockers, issues


def _score_object_storage(r: Resource, profile: TargetProfile) -> tuple[int, int, list[str], list[str]]:
    compat, perf = 100, 100
    blockers, issues = [], []
    if not profile.supports_managed_object_storage:
        issues.append(
            f"{profile.name} in this profile has no managed object storage -- requires "
            f"a self-hosted equivalent (e.g. MinIO) or falling back to a file share/NFS."
        )
        compat -= 20
    return compat, perf, blockers, issues


_SCORERS = {
    ResourceType.DATABASE: _score_database,
    ResourceType.COMPUTE: _score_compute,
    ResourceType.FUNCTION: _score_function,
    ResourceType.LOAD_BALANCER: _score_load_balancer,
    ResourceType.OBJECT_STORAGE: _score_object_storage,
}


def _score_resource(r: Resource, profile: TargetProfile) -> CompatibilityResult:
    scorer = _SCORERS.get(r.type)
    if scorer:
        compat, perf, blockers, issues = scorer(r, profile)
    else:
        # Network/DNS/queue: not scored with specific rules yet, treated as
        # generally portable. Flagged in notes rather than silently assumed perfect.
        compat, perf, blockers, issues = 100, 100, [], []

    compat = max(0, min(100, compat))
    perf = max(0, min(100, perf))
    combined = round(0.6 * compat + 0.4 * perf)
    if blockers:
        combined = min(combined, 40)  # a hard blocker caps how "successful" this resource can be

    return CompatibilityResult(
        resource_id=r.id,
        compatibility_score=compat,
        performance_score=perf,
        combined_score=combined,
        blockers=blockers,
        issues=issues,
    )


def analyze(
    inventory: Inventory,
    graph: DependencyGraph,
    target_profile_key: str,
) -> ComparisonReport:
    if target_profile_key not in TARGET_PROFILES:
        raise ValueError(
            f"Unknown target profile '{target_profile_key}'. "
            f"Available: {sorted(TARGET_PROFILES.keys())}"
        )
    profile = TARGET_PROFILES[target_profile_key]

    results = [_score_resource(r, profile) for r in inventory.resources]

    # Weight by blast radius: a resource with N dependents is weighted N+1,
    # so foundational resources (networks, core databases) pull the overall
    # score more than an isolated batch job would.
    weights = {res.resource_id: 1 + len(graph.dependents_of(res.resource_id)) for res in results}
    total_weight = sum(weights.values()) or 1

    overall_compat = round(sum(r.compatibility_score * weights[r.resource_id] for r in results) / total_weight)
    overall_perf = round(sum(r.performance_score * weights[r.resource_id] for r in results) / total_weight)
    weighted_combined = sum(r.combined_score * weights[r.resource_id] for r in results) / total_weight

    all_blockers = [b for r in results for b in r.blockers]
    all_issues = [i for r in results for i in r.issues]

    # Each project-wide blocker knocks a further percentage off success probability
    # on top of the per-resource cap, since a blocker anywhere threatens the whole
    # cutover timeline, not just the one resource.
    blocker_penalty = min(30, 6 * len(all_blockers))
    success_probability = max(0, round(weighted_combined) - blocker_penalty)

    notes = [profile.notes] if profile.notes else []
    notes.append(
        f"Scored {len(results)} resource(s) against the '{profile.name}' profile, "
        f"weighted by dependency blast radius (resources more depended-upon count more)."
    )
    if all_blockers:
        notes.append(
            f"{len(all_blockers)} hard blocker(s) found -- each caps its resource's score "
            f"at 40 and reduces the overall success probability by 6 points (max 30)."
        )

    return ComparisonReport(
        target_name=profile.name,
        target_kind=profile.kind,
        results=results,
        overall_compatibility=overall_compat,
        overall_performance=overall_perf,
        success_probability=success_probability,
        top_blockers=all_blockers[:10],
        top_risks=all_issues[:10],
        notes=notes,
    )


def summarize(report: ComparisonReport) -> str:
    lines = [
        f"Target: {report.target_name} ({report.target_kind})",
        f"Overall compatibility: {report.overall_compatibility}%",
        f"Overall performance fit: {report.overall_performance}%",
        f"Success probability: {report.success_probability}%",
    ]
    if report.top_blockers:
        lines.append(f"\nBlockers ({len(report.top_blockers)}):")
        lines += [f"  - {b}" for b in report.top_blockers]
    if report.top_risks:
        lines.append(f"\nRisks/issues ({len(report.top_risks)}):")
        lines += [f"  - {i}" for i in report.top_risks]
    return "\n".join(lines)
