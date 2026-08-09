# Architecture

## Goals

1. Take a source environment (on-prem or another cloud) and get a
   trustworthy inventory of what's actually there — not what a wiki says
   is there.
2. Never schedule a migration step before the things it depends on.
3. Produce infrastructure-as-code, not manual click-ops, so the target
   state is reviewable and repeatable.
4. Execute in a way that survives partial failure: resumable, not
   restart-from-scratch.
5. Treat "it deployed" and "it works" as two different questions —
   validation is a distinct stage from execution.
6. Assume something will fail eventually, and make rollback a first-class
   output of the pipeline, not an afterthought written at 2am.

## Pipeline

```
 ┌───────────┐   ┌───────────────┐   ┌──────────────┐   ┌──────────┐   ┌────────────┐   ┌───────────┐   ┌────────────┐
 │ Discovery │ → │ Compatibility │ → │ Dependency   │ → │ Planning │ → │ IaC        │ → │ Execution │ → │ Validation │
 │           │   │ & Success-Prob│   │ Analysis     │   │          │   │ Generation │   │           │   │            │
 └───────────┘   └───────────────┘   └──────────────┘   └──────────┘   └────────────┘   └───────────┘   └─────┬──────┘
                                                                                                                │
                                                                                                ┌───────────────┴───┐
                                                                                                │  pass → Report     │
                                                                                                │  fail → Rollback   │
                                                                                                └────────────────────┘
```

Each stage is a separate module with a narrow, typed interface
(`migration_platform/models.py` defines the shared vocabulary:
`Inventory`, `MigrationPlan`, `ExecutionRecord`, `ValidationResult`).
Stages communicate via those types (persisted to JSON between CLI
invocations), so any stage can be swapped, re-run in isolation, or
inspected without touching the others.

### 1. Discovery (`discovery.py`)

A `Discoverer` answers "what exists, and what does it look like?" and
returns an `Inventory` of `Resource` objects. It makes no migration
decisions. Two implementations:

- `FileDiscoverer` — agentless; reads a JSON export (CMDB, vCenter,
  manual documentation). This is the default/demo path since it needs no
  live credentials or network access.
- `AWSDiscoverer` — a real `boto3`-based discoverer for cloud-to-cloud
  migrations, enumerating EC2/RDS today. New sources (Azure, GCP,
  on-prem hypervisors, ServiceNow) are added by subclassing `Discoverer`;
  nothing downstream needs to know the difference.

### 2. Compatibility & Success-Probability Scoring (`compatibility.py`)

Answers a different question than planning does: not "how would we
migrate this?" but "how well would it fit, and how likely is that
migration to succeed?" Runs independently of planning/execution — you
can score a migration before deciding to commit to it.

Each resource is compared against a `TargetProfile` (engine/OS support,
managed-service availability, typical sizing ceiling). Three profiles
ship today — `aws`, `azure`, `on_prem` (the last for cloud→on-prem or
datacenter-consolidation moves) — each a plain dataclass, so adding GCP
or a custom internal-cloud profile is a matter of adding one more
dataclass instance, not new logic.

Per resource this produces a **compatibility score** (does it fit at
all), a **performance score** (will it run well — sizing headroom, HA
posture), **blockers** (hard incompatibilities, e.g. an EOL OS or a
database engine the target profile doesn't support) and **issues**
(softer risks worth knowing but not migration-blocking).

Resource scores roll up into one project-level **success probability
percentage**, weighted by each resource's blast radius — reusing the
same `DependencyGraph.dependents_of()` that planning and rollback use,
so a core database three services depend on pulls the overall score more
than an isolated batch job. Any hard blocker caps that resource's score
at 40 and knocks a further 6 points (max 30) off the project-wide
success probability, since a blocker anywhere threatens the whole
cutover timeline.

This is intentionally decoupled from `--target` on `plan`/`run-all` — you
can score compatibility against Azure or on-prem even though Terraform
generation (`iac_generator.py`) only implements AWS today. Extending IaC
generation to match is the natural next step (see "Known simplifications").

### 3. Dependency Analysis (`dependency_analysis.py`)

Builds a directed graph from each resource's `depends_on` list and
computes a **wave-based topological order** (Kahn's algorithm, batched
by depth): resources with no unmigrated dependencies go in wave 1,
resources that only depend on wave-1 resources go in wave 2, etc.
Resources within a wave have no ordering constraint between them and are
candidates for parallel migration. Cycles are detected explicitly
(`CycleError`) rather than producing a plan that silently deadlocks.

This same graph is reused later by validation (to check "are my
dependencies healthy?") and rollback (to compute the blast radius of
tearing a resource back out) — one source of truth for "what depends on
what."

### 4. Planning (`planner.py`)

For every resource, decides:

- **Strategy** — one of the "6 R's" (rehost / replatform / repurchase /
  refactor / retain / retire), via a small declarative rule set keyed on
  resource type and attributes (e.g., EOL OS → refactor and flag high
  risk; stateless web tier → replatform, low risk; legacy database engine
  → replatform with review). The rules are intentionally simple and
  visible in one file — swap in a cost-based or ML-based scorer without
  touching any other stage.
- **Target service** — the target-cloud equivalent (`aws_instance`,
  `aws_db_instance`, etc.)
- **Risk** and **estimated downtime** — used to surface high-risk
  resources in the plan's notes before anyone hits "execute."

Waves come straight from the dependency graph, so the plan's execution
order is migration-safe by construction.

### 5. IaC Generation (`iac_generator.py`)

Renders the plan into Terraform, one file per wave, tagging every
resource with `SourceId = "<original resource id>"` so the mapping
between source system and target infrastructure is traceable in both
directions. AWS is implemented for compute, database, object storage,
network, load balancer, function, and DNS resource types; unmapped types
are emitted as a commented TODO rather than silently dropped.

### 6. Execution (`executor.py`)

A `MigrationExecutor` walks waves in order and calls a pluggable
`StepRunner` per resource:

- `DryRunRunner` — simulates the step; the default, since it's safe to
  run repeatedly and lets you review a plan's behavior (including
  simulated failures) before touching real infrastructure.
