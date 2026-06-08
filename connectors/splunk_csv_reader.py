import pandas as pd
import os


def load_pipeline_list(csv_path: str) -> pd.DataFrame:
    """
    Load the pipeline execution list CSV exported from Splunk.
    Returns a cleaned dataframe with all executions.
    """
    df = pd.read_csv(csv_path)
    df.columns = df.columns.str.strip()

    # Keep only the columns we care about
    columns = [
        "Deploy Start Time", "End Time", "Duration (Min)",
        "programId", "pipelineId", "pipelineName",
        "Execution", "Status"
    ]
    df = df[[c for c in columns if c in df.columns]]
    df = df.rename(columns={"Execution": "executionId"})
    df["executionId"] = df["executionId"].astype(str)
    df["Duration (Min)"] = pd.to_numeric(df["Duration (Min)"], errors="coerce")
    return df


def load_failed_steps(csv_path: str) -> pd.DataFrame:
    """
    Load the firstFailedStep CSV exported from Splunk.
    Returns a dataframe mapping executionId → firstFailedStep.
    """
    df = pd.read_csv(csv_path)
    df.columns = df.columns.str.strip()
    df["executionId"] = df["executionId"].astype(str)

    # Drop malformed rows where executionId is not numeric
    df = df[df["executionId"].str.match(r"^\d+$")]
    return df


def get_failed_executions(pipeline_df: pd.DataFrame, failed_steps_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge pipeline list with failed steps to get a single table
    of all failed executions with their firstFailedStep.
    """
    failed_statuses = ["FAILED", "ERROR", "CANCELLED"]
    failed = pipeline_df[pipeline_df["Status"].isin(failed_statuses)].copy()
    merged = failed.merge(failed_steps_df[["executionId", "firstFailedStep"]], on="executionId", how="left")
    return merged


def load_share_names(csv_path: str) -> dict:
    """
    Load the share-names CSV from Splunk.
    Returns a dict mapping executionId → Azure share name (UUID).
    Only includes executions that have a share name.
    """
    df = pd.read_csv(csv_path)
    df.columns = df.columns.str.strip()
    df["executionId"] = df["executionId"].astype(str)
    df = df[df["executionId"].str.match(r"^\d+$")]
    df = df[df["shareName"].notna()]
    return dict(zip(df["executionId"], df["shareName"]))


def build_failed_share_map(share_names: dict, failed_steps_df: pd.DataFrame) -> dict:
    """
    Filter the full share name map down to only failed executions.
    Returns dict of executionId → shareName for failed executions only.
    """
    failed_ids = set(failed_steps_df["executionId"].astype(str))
    filtered = {eid: sname for eid, sname in share_names.items() if eid in failed_ids}

    missing = failed_ids - set(filtered.keys())
    if missing:
        print(f"  WARNING: No share name found for {len(missing)} executions: {missing}")

    return filtered


def summarize_failures(merged_df: pd.DataFrame) -> dict:
    """
    Produce a summary of failure counts grouped by firstFailedStep and pipeline.
    """
    summary = (
        merged_df.groupby(["pipelineName", "firstFailedStep", "Status"])
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )
    return summary.to_dict(orient="records")
