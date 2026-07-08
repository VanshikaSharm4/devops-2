"""
Cross-process LLM semaphore.

Prevents 429 rate-limit storms when multiple Streamlit workers hit the same
Azure OpenAI key simultaneously. Uses a file-based counter (works across
3 Streamlit processes without Redis).

Default: max 4 concurrent LLM calls (set LLM_MAX_CONCURRENT in .env).
Each call acquires the semaphore, makes the API request, releases.
If max is reached, caller waits up to LLM_SEMAPHORE_TIMEOUT_SEC (default 90s).
"""
from __future__ import annotations

import os
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path

_LOCK_DIR   = Path(os.getenv("LLM_SEMAPHORE_DIR", "data"))
_LOCK_FILE  = _LOCK_DIR / "llm_semaphore.lock"
_COUNT_FILE = _LOCK_DIR / "llm_semaphore.count"
MAX_CONCURRENT = int(os.getenv("LLM_MAX_CONCURRENT", "4"))
TIMEOUT_SEC    = int(os.getenv("LLM_SEMAPHORE_TIMEOUT_SEC", "90"))


def _read_count(f) -> int:
    f.seek(0)
    try:
        return int(f.read().strip() or "0")
    except (ValueError, OSError):
        return 0


def _write_count(f, n: int) -> None:
    f.seek(0)
    f.truncate()
    f.write(str(max(0, n)))
    f.flush()


@contextmanager
def llm_slot(caller: str = ""):
    """
    Acquire one LLM slot before calling Azure OpenAI.
    Releases automatically when the `with` block exits.

    Usage:
        with llm_slot("risk_analysis"):
            response = client.chat.completions.create(...)
    """
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)

    acquired = False
    start = time.time()

    while time.time() - start < TIMEOUT_SEC:
        try:
            with open(_LOCK_FILE, "a+") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with open(_COUNT_FILE, "a+") as cf:
                        count = _read_count(cf)
                        if count < MAX_CONCURRENT:
                            _write_count(cf, count + 1)
                            acquired = True
                            if caller:
                                print(f"  [llm_semaphore] acquired slot {count+1}/{MAX_CONCURRENT} ({caller})")
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        except (BlockingIOError, OSError):
            pass

        if acquired:
            break
        time.sleep(0.5)

    if not acquired:
        print(f"  [llm_semaphore] timeout waiting for slot after {TIMEOUT_SEC}s ({caller}) — proceeding anyway")

    try:
        yield
    finally:
        if acquired:
            try:
                with open(_LOCK_FILE, "a+") as lf:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                    try:
                        with open(_COUNT_FILE, "a+") as cf:
                            count = _read_count(cf)
                            _write_count(cf, count - 1)
                    finally:
                        fcntl.flock(lf, fcntl.LOCK_UN)
            except OSError:
                pass


def current_count() -> int:
    """Return number of in-flight LLM calls across all workers."""
    try:
        with open(_COUNT_FILE) as f:
            return _read_count(f)
    except OSError:
        return 0
