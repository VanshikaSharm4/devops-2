"""
Ingest resolved Splunk failures into ChromaDB failure_memory.

For each resolved prediction (RESOLVED, actual_status=FAILED/ERROR):
- Load the execution's Splunk data
- Fetch Azure logs if available
- Parse key_lines and error_type from logs
- Get commit diff (changed_files) from git
- Upsert to vector_store with tenant_id + program_id metadata

Run periodically to keep ChromaDB up to date with real failure patterns.
Usage: python3 scripts/ingest_resolved_failures.py
"""
import os, sys, json
sys.path.insert(0, '/Users/vanshika/projects/devops-agent-2')

from analysis.prediction_store import load_resolved
from vector_store.store import store_failure

CUSTOMERS = {
    "19905": {"tenant_id": "idfc",     "git_dir": "/Users/vanshika/Downloads/idfc",           "branch": "master"},
    "16360": {"tenant_id": "hdfc",     "git_dir": "/Users/vanshika/projects/hdfcbankformsmaster", "branch": "stage_and_prod"},
    "465":   {"tenant_id": "malaysia", "git_dir": "/Users/vanshika/projects/malaysiaairlines",  "branch": "master"},
}

def ingest_resolved():
    total = 0
    for prog_id, cfg in CUSTOMERS.items():
        resolved_failures = [
            r for r in load_resolved(prog_id)
            if r.get("actual_status") in ("FAILED", "ERROR")
            and r.get("actual_failed_step")
            and r.get("commit_sha")
        ]
        print(f"{cfg['tenant_id']}: {len(resolved_failures)} resolved failures to ingest")

        os.environ["GIT_LOCAL_DIR"] = cfg["git_dir"]

        for pred in resolved_failures:
            sha        = pred["commit_sha"]
            step       = pred.get("actual_failed_step", "")
            tenant_id  = cfg["tenant_id"]
            eid        = pred.get("execution_id", "")

            try:
                # Get changed files from git
                from connectors.git_connector import get_commit_diff
                diff_data = get_commit_diff(cfg["git_dir"], sha)
                changed_files = diff_data.get("changed_files", [])
                author = diff_data.get("author", "")
                title  = diff_data.get("title", "")

                # Build error message from top_factors (what we know)
                top_factors = pred.get("top_factors", [])
                error_msg = f"Pipeline {pred['actual_status']} at {step}. " + " | ".join(top_factors[:3])

                # Upsert into vector store
                store_failure(
                    execution_id=eid or f"pred:{pred['id']}",
                    step=step,
                    error_type=step,
                    error_message=error_msg,
                    key_lines=top_factors[:5],
                    root_cause=f"Pipeline failed at {step}. Commit: {title[:100]}",
                    fix=f"Review {step} configuration before deploying",
                    pipeline="Production Pipeline",
                    extra_meta={
                        "changed_files":   json.dumps(changed_files[:10]),
                        "modules_touched": json.dumps(pred.get("modules_at_risk", [])),
                        "tenant_id":       tenant_id,
                        "program_id":      prog_id,
                        "environment":     "prod",
                        "source":          "resolved_prediction",
                        "commit_sha":      sha,
                        "author":          author,
                    },
                )
                total += 1
                print(f"  Ingested: {sha[:10]} → {step} ({tenant_id})")

            except Exception as e:
                print(f"  Skipped {sha[:10]}: {e}")

    print(f"\nTotal ingested: {total}")

if __name__ == "__main__":
    ingest_resolved()
