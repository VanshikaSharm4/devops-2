"""
Thread/context-safe customer state using Python contextvars.

Replaces os.environ reads for customer-specific values so 20 concurrent
Streamlit users don't overwrite each other's GIT_LOCAL_DIR, PROGRAM_ID, etc.

Each Streamlit session calls set_customer_context() at the top of every rerun.
When analysis spawns a thread via ThreadPoolExecutor, the caller uses
contextvars.copy_context().run() to give the thread its own isolated snapshot
of these values — completely independent of every other thread.
"""
from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Dict

_git_local_dir = ContextVar("git_local_dir", default="")
_program_id    = ContextVar("program_id",    default="")
_git_url       = ContextVar("git_url",       default="")
_git_username  = ContextVar("git_username",  default="")
_git_password  = ContextVar("git_password",  default="")
_git_branch    = ContextVar("git_branch",    default="master")
_pipeline_prod = ContextVar("pipeline_prod", default="")
_pipeline_dev  = ContextVar("pipeline_dev",  default="")
_tenant_id     = ContextVar("tenant_id",     default="")
_splunk_username = ContextVar("splunk_username", default="")
_splunk_password = ContextVar("splunk_password", default="")


def set_customer_context(customer: Dict) -> None:
    """
    Set all customer-specific context vars for the current Streamlit session.
    Call this at the top of app.py on every rerun (after _active_customer is resolved).
    Falls back to os.environ for backward compat when values are missing.
    """
    _git_local_dir.set(customer.get("git_local_dir", "")  or os.getenv("GIT_LOCAL_DIR", ""))
    _program_id.set(str(customer.get("program_id", ""))   or os.getenv("PROGRAM_ID", ""))
    _git_url.set(customer.get("git_url", "")              or os.getenv("CM_GIT_REPO_URL", ""))
    _git_username.set(customer.get("git_username", "")    or os.getenv("CM_GIT_USERNAME", ""))
    _git_password.set(customer.get("git_password", "")    or os.getenv("CM_GIT_PASSWORD", ""))
    _git_branch.set(customer.get("git_branch", "master")  or os.getenv("GIT_BRANCH", "master"))
    _pipeline_prod.set(str(customer.get("pipeline_prod", "") or os.getenv("PIPELINE_ID_PROD", "")))
    _pipeline_dev.set(str(customer.get("pipeline_dev", "")   or os.getenv("PIPELINE_ID_DEV", "")))
    _tenant_id.set(customer.get("tenant_id", "")          or "")


# ── Getters — use these everywhere instead of os.getenv() ────────────────────
# Each getter falls back to os.environ so existing code that hasn't been
# migrated yet still works correctly.

def get_git_local_dir() -> str:
    return _git_local_dir.get() or os.getenv("GIT_LOCAL_DIR", "")

def get_program_id() -> str:
    return _program_id.get() or os.getenv("PROGRAM_ID", "")

def get_git_url() -> str:
    return _git_url.get() or os.getenv("CM_GIT_REPO_URL", "")

def get_git_username() -> str:
    return _git_username.get() or os.getenv("CM_GIT_USERNAME", "")

def get_git_password() -> str:
    return _git_password.get() or os.getenv("CM_GIT_PASSWORD", "")

def get_git_branch() -> str:
    return _git_branch.get() or os.getenv("GIT_BRANCH", "master")

def get_pipeline_prod() -> str:
    return _pipeline_prod.get() or os.getenv("PIPELINE_ID_PROD", "")

def get_pipeline_dev() -> str:
    return _pipeline_dev.get() or os.getenv("PIPELINE_ID_DEV", "")

def get_tenant_id() -> str:
    return _tenant_id.get() or ""


def set_splunk_context(username: str, password: str) -> None:
    """Set per-session Splunk LDAP credentials (full email + password)."""
    from analysis.splunk_credentials import normalize_splunk_username

    _splunk_username.set(normalize_splunk_username(username))
    _splunk_password.set(password or "")


def clear_splunk_context() -> None:
    _splunk_username.set("")
    _splunk_password.set("")


def get_splunk_username() -> str:
    return _splunk_username.get() or ""


def get_splunk_password() -> str:
    return _splunk_password.get() or ""


def has_splunk_credentials() -> bool:
    return bool(get_splunk_username() and get_splunk_password())
