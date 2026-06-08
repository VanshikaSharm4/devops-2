"""
Splunk REST API connector.
Uses the streaming export endpoint (/search/jobs/export) — results stream
back immediately without the create-job → poll → fetch cycle that times out
on large queries.
"""

import json
import os
import pandas as pd
import requests
import urllib3
from requests.auth import HTTPBasicAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL   = "https://splunk-api.or1.adobe.net"
EXPORT_URL = f"{BASE_URL}/servicesNS/admin/TA-AMS_ui/search/jobs/export"
INDEX      = "ams_linux-os"
SOURCETYPE = "ssg-summit-prod"
EARLIEST   = "-30d"
LATEST     = "now"


def _auth() -> HTTPBasicAuth:
    username = os.getenv("SPLUNK_USERNAME")
    password = os.getenv("SPLUNK_PASSWORD")
    if not username or not password:
        raise ValueError("SPLUNK_USERNAME and SPLUNK_PASSWORD must be set in .env")
    return HTTPBasicAuth(username, password)


def _stream_query(spl: str, earliest: str = EARLIEST, timeout: int = 300) -> pd.DataFrame:
    """
    Run a Splunk search via the streaming export endpoint.
    Results arrive as newline-delimited JSON — no polling needed.
    """
    response = requests.post(
        EXPORT_URL,
        auth=_auth(),
        data={
            "search": spl,
            "output_mode": "json",
            "earliest_time": earliest,
            "latest_time": LATEST,
        },
        verify=False,
        stream=True,
        timeout=timeout,
    )
    response.raise_for_status()

    rows = []
    for line in response.iter_lines():
        if not line:
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
            if "result" in parsed:
                rows.append(parsed["result"])
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df.columns = df.columns.str.strip()
    return df


# ── Query 1: Pipeline executions list ────────────────────────────────────────

def fetch_pipeline_list(program_id: int = 19905) -> pd.DataFrame:
    """Replaces: data/splunk_exports/pipelines-list.csv"""
    print("  [Splunk] Fetching pipeline executions...")

    spl = f"""search index="{INDEX}" sourcetype="{SOURCETYPE}" TERM({program_id})
        (CASE("pipeline_execution_start") OR CASE("pipeline_execution_end"))
        (logger_name="com.adobe.platform.experience.selfservice.services.AdobeIOEventService"
         OR logger_name="c.a.p.e.s.s.AdobeIOEventService")
        | rex field=_raw "program/(?<programId>[0-9]+)/pipeline/(?<pipelineId>[0-9]+)/execution/(?<executionId>[0-9]+)"
        | rex field=_raw "(?<hasEnded>PipelineExecutionEndedEvent)"
        | search programId={program_id}
        | stats min(_time) as minStartedTime, max(_time) as maxEndTime,
                min(status) as execStatus,
                values(pipelineName) as pipelineName,
                values(hasEnded) as hasEnded
          by executionId, pipelineId, programId
        | eval Status=if(isnull(execStatus) OR match(execStatus,"STARTED"),
                         if(isnotnull(hasEnded),"ERROR","RUNNING"), execStatus)
        | eval "Deploy Start Time"=strftime(minStartedTime, "%Y-%m-%dT%H:%M:%S %Z")
        | eval "End Time"=strftime(maxEndTime, "%Y-%m-%dT%H:%M:%S %Z")
        | eval "Duration (Min)"=round(if(match(Status,"RUNNING"),
                                    now()-minStartedTime,
                                    maxEndTime-minStartedTime)/60, 2)
        | eval pipelineName=mvindex(pipelineName,0)
        | sort 0 -minStartedTime
        | table "Deploy Start Time", "End Time", "Duration (Min)",
                programId, pipelineId, pipelineName, executionId, Status"""

    df = _stream_query(spl)
    if df.empty:
        raise RuntimeError("No pipeline data returned from Splunk")

    df["executionId"] = df["executionId"].astype(str)
    df["Duration (Min)"] = pd.to_numeric(df["Duration (Min)"], errors="coerce")
    print(f"  [Splunk] Got {len(df)} executions")
    return df


# ── Query 2: First failed step per execution ──────────────────────────────────

def fetch_failed_steps(program_id: int = 19905) -> pd.DataFrame:
    """Replaces: data/splunk_exports/first-failed-steps.csv"""
    print("  [Splunk] Fetching failed steps...")

    spl = f"""search index="{INDEX}" sourcetype="{SOURCETYPE}" TERM({program_id}) firstFailedStep=*
        | rex field=_raw "program/(?<programId>[0-9]+)/pipeline/(?<pipelineId>[0-9]+)/execution/(?<executionId>[0-9]+)"
        | eval executionId=mvindex(executionId, 0)
        | search programId={program_id}
        | dedup executionId
        | eval status=if(isnull(status) OR match(status,"STARTED"),"RUNNING",status)
        | where isnotnull(firstFailedStep) AND firstFailedStep!=""
        | table executionId, firstFailedStep, status"""

    df = _stream_query(spl)
    if df.empty:
        return pd.DataFrame(columns=["executionId", "firstFailedStep", "status"])

    df["executionId"] = df["executionId"].astype(str).str.split("\n").str[0].str.strip()
    df = df[df["executionId"].str.match(r"^\d+$")]
    print(f"  [Splunk] Got {len(df)} failed step records")
    return df


# ── Query 3: Azure share names ────────────────────────────────────────────────

def fetch_share_names(program_id: int = 19905) -> dict:
    """Replaces: data/splunk_exports/share-names.csv"""
    print("  [Splunk] Fetching Azure share names...")

    spl = f"""search index="{INDEX}" sourcetype="{SOURCETYPE}" TERM({program_id}) shareName=*
        | rex field=_raw "program/(?<programId>[0-9]+)/pipeline/(?<pipelineId>[0-9]+)/execution/(?<executionId>[0-9]+)"
        | eval executionId=mvindex(executionId, 0)
        | search programId={program_id}
        | where isnotnull(shareName) AND shareName!=""
        | stats first(shareName) as shareName by executionId
        | table executionId, shareName"""

    df = _stream_query(spl, earliest="-15d")
    if df.empty:
        print("  [Splunk] WARNING: No share names returned")
        return {}

    df["executionId"] = df["executionId"].astype(str).str.split("\n").str[0].str.strip()
    df = df[df["executionId"].str.match(r"^\d+$")]
    df = df[df["shareName"].notna()]

    result = dict(zip(df["executionId"], df["shareName"]))
    print(f"  [Splunk] Got {len(result)} share name mappings")
    return result


# ── Test connection ───────────────────────────────────────────────────────────

def test_connection() -> bool:
    r = requests.get(
        f"{BASE_URL}/services/authentication/current-context",
        auth=_auth(),
        params={"output_mode": "json"},
        verify=False,
        timeout=10,
    )
    return r.status_code == 200
