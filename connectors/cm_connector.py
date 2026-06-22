"""Cloud Manager REST API — thin client for devops-agent-2."""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

load_dotenv()

CM_BASE   = "https://cloudmanager.adobe.io"
LOG_REL   = "http://ns.adobe.com/adobecloud/rel/pipeline/logs"
IMS_TOKEN = "https://ims-na1.adobelogin.com/ims/token/v3"
IMS_SCOPE = "openid,AdobeID,read_organizations,additional_info.projectedProductContext,read_pc.dma_aem_ams"

# tenant_id → (token_env, key_env, secret_env, org_id)
TENANT_ENV = {
    "idfc":    ("IDFC_TOKEN",   "IDFC_KEY",   "IDFC_CLIENT_SECRET",   "358458CC558C6B5D7F000101@AdobeOrg"),
    "hdfc":    ("HDFC_TOKEN",   "HDFC_KEY",   "HDFC_CLIENT_SECRET",   "3817033753EE89720A490D4D@AdobeOrg"),
    "apollo":  ("APOLLO_TOKEN", "APOLLO_KEY", "APOLLO_CLIENT_SECRET", "DF73235C5CDDA67A0A495C2A@AdobeOrg"),
    "malaysia":("MALASIA_TOKEN","MALASIA_KEY","MALASIA_CLIENT_SECRET","4D9676A8531512ED0A490D44@AdobeOrg"),
}

DEFAULT_TENANT = os.getenv("ML_TENANT_ID", "idfc")

# In-memory token cache: tenant_id → {"token": str, "expires_at": float}
_token_cache: Dict[str, Dict] = {}


def _is_token_expired(token: str) -> bool:
    """Decode JWT payload to check expiry. Returns True if expired or within 5 min."""
    try:
        payload_b64 = token.split(".")[1]
        # Add padding if needed
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        created_at  = int(payload.get("created_at", 0)) / 1000
        expires_in  = int(payload.get("expires_in", 0)) / 1000
        expires_at  = created_at + expires_in
        return time.time() > (expires_at - 300)  # refresh 5 min before expiry
    except Exception:
        return True  # if we can't decode, assume expired


def _refresh_token(tenant_id: str) -> Optional[str]:
    """
    Generate a fresh Bearer token using client credentials.
    Requires {TENANT}_CLIENT_SECRET in .env.
    Returns the new token or None if credentials are missing.
    """
    cfg = TENANT_ENV.get(tenant_id, TENANT_ENV["idfc"])
    token_env, key_env, secret_env = cfg[0], cfg[1], cfg[2]

    client_id     = os.getenv(key_env, "")
    client_secret = os.getenv(secret_env, "")

    if not client_id or not client_secret:
        return None  # no secret configured — caller must use manual token

    try:
        r = requests.post(
            IMS_TOKEN,
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
                "scope":         IMS_SCOPE,
            },
            timeout=15,
        )
        r.raise_for_status()
        new_token = r.json().get("access_token", "")
        if new_token:
            # Update env so rest of app sees it
            os.environ[token_env] = new_token
            print(f"  [CM] Refreshed token for {tenant_id}")
        return new_token or None
    except Exception as e:
        print(f"  [CM] Token refresh failed for {tenant_id}: {e}")
        return None


def _get_token(tenant_id: str) -> str:
    """
    Return a valid token for the tenant.
    Auto-refreshes if expired and client_secret is configured.
    Falls back to the manually set token in .env.
    """
    cfg = TENANT_ENV.get(tenant_id, TENANT_ENV["idfc"])
    token_env = cfg[0]
    current_token = os.getenv(token_env, "")

    if current_token and not _is_token_expired(current_token):
        return current_token

    # Token expired or missing — try to refresh
    new_token = _refresh_token(tenant_id)
    return new_token or current_token  # fall back to expired token if no secret


def _headers(tenant_id: str = DEFAULT_TENANT) -> Dict[str, str]:
    cfg = TENANT_ENV.get(tenant_id, TENANT_ENV["idfc"])
    token_env, key_env, secret_env, org_id = cfg
    token = _get_token(tenant_id)
    key   = os.getenv(key_env, os.getenv("IDFC_KEY", ""))
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key":     key,
        "x-gw-ims-org-id": org_id,
    }


def _resolve_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urljoin(CM_BASE, href)


def get_execution(
    program_id: str,
    pipeline_id: str,
    execution_id: str,
    tenant_id: str = DEFAULT_TENANT,
    timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    url = f"{CM_BASE}/api/program/{program_id}/pipeline/{pipeline_id}/execution/{execution_id}"
    r = requests.get(url, headers=_headers(tenant_id), timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def get_step_log_href(step: Dict[str, Any]) -> str:
    links = step.get("_links") or {}
    log_link = links.get(LOG_REL) or {}
    return log_link.get("href", "") or ""


def fetch_step_log(
    log_href: str,
    tenant_id: str = DEFAULT_TENANT,
    timeout: int = 120,
) -> str:
    """Fetch log text; follows CM redirect to pre-signed URL (like curl -L)."""
    url = _resolve_url(log_href)
    r = requests.get(
        url,
        headers=_headers(tenant_id),
        timeout=timeout,
        allow_redirects=True,
    )
    r.raise_for_status()
    return r.text


def fetch_log_for_step_action(
    program_id: str,
    pipeline_id: str,
    execution_id: str,
    action: str,
    tenant_id: str = DEFAULT_TENANT,
) -> Optional[str]:
    payload = get_execution(program_id, pipeline_id, execution_id, tenant_id=tenant_id)
    if not payload:
        return None
    steps = (payload.get("_embedded") or {}).get("stepStates") or []
    for step in steps:
        if step.get("action") != action:
            continue
        href = get_step_log_href(step)
        if not href:
            return None
        return fetch_step_log(href, tenant_id=tenant_id)
    return None


def get_commit_sha_from_execution(
    program_id: str,
    pipeline_id: str,
    execution_id: str,
    tenant_id: str = DEFAULT_TENANT,
) -> Optional[str]:
    payload = get_execution(program_id, pipeline_id, execution_id, tenant_id=tenant_id)
    if not payload:
        return None
    steps = (payload.get("_embedded") or {}).get("stepStates") or []
    for step in steps:
        sha = step.get("commitId")
        if sha:
            return str(sha)
        details = step.get("details") or {}
        env = details.get("buildEnvironmentDetails") or {}
        sha = env.get("gitCommitSha")
        if sha:
            return str(sha)
    return None


def parse_step_states(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    steps = (payload.get("_embedded") or {}).get("stepStates") or []
    return [
        {
            "action": s.get("action", ""),
            "status": s.get("status", ""),
            "log_href": get_step_log_href(s),
        }
        for s in steps
    ]
