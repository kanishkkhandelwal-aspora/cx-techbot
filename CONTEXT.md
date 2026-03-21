# CX-Tech Bot — Project Context

> **Last updated:** 2026-03-21
> Update this file every time code changes are made.

---

## What This Is

A Python Slack bot that automatically classifies CX (Customer Experience) queries, investigates them via CloudWatch logs + Databricks SQL, extracts structured case facts from that evidence, selects approved playbook guidance from confirmed facts, synthesizes root causes using Claude AI, and assigns only the cases that actually need human follow-up.

**Multi-agent architecture:** Agent 1 (CloudWatch logs) + Agent 2 (Databricks SQL) → Structured Fact Extractor → Evidence-backed Playbook → Response Mode Gate → Parent Agent (Claude synthesizer)

**Slack channel:** `#testing-openclawbot` (C0AKF9U2RCL)
**Runtime:** Python 3.14, runs as a polling loop (not websocket/events API)

---

## Architecture

```
Slack message
    ↓
Poller (polls every 15s, also scans threads for @bot mentions)
    ↓
Handler.handle()
    ├── @bot mention? → _handle_direct_search() → targeted CloudWatch search
    └── normal message? → _handle_classify()
            ↓
        Classifier (Claude Haiku) → category + extracted IDs
            ↓
        ┌─────────── PARALLEL (ThreadPoolExecutor) ───────────┐
        │  Agent 1: CloudWatch Investigation (unstructured)    │
        │  Agent 2: Databricks SQL queries (structured)        │
        └──────────────────────────────────────────────────────┘
            ↓
        Structured Fact Extractor
            (normalizes payment reason, statuses, provider, rejection, sync issue)
            ↓
        Evidence-backed Playbook Matcher
            (matches only from confirmed facts, never Slack wording alone)
            ↓
        Response Mode Gate
            (auto_resolve / hybrid / escalate / triage)
            ↓
        Parent Agent: Claude Synthesizer
            (writes final [ROOT_CAUSE] / [CX_ADVICE] using facts + mode)
            ↓
        Assignment only for escalate/triage cases
            ↓
        Formatter → single Slack thread reply with bullet points + data sources
```

---

## File Structure

```
├── main.py                  # Entry point — wires everything, starts poller
├── config.py                # Loads .env into typed Config dataclass
├── handler.py               # Routes messages: classify flow OR direct @bot search
├── .env                     # Secrets (gitignored) — Slack, Anthropic, AWS creds
├── .env.example             # Template for .env
├── .gitignore
│
├── classifier/
│   ├── classifier.py        # Claude-powered CX query classifier (9 categories)
│   ├── extractor.py         # Extracts IDs (user_id, order_id, etc.) from text
│   └── fallback.py          # Regex fallback if Claude fails
│
├── cloudwatch/
│   ├── log_searcher.py      # Agent 1: CloudWatch Logs Insights queries, category-specific investigation
│   ├── log_analyzer.py      # Parent Agent: Claude synthesis prompts (general + KYC), structured output parser
│   └── formatter.py         # (Legacy, largely unused — formatting moved to slack_bot/formatter.py)
│
├── db_agent/
│   ├── __init__.py
│   └── db_searcher.py       # Agent 2: Databricks SQL queries against prod.silver_schema.*
│
├── knowledge_base/
│   ├── __init__.py
│   ├── case_facts.py        # Extracts normalized case facts from DB/log investigation
│   ├── cx_response_playbook.py  # Evidence-backed playbook matcher
│   └── response_engine.py   # Chooses response mode: auto_resolve / hybrid / escalate / triage
│
├── slack_bot/
│   ├── poller.py            # Polls Slack for new messages + scans threads for @bot mentions
│   └── formatter.py         # Formats Slack responses (full, triage, direct search)
│
├── assigner/
│   └── assigner.py          # Pure round-robin across 3 engineers, daily reset
│
├── metrics/
│   └── db.py                # SQLite metrics recording (cxbot_metrics.db)
│
├── kyc-resolution/
│   ├── SKILL.md             # KYC diagnosis skill doc
│   └── references/
│       └── rejection_reasons.md  # KYC rejection reasons knowledge base (fed to Claude)
│
├── dashboard/
│   ├── app.py               # Flask web dashboard — real-time monitoring (port 5050)
│   └── templates/
│       └── dashboard.html   # Single-page dashboard with Chart.js charts
│
├── tests/
│   ├── fixtures/
│   │   └── evidence_cases.json   # Regression fixtures for evidence-backed response flow
│   └── test_evidence_pipeline.py # Offline regression tests for facts + playbook + formatting
│
├── CONTEXT.md               # ← This file
├── CX-Tech-Bot-Overview.md  # Original project overview doc
└── PHASE1_PROMPT.md         # Original Phase 1 requirements
```

