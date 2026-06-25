"""
evaluate_predictions.py

Evaluates pre-deploy risk predictions against actual pipeline outcomes.
Loads Splunk cache for HDFC (16360) and IDFC (19905), correlates executions
to commits, runs risk analysis, and writes results to an Excel report.
"""

import os
import sys
import pickle
import traceback
from collections import defaultdict

# Ensure project root is on path
PROJECT_ROOT = "/Users/vanshika/projects/devops-agent-2"
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from connectors.git_connector import correlate_executions_to_commits
from analysis.risk_analyzer import run_pre_deploy_risk

CUSTOMERS = [
    {
        "name": "HDFC",
        "program_id": "16360",
        "cache_path": os.path.join(PROJECT_ROOT, "data/cache/splunk_cache_16360.pkl"),
        "git_dir": "/Users/vanshika/projects/hdfcbankformsmaster",
    },
    {
        "name": "IDFC",
        "program_id": "19905",
        "cache_path": os.path.join(PROJECT_ROOT, "data/cache/splunk_cache_19905.pkl"),
        "git_dir": "/Users/vanshika/Downloads/idfc",
    },
]

MAX_EXECUTIONS = 20
REPORT_PATH = os.path.join(PROJECT_ROOT, "reports/prediction_evaluation.xlsx")


