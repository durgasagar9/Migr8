"""
Command-line entry point for the Migration Automation Platform.

Subcommands mirror the pipeline stages so each can be run and inspected
independently, plus a `run-all` command that chains everything for a
single end-to-end demo.

    migrate discover   --source sample_data/on_prem_inventory.json
    migrate plan        --target aws
    migrate generate-iac
    migrate execute
    migrate validate
    migrate rollback
    migrate run-all     --source sample_data/on_prem_inventory.json --target aws
"""
from __future__ import annotations

import argparse
import os
import sys

from . import compatibility, discovery, iac_generator, planner, report, rollback, validator
from .dependency_analysis import DependencyGraph
from .discovery import FileDiscoverer
from .executor import DryRunRunner, MigrationExecutor
from .models import Inventory, MigrationPlan

DEFAULT_OUT = "outputs"


def _paths(out_dir: str) -> dict:
    return {
        "inventory": os.path.join(out_dir, "inventory.json"),
        "plan": os.path.join(out_dir, "plan.json"),
        "terraform": os.path.join(out_dir, "terraform"),
        "state": os.path.join(out_dir, "execution_state.json"),
        "rollback": out_dir,
        "report": os.path.join(out_dir, "report.html"),
        "comparison": os.path.join(out_dir, "comparison.json"),
    }


def cmd_discover(args) -> None:
    p = _paths(args.out)
    os.makedirs(args.out, exist_ok=True)
    inv = FileDiscoverer(args.source).discover()
    inv.save(p["inventory"])
    print(discovery.summarize(inv))
    print(f"\nSaved inventory -> {p['inventory']}")


def cmd_compare(args) -> None:
    p = _paths(args.out)
    inv = Inventory.load(p["inventory"])
    graph = DependencyGraph(inv)
    comparison = compatibility.analyze(inv, graph, args.target_profile)
    comparison.save(p["comparison"])
    print(compatibility.summarize(comparison))
    print(f"\nSaved comparison -> {p['comparison']}")
    return comparison


def cmd_plan(args) -> None:
    p = _paths(args.out)
    inv = Inventory.load(p["inventory"])
    graph = DependencyGraph(inv)
    plan = planner.build_plan(inv, graph, target_cloud=args.target)
    plan.save(p["plan"])
    print(f"Built plan with {len(plan.waves)} wave(s), target cloud: {plan.target_cloud}")
    for i, wave in enumerate(plan.waves, start=1):
        print(f"  Wave {i}: {', '.join(wave)}")
    for note in plan.notes:
        print(f"  NOTE: {note}")
    print(f"\nSaved plan -> {p['plan']}")


def cmd_generate_iac(args) -> None:
    p = _paths(args.out)
    inv = Inventory.load(p["inventory"])
    plan = MigrationPlan.load(p["plan"])
    files = iac_generator.generate_terraform(inv, plan, p["terraform"])
    print("Generated Terraform files:")
    for f in files:
        print(f"  {f}")


def cmd_execute(args) -> None:
    p = _paths(args.out)
    inv = Inventory.load(p["inventory"])
    plan = MigrationPlan.load(p["plan"])
    fail_on = set(args.fail_on.split(",")) if args.fail_on else set()
    runner = DryRunRunner(fail_on=fail_on)
    executor = MigrationExecutor(runner, p["state"])
    records = executor.run(inv, plan)
    print(executor.status_summary())
    return records


def cmd_validate(args) -> None:
    p = _paths(args.out)
    inv = Inventory.load(p["inventory"])
    plan = MigrationPlan.load(p["plan"])
    graph = DependencyGraph(inv)
    executor = MigrationExecutor(DryRunRunner(), p["state"])
    records = list(executor.state.values())
    fail_checks = {}
    if args.fail_checks_on:
        rid, checks = args.fail_checks_on.split(":")
        fail_checks = {rid: checks.split(",")}
    results = validator.validate(inv, plan, records, graph, fail_checks_on=fail_checks)
    print(validator.summarize(results))
    return results


