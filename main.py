"""
Entry point — delegates to CLI report command.
Splunk live API: configured per-user in dashboard (Repo Settings → Customer Information).
CLI falls back to disk cache or data/splunk_exports/ CSVs when no user session exists.
"""

import sys

from cli import cmd_report


class Args:
    no_llm = False
    no_logs = False


def main():
    print("  Tip: use `python cli.py report` or `python cli.py risk --commit SHA` for all features.\n")
    cmd_report(Args())


if __name__ == "__main__":
    main()
