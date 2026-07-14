"""
TenantContext — per-request tenant isolation.

Replaces os.environ["PROGRAM_ID"] global mutation with an explicit
context object passed through the analysis pipeline.

Critical for multi-user deployments: Streamlit shares one Python process
across all browser sessions. os.environ writes from one user's session
overwrite another user's in-flight analysis.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TenantContext:
    customer_name:  str
    program_id:     str
    tenant_id:      str
    git_local_dir:  str
    git_url:        str
    git_branch:     str
    git_username:   str
    git_password:   str
    pipeline_prod:  str
    pipeline_dev:   str
    splunk_index:   str = "ams_linux-os"
    short:          str = ""

    @classmethod
    def from_customer_dict(cls, name: str, cfg: dict) -> "TenantContext":
        """Build from the _CUSTOMERS dict entry."""
        return cls(
            customer_name = name,
            program_id    = str(cfg.get("program_id") or ""),
            tenant_id     = cfg.get("tenant_id") or (name.lower().split()[0] if name.strip() else ""),
            git_local_dir = cfg.get("git_local_dir") or "",
            git_url       = cfg.get("git_url") or "",
            git_branch    = cfg.get("git_branch") or "master",
            git_username  = cfg.get("git_username") or "",
            git_password  = cfg.get("git_password") or "",
            pipeline_prod = str(cfg.get("pipeline_prod") or ""),
            pipeline_dev  = str(cfg.get("pipeline_dev") or ""),
            splunk_index  = cfg.get("splunk_index") or "ams_linux-os",
            short         = cfg.get("short") or name[:4].upper(),
        )

    def apply_to_env(self) -> None:
        """
        Write to os.environ for code that still reads from env.
        Call this at the START of a request, not globally.
        In a future async refactor this becomes thread-local storage.
        """
        if self.program_id:
            os.environ["PROGRAM_ID"]       = self.program_id
        if self.pipeline_prod:
            os.environ["PIPELINE_ID_PROD"] = self.pipeline_prod
        if self.pipeline_dev:
            os.environ["PIPELINE_ID_DEV"]  = self.pipeline_dev
        if self.git_url:
            os.environ["CM_GIT_REPO_URL"]  = self.git_url   # ← was missing: caused wrong-repo fetches
        if self.git_local_dir:
            os.environ["GIT_LOCAL_DIR"]    = self.git_local_dir
        if self.git_branch:
            os.environ["GIT_BRANCH"]       = self.git_branch
        if self.git_username:
            os.environ["CM_GIT_USERNAME"]  = self.git_username
        if self.git_password:
            os.environ["CM_GIT_PASSWORD"]  = self.git_password

    def cache_key(self) -> str:
        """Unique key for Splunk/assessment caches scoped to this tenant."""
        return self.program_id or self.tenant_id
