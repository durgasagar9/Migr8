"""
Generates a single self-contained HTML report summarizing discovery,
plan, execution, validation, and rollback readiness -- the artifact a
migration lead would actually look at.
"""
from __future__ import annotations

from .compatibility import ComparisonReport
from .models import ExecutionRecord, Inventory, MigrationPlan, StepStatus, ValidationResult
from .rollback import RollbackPlan

CSS = """
:root {
  --bg: #0b0e14; --panel: #131722; --border: #232838; --text: #e6e9f0;
  --muted: #8b93a7; --accent: #5b8cff; --ok: #34d399; --warn: #fbbf24; --bad: #f87171;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 40px 24px; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
  line-height: 1.5;
}
.wrap { max-width: 980px; margin: 0 auto; }
h1 { font-size: 28px; margin-bottom: 4px; }
.subtitle { color: var(--muted); margin-bottom: 32px; font-size: 14px; }
h2 { font-size: 18px; border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-top: 40px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 16px 0; }
.stat { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.stat .n { font-size: 24px; font-weight: 700; }
.stat .l { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: .04em; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.ok { background: rgba(52,211,153,.15); color: var(--ok); }
.bad { background: rgba(248,113,113,.15); color: var(--bad); }
.warn { background: rgba(251,191,36,.15); color: var(--warn); }
.muted { color: var(--muted); }
.notes { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin: 12px 0; }
.notes li { margin-bottom: 6px; }
"""


def _badge(text: str, kind: str) -> str:
    return f'<span class="badge {kind}">{text}</span>'


def _status_badge(status: StepStatus) -> str:
    kind = {"succeeded": "ok", "failed": "bad"}.get(status.value, "warn")
    return _badge(status.value, kind)