def load_cache(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def get_completed_executions(cache):
    """Deduplicate pipeline_df, keep only FINISHED/FAILED/ERROR, join with failed steps."""
    pdf = cache["pipeline_df"].copy()
    fdf = cache.get("failed_steps_df", pd.DataFrame()).copy()

    # Deduplicate by executionId (keep latest)
    pdf = pdf.drop_duplicates(subset=["executionId"], keep="first")

    # Filter completed
    completed = pdf[pdf["Status"].isin(["FINISHED", "FAILED", "ERROR"])].copy()

    # Join failed steps
    if not fdf.empty:
        completed = completed.merge(
            fdf[["executionId", "firstFailedStep"]],
            on="executionId",
            how="left",
        )
    else:
        completed["firstFailedStep"] = None

    return completed


def determine_correctness(predicted_risk, actual_status, predicted_step, actual_failed_step):
    """Returns (correct, step_correct)."""
    risk_lower = predicted_risk.lower() if predicted_risk else ""
    status_upper = actual_status.upper() if actual_status else ""

    if risk_lower == "low" and status_upper == "FINISHED":
        correct = True
    elif risk_lower in ("high", "medium") and status_upper in ("FAILED", "ERROR"):
        correct = True
    else:
        correct = False

    # Step correctness
    if predicted_step and actual_failed_step:
        step_correct = predicted_step.lower().strip() == actual_failed_step.lower().strip()
    else:
        step_correct = False

    return correct, step_correct


def detect_structural_patterns(commit_sha, git_dir):
    """Detect structural patterns from git diff for improvement signals."""
    patterns = []
    try:
        import subprocess
        result = subprocess.run(
            ["git", "show", "--name-only", "--format=", commit_sha],
            capture_output=True, text=True, cwd=git_dir, timeout=10
        )
        changed_files = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        for f in changed_files:
            fl = f.lower()
            if ".gitmodules" in fl or "submodule" in fl:
                patterns.append("submodule_bump")
            elif "pom.xml" in fl:
                patterns.append("pom_change")
            elif fl.endswith(".java"):
                patterns.append("java_change")
            elif fl.endswith(".xml") and "pom" not in fl:
                patterns.append("xml_change")
            elif fl.endswith(".json"):
                patterns.append("json_change")
            elif fl.endswith(".yml") or fl.endswith(".yaml"):
                patterns.append("yaml_change")
            elif fl.endswith(".js") or fl.endswith(".ts"):
                patterns.append("js_ts_change")
            elif fl.endswith(".html") or fl.endswith(".jinja") or fl.endswith(".ftl"):
                patterns.append("template_change")
    except Exception:
        pass
    return list(set(patterns)) if patterns else ["unknown"]


def run_evaluation():
    all_rows = []

    for customer in CUSTOMERS:
        name = customer["name"]
        program_id = customer["program_id"]
        cache_path = customer["cache_path"]
        git_dir = customer["git_dir"]

        print(f"\n{'='*60}")
        print(f"Processing customer: {name} (program {program_id})")
        print(f"{'='*60}")

        # Set env vars
        os.environ["PROGRAM_ID"] = program_id
        os.environ["GIT_LOCAL_DIR"] = git_dir

        # Load cache
        try:
            cache = load_cache(cache_path)
        except Exception as e:
            print(f"  ERROR loading cache: {e}")
            continue

        completed = get_completed_executions(cache)
        print(f"  Total completed executions: {len(completed)}")

        # Limit to MAX_EXECUTIONS
        # Prefer FAILED/ERROR first for more interesting evaluation
        failed_execs = completed[completed["Status"].isin(["FAILED", "ERROR"])]
        finished_execs = completed[completed["Status"] == "FINISHED"]

        n_failed = min(len(failed_execs), MAX_EXECUTIONS // 2)
        n_finished = min(len(finished_execs), MAX_EXECUTIONS - n_failed)
        # If not enough failed, take more finished
        if n_failed < MAX_EXECUTIONS // 2:
            n_finished = min(len(finished_execs), MAX_EXECUTIONS - n_failed)

        sample = pd.concat([
            failed_execs.head(n_failed),
            finished_execs.head(n_finished)
        ]).head(MAX_EXECUTIONS)

        print(f"  Sampled {len(sample)} executions ({n_failed} failed/error, {n_finished} finished)")

        # Build execution rows for correlation
        execution_rows = sample[["executionId", "Deploy Start Time"]].to_dict("records")

        # Correlate to commits
        print(f"  Correlating executions to commits (git dir: {git_dir})...")
        try:
            sha_map = correlate_executions_to_commits(execution_rows)
        except Exception as e:
            print(f"  ERROR correlating commits: {e}")
            sha_map = {}

        print(f"  Correlated {len(sha_map)} / {len(sample)} executions to SHAs")

        # Evaluate each execution
        for _, row in sample.iterrows():
            exec_id = str(row["executionId"])
            actual_status = row["Status"]
            actual_failed_step = row.get("firstFailedStep", None)
            pipeline_name = row.get("pipelineName", "")

            corr = sha_map.get(exec_id, {})
            commit_sha = corr.get("sha") if corr else None

            if not commit_sha:
                print(f"  [{exec_id}] No SHA correlation, skipping")
                continue

            print(f"  [{exec_id}] SHA={commit_sha[:8]}  status={actual_status}  pipeline={pipeline_name}")

            result_row = {
                "customer": name,
                "executionId": exec_id,
                "commit_sha": commit_sha,
                "pipeline_name": pipeline_name,
                "actual_status": actual_status,
                "actual_failed_step": actual_failed_step or "",
                "predicted_risk": "",
                "predicted_step": "",
                "confidence": "",
                "correct": "",
                "step_correct": "",
                "rationale": "",
                "error": "",
            }

            try:
                _bundle, report, _md = run_pre_deploy_risk(
                    commit_sha=commit_sha,
                    fetch_logs=False,
                    use_llm=True,
                )

                if report:
                    predicted_risk = report.risk_level.value if hasattr(report.risk_level, "value") else str(report.risk_level)
                    predicted_step = report.most_likely_failure_step or ""
                    confidence = report.confidence_score

                    correct, step_correct = determine_correctness(
                        predicted_risk, actual_status, predicted_step, actual_failed_step
                    )

                    result_row.update({
                        "predicted_risk": predicted_risk,
                        "predicted_step": predicted_step,
                        "confidence": confidence,
                        "correct": correct,
                        "step_correct": step_correct,
                        "rationale": (report.narrative or "")[:300],
                    })

                    status_icon = "OK" if correct else "WRONG"
                    print(f"    -> predicted={predicted_risk} step={predicted_step} conf={confidence} [{status_icon}]")
                else:
                    result_row["error"] = "No report returned"
                    print(f"    -> No report returned")

            except Exception as e:
                err_msg = f"{type(e).__name__}: {str(e)}"
                result_row["error"] = err_msg
                print(f"    -> ERROR: {err_msg}")
                traceback.print_exc()

            all_rows.append(result_row)

    return all_rows


def compute_summary(rows):
    evaluated = [r for r in rows if r["correct"] != "" and r["error"] == ""]
    total = len(evaluated)
    if total == 0:
        return {"total": 0, "accuracy_pct": 0, "false_negative_rate": 0, "false_positive_rate": 0, "step_accuracy": 0}

    correct_count = sum(1 for r in evaluated if r["correct"] is True)

    # False negatives: predicted Low but actual FAILED/ERROR
    actual_failures = [r for r in evaluated if r["actual_status"] in ("FAILED", "ERROR")]
    false_negatives = [r for r in actual_failures if str(r["predicted_risk"]).lower() == "low"]
    fn_rate = len(false_negatives) / len(actual_failures) if actual_failures else 0

    # False positives: predicted High/Med but actual FINISHED
    actual_successes = [r for r in evaluated if r["actual_status"] == "FINISHED"]
    false_positives = [r for r in actual_successes if str(r["predicted_risk"]).lower() in ("high", "medium")]
    fp_rate = len(false_positives) / len(actual_successes) if actual_successes else 0

    step_evaluated = [r for r in evaluated if r["actual_failed_step"]]
    step_correct_count = sum(1 for r in step_evaluated if r["step_correct"] is True)
    step_acc = step_correct_count / len(step_evaluated) if step_evaluated else 0

    return {
        "total": total,
        "accuracy_pct": round(correct_count / total * 100, 1),
        "false_negative_rate": round(fn_rate * 100, 1),
        "false_positive_rate": round(fp_rate * 100, 1),
        "step_accuracy": round(step_acc * 100, 1),
    }


def compute_improvement_signals(rows):
    """Group wrong predictions by structural pattern and actual failure."""
    wrong = [r for r in rows if r["correct"] is False and r["error"] == ""]
    signals = defaultdict(int)

    for r in wrong:
        git_dir = None
        for c in CUSTOMERS:
            if c["name"] == r["customer"]:
                git_dir = c["git_dir"]
                break

        patterns = detect_structural_patterns(r["commit_sha"], git_dir) if git_dir else ["unknown"]
        actual_step = r["actual_failed_step"] or "unknown"
        for p in patterns:
            key = (p, actual_step)
            signals[key] += 1

    result = []
    for (pattern, actual_step), count in sorted(signals.items(), key=lambda x: -x[1]):
        result.append({"pattern": pattern, "actual_failed_step": actual_step, "frequency": count})
    return result


def write_excel(rows, summary, improvement_signals, output_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Predictions"

    # Header style
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F4F8F")

    columns = [
        "customer", "executionId", "commit_sha", "pipeline_name",
        "actual_status", "actual_failed_step",
        "predicted_risk", "predicted_step", "confidence",
        "correct", "step_correct", "rationale", "error"
    ]

    for col_idx, col_name in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    # Data rows
    green_fill = PatternFill("solid", fgColor="C6EFCE")
    red_fill = PatternFill("solid", fgColor="FFC7CE")

    for row_idx, row in enumerate(rows, 2):
        for col_idx, col_name in enumerate(columns, 1):
            val = row.get(col_name, "")
            if isinstance(val, bool):
                val = "YES" if val else "NO"
            cell = ws.cell(row=row_idx, column=col_idx, value=val)

            # Color correct/incorrect
            if col_name == "correct":
                if val == "YES":
                    cell.fill = green_fill
                elif val == "NO":
                    cell.fill = red_fill

    # Summary section (2 blank rows after data)
    summary_start = len(rows) + 3
    ws.cell(row=summary_start, column=1, value="SUMMARY").font = Font(bold=True, size=12)

    summary_labels = [
        ("Total Evaluated", summary["total"]),
        ("Accuracy %", summary["accuracy_pct"]),
        ("False Negative Rate %", summary["false_negative_rate"]),
        ("False Positive Rate %", summary["false_positive_rate"]),
        ("Step Accuracy %", summary["step_accuracy"]),
    ]

    for i, (label, value) in enumerate(summary_labels):
        ws.cell(row=summary_start + 1 + i, column=1, value=label).font = Font(bold=True)
        ws.cell(row=summary_start + 1 + i, column=2, value=value)

    # Auto-width columns
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                max_len = max(max_len, len(str(cell.value or "")))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 2, 60)

    # Second sheet: improvement signals
    ws2 = wb.create_sheet("improvement_signals")
    sig_headers = ["pattern", "actual_failed_step", "frequency"]
    for col_idx, h in enumerate(sig_headers, 1):
        cell = ws2.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill

    for row_idx, sig in enumerate(improvement_signals, 2):
        for col_idx, h in enumerate(sig_headers, 1):
            ws2.cell(row=row_idx, column=col_idx, value=sig[h])

    for col in ws2.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws2.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 2, 50)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    wb.save(output_path)
    print(f"\nReport saved to: {output_path}")


def main():
    rows = run_evaluation()

    if not rows:
        print("\nNo results to report.")
        return

    summary = compute_summary(rows)
    improvement_signals = compute_improvement_signals(rows)

    print("\n" + "="*60)
    print("EVALUATION SUMMARY")
    print("="*60)
    for k, v in summary.items():
        print(f"  {k}: {v}")

    print("\nImprovement signals (wrong predictions by pattern):")
    for sig in improvement_signals[:10]:
        print(f"  pattern={sig['pattern']}  step={sig['actual_failed_step']}  freq={sig['frequency']}")

    write_excel(rows, summary, improvement_signals, REPORT_PATH)


if __name__ == "__main__":
    os.chdir(PROJECT_ROOT)
    main()