- `ShellStepRunner` — a real runner that shells out to
  `terraform apply -target=...` for the resource's address. This is the
  extension point for actually applying the generated IaC.

State is persisted after **every** resource, not just at the end of a
run. Re-running `execute` skips anything already `succeeded`
(resumable), and if any resource in a wave fails, that wave is marked
complete but no later wave is attempted — later waves may depend on the
resource that just failed.

### 7. Validation (`validator.py`)

Runs a set of checks appropriate to each resource's type (existence,
health/connectivity, integrity spot-checks) against everything that
reported `succeeded` in execution. Validates in dependency order so a
resource's `dependencies_healthy` check reflects the *actual* validated
status of its dependencies, not an assumption. In this environment the
checks are simulated (no live cloud calls); in production these are the
integration points for CloudWatch alarms, synthetic transactions, a
`SELECT 1`, or an HTTP health probe.

### 8. Rollback (`rollback.py`)

For anything that failed execution *or* failed validation, generates a
`RollbackStep` with the reason, a strategy-appropriate rollback action
(e.g., a `replatform` rollback means "deprovision the managed target,
restore traffic to the original, replicate back any data written only to
the target"), and — via the same dependency graph — the list of
resources that would be affected by rolling it back. Rendered as both a
Markdown runbook (`ROLLBACK.md`) for a human and a shell script skeleton
(`rollback.sh`) with the `terraform destroy -target=...` commands
commented in, not auto-executed — rollback is a checklist made
executable, not a fire-and-forget script.

### 9. Report (`report.py`)

Rolls discovery counts, the plan, execution results, validation results,
and rollback readiness into one self-contained HTML file — the artifact
a migration lead actually looks at rather than reading five JSON files.

## Why this shape

- **Typed contracts between stages** (`models.py`) mean any stage can be
  replaced (a smarter planner, a real AWS discoverer, a Terragrunt-based
  IaC generator) without the rest of the pipeline changing.
- **One dependency graph, reused four times** (compatibility-score
  weighting, planning order, validation order, rollback blast-radius)
  avoids four different, potentially inconsistent notions of "what
  depends on what."
- **Fail-stop execution + automatic rollback plan generation** means a
  bad migration doesn't cascade into a bigger mess before anyone notices
  — and the rollback runbook exists before the incident, not written
  during it.
- **CLI stages are independently runnable** so a plan can be reviewed
  (`plan`), IaC can be hand-inspected (`generate-iac`), or validation
  re-run after a manual fix (`validate`) without redoing the whole
  pipeline.

## Known simplifications (and how to extend past them)

- Compatibility scoring rules are declarative and per-type, same spirit
  as the planner — swap the `_score_*` functions in `compatibility.py`
  for something more sophisticated (real pricing/SKU lookups, actual
  service-availability APIs) without touching any other stage.
- Only `network`/`dns`/`queue` resource types fall through to a default
  "fully compatible" score in `compatibility.py` since no type-specific
  rule exists yet — add a `_score_network` / `_score_dns` function
  following the existing pattern if you need those scored too.
- Planning rules are a small rule set, not a cost/compliance optimizer —
  swap `planner._decide_strategy` for something more sophisticated.
- `AWSDiscoverer` covers EC2/RDS only; extend with the same pattern for
  S3, Lambda, ELB, etc.
- Only AWS is implemented as a Terraform target; add
  `_azure_block_for_*` / `_gcp_block_for_*` builders in
  `iac_generator.py` alongside the existing `_block_*` functions.
- Validation checks are simulated; wire real checks (CloudWatch,
  synthetic probes, DB queries) into `validator._checks_for` /
  `validate()`.
- `ShellStepRunner` (real Terraform execution) is implemented but not
  exercised here since this environment has no network access — it's the
  natural on-ramp to a production run.
