"""
Per-user Splunk LDAP credential vault (encrypted at rest).

Passwords are stored under data/.splunk_vault/ — separate from data/.secrets.json
which holds per-customer git passwords in plaintext.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from analysis.paths import splunk_vault_dir

_VAULT_SESSION_KEY = "splunk_ldap_short"


def normalize_splunk_username(short: str) -> str:
    """
    Splunk HTTP Basic auth expects the bare Adobe LDAP short name (e.g.
    'vanssharma'), NOT the full email. If the user pastes the full email we
    strip the domain. Confirmed against the export endpoint: 'vanssharma' → 200,
    'vanssharma@adobe.com' → 401.
    """
    short = (short or "").strip().lower()
    if not short:
        return ""
    if "@" in short:
        return short.split("@", 1)[0]
    return short


def ldap_short_from_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" in email:
        return email.split("@", 1)[0]
    return email


def _fernet():
    from cryptography.fernet import Fernet

    key = os.getenv("ARGUS_CREDENTIALS_KEY", "").strip()
    if not key:
        raise ValueError(
            "ARGUS_CREDENTIALS_KEY is not set in .env. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def _vault_file(email: str) -> Path:
    normalized = normalize_splunk_username(email)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return splunk_vault_dir() / f"{digest}.json"


def list_saved_ldap_shorts() -> list[str]:
    """All LDAP short IDs with vault entries."""
    root = splunk_vault_dir()
    if not root.exists():
        return []
    shorts = []
    for path in root.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            s = data.get("ldap_short", "")
            if s:
                shorts.append(s)
        except Exception:
            continue
    return sorted(shorts)


def save_credentials(short_username: str, password: str) -> str:
    """Validate, encrypt, and persist. Returns normalized email."""
    email = normalize_splunk_username(short_username)
    if not email or not password:
        raise ValueError("LDAP ID and password are required")

    ok, reason = check_splunk_credentials(email, password)
    if not ok:
        raise ValueError(reason)

    splunk_vault_dir().mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(splunk_vault_dir(), 0o700)
    except OSError:
        pass

    payload = {
        "ldap_short": ldap_short_from_email(email),
        "ldap_email": email,
        "password_enc": _fernet().encrypt(password.encode("utf-8")).decode("ascii"),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    _vault_file(email).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(_vault_file(email), 0o600)
    except OSError:
        pass
    return email


def load_credentials(short_or_email: str) -> Optional[Tuple[str, str]]:
    """Return (normalized_email, password) or None."""
    email = normalize_splunk_username(short_or_email)
    path = _vault_file(email)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        password = _fernet().decrypt(data["password_enc"].encode("ascii")).decode("utf-8")
        return data.get("ldap_email", email), password
    except Exception:
        return None


def delete_credentials(short_or_email: str) -> None:
    email = normalize_splunk_username(short_or_email)
    path = _vault_file(email)
    if path.exists():
        path.unlink()


def has_vault_entry(short_or_email: str) -> bool:
    return _vault_file(normalize_splunk_username(short_or_email)).exists()


def check_splunk_credentials(username: str, password: str) -> Tuple[bool, str]:
    """
    Validate credentials by exercising the real connector path
    (connectors.splunk_connector.test_connection → export endpoint via _auth()).
    We temporarily set the candidate creds into the Splunk context so the
    connector authenticates exactly as it will in production, then restore the
    previous context. Returns (ok, reason).
    """
    import requests

    email = normalize_splunk_username(username)
    if not email or not password:
        return False, "LDAP ID and password are required"

    from analysis.customer_context import (
        clear_splunk_context,
        get_splunk_password,
        get_splunk_username,
        has_splunk_credentials,
        set_splunk_context,
    )
    from connectors.splunk_connector import test_connection

    had_prev = has_splunk_credentials()
    prev_user, prev_pass = get_splunk_username(), get_splunk_password()
    set_splunk_context(email, password)
    try:
        if test_connection():
            return True, "ok"
        return False, (
            "Splunk rejected the credentials. Enter your LDAP short name "
            "without @adobe.com (e.g. 'vanssharma') and your Adobe LDAP password."
        )
    except requests.exceptions.RequestException as e:
        return False, f"Could not reach Splunk ({type(e).__name__}): {e}"
    finally:
        if had_prev:
            set_splunk_context(prev_user, prev_pass)
        else:
            clear_splunk_context()


def validate_splunk_credentials(username: str, password: str) -> bool:
    """Backwards-compatible boolean wrapper around check_splunk_credentials."""
    ok, _ = check_splunk_credentials(username, password)
    return ok


def restore_splunk_session(session_state: dict, query_params, cookie_short: str = "") -> bool:
    """
    Load vault creds into customer_context for this Streamlit session.
    Returns True if Splunk context is active.

    Identity is recovered, in priority order, from: session_state (same
    session), the ?splunk_user query param, then a durable browser cookie
    (cookie_short) — the last is what lets a user stay logged in across app
    restarts and fresh browser sessions on the Eris deployment.
    """
    from analysis.customer_context import has_splunk_credentials, set_splunk_context

    if has_splunk_credentials():
        return True

    short = (session_state.get(_VAULT_SESSION_KEY) or "").strip().lower()
    if not short:
        short = (query_params.get("splunk_user") or "").strip().lower()
    if not short:
        short = (cookie_short or "").strip().lower()

    if short:
        loaded = load_credentials(short)
        if loaded:
            email, password = loaded
            set_splunk_context(email, password)
            session_state[_VAULT_SESSION_KEY] = ldap_short_from_email(email)
            return True

    return False


def activate_splunk_session(session_state: dict, query_params, short_username: str, password: str) -> str:
    """Save to vault, set context, persist session keys. Returns ldap short id."""
    email = save_credentials(short_username, password)
    from analysis.customer_context import set_splunk_context

    set_splunk_context(email, password)
    short = ldap_short_from_email(email)
    session_state[_VAULT_SESSION_KEY] = short
    try:
        query_params["splunk_user"] = short
    except Exception:
        pass
    return short


def clear_splunk_session(session_state: dict, query_params, short_username: str = "") -> None:
    from analysis.customer_context import clear_splunk_context

    short = (short_username or session_state.get(_VAULT_SESSION_KEY, "")).strip().lower()
    if short:
        delete_credentials(short)
    clear_splunk_context()
    session_state.pop(_VAULT_SESSION_KEY, None)
    try:
        if "splunk_user" in query_params:
            del query_params["splunk_user"]
    except Exception:
        pass


def session_ldap_short(session_state: dict) -> str:
    return (session_state.get(_VAULT_SESSION_KEY) or "").strip().lower()
