"""HTTP client for devops-risk-ml scoring service."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

ML_SERVICE_URL = os.getenv("ML_SERVICE_URL", "http://127.0.0.1:8090").rstrip("/")

TENANT_PROGRAM = {
    "idfc": ("19905", "2357452"),
    "hdfc": ("16360", "43468192"),
    "apollo": ("178453", "1601852"),
}


def predict_risk(
    commit_sha: str,
    tenant_id: Optional[str] = None,
    dev_execution_id: Optional[str] = None,
    git_features: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
) -> Optional[Dict[str, Any]]:
    tenant_id = tenant_id or os.getenv("ML_TENANT_ID", "idfc")
    program_id, pipeline_prod = TENANT_PROGRAM.get(tenant_id, TENANT_PROGRAM["idfc"])

    body = {
        "tenant_id": tenant_id,
        "program_id": program_id,
        "pipeline_id_prod": pipeline_prod,
        "commit_sha": commit_sha,
        "dev_execution_id": dev_execution_id,
        "git_features": git_features,
    }
    try:
        r = requests.post(
            f"{ML_SERVICE_URL}/v1/predict",
            json=body,
            timeout=timeout,
        )
        if r.status_code == 503:
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def ml_service_health() -> Dict[str, Any]:
    try:
        r = requests.get(f"{ML_SERVICE_URL}/v1/health", timeout=3)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"status": "unavailable", "error": str(e)}
