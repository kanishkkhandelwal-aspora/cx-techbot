# CX-Tech Bot — Project Context

> **Last updated:** 2026-03-16
> Update this file every time code changes are made.

---

## What This Is

A Python Slack bot that automatically classifies CX (Customer Experience) queries, investigates them via CloudWatch logs, analyzes root causes using Claude AI, and assigns them to engineers in round-robin fashion.

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
        CloudWatch Investigation (category-specific flow)
            ↓
        Claude Log Analysis → structured [ROOT_CAUSE] / [CX_ADVICE]
            ↓
        Round-robin Assignment → one of 3 engineers
            ↓
        Formatter → single Slack thread reply with bullet points
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
│   ├── log_searcher.py      # CloudWatch Logs Insights queries, category-specific investigation
│   ├── log_analyzer.py      # Claude log analysis prompts (general + KYC), structured output parser
│   └── formatter.py         # (Legacy, largely unused — formatting moved to slack_bot/formatter.py)
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

### Log Analyzer (`cloudwatch/log_analyzer.py`)
- Two prompts: `LOG_ANALYSIS_PROMPT` (general) and `KYC_ANALYSIS_PROMPT` (includes rejection_reasons.md KB)
- Claude outputs structured `[ROOT_CAUSE]` and `[CX_ADVICE]` sections with bullet points
- `parse_structured_analysis()` parses into `{"root_cause": "...", "cx_advice": "..."}`
- max_tokens: 400 (KYC), 350 (general)
- Smart line selection to stay within token budget:
  - Each line truncated to 2000 chars (KYC logs have huge JSON/base64 bodies)
  - Error lines prioritized over generic INFO lines (using `ERROR_PATTERNS`)
  - Max 60 lines, max 120k total chars (~30k tokens)

### Handler (`handler.py`)
- `handle()` → routes: `is_bot_mention` → `_handle_direct_search()`, else → `_handle_classify()`
- For KYC, sends ALL log lines to Claude (not just error_lines) — JSON response bodies contain diagnosis data
- When CloudWatch returns 0 results, generates a helpful fallback analysis
- `_handle_direct_search()` → parses @bot command, extracts UUID + service name, runs targeted search (14-day window)
- `_get_ids_from_parent()` → if no UUID in command, grabs it from the parent thread message

### Poller (`slack_bot/poller.py`)
- Polls `conversations.history` every 15s for new top-level messages
- Every 2nd cycle, also runs `_poll_thread_mentions()` — scans last 10 threads for @bot mentions
- Dedup via `processing` set + `completed` dict (1-hour TTL)
- Eyes reaction on pickup, checkmark on completion
- Cursor persistence to `.cxbot_cursor` file

### Formatter (`slack_bot/formatter.py`)
- `format_full_response()` — main response: Root Cause + CX Advice + footer with assignment + services
- `format_triage_response()` — low-confidence fallback
- `format_direct_search_response()` — for @bot direct search results
- Tags the original poster (`<@user_id>`) in CX Advice section

### Assigner (`assigner/assigner.py`)
- Pure round-robin across 3 engineers
- **Engineers:** Vatsal (U0A0E1KCDM2), Adarsh (U0A0E1KSDQC), Kanishk (U0A0716P36Z)
- State persisted to `cxbot_assigner_state.json`, resets daily

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
- Claude analyzes results with appropriate prompt (KYC vs general)
- Posts Root Cause + CX Advice in the thread

---

## Response Format

Single Slack thread reply with:
1. **Root Cause** — bullet points with specific errors/codes/statuses
2. **CX Advice** — actionable bullet points for CX agent (tags the poster)
3. **Footer** — assigned engineer + services searched

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
- **SQLite** (`cxbot_metrics.db`) — metrics, NOT Postgres
- **AWS Region:** `eu-west-2` (London)

---

## Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=
SLACK_BOT_TOKEN=
SLACK_CHANNEL_ID=C0AKF9U2RCL
AWS_ACCESS_KEY_ID=        # STS temporary credentials
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
AWS_REGION=eu-west-2
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
```

---

## GitHub

**Repo:** https://github.com/kanishkkhandelwal-aspora/cx-techbot

---

## Change Log

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
