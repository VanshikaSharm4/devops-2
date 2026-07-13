# Argus

Argus is an AI-powered DevOps intelligence platform for Adobe Experience Manager (AEM) customers running on Adobe Cloud Manager. It ingests pipeline execution history, build logs, and Git changes to provide:

- **Pre-deployment risk assessment**
- **Post-failure root cause diagnosis**
- **Program-wide failure trend analysis**

Argus is designed for Adobe Managed Services (AMS) teams supporting multiple AEM customers from a single deployment.

---

# What Argus Does

Argus answers three classes of DevOps questions.

| Question | Feature | How |
|----------|---------|-----|
| **Should we deploy this commit?** | **Risk Assessment** | Structural code analysis + environment health + historical similarity + LLM synthesis |
| **What failed and why?** | **Post-Failure Diagnosis** | Log filtering + hybrid retrieval (BM25 + embeddings) + LLM root-cause analysis |
| **What is failing across the program?** | **Failure Analysis** | 30-day Splunk rollup with AI-generated executive report |

Additional capabilities include:

- **Overview** – Pipeline health dashboard and execution trends
- **Memory Explorer** – Browse ChromaDB failure memory
- **Repo Settings** – Multi-customer configuration management

---

# Architecture

Argus follows an eight-layer AI pipeline that transforms raw DevOps telemetry into structured AI reports.

```
┌───────────────────────────────────────────────────────────────┐
│                       Data Sources                            │
│ Splunk • Azure File Share • Cloud Manager Git                │
└─────────────────────────────┬─────────────────────────────────┘
                              │
                              ▼
Layer 1  Collection        Fetch metadata, logs, commits
Layer 2  Parsing           Extract structured events
Layer 3  Enrichment        Link executions, commits, modules
Layer 4  Compression       Remove noise (~80% token reduction)
Layer 5  Context Builder   Assemble feature-specific context

           ┌─────────────────────────────────────────┐
           │ ChromaDB Failure Memory (RAG)           │
           │ Hybrid Retrieval (BM25 + Embeddings)    │
           └─────────────────────────────────────────┘

Layer 6  Prompt Engineering
Layer 7  LLM Processing
Layer 8  Structured Output Validation

                              ▼
          Streamlit Dashboard + SQLite Persistence
```

---

# Three-Signal Risk Model

Risk Assessment combines three independent signals.

### 1. Environment Health

Derived entirely from Splunk pipeline history.

Examples:

- Consecutive failures
- Last successful deployment
- Environment-specific instability
- Dominant failing deployment step

No LLM involved.

---

### 2. Code Risk

Deterministic structural analysis of Git changes.

Checks include:

- Maven dependency modifications
- OSGi reference changes
- Vault filter conflicts
- Java interface compatibility
- Module structure

High-confidence structural failures can override the LLM recommendation.

---

### 3. Historical Similarity

Uses ChromaDB vector search to retrieve similar historical failures from the same customer or program.

---

The three signals combine into one deployment recommendation:

- ✅ GO
- ⚠️ CAUTION
- ⛔ HOLD

All predictions are logged for future accuracy evaluation.

---

# LogSage (Post-Failure Diagnosis)

LogSage performs root cause analysis for a single failed execution.

### Stage 1

Log preprocessing

- Drain3 log templating
- Success/failure comparison
- Token pruning
- Noise removal

### Stage 2

AI diagnosis

- Hybrid retrieval (BM25 + vector search)
- Historical failure matching
- Structured LLM output (`PostFailureRiskReport`)

Synthetic logs are **never** sent to the LLM.

Missing Azure logs produce explicit errors instead.

---

# Data Sources

| Source | Connector | Purpose |
|---------|-----------|----------|
| Splunk | `connectors/splunk_connector.py` | Pipeline executions, failure history |
| Azure File Share | `connectors/azure_connector.py` | Build and deployment logs |
| Cloud Manager Git | `connectors/git_connector.py` | Commits, diffs, changed files |
| Cloud Manager API | `connectors/cm_connector.py` | Execution → Commit resolution |
| Submodules | `connectors/submodule_connector.py` | Multi-repository AEM projects |

Splunk can operate either:

- Live using API credentials
- Offline using CSV exports in `data/splunk_exports/`

---

# Multi-Tenant Design

Argus supports multiple AEM customers from a single deployment.

## Customer Registry

```
data/customer_config.json
```

Stores:

- Program IDs
- Pipeline IDs
- Git repositories

---

## Secrets

```
data/.secrets.json
```

Stores customer credentials (gitignored).

---

## Tenant Context

