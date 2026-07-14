#!/usr/bin/env python3
"""
Export ML dataset JSONL files to a flat Parquet (or CSV) file for model training.

Usage:
  python scripts/export_ml_dataset.py --program-id 19905
  python scripts/export_ml_dataset.py --all
  python scripts/export_ml_dataset.py --all --output data/ml_exports/all.parquet
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from analysis.ml_dataset_store import load_all
from analysis.paths import ml_dataset_dir, ml_exports_dir

ID_COLS = {
    "id", "program_id", "tenant_id", "execution_id", "commit_sha",
    "observed_at", "resolved_at", "deploy_start_time", "commit_author", "commit_date",
}
LABEL_COLS = {"status", "actual_status", "actual_failed_step", "is_failure"}
LIST_COLS = {"modules_touched"}


def _expand_modules(df: pd.DataFrame) -> pd.DataFrame:
    if "modules_touched" not in df.columns:
        return df
    all_modules = set()
    for val in df["modules_touched"].dropna():
        if isinstance(val, list):
            all_modules.update(val)
    for mod in sorted(all_modules):
        col = f"module_{mod.replace('.', '_')}"
        df[col] = df["modules_touched"].apply(
            lambda x, m=mod: m in x if isinstance(x, list) else False
        )
    return df.drop(columns=["modules_touched"], errors="ignore")


def _one_hot_categoricals(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        if col not in df.columns:
            continue
        dummies = pd.get_dummies(df[col].fillna("").astype(str), prefix=col, dtype=int)
        df = pd.concat([df.drop(columns=[col]), dummies], axis=1)
    return df


def export_dataset(
    program_id: str = "",
    output: Path | None = None,
    include_pending: bool = False,
    all_tenants: bool = False,
) -> Path:
    if all_tenants:
        records = load_all()
        default_name = "all_tenants"
    elif program_id:
        records = load_all(program_id)
        default_name = program_id
    else:
        raise ValueError("Specify --program-id or --all")

    if not include_pending:
        records = [r for r in records if r.get("status") == "RESOLVED"]

    if not records:
        raise SystemExit("No records to export.")

    df = pd.DataFrame(records)
    df = _expand_modules(df)
    df = _one_hot_categoricals(df, ["env_status", "env_dominant_step"])

    # bool/int for training
    for col in df.columns:
        if col in LIST_COLS:
            continue
        if df[col].dtype == object:
            sample = df[col].dropna().head(1)
            if len(sample) and isinstance(sample.iloc[0], bool):
                df[col] = df[col].astype("boolean")

    out_dir = ml_exports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = output or (out_dir / f"{default_name}.parquet")

    try:
        df.to_parquet(out_path, index=False)
    except Exception:
        out_path = out_path.with_suffix(".csv")
        df.to_csv(out_path, index=False)

    resolved_n = len(df)
    fail_n = int(df["is_failure"].sum()) if "is_failure" in df.columns else 0
    feature_cols = [
        c for c in df.columns
        if c not in ID_COLS and c not in LABEL_COLS
    ]

    print(f"Exported {resolved_n} rows → {out_path}")
    print(f"Features: {len(feature_cols)} columns")
    if resolved_n:
        print(f"Failure rate: {fail_n}/{resolved_n} ({round(fail_n/resolved_n*100, 1)}%)")
    print(f"Label columns: {sorted(LABEL_COLS & set(df.columns))}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export ML dataset JSONL to Parquet/CSV")
    parser.add_argument("--program-id", help="Tenant program_id (e.g. 19905)")
    parser.add_argument("--all", action="store_true", help="Export all tenants")
    parser.add_argument("--output", type=Path, help="Output file path")
    parser.add_argument(
        "--include-pending",
        action="store_true",
        help="Include unresolved PENDING rows (no labels yet)",
    )
    args = parser.parse_args()

    if not args.all and not args.program_id:
        parser.error("Specify --program-id or --all")

    store = ml_dataset_dir()
    if not store.exists() or not any(store.glob("*.jsonl")):
        print(f"No ML dataset files found in {store}")
        sys.exit(1)

    export_dataset(
        program_id=args.program_id or "",
        output=args.output,
        include_pending=args.include_pending,
        all_tenants=args.all,
    )


if __name__ == "__main__":
    main()
