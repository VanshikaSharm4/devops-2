# Argus — Complete Handover Document

## What Is Argus?

Argus is an AI-powered DevOps intelligence platform for Adobe Cloud Manager (AEM). It has two core jobs:

1. **Pre-deployment risk assessment** — Before a developer promotes code to production, Argus analyses the commit and predicts whether it will cause a build failure, security test failure, or deployment issue. It gives a verdict: Safe to Promote / Review First / Do Not Promote.

2. **Post-failure root cause analysis** — After a pipeline fails, Argus fetches the build log, identifies the exact file and line that caused the failure, and suggests a fix.


---

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit (Python) |
| LLM | Azure OpenAI (GPT-4) |
| Vector store | ChromaDB (SQLite-backed) |
| Pipeline data | Splunk REST API |
| Build logs | Azure Blob Storage |
| Git | Adobe Cloud Manager git (git.cloudmanager.adobe.com) |
| Background jobs | Python threading |

---

## Repository Structure

```
devops-agent-2/
├── dashboard/
│   ├── app.py              ← Main Streamlit app (8000+ lines, all pages)
│   └── argus_home.py       ← Home page component
├── analysis/
│   ├── build_predictor.py  ← Deterministic code checks (AST, pom.xml, imports)
│   ├── risk_analyzer.py    ← Orchestrates prediction pipeline
│   ├── risk_scorer.py      ← Combines signals into a score
│   ├── compressor.py       ← Compresses data before sending to LLM
│   ├── ingest.py           ← Loads Splunk data, manages cache
│   ├── env_readiness.py    ← Assesses environment health from Splunk
│   ├── post_failure_assessor.py ← Orchestrates log analysis
│   ├── failure_classifier.py    ← Classifies failures (code vs infra)
│   ├── tenant_context.py   ← Per-customer thread-safe context
│   ├── customer_context.py ← ContextVar-based (Eris concurrency safe)
│   └── paths.py            ← All data paths (configurable via env vars)
├── connectors/
│   ├── git_connector.py    ← git clone, fetch, diff, SHA lookup
│   ├── splunk_connector.py ← Splunk REST API queries
│   ├── azure_connector.py  ← Azure Blob Storage (build logs)
│   ├── cm_connector.py     ← Adobe Cloud Manager API
│   └── submodule_connector.py ← Submodule diff fetching
├── prompts/
│   ├── pre_deploy_risk.md  ← LLM prompt for risk assessment (most important)
│   └── post_failure_risk.md ← LLM prompt for RCA
├── vector_store/
│   └── store.py            ← ChromaDB read/write
├── data/
│   ├── customer_config.json ← Customer registry (in repo)
│   ├── .secrets.json       ← Git passwords (NOT in repo, gitignored)
│   ├── repo_config.json    ← Submodule configs per customer
│   ├── cache/              ← Splunk data cache (pickle files)
│   └── chroma_db/          ← ChromaDB vector store
├── .env                    ← All API keys (NOT in repo, gitignored)
├── .env.example            ← Template showing all required vars
└── requirements.txt        ← Python dependencies
```

---

## How Predictions Are Made

This is the most important thing to understand. Every risk assessment goes through these steps in order:

### Step 1 — Git diff

When a developer pastes a commit SHA or selects an execution, Argus runs:
```python
git diff <parent_sha>..<commit_sha>
```
This gives the full list of changed files and the actual code changes.

### Step 2 — Deterministic structural analysis (`build_predictor.py`)

Before the LLM is called, a deterministic checker scans the diff for specific patterns. This is fast (~200ms) and doesn't cost API tokens. It checks for:

- Java syntax errors (via `javalang` AST parser)
- Missing class imports ("cannot find symbol")
- Re-enabled reactor modules with broken frontends
- npm dependency issues (missing modules, broken package.json)
- Dispatcher config syntax errors
- pom.xml version skew between modules
- Submodule commits where the submodule diff couldn't be fetched

If it finds a **certain compile error** (syntax error, missing class), it can set `override_llm=True` and skip the LLM entirely. Otherwise, findings go into the context as `structural_findings`.

**Important:** The structural checker is deterministic — same input always gives same output. It doesn't use ML or the LLM.

### Step 3 — Signal computation (`risk_scorer.py`)

Three signals are computed independently:

1. **Environment signal** — reads Splunk data to check recent pipeline failures. If there are 13 consecutive failures at securityTest, that's an environment issue, not code.