---

## Key Components

### Classifier (`classifier/classifier.py`)
- Uses Claude Haiku (`claude-haiku-4-5-20251001`)
- 9 categories: `payment_error_diagnosis`, `kyc_verification`, `db_lookup_status`, `referral_promo`, `bbps_partner_escalation`, `manual_backend_action`, `rate_fx_investigation`, `app_bug_engineering`, `other_needs_triage`
- Extracts: `user_ids`, `order_ids`, `payment_attempt_ids`, `fulfillment_ids`, `checkout_pay_ids`

### CloudWatch Investigation (`cloudwatch/log_searcher.py`)
- **Currently enabled categories:** `payment_error_diagnosis`, `kyc_verification`
- **Payment flow:** Search goblin → app-server → goms with order/user/payment IDs. Stop at first error hit. Device ID fallback search.
- **KYC flow:**
  - Step 1: Search verification-service by user_id (progressive window: 48h → 7d → 14d)
  - Step 1b: If errors found, dig deeper with request_id (up to 2)
  - Step 2: Search workflow-service by user_id
  - Step 3: Search workflow-service by device_id (if found from earlier steps)
- **Log group base:** `/ecs/vance-core/prod/london/01/`
- **Services:** goblin-service, app-server-service, goms-service, verification-service, workflow-service

### Parent Agent / Synthesizer (`cloudwatch/log_analyzer.py`)
- **Role:** Writes final Root Cause / CX Advice after facts and response mode are already decided
- Two prompts: `LOG_ANALYSIS_PROMPT` (general) and `KYC_ANALYSIS_PROMPT` (includes rejection_reasons.md KB)
- Both prompts accept `{log_lines}` placeholder which now includes both "Structured Database Records (Databricks)" and "CloudWatch Log Lines" sections
- DB records are marked as AUTHORITATIVE — Claude trusts exact field values over log parsing
- Claude outputs structured `[ROOT_CAUSE]` and `[CX_ADVICE]` sections with bullet points
- `parse_structured_analysis()` parses into `{"root_cause": "...", "cx_advice": "..."}`
- max_tokens: 500 (KYC), 600 (general) — increased for richer multi-source analysis
- Smart line selection to stay within token budget:
  - Each line truncated to 2000 chars (KYC logs have huge JSON/base64 bodies)
  - Error lines prioritized over generic INFO lines (using `ERROR_PATTERNS`)
  - Max 60 lines, max 120k total chars (~30k tokens)

