import hashlib
import os
from pathlib import Path
from typing import Optional

from azure.storage.fileshare import ShareClient, ShareFileClient
from dotenv import load_dotenv

load_dotenv()

AZURE_CONNECTION_STRING = (
    os.getenv("AZURE_CONNECTION_STRING")
    or os.getenv("AZURE_STORAGE_CONNECTION_STRING")
)

# Disk cache for Azure log files — avoids re-downloading the same log on every click
_LOG_CACHE_DIR = Path("data/cache/logs")


def _log_cache_path(share_name: str, file_path: str) -> Path:
    key = hashlib.md5(f"{share_name}:{file_path}".encode()).hexdigest()
    return _LOG_CACHE_DIR / f"{key}.txt"


def _read_log_cache(share_name: str, file_path: str) -> Optional[str]:
    path = _log_cache_path(share_name, file_path)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return None


def _write_log_cache(share_name: str, file_path: str, content: str) -> None:
    _LOG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _log_cache_path(share_name, file_path).write_text(content, encoding="utf-8")


def _connection_string() -> str:
    if not AZURE_CONNECTION_STRING:
        raise ValueError(
            "AZURE_CONNECTION_STRING or AZURE_STORAGE_CONNECTION_STRING must be set"
        )
    return AZURE_CONNECTION_STRING


def get_file_from_share(share_name: str, file_path: str, use_cache: bool = True) -> str:
    """Download a file from an Azure File Share and return its contents.
    Results are cached to disk so repeated calls for the same file are instant.
    """
    if use_cache:
        cached = _read_log_cache(share_name, file_path)
        if cached is not None:
            return cached

    file_client = ShareFileClient.from_connection_string(
        conn_str=_connection_string(),
        share_name=share_name,
        file_path=file_path
    )
    content = file_client.download_file().readall().decode("utf-8", errors="replace")

    if use_cache:
        _write_log_cache(share_name, file_path, content)

    return content


def list_files_in_share(share_name: str, directory: str = "") -> list:
    """List all files inside a directory of an Azure File Share."""
    share_client = ShareClient.from_connection_string(
        conn_str=_connection_string(),
        share_name=share_name
    )
    directory_client = share_client.get_directory_client(directory)
    items = []
    for item in directory_client.list_directories_and_files():
        items.append({"name": item["name"], "is_directory": item["is_directory"]})
    return items


def get_log_for_execution(share_name: str, failed_step: str, execution_id: str = "") -> str:
    """
    Given an Azure share name, failed step, and execution ID,
    fetch the relevant log file for that step.
    """
    log_map = {
        "build":        "build_debug_logs_{eid}/build.log",
        "securityTest": "securityTests.log",
        "deploy":       "deploy/deploy.log",
        "loadTest":     "load-test.log",
        "codeQuality":  "build_debug_logs_{eid}/build.log",
    }

    file_path = log_map.get(failed_step, "build.log")
    if execution_id and "{eid}" in file_path:
        file_path = file_path.replace("{eid}", execution_id)
    else:
        file_path = file_path.replace("_{eid}", "")

    try:
        return get_file_from_share(share_name, file_path)
    except Exception as e:
        if execution_id and failed_step in ("build", "codeQuality"):
            for alt in ("build.log", f"build_debug_logs_{execution_id}/build.log"):
                try:
                    return get_file_from_share(share_name, alt)
                except Exception:
                    continue
        return f"ERROR: Could not fetch log for step '{failed_step}' from share '{share_name}': {e}"