2. **Code signal** — from Step 2 structural findings.

3. **Historical signal** — queries ChromaDB for similar past failures (semantic search on the commit's diff).

These three signals are combined by `make_decision()` into a preliminary verdict (GO/CAUTION/HOLD) and confidence score.

### Step 4 — Compressor (`compressor.py`)

Before the LLM is called, `compress_bundle_for_risk()` prepares the context. It:
- Detects low-risk patterns (subtree imports, bot commits, empty commits) and injects context notes
- Separates real code failures from infrastructure noise
- Truncates the diff to 2500 chars (LLM has a token limit)
- Classifies historical failures by type (code regression vs infra noise)

**Key concept:** The `build_risk_override` field is advisory context, NOT a directive. It tells the LLM "here's what kind of commit this is" but doesn't tell it what verdict to reach.

### Step 5 — LLM analysis (`prompts/pre_deploy_risk.md`)

The LLM (Azure OpenAI GPT-4) receives:
- The compressed diff
- Structural findings from Step 2
- Environment readiness from Step 3
- ChromaDB similar incidents from Step 3
- Historical baseline from Splunk
- The `build_risk_override` context note

It returns structured JSON with:
- `reasoning` — free-form analysis (most accurate signal)
- `step_risks[]` — risk level per pipeline step (build/deploy/securityTest/etc.)
- `technical_failure_hypotheses` — specific failure mechanisms
- `confidence_score` — overall confidence

**The `reasoning` field is the most accurate.** The structured `step_risks` sometimes hedges. The dashboard extracts key sentences from `reasoning` to show the developer.

### Step 6 — Hero verdict computation (`dashboard/app.py`)

After the LLM responds, the dashboard computes the final hero verdict (SAFE TO PROMOTE / REVIEW / DO NOT PROMOTE) by:

1. Starting with the LLM's `step_risks["build"].level`
2. Checking if deterministic structural findings should override (e.g. compile error → HOLD regardless of LLM)
3. Checking if ChromaDB similarity is high enough to escalate
4. Running the LLM's `reasoning` through a keyword parser to upgrade if reasoning says "will fail" but step_risks says Low
5. Checking if unfetched submodules mean unknown risk

The precedence is: **deterministic structural findings > LLM reasoning > LLM step_risks > historical baseline**

---

## Setting Up From Scratch

### Prerequisites
- Python 3.9+
- Git
- Access to Adobe VPN (required — git.cloudmanager.adobe.com is not publicly accessible)

### Step 1 — Clone and install
```bash
git clone <repo-url> /opt/argus
cd /opt/argus
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 2 — Create .env
Copy from `.env.example` and fill in:

```bash
# LLM
LLM_PROVIDER=azure_openai
AZURE_OPENAI_KEY=<key>
AZURE_OPENAI_ENDPOINT=https://ams-india-devops-agent.openai.azure.com/openai/v1
AZURE_OPENAI_DEPLOYMENT=devops-agent

# Azure Storage (for build logs)
AZURE_CONNECTION_STRING=<connection-string>

# Splunk
SPLUNK_USERNAME=<adobe-email>
SPLUNK_PASSWORD=<splunk-password>

# Paths (critical for server deployment)
ARGUS_DATA_DIR=/opt/argus/data
ARGUS_REPOS_DIR=/opt/argus/repos
REPOS_BASE_DIR=/opt/argus/repos

# Per-customer CM API keys (from Adobe IO console)
IDFC_KEY=<key>
HDFC_KEY=<key>
MALASIA_KEY=<key>
# ... etc
```

### Step 3 — Create .secrets.json
File: `data/.secrets.json`
```json
{
  "IDFC First Bank": { "git_password": "<generated-from-CM>" },
  "HDFC Bank": { "git_password": "<generated-from-CM>" },
  "Malaysia Airlines": { "git_password": "<generated-from-CM>" }
}
```
The git password is generated in Adobe Cloud Manager → Your Program → Repositories → "Access Repo Info" → "Generate password". It's valid for git clone/fetch only (not the CM REST API).

### Step 4 — Create data directories
```bash
mkdir -p /opt/argus/data/cache
mkdir -p /opt/argus/data/chroma_db
mkdir -p /opt/argus/repos
```

### Step 5 — Run
```bash
streamlit run dashboard/app.py \
  --server.port 8501 \
  --server.address 127.0.0.1 \
  --server.headless true
```

### Step 6 — Add customers via UI
Open browser → Repo Settings → Add each customer with:
- Customer name
- Program ID (from CM)
- Production Pipeline ID
- Dev Pipeline ID
- Git URL (`https://git.cloudmanager.adobe.com/<org>/<repo>/`)
- Git Username
- Git Password
- Deploy Branch (leave blank to show commits from all branches)

Click "Save & Fetch Repository" — this clones the repo and auto-discovers submodules.

---

## Key Configuration Files

### `data/customer_config.json`
Stores all customer settings (in git, safe to commit):
```json
{
  "IDFC First Bank": {
    "program_id": "19905",
    "pipeline_prod": "2357452",
    "pipeline_dev": "47202398",
    "git_url": "https://git.cloudmanager.adobe.com/idfc/idfc/",
    "git_username": "vanssharma-adobe-com",
    "git_branch": "master",
    "git_local_dir": "/opt/argus/repos/idfc",
    "tenant_id": "idfc",
    "short": "IDFC"
  }
}
```

### `data/repo_config.json`
Submodule configurations (auto-populated when repos are cloned). For HDFC this has 37 submodule entries.

### `data/chroma_db/`
ChromaDB SQLite database. Contains classified historical pipeline failures as vector embeddings. Used for semantic similarity search ("this commit is similar to past failure X at 0.71 similarity"). 

**If this is deleted, historical matching stops working until you run "Initialize Historical Data" in Repo Settings for each customer.**

---

## How ChromaDB (Historical Matching) Works

When a pipeline fails and is analysed:
1. The error type, affected files, and root cause are extracted
2. These are embedded (converted to vectors) using the LLM
3. Stored in ChromaDB tagged with the customer's `program_id`

When a new assessment runs:
1. The commit's diff is embedded
2. ChromaDB is searched for similar past failures (cosine similarity)
3. Matches above 0.65 similarity are included in the LLM context
4. The LLM says "this pattern matches a past failure at X% similarity, therefore..."

**To populate ChromaDB for a new customer:** Go to Repo Settings → select customer → "Initialize Historical Data". This fetches the last 30 days of Splunk data, classifies failures, and ingests them into ChromaDB.

---

## Common Issues

### "SHA not found after fetch"
The commit SHA the developer pasted doesn't exist in the local git repo. Either:
- The repo hasn't been cloned (go to Repo Settings and clone it)
- The SHA is on a branch that wasn't fetched (Argus fetches all branches automatically, but this takes time on first run)
- The git password expired (regenerate in CM → Repositories)

### "Log Unavailable" in Failure Pinpoint
The Azure build log couldn't be fetched. Either:
- `AZURE_CONNECTION_STRING` is not set or expired
- The execution is too old and the log was rotated
- The Splunk share names weren't cached yet (click Refresh and try again)

### Splunk timeout (300s/120s)
Splunk is slow under load. Argus uses a 14-day window (was 30 days — changed to reduce timeouts). Background refresh runs every 30 minutes. If Splunk keeps timing out, you can set `SPLUNK_EARLIEST=-7d` in `.env` to use a 7-day window.

### Wrong commit SHA shown for execution
The git tag CM creates points to a Jenkins bot commit ("Updated pom.xml..."), not the developer's commit. Argus detects this and walks to the parent commit. If still wrong, ask the developer to paste the SHA from CM → Execution → "COMMIT:" field.

---

## How Customer Isolation Works

ChromaDB is shared but all records are tagged with `tenant_id`/`program_id`. Queries are always filtered by the active customer's program ID. HDFC's failures never appear in IDFC's searches.

Splunk cache is per-customer: `data/cache/splunk_cache_19905.pkl` (IDFC), `data/cache/splunk_cache_16360.pkl` (HDFC), etc.

SQLite (`data/argus.db`) uses `(program_id, sha)` as primary key.

---

## Concurrency Safety (for Eris deployment)

Streamlit runs all users in ONE Python process. 20 concurrent users share memory.

The fix we implemented:
- `analysis/customer_context.py` — uses Python `ContextVar` so each user session has its own isolated copy of git credentials, program ID, etc.
- `copy_context()` in the analysis thread — when the assessment thread starts, it gets a frozen snapshot of the current user's context. Other users' context changes can't affect it.
- Thread-safe Splunk refresh via Python locks per program ID

**For >50 users:** you'll need to move ChromaDB to a proper vector DB (Pinecone/Weaviate) and switch from SQLite to PostgreSQL.

---

## The LLM Prompt (`prompts/pre_deploy_risk.md`)

This is the most important file after `app.py`. It's 33KB of instructions to the LLM. Key sections:

- **Priority order** — environment > historical > structural > LLM reasoning (always weight env signals highest)
- **Rule 8** — low-risk commit patterns (subtree imports, bot commits, empty commits) — these are context notes, not directives
- **STEP 1** — what the LLM must check internally before writing JSON
- **STEP 2** — the JSON schema it must return
- **Consistency rule** — the LLM must not say "build will fail" in reasoning while saying Low in step_risks
- **Few-shot example** — shows correct analysis for a dependency upgrade

If the LLM starts giving wrong results, this prompt is where to tune.

---

## What NOT to Change (will break things)

1. **`data/customer_config.json` customer names** — changing "IDFC First Bank" to anything else will break all ChromaDB queries for that customer (wrong tenant_id)
2. **ChromaDB directory structure** — don't move or rename `data/chroma_db/`
3. **The `(program_id, sha)` primary key in SQLite** — the report_store depends on this
4. **`analysis/paths.py`** — all modules import from here; changing paths here changes them everywhere
5. **The LLM JSON schema in `prompts/pre_deploy_risk.md`** — the dashboard parses specific fields like `step_risks`, `reasoning`, `most_likely_failure_step`

---

## Adding a New Customer

1. Go to Repo Settings in the UI
2. Fill in the form — Program ID and Git URL are mandatory
3. The git password comes from: CM → Your Program → Repositories → "Access Repo Info" → "Generate password"
4. CM API keys come from: Adobe IO Console → Create a project → Add Cloud Manager API → generate credentials
5. After saving, click "Initialize Historical Data" to populate ChromaDB from Splunk

---

## Important — This Is a Prototype

Argus is a working prototype, not a production ML system. It predicts using a combination of deterministic rules + LLM reasoning + limited historical similarity search. It does NOT have access to many signals that would make predictions significantly better:

**What Argus cannot see:**
- **Server metrics** — CPU, memory, disk I/O on AEM author/publish nodes. A build timeout caused by OOM is indistinguishable from a code bug.
- **JVM internals** — GC pressure, heap allocation, thread dumps. These cause many "build failure" symptoms that look like code issues.
- **Network latency** — slow npm installs, slow Maven artifact downloads. These cause build timeouts that look like code failures.
- **AEM version internals** — which bundles are active, OSGi resolver state, actual classpath at runtime.
- **Test coverage maps** — which tests actually exercise which code paths. Argus guesses based on file naming conventions.
- **Sonar/quality gate rules** — the exact thresholds that trigger codeQuality failures.
- **Cross-pipeline dependencies** — if Pipeline A deploys a library that Pipeline B depends on, Argus doesn't know about that relationship.

Because of these gaps, Argus is accurate on **compile errors, npm failures, and dispatcher config issues**. It is less accurate on **environment-caused failures, performance regressions, and novel failure patterns** where it has to guess from limited signals.

---

## Known Limitations

**1. Splunk indexing lag**
Splunk takes minutes to hours to index new pipeline events. Argus may show a pipeline as RUNNING when it already failed. We mitigate this with heuristics (if firstFailedStep is set → treat as FAILED) but the root cause is outside Argus's control.

**3. Submodule visibility**
example : HDFC has 37 submodules. Each is a separate git repo. Argus can only analyse a submodule's code changes if it has fetched that submodule's bare repo. If the bare repo is stale or missing, the diff is unavailable and Argus says "unknown risk" for that submodule. This is the most common source of wrong predictions for HDFC.

**6. Streamlit single-process limitation**
20 concurrent users share one Python process. We added ContextVar isolation but 11 `os.environ` writes still exist outside the main analysis thread. Fine for 20 -25 users in the prototype stage; will need proper async workers for 100+ users.

**8. Large-scale deployment not tested**
Currently tested with 6 customers and ~20 concurrent users. For 200+ customers, ChromaDB needs to move to a managed vector store (Pinecone/Weaviate) and SQLite needs to move to PostgreSQL.

**9. Historical data quality : cold start for new customers**
Splunk only has 14 days of pipeline history at any time (configurable). ChromaDB is populated from those 14 days. For new customers, ChromaDB is empty until "Initialize Historical Data" is run. Predictions for new customers rely almost entirely on deterministic rules and the LLM.

**10. Prediction accuracy unmeasured**
There is no formal accuracy baseline yet. The ML dataset collection (see next section) is building toward this.

---

## ML Data Collection — Building Toward a Trained Model

This is a long-term investments in the codebase. Every assessment Argus runs silently collects ground-truth training data that will eventually be used to train a proper ML model to replace the LLM for routine predictions.

### Why collect data now?

The current LLM-based approach:
- Takes 3-8 seconds per assessment
- Cannot run offline
- Cannot be fine-tuned on our specific failure patterns

A trained XGBoost/gradient boosting model on our own data:
- Would run in <100ms
- Would work offline
- Would be tuned specifically to AEM Cloud Manager failure patterns

To train such a model, we need labeled examples: (commit features) → (did it fail? where?). We're collecting these now.

### What is collected

Every time Argus runs a risk assessment and then the actual pipeline outcome is known (from Splunk), the system stores a labeled example. The relevant files:

**`analysis/ml_feature_extractor.py`**
Extracts purely factual, deterministic features from the git diff — no LLM outputs, no risk levels, no text. Features include:
- Lines added/removed
- File types changed (Java, JS, pom.xml, dispatcher, etc.)
- Whether tests were updated
- Number of modules touched
- Whether submodule pointers changed
- Environment failure probability (from Splunk)
- Historical failure rate for this customer
- Structural finding counts (HIGH/MEDIUM/LOW)

These features are stable, reproducible, and suitable for ML training.

**`analysis/ml_dataset_store.py`**
Stores the features + ground truth labels in JSONL files (one per customer):
```
data/ml_dataset/19905.jsonl   ← IDFC training data
data/ml_dataset/16360.jsonl   ← HDFC training data
```

Each record looks like:
```json
{
  "sha": "abc123...",
  "program_id": "19905",
  "features": {
    "lines_added": 47,
    "lines_removed": 12,
    "has_java_changes": true,
    "test_files_changed": false,
    "dispatcher_changed": false,
    "pom_changed": true,
    ...
  },
  "label": {
    "failed": true,
    "failed_step": "build",
    "actual_outcome": "FAILED"
  },
  "argus_prediction": "CAUTION",
  "argus_correct": false
}
```

**`analysis/prediction_store.py`**
Tracks every Argus prediction and, once the pipeline completes, enriches it with the actual outcome. This is what feeds the ML dataset — `enrich_from_splunk()` runs automatically and marks predictions as correct/incorrect.

### Implementation order (for the next developer)

The ML pipeline is partially built. To complete it:

1. **`ml_feature_extractor.py` + unit tests** — already built. Add tests with fixture diffs (sample git diffs stored in `tests/fixtures/`) to verify features are extracted correctly. Test edge cases: empty diff, submodule-only, large diff truncation.

2. **`ml_dataset_store.py` + tests** — already built. Write tests mirroring `prediction_store` patterns. Verify JSONL append is atomic (no partial writes). Verify tenant isolation (IDFC data never in HDFC file).

3. **Dashboard hooks** — wire the dataset collection into `dashboard/app.py`. After every successful assessment + known outcome: call `ml_dataset_store.save_labeled_example(sha, program_id, features, label)`. This happens in the `enrich_from_splunk()` path in `prediction_store.py`.

4. **`scripts/export_ml_dataset.py`** — write a script that reads all JSONL files, combines them, balances classes (failures are rare ~20% of assessments), and outputs a CSV/Parquet file ready for XGBoost training.

5. **Training** — once ~500+ labeled examples are collected per customer (estimate: 3-6 months of usage), train an XGBoost classifier. Features are already numeric/boolean so minimal preprocessing needed. Target: predict `(failed, failed_step)` from features.

6. **Integration** — replace the LLM call with the trained model for the common case. Use LLM only for novel patterns the model hasn't seen (low-confidence predictions, new customers with <100 examples).

### How much data do we have now?

```bash
# Check how many labeled examples per customer
wc -l data/ml_dataset/*.jsonl 2>/dev/null || echo "Dataset directory not yet created"

# Check prediction accuracy
python3 -c "
from analysis.prediction_store import get_accuracy_summary
print(get_accuracy_summary())
"
```

---

