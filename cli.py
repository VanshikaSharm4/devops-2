#!/usr/bin/env python3
"""DevOps AI Agent CLI — report | risk | compare | correlate"""

import argparse
import json
import os
import sys

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def cmd_report(args):
    from analysis.ingest import build_base_bundle
    from analysis.context_builder import build_report_context
    from agent.devops_agent import run_analysis

    print("=" * 60)
    print("  DevOps AI Agent — Failure Analysis Report")
    print("=" * 60)

    bundle, _pdf, _fdf, _ = build_base_bundle(fetch_logs=not args.no_logs)

    print(f"\n  Total executions: {bundle.execution_summary.total_executions}")
    print(f"  Success rate: {bundle.execution_summary.success_rate_pct}%")

    os.makedirs("reports", exist_ok=True)
    json_path = "reports/latest_bundle.json"
    with open(json_path, "w") as f:
        json.dump(bundle.model_dump(mode="json"), f, indent=2)

    if args.no_llm:
        print(f"\n  Skipping LLM (--no-llm). Bundle saved to {json_path}")
        return

    print("\n  Running AI analysis...")
    context = build_report_context(bundle)
    report_obj = run_analysis(context, pipeline_df=_pdf, failed_df=_fdf)

    # Render markdown from the structured object (no second LLM call)
    lines = [
        f"# Failure Analysis Report — Program {report_obj.program_id}",
        f"**Period:** Last {report_obj.window_days} days  |  "
        f"**Total Executions:** {report_obj.total_executions}  |  "
        f"**Success Rate:** {report_obj.success_rate_pct:.1f}%",
        f"**Estimated Hours Wasted:** {report_obj.estimated_hours_wasted:.1f}h",
        "",
        "## Executive Summary",
    ]
    for bullet in report_obj.executive_summary:
        lines.append(f"- {bullet}")
    lines += ["", "## Critical Findings"]
    for finding in report_obj.critical_findings:
        lines += [
            f"### {finding.step} — {finding.error_type} (×{finding.occurrence_count})",
            f"**Root Cause:** {finding.root_cause}",
            f"**Impact:** {finding.business_impact}",
            f"**Fix:** {finding.recommended_fix}",
            "",
        ]
    if report_obj.recurring_findings:
        lines.append("## Recurring Findings")
        for finding in report_obj.recurring_findings:
            lines += [
                f"### {finding.step} — {finding.error_type} (×{finding.occurrence_count})",
                f"**Root Cause:** {finding.root_cause}",
                f"**Fix:** {finding.recommended_fix}",
                "",
            ]
    lines.append("## Top Recommended Actions")
    for action in report_obj.top_recommended_actions:
        lines.append(f"- {action}")
    report_md = "\n".join(lines)

    md_path = "reports/latest_report.md"
    report_json_path = "reports/latest_report.json"
    with open(md_path, "w") as f:
        f.write(report_md)
    with open(report_json_path, "w") as f:
        json.dump(report_obj.model_dump(mode="json"), f, indent=2)

    print("\n" + "=" * 60)
    print(report_md)
    print("=" * 60)
    print(f"\n  Report:     {md_path}")
    print(f"  Report JSON:{report_json_path}")
    print(f"  Bundle:     {json_path}")


def cmd_risk(args):
    from analysis.risk_analyzer import run_pre_deploy_risk, save_risk_report

    print("=" * 60)
    print("  DevOps AI Agent — Pre-Deployment Risk Analysis")
    print("=" * 60)

    bundle, report, md = run_pre_deploy_risk(
        commit_sha=args.commit,
        fetch_logs=not args.no_logs,
        use_llm=not args.no_llm,
    )

    if report:
        md_path, json_path = save_risk_report(
            report, md, commit_sha=args.commit
        )
        print("\n" + md)
        print(f"\n  Saved: {md_path}")
        print(f"  JSON:  {json_path}")
    else:
        print(md)


def cmd_compare(args):
    from analysis.deploy_compare import run_compare

    print("=" * 60)
    print("  DevOps AI Agent — Cross-Deployment Comparison")
    print("=" * 60)

    data, report = run_compare(
        args.exec_a,
        args.exec_b,
        sha_a=args.sha_a,
        sha_b=args.sha_b,
        use_llm=not args.no_llm,
        fetch_logs=not args.no_logs,
    )

    os.makedirs("reports", exist_ok=True)
    out = f"reports/compare_{args.exec_a}_vs_{args.exec_b}.md"
    with open(out, "w") as f:
        f.write(report)
    with open(out.replace(".md", ".json"), "w") as f:
        json.dump(data, f, indent=2)

    print("\n" + report)
    print(f"\n  Saved: {out}")