### Databricks Agent (`db_agent/db_searcher.py`)
- **Role:** Agent 2 — runs category-specific SELECT queries against `prod.silver_schema.*`
- **CRITICAL: Read-only.** Only SELECT/SHOW/DESCRIBE queries. Never writes to production.
- **Enabled categories:** `payment_error_diagnosis`, `kyc_verification`, `db_lookup_status`, `rate_fx_investigation`, `bbps_partner_escalation`
- **Payment tables:** `goms_db_payment_attempts` (failure reasons), `goms_db_orders` (status), `appserver_db_checkout_payment_data` (response codes), `appserver_db_orders` (rates/corridor), `goms_db_fulfillments` (payout), `falcondb_falcon_transactions_v2` (partner payout)
- **KYC tables:** `appserver_db_user_kyc` (status, rejection_reason, provider), `appserver_db_vance_user_kyc` (rejection_count, resolving_providers)
- Returns `DBInvestigationResult` with structured `summary_text` that gets fed to the Parent Agent
- Connection via `databricks-sql-connector` library

### Structured Fact Layer (`knowledge_base/case_facts.py`)
- Builds `CaseFacts` from Databricks rows first, then CloudWatch evidence
- Extracts normalized facts such as:
  - `payment_failure_reason`
  - `provider` / `acquirer`
  - `order_status` / `order_sub_state`
  - `fulfillment_status` / `fulfillment_sub_status`
  - `payout_status` / `payout_error`
  - `kyc_status`
  - `rejection_reason` / `rejection_count`
  - `sync_issue`, `retryable`, `manual_action_needed`
- Converts known signals into a normalized set (`do_not_honour`, `provider_rejected`, `no_match`, `webhook_not_sent`, etc.)
- Produces `evidence_strength` and prompt-ready structured fact text

### Evidence-backed Playbook (`knowledge_base/cx_response_playbook.py`)
- Matches only from `CaseFacts`
- Never uses the original Slack message or Claude wording for playbook selection
- Each rule contains:
  - category
  - required facts
  - supporting facts
  - excluded facts
  - required/supporting/excluded signals
  - guidance text
  - `response_mode`
- If multiple rules tie, returns no match instead of guessing

### Response Engine (`knowledge_base/response_engine.py`)
- Chooses one of:
  - `auto_resolve`
  - `hybrid`
  - `escalate`
  - `triage`
- Uses classifier confidence + fact strength + playbook match + manual-action signals
- Auto-resolved cases do not require assignment

### Handler (`handler.py`)
- `handle()` → routes: `is_bot_mention` → `_handle_direct_search()`, else → `_handle_classify()`
- **Multi-agent orchestration:** `_investigate_parallel()` runs Agent 1 + Agent 2 concurrently via `ThreadPoolExecutor`
- Results from both agents are converted into `CaseFacts`
- Playbook is matched from facts, not message text
- Response mode is decided before Claude synthesis
- Claude receives:
  - response mode
  - structured facts
  - approved playbook guidance if one matched
- When synthesis is unavailable, handler generates a safe fallback from facts
- Assignment happens only for `escalate` and `triage`
- `_handle_direct_search()` → parses @bot command, extracts UUID + service name, runs targeted search (14-day window)
- `_get_ids_from_parent()` → if no UUID in command, grabs it from the parent thread message

### Poller (`slack_bot/poller.py`)
- Polls `conversations.history` every 15s for new top-level messages
- Every 2nd cycle, also runs `_poll_thread_mentions()` — scans last 10 threads for @bot mentions
- Dedup via `processing` set + `completed` dict (1-hour TTL)
- Eyes reaction on pickup, checkmark on completion
- Cursor persistence to `.cxbot_cursor` file

### Formatter (`slack_bot/formatter.py`)
- `format_full_response()` — main response: Root Cause + CX Advice + footer with assignment or auto-resolved status
- `format_triage_response()` — low-confidence fallback
- `format_direct_search_response()` — for @bot direct search results
- Tags the original poster (`<@user_id>`) in CX Advice section
- Approved playbook guidance is appended as extra bullets under `CX Advice`
- No separate "Playbook Guidance" section is shown to users

### Assigner (`assigner/assigner.py`)
- Pure round-robin across 3 engineers
- **Engineers:** Vatsal (U0A0E1KCDM2), Adarsh (U0A0E1KSDQC), Kanishk (U0A0716P36Z)
- State persisted to `cxbot_assigner_state.json`, resets daily