def generate_html_report(
    inventory: Inventory,
    plan: MigrationPlan,
    execution_records: list[ExecutionRecord],
    validation_results: list[ValidationResult],
    rollback_plan: RollbackPlan,
    out_path: str,
    comparison: ComparisonReport | None = None,
) -> str:
    n_resources = len(inventory.resources)
    n_waves = len(plan.waves)
    n_succeeded = sum(1 for r in execution_records if r.status == StepStatus.SUCCEEDED)
    n_failed = sum(1 for r in execution_records if r.status == StepStatus.FAILED)
    n_valid_pass = sum(1 for v in validation_results if v.passed)
    n_valid_fail = sum(1 for v in validation_results if not v.passed)

    exec_rows = "\n".join(
        f"<tr><td>{r.resource_id}</td><td>{r.wave}</td>"
        f"<td>{plan.resource_plans[r.resource_id].strategy.value}</td>"
        f"<td>{_status_badge(r.status)}</td><td class='muted'>{r.detail}</td></tr>"
        for r in sorted(execution_records, key=lambda x: (x.wave, x.resource_id))
    )

    valid_rows = "\n".join(
        f"<tr><td>{v.resource_id}</td>"
        f"<td>{_badge('pass', 'ok') if v.passed else _badge('fail', 'bad')}</td>"
        f"<td class='muted'>{v.detail}</td></tr>"
        for v in sorted(validation_results, key=lambda x: x.resource_id)
    )

    plan_rows = "\n".join(
        f"<tr><td>{rid}</td><td>{rp.wave}</td><td>{rp.strategy.value}</td>"
        f"<td>{rp.target_service}</td>"
        f"<td>{_badge(rp.risk.value, {'low':'ok','medium':'warn','high':'bad'}[rp.risk.value])}</td>"
        f"<td>{rp.estimated_downtime_minutes} min</td></tr>"
        for rid, rp in sorted(plan.resource_plans.items(), key=lambda kv: (kv[1].wave, kv[0]))
    )

    rollback_section = (
        "<p class='muted'>No rollback actions required.</p>"
        if not rollback_plan.steps
        else "\n".join(
            f"<tr><td>{s.resource_id}</td><td>{s.reason}</td><td>{s.action}</td>"
            f"<td class='muted'>{', '.join(s.dependents_at_risk) or '—'}</td></tr>"
            for s in rollback_plan.steps
        )
    )
    rollback_table = "" if not rollback_plan.steps else f"""
    <table>
      <tr><th>Resource</th><th>Reason</th><th>Action</th><th>Dependents at risk</th></tr>
      {rollback_section}
    </table>"""
    if not rollback_plan.steps:
        rollback_table = rollback_section

    notes = "\n".join(f"<li>{n}</li>" for n in plan.notes)

    comparison_section = ""
    if comparison is not None:
        def _score_kind(v: int) -> str:
            return "ok" if v >= 75 else ("warn" if v >= 50 else "bad")

        comp_rows = "\n".join(
            f"<tr><td>{r.resource_id}</td><td>{r.compatibility_score}%</td>"
            f"<td>{r.performance_score}%</td>"
            f"<td>{_badge(str(r.combined_score) + '%', _score_kind(r.combined_score))}</td>"
            f"<td class='muted'>{'; '.join(r.blockers + r.issues) or '—'}</td></tr>"
            for r in sorted(comparison.results, key=lambda x: x.combined_score)
        )
        blockers_html = (
            "<p class='muted'>No hard blockers found.</p>"
            if not comparison.top_blockers
            else "<ul>" + "".join(f"<li>{b}</li>" for b in comparison.top_blockers) + "</ul>"
        )
        comparison_section = f"""
  <h2>Compatibility &amp; Success Probability — target: {comparison.target_name}</h2>
  <div class="grid">
    <div class="stat"><div class="n">{comparison.overall_compatibility}%</div><div class="l">Compatibility</div></div>
    <div class="stat"><div class="n">{comparison.overall_performance}%</div><div class="l">Performance fit</div></div>
    <div class="stat"><div class="n">{comparison.success_probability}%</div><div class="l">Success probability</div></div>
    <div class="stat"><div class="n">{len(comparison.top_blockers)}</div><div class="l">Hard blockers</div></div>
  </div>
  <div class="notes"><strong>Blockers to resolve before migration:</strong>{blockers_html}</div>
  <table>
    <tr><th>Resource</th><th>Compatibility</th><th>Performance</th><th>Combined</th><th>Notes</th></tr>
    {comp_rows}
  </table>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Migration Report — {inventory.source_environment} → {plan.target_cloud}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>Migration Automation Report</h1>
  <div class="subtitle">{inventory.source_environment} → {plan.target_cloud} · generated by Migration Automation Platform</div>

  <div class="grid">
    <div class="stat"><div class="n">{n_resources}</div><div class="l">Resources discovered</div></div>
    <div class="stat"><div class="n">{n_waves}</div><div class="l">Migration waves</div></div>
    <div class="stat"><div class="n">{n_succeeded}/{len(execution_records)}</div><div class="l">Steps succeeded</div></div>
    <div class="stat"><div class="n">{n_valid_pass}/{len(validation_results)}</div><div class="l">Validations passed</div></div>
    <div class="stat"><div class="n">{len(rollback_plan.steps)}</div><div class="l">Rollback actions pending</div></div>
  </div>
{comparison_section}
  <h2>Migration Plan</h2>
  <table>
    <tr><th>Resource</th><th>Wave</th><th>Strategy</th><th>Target service</th><th>Risk</th><th>Est. downtime</th></tr>
    {plan_rows}
  </table>
  <div class="notes"><ul>{notes}</ul></div>

  <h2>Execution</h2>
  <table>
    <tr><th>Resource</th><th>Wave</th><th>Strategy</th><th>Status</th><th>Detail</th></tr>
    {exec_rows}
  </table>

  <h2>Validation</h2>
  <table>
    <tr><th>Resource</th><th>Result</th><th>Detail</th></tr>
    {valid_rows}
  </table>

  <h2>Rollback Readiness</h2>
  {rollback_table}

</div>
</body>
</html>
"""
    with open(out_path, "w") as f:
        f.write(html)
    return out_path
