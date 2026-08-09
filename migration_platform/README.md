# Migration Automation Platform

An end-to-end pipeline for cloud infrastructure migration: discover the
source environment, score its compatibility and success probability
against a target environment, map dependencies, build a wave-based
migration plan, generate Terraform, execute it (with resumable state),
validate the result, and produce a rollback runbook automatically for
anything that fails.

```
Discovery -> Compatibility & Success-Probability Scoring -> Dependency Analysis
          -> Planning -> IaC Generation -> Execution -> Validation -> Rollback -> Report
```

See `ARCHITECTURE.md` for the full design rationale.

## Web app (form-based UI)

`webapp.html` is a self-contained, no-backend web front end for the same
analysis engine — open it directly in a browser, no server or install
required.

1. Fill in the **Manifest** screen: name your environment, pick a destination
   (AWS / Azure / On-Premises), and add resources through the form (or click
   **Load sample environment** to see it populated).
2. Click **Run analysis** to get the **Flight Report**: a success-probability
   gauge, compatibility/performance scores, blockers, a wave-by-wave route
   visualization, and the full scored manifest table.
3. **Export JSON** on either screen produces a file in the exact schema the
   CLI's `--source` expects — build your inventory in the browser, then hand
   it to the CLI for Terraform generation/execution, or the reverse (`compare
   --target-profile ...` output can inform what you enter in the form).

The web app re-implements the compatibility-scoring and 6R-planning logic in
JavaScript (verified to produce identical numbers to the Python engine on
the sample inventory across all three profiles) so it runs entirely
client-side — nothing is sent anywhere. It does not run Terraform generation,
execution, or rollback; those remain CLI-only for now (see "Known
simplifications").

## Quickstart

```bash
cd migration_platform
python3 -m migration_platform.cli run-all \
  --source sample_data/on_prem_inventory.json \
  --target aws
```

This runs the entire pipeline against the bundled sample on-prem
inventory (a small "orders" web app + a legacy CRM on Oracle) and writes
everything to `outputs/`:

| File | What it is |
|---|---|
| `outputs/inventory.json` | Normalized resource inventory from discovery |
| `outputs/plan.json` | The migration plan (strategy, wave, risk per resource) |
| `outputs/terraform/*.tf` | Generated Terraform, one file per wave |
| `outputs/execution_state.json` | Resumable execution state |
| `outputs/ROLLBACK.md` / `rollback.sh` | Rollback runbook + script skeleton |
| `outputs/report.html` | Single-page report of the whole run |

Open `outputs/report.html` in a browser to see the summary.

## Running stages individually

```bash
python3 -m migration_platform.cli discover  --source sample_data/on_prem_inventory.json
python3 -m migration_platform.cli compare   --target-profile aws   # or azure / on_prem
python3 -m migration_platform.cli plan      --target aws
python3 -m migration_platform.cli generate-iac
python3 -m migration_platform.cli execute
python3 -m migration_platform.cli validate
python3 -m migration_platform.cli rollback
```

## Compatibility & success-probability scoring

`compare` is independent of planning/execution — run it right after
`discover` to get a score before committing to a migration:

```bash
python3 -m migration_platform.cli compare --target-profile azure
```

For every resource it checks against a `TargetProfile` (engine/OS
support, managed-service availability, typical sizing ceiling) and
reports:

- **Compatibility score** — does it fit at all (supported engine/OS/service)?
- **Performance score** — will it run well (sizing headroom, HA posture)?
- **Blockers** — hard incompatibilities that must be resolved before migrating
  (e.g. an EOL Windows Server image, an Oracle DB on a profile with no managed
  Oracle offering)
- **Success probability (%)** — a single project-level number, weighted by
  each resource's dependency "blast radius" (a core database three services
  depend on counts more than an isolated batch job), with an extra penalty
  per hard blocker found

Three profiles ship today — `aws`, `azure`, `on_prem` — defined in
`compatibility.py`. `on_prem` is there for cloud→on-prem or
datacenter-consolidation comparisons, not just cloud targets. Results save to
`outputs/comparison.json`, and `run-all` folds the score into `report.html`
automatically (pass `--target-profile` to `run-all` to choose which one).

Note this is separate from `--target` on `plan`/`run-all`, which controls
*Terraform generation* and only supports `aws` today — you can score
compatibility against Azure or on-prem even though IaC generation for those
targets isn't built yet (see Architecture doc for the extension point).

Each stage reads/writes to `outputs/` (override with `--out`), so you can
inspect the plan before executing, hand-edit Terraform, re-run
validation after a fix, etc.

## Demoing failure handling

Simulate a failed migration step, and watch execution stop the affected
wave and a rollback plan get generated automatically:

```bash
python3 -m migration_platform.cli run-all \
  --source sample_data/on_prem_inventory.json \
  --target aws \
  --fail-on db-legacy-crm
```

Or simulate a resource that "migrated" but failed a post-migration
health check:

```bash
python3 -m migration_platform.cli run-all \
  --source sample_data/on_prem_inventory.json \
  --target aws \
  --fail-checks-on vm-web-01:app_health_endpoint_200
```

## Using your own environment

Discovery is pluggable (`migration_platform/discovery.py`). Two
discoverers ship today:

- **`FileDiscoverer`** — reads a JSON inventory (works today, no
  credentials needed). Export your CMDB / vCenter inventory / manual
  documentation into the same shape as `sample_data/on_prem_inventory.json`.
- **`AWSDiscoverer`** — a real, live discoverer stub using `boto3` for
  cloud-to-cloud migrations. Requires `pip install boto3` and AWS
  credentials; not exercised in this sandbox (no network access here).

Add a new source (Azure, GCP, VMware, ServiceNow) by subclassing
`Discoverer` and implementing `discover() -> Inventory`. Nothing
downstream needs to change.

To actually apply the generated Terraform instead of dry-running,
swap `DryRunRunner` for `ShellStepRunner` in `cli.py`'s `cmd_execute`
(it shells out to `terraform apply -target=...`, requires the
`terraform` CLI on PATH).

## Design highlights

- **Wave-based scheduling**: dependency graph is topologically sorted
  into waves; resources with no ordering constraint between them land in
  the same wave and can migrate in parallel. Cycles are detected and
  reported, not silently ignored.
- **Resumable execution**: state is persisted after every resource, not
  just at the end. Re-running `execute` skips anything already
  `succeeded`, so a partial failure doesn't mean starting over.
- **Fail-stop, not fail-through**: if a resource fails, its wave is
  marked failed and no later wave starts, since later waves may depend
  on the resource that just failed.
- **Rollback knows the blast radius**: every rollback step lists which
  other resources depend on it (via the same dependency graph used for
  planning), so an operator can see what else is affected before tearing
  something down.
- **Traceability**: every generated Terraform resource is tagged with
  the source resource ID it came from, so discovery -> plan -> IaC ->
  execution -> validation -> rollback all refer to the same identifier.
