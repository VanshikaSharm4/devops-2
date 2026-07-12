"""
Central path resolver for Argus.
All data paths go through here so deploying to Eris just needs:
  ARGUS_DATA_DIR=/opt/argus/data
  ARGUS_REPOS_DIR=/opt/argus/repos
"""
import os
from pathlib import Path


def data_dir() -> Path:
    return Path(os.getenv("ARGUS_DATA_DIR", "data"))


def repos_dir() -> Path:
    return Path(os.getenv("ARGUS_REPOS_DIR", os.getenv("REPOS_BASE_DIR", str(Path.home() / "projects"))))


# Common sub-paths
def cache_dir() -> Path:
    return data_dir() / "cache"


def chroma_dir() -> Path:
    return data_dir() / "chroma_db"


def secrets_path() -> Path:
    return data_dir() / ".secrets.json"


def customer_config_path() -> Path:
    return data_dir() / "customer_config.json"


def repo_config_path() -> Path:
    return data_dir() / "repo_config.json"


def sqlite_db_path() -> Path:
    return data_dir() / "argus.db"


def splunk_exports_dir() -> Path:
    return data_dir() / "splunk_exports"
