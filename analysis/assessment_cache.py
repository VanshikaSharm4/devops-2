"""
Assessment cache — avoids redundant LLM calls for the same commit SHA.

Key: (program_id, commit_sha)
TTL: 24h OR until consecutive_failures count changes (env state changed).

Cache is stored as JSON files in data/cache/assessments/.
One file per (program_id, commit_sha[:12]).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

def _resolve_cache_dir() -> Path:
    from analysis.paths import cache_dir
    _env = os.getenv("ASSESSMENT_CACHE_DIR", "")
    return Path(_env) if _env else cache_dir() / "assessments"


_CACHE_DIR    = _resolve_cache_dir()
_TTL_HOURS    = int(os.getenv("ASSESSMENT_CACHE_TTL_HOURS", "24"))

# Bump this string whenever scorer logic changes (make_decision, confidence
# calibration, _pick_failure_step, env threshold, etc.).
# Old cache entries with a different version are treated as misses.
# Format: YYYY-MM-DD.N
CACHE_VERSION = "2026-06-29.1"

# What we cache: only LLM-generated fields.
# Scorer fields (risk_level, confidence_score, most_likely_failure_step)
# are always recomputed fresh — they depend on current env state and scorer
# version. Caching them would serve stale results when env changes or scorer
# is updated.
_LLM_FIELDS = {
    "narrative", "reasoning", "technical_failure_hypotheses",
    "recommended_actions", "blast_radius_analysis", "likely_failure_modes",
    "primary_risk_drivers", "evidence_used", "counterevidence",
    "change_intent", "modules_at_risk", "commit_sha",
}


def _cache_key(program_id: str, commit_sha: str) -> str:
    raw = f"{program_id}:{commit_sha.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_path(program_id: str, commit_sha: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{program_id}_{commit_sha[:12]}.json"


def get_cached_llm_fields(
    program_id: str,
    commit_sha: str,
    current_consecutive_failures: int = -1,
) -> Optional[dict]:
    """
    Return cached LLM fields if valid, else None.

    Returns ONLY the LLM-generated fields (_LLM_FIELDS) — never scorer fields.
    Scorer fields (risk_level, confidence_score, most_likely_failure_step) are
    always recomputed fresh to reflect current env state and scorer version.

    Invalidated if:
    - CACHE_VERSION changed (scorer logic updated)
    - More than TTL_HOURS old
    - consecutive_failures changed (environment state changed)
    """
    path = _cache_path(program_id, commit_sha)
    if not path.exists():
        return None

    try:
        cached = json.loads(path.read_text(encoding="utf-8"))

        # Version check — invalidate if scorer logic changed
        if cached.get("cache_version") != CACHE_VERSION:
            path.unlink(missing_ok=True)
            return None

        # Age check
        cached_at = cached.get("cached_at", "")
        if cached_at:
            age_hours = (
                datetime.now(timezone.utc) -
                datetime.fromisoformat(cached_at)
            ).total_seconds() / 3600
            if age_hours > _TTL_HOURS:
                path.unlink(missing_ok=True)
                return None

        # Env state check — if consecutive_failures changed, env is different
        if current_consecutive_failures >= 0:
            cached_consec = cached.get("consecutive_failures_at_cache_time", -1)
            if cached_consec >= 0 and cached_consec != current_consecutive_failures:
                path.unlink(missing_ok=True)
                return None

        # Return only LLM fields — caller runs scorer fresh on top
        llm_fields = cached.get("llm_fields") or {}
        return llm_fields if llm_fields else None

    except Exception:
        path.unlink(missing_ok=True)
        return None


# Backward compat alias
def get_cached(program_id: str, commit_sha: str, current_consecutive_failures: int = -1) -> Optional[dict]:
    return get_cached_llm_fields(program_id, commit_sha, current_consecutive_failures)


def save_cached(
    program_id: str,
    commit_sha: str,
    assessment: dict,
    consecutive_failures: int = -1,
) -> None:
    """
    Cache LLM-generated fields only.
    Scorer fields (risk_level, confidence_score, most_likely_failure_step)
    are excluded — they are always recomputed fresh on cache hit.
    """
    path = _cache_path(program_id, commit_sha)
    try:
        # Extract only LLM fields — never store scorer overrides
        llm_fields = {k: v for k, v in assessment.items() if k in _LLM_FIELDS}
        payload = {
            "program_id":    program_id,
            "commit_sha":    commit_sha,
            "cache_version": CACHE_VERSION,
            "cached_at":     datetime.now(timezone.utc).isoformat(),
            "consecutive_failures_at_cache_time": consecutive_failures,
            "llm_fields":    llm_fields,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def invalidate(program_id: str, commit_sha: str) -> None:
    """Force invalidate a cached assessment."""
    _cache_path(program_id, commit_sha).unlink(missing_ok=True)


def invalidate_all_for_program(program_id: str) -> int:
    """Invalidate all cached assessments for a program (e.g. after Splunk refresh)."""
    if not _CACHE_DIR.exists():
        return 0
    count = 0
    for f in _CACHE_DIR.glob(f"{program_id}_*.json"):
        f.unlink(missing_ok=True)
        count += 1
    return count