```
analysis/customer_context.py
analysis/tenant_context.py
```

Maintains thread-local customer context.

---

## Tenant-Isolated Storage

Predictions

```
data/predictions/{program_id}.jsonl
```

Reports

```
SQLite (argus.db)
```

Vector Memory

```
ChromaDB
```

Splunk Cache

```
data/cache/splunk_cache_{program_id}.pkl
```

---

# Project Structure

```
argus/
├── agent/
│   └── devops_agent.py
│
├── analysis/
│   ├── ingest.py
│   ├── build_predictor.py
│   ├── env_readiness.py
│   ├── risk_analyzer.py
│   ├── risk_scorer.py
│   ├── post_failure_assessor.py
│   ├── logsage/
│   ├── retrieval/
│   ├── context_builder.py
│   ├── compressor.py
│   └── prediction_store.py
│
├── connectors/
│
├── dashboard/
│   ├── app.py
│   └── argus_home.py
│
├── db/
│   └── report_store.py
│
├── models/
├── prompts/
├── vector_store/
├── deploy/
├── data/
└── cli.py
```

---

# Getting Started

## Requirements

- Python 3.10+
- Install dependencies

```bash
pip install -r requirements.txt
```

---

## Configuration

```bash
cp .env.example .env
```

Fill in the required credentials.

### Important Environment Variables

| Variable | Purpose |
|----------|----------|
| `LLM_PROVIDER` | Azure OpenAI / Anthropic / Gemini |
| `AZURE_OPENAI_KEY` | Azure OpenAI |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI |
| `AZURE_OPENAI_DEPLOYMENT` | Azure deployment |
| `SPLUNK_USERNAME` | Splunk access |
| `SPLUNK_PASSWORD` | Splunk access |
| `AZURE_CONNECTION_STRING` | Azure File Share |
| `CM_GIT_REPO_URL` | Cloud Manager Git |
| `CM_GIT_USERNAME` | Git username |
| `CM_GIT_PASSWORD` | Git password |
| `GIT_LOCAL_DIR` | Local Git clone |
| `ARGUS_DATA_DIR` | Override runtime data directory |
| `ARGUS_DB_PATH` | Override SQLite location |

---

# Offline Smoke Test

```bash
python3 -B tests/test_offline_smoke.py

python3 -B cli.py report --no-llm --no-logs
```

---

# Run the Dashboard

```bash
streamlit run dashboard/app.py
```

Open

```
http://localhost:8501
```

Select a customer from the sidebar.

---

# CLI

```bash
python3 cli.py report
```

Generate 30-day Failure Analysis.

```bash
python3 cli.py risk --commit <sha>
```

Run Risk Assessment.

```bash
python3 cli.py pinpoint --execution-id <id>
```

Run Post-Failure Diagnosis.

```bash
python3 cli.py compare --exec-a <id> --exec-b <id>
```

Compare two executions.

```bash
python3 cli.py assess-failure --execution-id <id>
```

Run LogSage analysis.

Optional flags:

```bash
--no-llm
```

Skip AI calls.

```bash
--no-logs
```

Skip Azure log retrieval.

---

# Dashboard Pages

| Page | Description |
|------|-------------|
| Overview | Pipeline health dashboard |
| Risk Assessment | Pre-deployment AI risk analysis |
| Post-Failure Diagnosis | Root cause analysis using LogSage |
| Failure Analysis | 30-day executive failure report |
| Memory Explorer | Browse historical failures |
| Repo Settings | Customer configuration management |

---

# Production Deployment

For approximately 10–20 concurrent users:

```bash
NUM_WORKERS=3 ./deploy/start_server.sh
```

```bash
nginx -c deploy/nginx.conf
```

Features:

- Multiple Streamlit workers
- Sticky sessions via nginx
- Automatic worker restart
- Persistent SQLite and runtime storage

---

# Supported LLM Provider

- Azure OpenAI 

Per-feature temperature and token settings are configured in:

```
agent/devops_agent.py
```

A global LLM semaphore limits concurrent requests to avoid rate-limit spikes.

---

# Prediction Accuracy Loop

Each deployment prediction is stored as:

```
data/predictions/{program_id}.jsonl
```

Lifecycle:

```
PENDING
      │
      ▼
RESOLVED
      │
      ▼
Correct / Incorrect
```

Ground-truth evaluation can be performed with:

```bash
python3 scripts/evaluate_predictions.py
```

---

# Design Principles

- Data-first reasoning
- Deterministic checks before probabilistic AI
- Never fabricate logs
- Strict tenant isolation
- Token-efficient context compression
- Structured, validated AI outputs

---