### Dashboard (`dashboard/app.py`)
- **Flask web app** on port 5050 — real-time CX query monitoring
- **Auto-refreshes** every 30 seconds (no manual reload needed)
- **Charts** via Chart.js (CDN, no build step):
  - Daily query volume (bar chart, 30 days)
  - Category distribution (doughnut chart, 7 days)
  - Engineer workload (horizontal bar, 7 days)
- **Stat cards:** Today's queries, total all-time, avg response time, error rate, triage rate
- **Live feed:** Most recent 20 queries with category badges, summaries, timestamps
- **API endpoints:** `/api/stats`, `/api/categories`, `/api/daily-volume`, `/api/hourly-volume`, `/api/engineers`, `/api/response-times`, `/api/recent`, `/api/health`
- Reads from the same `cxbot_metrics.db` as the bot (read-only, separate connection)
- Run with: `python dashboard/app.py`

### Tests (`tests/test_evidence_pipeline.py`)
- Offline regression pack for evidence-backed response flow
- Covers:
  - TrueLayer cancel flow
  - GOMS `CREATED` stuck
  - Falcon/GOMS sync
  - `NO_MATCH`
  - `DOCUMENTS_EXPIRED`
  - unknown cases that must not match a playbook
- Also verifies formatter keeps playbook bullets under `CX Advice`

---

## @Bot Direct Search Feature

Users can tag the bot in any thread to do a targeted CloudWatch search:

```
@bot search <user_id> in <service-name>
@bot check verification for <user_id>
@bot search goblin                        ← grabs user_id from parent thread
```

**Available services:** `goblin`, `app-server`, `goms`, `verification`, `workflow`

- Service aliases defined in `SERVICE_ALIASES` dict in `log_searcher.py`
- Uses 14-day search window
- Direct search also builds facts from the found logs before attaching playbook guidance
- Posts Root Cause + CX Advice in the thread

---

## Response Format

Single Slack thread reply with:
1. **Root Cause** — bullet points with specific errors/codes/statuses
2. **CX Advice** — actionable bullet points for CX agent (tags the poster)
3. **Footer** — either assigned engineer + services searched, or auto-resolved status

Approved playbook guidance, if present, is appended under **CX Advice** as extra bullets.
No product bug section (removed). Point-to-point bullet style, not paragraphs.

---

## Error Patterns (`ERROR_PATTERNS` in `log_searcher.py`)

Covers:
- General: ERROR, fail/failure, exception, timeout, declined, rejected
- Payment: EXCEEDS_DAILY_LIMIT, 3DS, acquirer reject, payment expired
- KYC: kyc fail/reject/stuck, NO_MATCH, DOCUMENT_EXPIRED, onboarding failed, EFR timeout
- KYC JSON: `"status":"REJECTED"`, `"rejection_reasons":[non-empty]`, `"rejection_count": non-zero`
- Other: rate mismatch, webhook fail, partner fail

---

## KYC Providers by Region

| Region | Provider | Notes |
|--------|----------|-------|
| UAE    | Lulu (via EFR) | Native flow. Common: NO_MATCH, DOCUMENT_EXPIRED, no active visa |
| UK/US  | Persona | SDK flow. "Pending" = manual review |
| EU     | Sumsub (→ Persona) | SDK flow, migrating |

---

## Tech Stack

- **Python 3.14** with venv (`.venv/`)
- **Claude Haiku** (`claude-haiku-4-5-20251001`) for classification + log analysis
- **Anthropic SDK** (`anthropic`)
- **Slack SDK** (`slack-sdk`) — polling, not events API
- **Boto3** — CloudWatch Logs Insights
- **Databricks SQL Connector** (`databricks-sql-connector`) — structured DB queries
- **SQLite** (`cxbot_metrics.db`) — metrics, NOT Postgres
- **AWS Region:** `eu-west-2` (London)
- **Databricks:** `prod.silver_schema.*` (Unity Catalog, read-only)