def cmd_scan(args):
    from analysis.code_analyzer import run_scan

    print("=" * 60)
    print("  DevOps AI Agent — Proactive Code Risk Scan")
    print("=" * 60)

    risks, report = run_scan(use_llm=not args.no_llm)

    os.makedirs("reports", exist_ok=True)
    out = "reports/code_scan.md"
    with open(out, "w") as f:
        f.write(report)

    print("\n" + report)
    print(f"\n  Saved: {out}")
    print(f"  Total risks found: {len(risks)}")


def cmd_pinpoint(args):
    from analysis.code_analyzer import run_pinpoint

    print("=" * 60)
    print("  DevOps AI Agent — Pinpoint Failure in Code")
    print("=" * 60)

    findings, report = run_pinpoint(args.execution_id, use_llm=not args.no_llm)

    os.makedirs("reports", exist_ok=True)
    out = f"reports/pinpoint_{args.execution_id}.md"
    with open(out, "w") as f:
        f.write(report)

    print("\n" + report)
    print(f"\n  Saved: {out}")


def cmd_correlate(args):
    from analysis.code_correlator import run_correlate

    print("=" * 60)
    print("  DevOps AI Agent — Code-to-Log Correlation")
    print("=" * 60)

    parsed = None
    if args.parsed_error_json:
        with open(args.parsed_error_json) as f:
            parsed = json.load(f)

    data, report = run_correlate(
        args.execution_id or "",
        parsed_error=parsed,
        use_llm=not args.no_llm,
    )

    eid = args.execution_id or "manual"
    os.makedirs("reports", exist_ok=True)
    out = f"reports/correlate_{eid}.md"
    with open(out, "w") as f:
        f.write(report)
    print("\n" + report)
    print(f"\n  Saved: {out}")


def main():
    parser = argparse.ArgumentParser(
        description="Adobe Cloud Manager DevOps AI Agent (IDFC Program 19905)"
    )
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM calls (rules/data only)")
    parser.add_argument("--no-logs", action="store_true", help="Skip Azure log fetch")

    sub = parser.add_subparsers(dest="command", required=True)

    parent_flags = argparse.ArgumentParser(add_help=False)
    parent_flags.add_argument("--no-llm", action="store_true")
    parent_flags.add_argument("--no-logs", action="store_true")

    p_report = sub.add_parser("report", parents=[parent_flags], help="Feature 1: 30-day failure analysis")
    p_report.set_defaults(func=cmd_report)

    p_risk = sub.add_parser("risk", parents=[parent_flags], help="Feature 2: Pre-deployment risk")
    p_risk.add_argument("--commit", type=str, required=True, help="Git commit SHA to analyse")
    p_risk.set_defaults(func=cmd_risk)

    p_cmp = sub.add_parser("compare", parents=[parent_flags], help="Feature 3: Compare two executions")
    p_cmp.add_argument("--exec-a", required=True, help="First execution ID")
    p_cmp.add_argument("--exec-b", required=True, help="Second execution ID")
    p_cmp.add_argument("--sha-a", help="Git SHA for execution A (optional)")
    p_cmp.add_argument("--sha-b", help="Git SHA for execution B (optional)")
    p_cmp.set_defaults(func=cmd_compare)

    p_corr = sub.add_parser("correlate", parents=[parent_flags], help="Feature 4: Correlate log error to code")
    p_corr.add_argument("--execution-id", help="Cloud Manager execution ID")
    p_corr.add_argument("--parsed-error-json", help="Path to parsed error JSON file")
    p_corr.set_defaults(func=cmd_correlate)

    p_scan = sub.add_parser("scan", parents=[parent_flags], help="Feature 5a: Scan repo for future risk spots")
    p_scan.set_defaults(func=cmd_scan)

    p_pin = sub.add_parser("pinpoint", parents=[parent_flags], help="Feature 5b: Find which code line caused a failure")
    p_pin.add_argument("--execution-id", required=True, help="Cloud Manager execution ID")
    p_pin.set_defaults(func=cmd_pinpoint)

    args = parser.parse_args()

    if args.command == "risk" and not args.commit:
        parser.error("risk requires --commit <SHA>")
    if args.command == "correlate" and not args.execution_id and not args.parsed_error_json:
        parser.error("correlate requires --execution-id or --parsed-error-json")

    args.func(args)


if __name__ == "__main__":
    main()
