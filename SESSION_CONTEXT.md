# CX-Tech Bot — Session Context
> Auto-updated before each session ends. Read this first when resuming.

## Last Updated: 2026-03-19

## Project Overview
Python Slack bot monitoring #cx-tech-queries. Classifies CX queries into 9 categories, investigates via CloudWatch logs + Databricks SQL (multi-agent architecture), and posts structured responses with Root Cause and CX Advice.

## Architecture
```
Slack Message → Classifier (Claude Haiku) → Category
                                          ↓
                     ┌────────────────────┼────────────────────┐
                     ↓                                         ↓
              Agent 1: CloudWatch                    Agent 2: Databricks
              (unstructured logs)                    (structured DB records)
                     ↓                                         ↓
                     └────────────────────┬────────────────────┘
                                          ↓
                              Parent Agent: Claude
                              (synthesize → ROOT_CAUSE + CX_ADVICE)
                                          ↓
                              Slack Response + Assignment
```

## Key Files
| File | Purpose |
|------|---------|
| `handler.py` | Central routing: classify → investigate (parallel) → synthesize → post |
| `cloudwatch/log_searcher.py` | Agent 1: CloudWatch log search with progressive windows (48h→7d→14d) |
| `cloudwatch/log_analyzer.py` | Parent Agent: Claude prompts for synthesis + response parsing |
| `db_agent/db_searcher.py` | Agent 2: Databricks SQL queries (READ-ONLY) |
| `classifier/classifier.py` | Claude-based query classifier (9 categories) |
| `classifier/fallback.py` | Keyword fallback when Claude classifier fails |
| `slack_bot/poller.py` | Slack message polling |
| `slack_bot/formatter.py` | Slack response formatting |
| `assigner/assigner.py` | Round-robin engineer assignment |
| `metrics/db.py` | SQLite metrics recording |
| `config.py` | Config loading from .env |
| `main.py` | Entry point |
| `dashboard.html` | Real-time dashboard (reads from metrics API) |

## 9 Categories
1. `payment_error_diagnosis` — CW: goblin, app-server, goms | DB: 7 payment tables
2. `kyc_verification` — CW: verification, workflow | DB: 2 KYC tables
3. `db_lookup_status` — DB: orders, fulfillments, falcon
4. `rate_fx_investigation` — DB: appserver_orders, falcon
5. `bbps_partner_escalation` — DB: reuses payment tables
6. `referral_promo` — NOT YET BUILT (tables: rewards_db_*, analytics_referrals_master)
7. `manual_backend_action` — NOT YET BUILT
8. `app_bug_engineering` — NOT YET BUILT
9. `other_needs_triage` — Fallback

## Databricks Tables (prod.silver_schema.*)
### Payment
- `goms_db_payment_attempts` — payment_attempt_id, status, reason, meta_failure_reason
- `goms_db_orders` — order_id, owner_id, status, sub_state, amount
- `goms_db_payments` — payment_id, payment_status, sub_status, owner_id (**NOT YET QUERIED**)
- `goms_db_fulfillments` — fulfillment_id, order_id, status, sub_status
- `appserver_db_checkout_payment_data` — checkout_payment_id, response_code, response_summary, risk_flagged
- `appserver_db_orders` — order_id, order_status, transfer_rate, fulfillment_provider
- `falcondb_falcon_transactions_v2` — transaction_id, status, error, exchange_rate

### KYC
- `appserver_db_user_kyc` — user_id, kyc_status, provider, rejection_reason, rejection_count
- `appserver_db_vance_user_kyc` — user_id, kyc_status, rejection_reasons, resolving_providers

### Referral/Promo (NOT YET INTEGRATED)
- `rewards_db_reward` — reward_id, beneficiary_id, status, amount
- `rewards_db_wallet` — user_id, balance
- `rewards_db_campaign` — campaign_id, code, template
- `rewards_db_pariticipation` — participant_id, campaign_id, status
- `rewards_db_task` — participant_id, task_id, status, completion_percentage
- `analytics_referrals_master` — referrer_id, referee_id, referral_code

### BBPS (NOT YET INTEGRATED)
- `bbps_db_bill` — bill_id, user_id, biller_id, status
- `bbps_db_biller` — display_name, category_id
- `bbps_db_quote`, `bbps_db_mobile_recharges`

## Credentials
- AWS CloudWatch: STS tokens in .env (expire frequently — user provides new ones)
- Databricks: Personal access token in .env (90-day lifetime)
  - **Prod**: dbc-02d72862-314d.cloud.databricks.com, warehouse 557f5c781a55d9e5
  - **Stage** (testing): dbc-2413ffb5-f638.cloud.databricks.com, warehouse 7b559589f8652677
- Anthropic: API key in .env
- Slack: Bot token in .env, channel C0AKF9U2RCL

## Critical Constraints
1. **Databricks is READ-ONLY** — Only SELECT/SHOW/DESCRIBE. Never write/delete/drop. Production data.
2. **Bot must never crash** — All errors caught and escalated gracefully.
3. **Both agents run in parallel** — ThreadPoolExecutor with 2 workers.
4. **Progressive search windows** — 48h → 7d → 14d for both KYC and payment CW queries.
5. **Response consistency** — Must always follow [ROOT_CAUSE] + [CX_ADVICE] format. No filler, no generic advice.

## Known Issues / TODOs
- [ ] AWS STS tokens expire frequently — no auto-refresh
- [ ] `goms_db_payments` table not queried yet
- [ ] referral_promo category has no investigation
- [ ] manual_backend_action has no investigation
- [ ] app_bug_engineering has no investigation
- [ ] Dashboard needs API server (Flask) to serve data from metrics DB
- [ ] Bot response inconsistency — Claude sometimes ignores format rules

## Git
- Repo: https://github.com/kanishkkhandelwal-aspora/cx-techbot
- Commit signing: ED25519-SK hardware key (must be plugged in)

## Engineers (Round Robin)
- Vatsal Gajjar
- Adarsh
- Kanishk
