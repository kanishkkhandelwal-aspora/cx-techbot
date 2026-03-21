# CX-Tech Bot — Active Session Context

> **Auto-updated before each session ends. Read this first when resuming.**
> **Last updated:** 2026-03-21 14:55 IST

---

## Current State

### What's Running
- **Bot**: `.venv/bin/python main.py` — polling #cx-tech-queries every 15s
- **Dashboard**: Not running
- **Databricks**: Connected to **prod** workspace (`dbc-02d72862-314d.cloud.databricks.com`)
- **CloudWatch**: Connected with refreshed AWS STS credentials
- **Git**: Evidence-backed response changes committed and pushed on `codex/evidence-backed-responses`

### What's Working
- ✅ Multi-agent architecture: CW (Agent 1) + Databricks (Agent 2) → Claude synthesis
- ✅ Databricks queries all 12 tables in `prod.silver_schema.*` (read-only)
- ✅ Payment investigation: 6 DB tables queried
- ✅ KYC investigation: 2 DB tables queried, rejection_reasons KB loaded
- ✅ Evidence-backed response flow: investigate → facts → playbook → response mode → Claude
- ✅ Fact-based playbook matching: no Slack-message-only playbook selection
- ✅ Response mode gate: `auto_resolve`, `hybrid`, `escalate`, `triage`
- ✅ Offline regression pack added for payment, KYC, and status-sync cases

### What Needs Work
1. **Other categories** — referral_promo, manual_backend_action, and app_bug still need evidence-backed support
2. **Escalation routing** — still uses generic round-robin, not contact-specific routing
3. **AWS credentials** — STS tokens expire every ~12h, no auto-refresh
4. **Feedback loop** — no Slack reaction capture or correction capture yet
5. **Dashboard** — does not yet show response mode / playbook hit-rate review

### Key Decisions Made
- Databricks is READ-ONLY. Only SELECT queries. Production data.
- Playbook guidance must be chosen only from evidence-backed facts, never from Slack wording alone
- Claude is the writer, not the decider: facts + response mode are fixed before synthesis
- Auto-resolved cases do not require engineer assignment
- Claude Haiku for classification + synthesis (not Sonnet — cost)
- Single Slack response per query (not multiple messages)
- Bot response format: [ROOT_CAUSE] bullets + [CX_ADVICE] bullets

---

## File Map (Modified This Session)

| File | What Changed |
|------|-------------|
| `.env` | Refreshed AWS STS credentials, prod Databricks config already in use |
| `handler.py` | Reworked flow to: classify → investigate → facts → playbook → response mode → Claude |
| `knowledge_base/case_facts.py` | NEW — extracts normalized payment/KYC/status facts from evidence |
| `knowledge_base/cx_response_playbook.py` | NEW — fact-based playbook matcher |
| `knowledge_base/response_engine.py` | NEW — response mode decision layer |
| `slack_bot/formatter.py` | Auto-resolved cases no longer force assignment; playbook guidance stays under CX advice |
| `metrics/db.py` | Metrics now allow auto-resolved cases without a real assignee |
| `tests/fixtures/evidence_cases.json` | NEW — regression fixtures for payment/KYC/status cases |
| `tests/test_evidence_pipeline.py` | NEW — offline regression coverage for evidence-backed flow |

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

## Contacts

### Engineers (round-robin assignment)
- Vatsal (U0A0E1KCDM2)
- Adarsh (U0A0E1KSDQC)
- Kanishk (U0A0716P36Z)

### Escalation contacts referenced in playbooks / docs
- Ayush — Falcon/GOMS sync, webhook resend
- Raj — Falcon/GOMS sync, webhook resend
- Shyam Tayal — CNR / await / refund-style ops follow-up

> Slack user IDs for Ayush, Raj, and Shyam Tayal are not stored in repo.

---

## GitHub
Repo: https://github.com/kanishkkhandelwal-aspora/cx-techbot
Branch pushed: `codex/evidence-backed-responses`
Commit: `1552b20` — `Add evidence-backed response pipeline`
