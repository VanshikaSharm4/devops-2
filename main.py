"""
Entry point — delegates to CLI report command.
Splunk data: fetched live from Splunk API (SPLUNK_USERNAME + SPLUNK_PASSWORD in .env).
Falls back to data/splunk_exports/ CSVs if credentials are missing.
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