def cmd_rollback(args) -> None:
    p = _paths(args.out)
    inv = Inventory.load(p["inventory"])
    plan = MigrationPlan.load(p["plan"])
    graph = DependencyGraph(inv)
    executor = MigrationExecutor(DryRunRunner(), p["state"])
    exec_records = list(executor.state.values())
    validation_results = cmd_validate(args)
    rb_plan = rollback.build_rollback_plan(inv, plan, exec_records, validation_results, graph)
    md_path, sh_path = rollback.write_rollback_artifacts(rb_plan, p["rollback"])
    print(f"Rollback plan: {len(rb_plan.steps)} action(s) required.")
    print(f"  Runbook -> {md_path}")
    print(f"  Script  -> {sh_path}")
    return rb_plan


def cmd_run_all(args) -> None:
    print("=== 1/7 Discovery ===")
    cmd_discover(args)
    print("\n=== 2/7 Compatibility & success-probability analysis ===")
    comparison = cmd_compare(args)
    print("\n=== 3/7 Dependency analysis + Planning ===")
    cmd_plan(args)
    print("\n=== 4/7 IaC generation ===")
    cmd_generate_iac(args)
    print("\n=== 5/7 Execution ===")
    exec_records = cmd_execute(args)
    print("\n=== 6/7 Validation ===")
    validation_results = cmd_validate(args)
    print("\n=== 7/7 Rollback plan ===")
    p = _paths(args.out)
    inv = Inventory.load(p["inventory"])
    plan = MigrationPlan.load(p["plan"])
    graph = DependencyGraph(inv)
    rb_plan = rollback.build_rollback_plan(inv, plan, exec_records, validation_results, graph)
    rollback.write_rollback_artifacts(rb_plan, p["rollback"])
    print(f"Rollback plan: {len(rb_plan.steps)} action(s) required.")

    print("\n=== Report ===")
    out_path = report.generate_html_report(
        inv, plan, exec_records, validation_results, rb_plan, p["report"], comparison=comparison
    )
    print(f"Report -> {out_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="migrate", description="Migration Automation Platform")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output directory (default: outputs)")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Scan the source environment")
    d.add_argument("--source", required=True, help="Path to inventory JSON export")
    d.set_defaults(func=cmd_discover)

    cp = sub.add_parser(
        "compare",
        help="Analyze compatibility and produce a performance/success-probability score",
    )
    cp.add_argument(
        "--target-profile", default="aws", choices=sorted(compatibility.TARGET_PROFILES.keys()),
        help="Target environment to compare the inventory against",
    )
    cp.set_defaults(func=cmd_compare)

    pl = sub.add_parser("plan", help="Analyze dependencies and build the migration plan")
    pl.add_argument("--target", default="aws", choices=["aws"],
                     help="Target cloud for Terraform generation (IaC support today: AWS only)")
    pl.set_defaults(func=cmd_plan)

    ia = sub.add_parser("generate-iac", help="Generate Terraform for the plan")
    ia.set_defaults(func=cmd_generate_iac)

    ex = sub.add_parser("execute", help="Execute the migration plan")
    ex.add_argument("--fail-on", default="", help="Comma-separated resource ids to simulate failing")
    ex.set_defaults(func=cmd_execute)

    va = sub.add_parser("validate", help="Run post-migration validation checks")
    va.add_argument("--fail-checks-on", default="", help="resource_id:check1,check2 to simulate a failed check")
    va.set_defaults(func=cmd_validate)

    rb = sub.add_parser("rollback", help="Generate rollback plan/runbook for anything that failed")
    rb.add_argument("--fail-checks-on", default="")
    rb.set_defaults(func=cmd_rollback)

    ra = sub.add_parser("run-all", help="Run the full pipeline end-to-end and produce a report")
    ra.add_argument("--source", required=True)
    ra.add_argument("--target", default="aws", choices=["aws"],
                     help="Target cloud for Terraform generation (IaC support today: AWS only)")
    ra.add_argument(
        "--target-profile", default="aws", choices=sorted(compatibility.TARGET_PROFILES.keys()),
        help="Target environment to score compatibility/success-probability against "
             "(independent of --target; can be aws, azure, or on_prem even though IaC "
             "generation only supports aws today)",
    )
    ra.add_argument("--fail-on", default="")
    ra.add_argument("--fail-checks-on", default="")
    ra.set_defaults(func=cmd_run_all)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