---

## Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=
SLACK_BOT_TOKEN=
SLACK_CHANNEL_ID=C0AKF9U2RCL
CXBOT_ENV=stage
CXBOT_POLL_INTERVAL=15
CXBOT_CURSOR_FILE=.cxbot_cursor
CXBOT_DB_PATH=cxbot_metrics.db
CLASSIFIER_MODEL=claude-haiku-4-5-20251001
AWS_ACCESS_KEY_ID=        # STS temporary credentials
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
AWS_REGION=eu-west-2
CW_GOBLIN_LOG_GROUP=/ecs/vance-core/prod/london/01/goblin-service-logs
DATABRICKS_SERVER_HOSTNAME=
DATABRICKS_HTTP_PATH=
DATABRICKS_ACCESS_TOKEN=
DASHBOARD_PORT=5050
```

---

## Running the Bot

```bash
# Start
.venv/bin/python main.py

# Kill
pkill -f "main.py"

# Reset state and restart
pkill -f "main.py"; sleep 1 && rm -f .cxbot_cursor cxbot_assigner_state.json && .venv/bin/python main.py

# Dashboard (runs separately from bot)
.venv/bin/python dashboard/app.py
# → http://localhost:5050
```

---

## GitHub

**Repo:** https://github.com/kanishkkhandelwal-aspora/cx-techbot
**Branch:** `codex/evidence-backed-responses`
**Commit:** `1552b20` — `Add evidence-backed response pipeline`

---

## Change Log

### 2026-03-19 (Dashboard + Bot Hardening)
- **Real-time Dashboard**: New Flask web app (`dashboard/app.py`) on port 5050
  - Dark-themed single-page dashboard with Chart.js charts
  - Stats cards: today's queries, total, avg response time, error rate, triage rate
  - Daily volume bar chart (30 days), category doughnut (7 days), engineer workload
  - Live feed of recent queries with category badges and summaries
  - Auto-refreshes every 30 seconds
  - REST API endpoints for all dashboard data
- **Enriched MetricsDB**: Added 8 new columns for investigation tracking:
  - `response_time_ms`, `data_sources`, `error_found`, `root_cause_summary`,
  - `services_searched`, `is_triage`, `cw_log_lines`, `db_rows`
  - Safe migrations for existing DBs (ALTERs wrapped in try/except)
  - Dashboard query methods: `get_stats_summary()`, `get_category_distribution()`, etc.
- **Response Time Tracking**: Handler now records processing time per query
- **AWS Credential Health Check**: `check_credentials()` method on CloudWatchSearcher
  - Detects expired STS tokens on startup and logs clear error message
  - Called in `main.py` startup sequence
- **Claude API Timeout**: Added 30s hard timeout to prevent hanging on synthesis calls
- **Thread-safe SQLite**: `check_same_thread=False` for MetricsDB connection
- Added `flask>=3.0.0` to requirements.txt

### 2026-03-21 (Evidence-backed Responses)
- Added `knowledge_base/case_facts.py` to extract normalized evidence-backed facts from Databricks + CloudWatch
- Added `knowledge_base/cx_response_playbook.py` to match approved responses only from confirmed facts
- Added `knowledge_base/response_engine.py` to choose response mode: `auto_resolve`, `hybrid`, `escalate`, `triage`
- Reworked handler flow to:
  - investigate first
  - extract facts
  - choose playbook from facts
  - decide response mode
  - call Claude only after those are fixed
- Removed Slack-message-driven playbook matching
- Auto-resolved cases no longer require engineer assignment
- Updated formatter so approved playbook guidance is appended under `CX Advice`
- Added offline regression coverage in `tests/test_evidence_pipeline.py`
- Added fixture pack in `tests/fixtures/evidence_cases.json`

### 2026-03-18
- **Multi-agent architecture**: Bot now runs two investigation agents in parallel:
  - **Agent 1 (CloudWatch)**: Searches unstructured service logs (existing)
  - **Agent 2 (Databricks)**: Runs targeted SQL queries against `prod.silver_schema.*` tables (NEW)
  - **Parent Agent (Claude)**: Synthesizes both inputs into unified Root Cause + CX Advice
- New `db_agent/` package with `DatabricksSearcher` class — category-aware SQL query builder
  - Payment: queries `goms_db_payment_attempts`, `goms_db_orders`, `appserver_db_checkout_payment_data`, `appserver_db_orders`, `goms_db_fulfillments`, `falcondb_falcon_transactions_v2`
  - KYC: queries `appserver_db_user_kyc`, `appserver_db_vance_user_kyc`
  - Status lookup, Rate/FX, BBPS also supported
- `ThreadPoolExecutor` in handler runs both agents concurrently (max 60s timeout)
- Claude prompts updated to accept both structured DB records + log lines, with DB records marked as AUTHORITATIVE
- max_tokens increased: 500 (KYC), 600 (general) for richer multi-source analysis
- Formatter now shows data sources (CloudWatch, Databricks) in footer
- Added `databricks-sql-connector` to requirements.txt
- Added `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_ACCESS_TOKEN` env vars
- **Safety: All Databricks queries are read-only (SELECT only). Write operations are blocked in code.**

### 2026-03-19
- **Classifier overhaul**: Sharpened category boundaries — payment failures no longer misclassified as db_lookup_status
  - Added explicit decision rule priority order (first match wins)
  - Added "IMPORTANT: payment_error_diagnosis vs db_lookup_status" disambiguation section with examples
  - Added many more payment failure signals ("unable to transact", "transfer failed", "money deducted but failed", etc.)
  - db_lookup_status now ONLY for status checks/lookups, not failures
- **Payment investigation**: No longer returns early on first error — collects ALL logs across all services for full context
- **Handler sends ALL lines to Claude for every category** (not just KYC) — Claude needs full context for point-to-point answers
- **Detailed payment-specific Claude prompt**: Added Goblin/App-Server/GOMS architecture, payment flow stages, common failure patterns with error codes, strict bullet-point examples
- **Payment max_tokens increased**: 350 → 500 for richer analysis
- **Keyword fallback updated**: Added payment failure keywords, removed "money deducted but" from db_lookup (moved to payment)
- Progressive search windows now work for payments (48h → 7d → 14d)

### 2026-03-16
- Added @bot direct search feature (tag bot in thread to search specific service)
- Poller now scans threads for @bot mentions every 2nd poll cycle
- Added `SERVICE_ALIASES` and `SERVICE_DISPLAY_NAMES` mappings
- Added `format_direct_search_response()` formatter
- Added `_handle_direct_search()`, `_get_ids_from_parent()` in handler
- Progressive verification-service search window (48h → 7d → 14d) for KYC
- Handler fallback: helpful Root Cause / CX Advice when CloudWatch returns 0 results
- Smart line selection in `analyze_logs_with_claude()`: truncate lines to 2000 chars, prioritize error lines, cap at 60 lines / 120k chars (fixes 200k token overflow on large KYC logs)
- Initial commit pushed to GitHub

### 2026-03-13
- Added KYC JSON response body patterns to `ERROR_PATTERNS`
- Handler sends ALL lines (not just error_lines) to Claude for KYC category
- Simplified KYC investigation: direct user_id search instead of two-hop kyc_request_id
- Merged two Slack messages into single combined response
- Response format: point-to-point bullets, [ROOT_CAUSE] + [CX_ADVICE] only (product bug removed)
- Updated Claude prompts for structured output with bullet points

### 2026-03-12
- Phase 1 build: classifier, assigner, poller, handler, metrics
- CloudWatch integration for payment_error_diagnosis
- Claude-powered log analysis
- KYC verification category added
