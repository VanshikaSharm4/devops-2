# DevOps Agent 2

Clean rebuild of the Adobe Cloud Manager pipeline intelligence agent for IDFC First Bank.

This workspace was seeded from the prototype in `/Users/vanshika/projects/devops-agent`, but excludes local secrets and generated runtime data:

- `.env`
- `data/cache/`
- `data/chroma_db/`
- `reports/`

## Current Status

The offline path works with the checked-in Splunk CSV exports. No Splunk, Azure, Git, or LLM credentials are required for the smoke test.

```bash
python3 -B tests/test_offline_smoke.py
python3 -B cli.py report --no-llm --no-logs
```

## Live Configuration

Copy `.env.example` to `.env` locally and fill in only the systems you need:

- `SPLUNK_USERNAME` / `SPLUNK_PASSWORD` for live execution history
- `AZURE_CONNECTION_STRING` or `AZURE_STORAGE_CONNECTION_STRING` for Azure File Share logs
- `CM_GIT_REPO_URL`, `CM_GIT_USERNAME`, `CM_GIT_PASSWORD`, `GIT_LOCAL_DIR` for Cloud Manager Git
- `AZURE_OPENAI_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` for LLM calls

## Main Entrypoints

```bash
python3 cli.py report --no-llm --no-logs
python3 cli.py risk --commit <sha> --no-llm
python3 cli.py compare --exec-a <id> --exec-b <id> --no-llm
python3 cli.py pinpoint --execution-id <id> --no-llm
python3 cli.py assess-failure --execution-id <id> --no-llm --no-reranker
streamlit run dashboard/app.py
```

## Notes

- The Cloud Manager Git connector avoids persisting credential-injected remote URLs.
- Prompts ask for concise evidence/rationale fields, not hidden chain-of-thought.
- `connectors/github_connector.py` is retained only as legacy compatibility code.
# devops-2
