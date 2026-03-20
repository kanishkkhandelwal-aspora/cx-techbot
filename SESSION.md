# CX-Tech Bot — Active Session Context

> **Auto-updated before each session ends. Read this first when resuming.**
> **Last updated:** 2026-03-19 17:30 IST

---

## Current State

### What's Running
- **Bot**: `python3 main.py` — polling #cx-tech-queries every 15s
- **Dashboard**: `python3 dashboard/app.py` — http://localhost:5050
- **Databricks**: Connected to **stage** workspace (`dbc-2413ffb5-f638.cloud.databricks.com`)
- **CloudWatch**: ❌ AWS STS credentials EXPIRED — needs fresh tokens in .env
- **Git**: Local changes NOT yet committed/pushed

### What's Working
- ✅ Multi-agent architecture: CW (Agent 1) + Databricks (Agent 2) → Claude synthesis
- ✅ Databricks queries all 12 tables in `prod.silver_schema.*` (read-only)
- ✅ Payment investigation: 6 DB tables queried
- ✅ KYC investigation: 2 DB tables queried, rejection_reasons KB loaded
- ✅ Dashboard: Flask app on port 5050 with Chart.js charts, auto-refresh 30s
- ✅ 74 total queries processed, 3 today

### What Needs Work
1. **Bot response quality** — needs to be more precise, shorter, actionable (IN PROGRESS)
2. **Other categories** — referral_promo, bbps, manual_backend_action, app_bug need CW + DB support
3. **AWS credentials** — STS tokens expire every ~12h, no auto-refresh
4. **Git push** — uncommitted: db_agent/, dashboard/, classifier fixes, handler multi-agent
5. **Prod Databricks** — currently on stage; swap 3 env vars when ready

### Key Decisions Made
- Databricks is READ-ONLY. Only SELECT queries. Production data.
- Stage workspace for testing, prod catalog (`prod.silver_schema.*`) has real data
- Claude Haiku for classification + synthesis (not Sonnet — cost)
- Single Slack response per query (not multiple messages)
- Bot response format: [ROOT_CAUSE] bullets + [CX_ADVICE] bullets

---

## File Map (Modified This Session)

| File | What Changed |
|------|-------------|
| `.env` | Added DATABRICKS_* credentials (stage) |
| `db_agent/db_searcher.py` | NEW — Databricks SQL agent (7 payment + 2 KYC tables) |
| `db_agent/__init__.py` | NEW — package init |
| `handler.py` | Multi-agent parallel execution, DB integration |
| `cloudwatch/log_analyzer.py` | Prompts accept DB summary, payment-specific prompt |
| `classifier/classifier.py` | Priority-ordered rules, payment vs db_lookup disambiguation |
| `classifier/fallback.py` | More payment keywords |
| `config.py` | Databricks env vars |
| `dashboard/app.py` | EXISTS but not committed — Flask dashboard |
| `dashboard/templates/dashboard.html` | EXISTS but not committed — Chart.js UI |
| `metrics/db.py` | Dashboard query methods, v2 columns |
| `CONTEXT.md` | Updated with multi-agent architecture docs |

---

## Databricks Schema Map

### Payment Tables
- `goms_db_payment_attempts` — payment_attempt_id, status, **reason**, **meta_failure_reason**, meta_response_summary
- `goms_db_orders` — order_id, owner_id, status, sub_state, amount
- `goms_db_payments` — payment_id, payment_status, sub_status, owner_id
- `goms_db_fulfillments` — fulfillment_id, order_id, status, sub_status (CDC)
- `appserver_db_checkout_payment_data` — checkout_payment_id, order_id, status, **response_code**, **response_summary**, risk_flagged
- `appserver_db_orders` — order_id, order_status, send_amount, receive_amount, currency_from/to, transfer_rate
- `falcondb_falcon_transactions_v2` — transaction_id, client_txn_id, status, **error**, exchange_rate (CDC)

### KYC Tables
- `appserver_db_user_kyc` — user_id, **kyc_status**, **provider**, **rejection_reason**, **rejection_reasons**, rejection_count
- `appserver_db_vance_user_kyc` — user_id, **kyc_status**, **rejection_reasons**, resolving_providers

### Referral/Rewards Tables (not yet integrated)
- `rewards_db_reward` — reward_id, beneficiary_id, status, amount, trigger_type
- `rewards_db_wallet` — user_id, balance, type
- `rewards_db_campaign` — campaign_id, code, template
- `rewards_db_pariticipation` — participant_id, campaign_id, status
- `rewards_db_task` — participant_id, task_id, status, completion_percentage
- `analytics_referrals_master` — referrer_id, referee_id, referral_code

### BBPS Tables (not yet integrated)
- `bbps_db_bill` — bill_id, user_id, biller_id, status, inr_amt
- `bbps_db_biller` — display_name, category_id
- `bbps_db_mobile_recharges`

### User Tables (not yet integrated)
- `user_vault_db_users`, `user_vault_db_user_identities`
- `appserver_db_user_permissions_v2`, `appserver_db_user_tier`

---

## Engineers
- Vatsal (U0A0E1KCDM2)
- Adarsh (U0A0E1KSDQC)
- Kanishk (U0A0716P36Z)

---

## GitHub
Repo: https://github.com/kanishkkhandelwal-aspora/cx-techbot
Last push: 2026-03-16 (dashboard + db_agent not yet pushed)
