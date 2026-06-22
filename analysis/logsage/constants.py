"""LogSage Stage 1 tunable constants (paper defaults)."""
from __future__ import annotations

import os

# Keyword pool K_error
K_ERROR = [
    "fatal",
    "panic",
    "error",
    "exit",
    "kill",
    "no such file",
    "err:",
    "failures:",
    "exception",
    "[ERROR]",
    "BUILD FAILURE",
    "npm ERR!",
    "COMPILATION ERROR",
    "FAIL:",
    "=== FAIL:",
]

# Step-specific patterns (subset from log_parser.py)
STEP_ERROR_PATTERNS: dict[str, list[str]] = {
    "build": [
        r"\[ERROR\]",
        r"BUILD FAILURE",
        r"Cannot find module",
        r"Module not found",
        r"npm ERR!",
        r"error TS\d+",
    ],
    "securityTest": [r"- Failed", r"WARN -", r"ERROR"],
    "deploy": [r"ERROR", r"FAILED", r"Deployment failed"],
    "loadTest": [r"FAILED", r"ERROR", r"AssertionError"],
    "codeQuality": [r"FAILED", r"Quality gate", r"ERROR"],
}

ALPHA = float(os.getenv("LOGSAGE_ALPHA", "0.7"))
BETA = int(os.getenv("LOGSAGE_BETA", "500"))
GAMMA = int(os.getenv("LOGSAGE_GAMMA", "500"))

CONTEXT_BEFORE = int(os.getenv("LOGSAGE_CONTEXT_BEFORE", "4"))   # m
CONTEXT_AFTER = int(os.getenv("LOGSAGE_CONTEXT_AFTER", "6"))     # n

TOKEN_LIMIT = int(os.getenv("LOGSAGE_TOKEN_LIMIT", "22000"))
QUERY_TOKEN_LIMIT = int(os.getenv("LOGSAGE_QUERY_TOKEN_LIMIT", "3000"))

SUCCESS_TEMPLATE_COUNT = int(os.getenv("LOGSAGE_SUCCESS_TEMPLATE_COUNT", "3"))

DRAIN_CACHE_DIR = os.getenv(
    "LOGSAGE_DRAIN_CACHE_DIR",
    "data/cache/drain_templates",
)

# Failure pattern max weight
FAILURE_PATTERN_WEIGHT = 10
HEADER_BOOST = 2
RECALL_BOOST = 1

FAILURE_PATTERNS = [
    r"FAIL:",
    r"Failures:",
    r"=== FAIL:",
    r"BUILD FAILURE",
    r"npm ERR!",
]
