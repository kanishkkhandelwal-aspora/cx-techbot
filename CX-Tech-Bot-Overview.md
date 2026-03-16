# CX-Tech Query Resolution Bot — Project Overview

---

## 1. Problem Statement

The CX-Tech team receives a high volume of customer-facing technical queries on the **#cx-tech-queries** Slack channel daily. These queries span payment failures, KYC issues, order status lookups, referral problems, partner escalations, rate discrepancies, and app bugs — but each one requires a human to manually look up data across AlphaDesk, Metabase, CloudWatch, and internal APIs before responding.

This creates three core problems:

- **Slow resolution times** — even straightforward queries (e.g., a known payment failure reason) take 5-15 minutes because a human must context-switch, look up the order, run a SQL query, match the failure reason, and type a response.
- **Knowledge concentration** — resolution steps live in a PDF guide and in people's heads. When the experienced team members are unavailable, queries stall or get resolved incorrectly.
- **Zero visibility** — there is no dashboard tracking query volume by category, auto-resolution rates, recurring failure patterns, average response time, or escalation load per engineer.

---

## 2. Proposed Solution

Build a **CX-Tech Query Resolution Bot** that monitors #cx-tech-queries, classifies every incoming message into one of 9 categories, follows the resolution playbook from the CX-Tech Guide automatically where possible, and posts structured responses back to Slack — escalating to the right person with full context when it cannot resolve autonomously.

### 2.1 Three-Tier Automation Model

Every query is handled at one of three automation tiers depending on its category:

| Tier | Behaviour | Categories |
|------|-----------|------------|
| **Tier 1 — Auto-Resolve** | Bot handles end-to-end. Looks up data, matches the known resolution from the guide, posts the answer. No human needed. | Payment Error Diagnosis, DB Lookup & Status Check |
| **Tier 2 — Investigate + Escalate** | Bot pulls relevant data (logs, order status, service health), summarizes findings, and tags the correct person with full context ready for action. | KYC / Verification Service Check, Referral / Promo System Check, Rate / FX Investigation |
| **Tier 3 — Smart Escalation** | Bot identifies the request type, extracts all relevant details, and immediately routes to the right team or partner with a structured handoff. | BBPS / Partner Escalation, Manual Backend Action, App Bug / Engineering Escalation, Other / Needs Triage |

---

## 3. Query Categories — Detailed Breakdown

### 3.1 Payment Error Diagnosis — *Tier 1 (Auto-Resolve)*

**What it covers:** Debit card failures, credit card declines, payment attempt timeouts, acquirer rejections, 3DS failures, daily limit breaches, UPI failures, wallet failures.

**Bot Workflow:**
1. Extract order ID / user ID / payment attempt ID from the Slack message
2. Look up the order in AlphaDesk — get payment attempt ID, order type (App-Server vs GOMS), payment method
3. If GOMS order — query Metabase for the failure reason
4. Match failure reason against the known resolution table (deterministic, no LLM):

| Failure Reason | Bot Response |
|----------------|-------------|
| Payment expired (15 min, no acquirer ref) | Advise customer to contact bank or try a different non-prepaid card |
| 3DS protocol issue | Advise customer to contact bank or try a different non-prepaid card |
| concurrent_authentication_request | Advise customer to let one auth session fail completely before retrying |
| EXCEEDS_DAILY_LIMIT | Advise customer to contact bank and request international transfer limit increase |
| *Unknown / unmapped reason* | Escalate to backend engineer with full failure reason + order context |

5. Post the resolution to the Slack thread with order details

**Expected resolution time:** 10-30 seconds.

---

### 3.2 KYC / Verification Service Check — *Tier 2 (Investigate + Escalate)*

**What it covers:** KYC pending/stuck, document verification failures, compliance service errors, identity verification timeouts, PEP/sanctions screening holds.

**Bot Workflow:**
1. Extract user ID from the Slack message
2. Look up user in AlphaDesk — get KYC status, verification stage, last verification attempt timestamp
3. Query compliance-service logs in CloudWatch for errors related to this user/device
4. Classify the sub-case:
   - **Verification stuck/pending** — check if the external provider (Onfido, etc.) returned an error or is simply slow
   - **Verification rejected** — surface the rejection reason from logs
   - **Service outage** — check compliance-service health via Datadog monitors
5. Post findings to Slack thread:
   - If a clear reason is found — post the reason + recommended CX response
   - If unclear — tag compliance team / backend engineer with log excerpts

**Expected resolution time:** 30-60 seconds for data gathering.

---

### 3.3 DB Lookup & Status Check — *Tier 1 (Auto-Resolve)*

**What it covers:** Order status lookups, transfer status discrepancies (AlphaDesk vs Falcon), transaction history queries, fulfillment status checks, refund status, CNR (Credit Not Received) detection.

**Bot Workflow:**
1. Extract order ID / user ID / fulfillment ID from the message
2. Look up in AlphaDesk — get order status, Falcon status, payment status, timestamps
3. If AlphaDesk status and Falcon status are inconsistent:
   - **Case A (Event Flow Issue):** Event didn't propagate — recommend manual re-trigger, tag @Ayush and @Raj
   - **Case B (CNR / Await):** Funds reversed — check if US order (tag @Shyam Tayal), check if > 10 days old, tag GOMS team
4. If statuses are consistent — post the current status with a plain-language summary
5. For refund queries — pull refund status from Metabase and post timeline

**Expected resolution time:** 10-45 seconds depending on sub-case.

---

### 3.4 Referral / Promo System Check — *Tier 2 (Investigate + Escalate)*

**What it covers:** Referral rewards not credited, promo code failures, cashback not applied, referral link tracking issues, campaign-specific errors.

**Bot Workflow:**
1. Extract user ID, referral/promo code, order ID from the message
2. Look up user in AlphaDesk — check referral status, promo application history
3. Query referral/promo service logs for this user:
   - Was the promo code valid at time of use?
   - Did the referral event fire correctly?
   - Was there a race condition or duplicate claim?
4. Post findings to Slack:
   - If root cause is clear (expired code, already-used referral) — post explanation
   - If system-side failure — tag the growth/promo team with log evidence

**Expected resolution time:** 30-60 seconds.

---

### 3.5 BBPS / Partner Escalation — *Tier 3 (Smart Escalation)*

**What it covers:** BBPS (Bharat Bill Payment System) failures, partner-side issues (Checkout.com, LULU, banking partners), webhook acknowledgment failures, partner SLA breaches, corridor-specific outages.

**Bot Workflow:**
1. Identify the partner/corridor from the message (BBPS, Checkout, LULU, specific bank)
2. Extract any order IDs, transaction references, or pay_ IDs
3. Pull recent partner health data (if available via Datadog monitors)
4. Post a structured escalation to Slack:
   - Partner name + corridor
   - Order/transaction details
   - Known partner status (healthy / degraded / down)
   - Tag the correct partner relationship owner or ops team
5. If this is a Checkout.com webhook issue — cross-reference with falcon-api-service logs

**Expected resolution time:** 10-20 seconds to post escalation.

---

### 3.6 Manual Backend Action — *Tier 3 (Smart Escalation)*

**What it covers:** State change requests, mobile number updates, manual DB corrections, user profile fixes, account unlocks, manual refund triggers — anything requiring write access to production.

**Bot Workflow:**
1. Identify the specific action requested (state change, mobile update, account unlock, etc.)
2. Extract user ID and the requested change value
3. Post a structured escalation to Slack:
   - The user ID
   - Exactly what needs to change (field + new value)
   - Pre-filled command template (CURL / SQL) for the human to review and execute
   - Tag a backend developer or support engineer with DB access

**Expected resolution time:** 5-10 seconds to post escalation.

> **Safety Rule:** The bot NEVER executes write operations (CURL commands, DB mutations, API PATCH calls) against any system. It only prepares the context and command templates for a human to review and execute.

---

### 3.7 Rate / FX Investigation — *Tier 2 (Investigate + Escalate)*

**What it covers:** Exchange rate discrepancies, FX rate complaints, mid-market rate deviations, rate lock issues, markup complaints, corridor-specific pricing issues.

**Bot Workflow:**
1. Extract user ID, order ID, corridor, and the reported rate (if mentioned)
2. Look up the order in AlphaDesk — get the applied rate, corridor, timestamp
3. Query rate service logs / Metabase for:
   - What rate was quoted vs what was applied?
   - Was there a rate lock? Did it expire?
   - What was the mid-market rate at the time of the transaction?
4. Post findings to Slack:
   - Applied rate vs expected rate
   - Whether the rate was within normal margin
   - If abnormal — tag the rates/treasury team with full context

**Expected resolution time:** 30-90 seconds.

---

### 3.8 App Bug / Engineering Escalation — *Tier 3 (Smart Escalation)*

**What it covers:** App crashes, UI bugs, API errors that indicate code bugs, feature malfunction reports, reproducible user-facing issues.

**Bot Workflow:**
1. Extract user ID, device ID from the message (via AlphaDesk if needed)
2. Search CloudWatch App-Server logs using device ID + time window
3. Search for recent error spikes in relevant services via Datadog
4. Post a structured engineering escalation to Slack:
   - User details + device info
   - Relevant log excerpts (errors found)
   - Error frequency (is this one user or systemic?)
   - Tag backend engineer / @Prajwal with a debugging context package

**Expected resolution time:** 45-90 seconds for log gathering, then human takes over.

---

### 3.9 Other / Needs Triage — *Tier 3 (Fallback)*

**What it covers:** Any query that doesn't clearly fit the above 8 categories. Ambiguous messages, multi-issue queries, new issue types not yet in the guide.

**Bot Workflow:**
1. Attempt best-effort classification — extract any IDs, services, error patterns
2. If any data can be pulled (order lookup, log search) — do it and include in the response
3. Post to Slack:
   - "I couldn't confidently classify this query. Here's what I found: [any data pulled]"
   - Tag the CX-Tech lead or on-call engineer for manual triage
4. Log the query for review — these are candidates for adding new categories to the guide

**Expected resolution time:** 15-30 seconds to post what's available.

---

## 4. System Architecture

### 4.1 High-Level Flow

```
Slack #cx-tech-queries
        │
        ▼
┌──────────────────┐
│   Slack Poller    │  Poll every 15 seconds
│  (Cursor-based)   │  Reaction-based dedup: eyes (processing) → checkmark (done)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   Classifier     │  Haiku model (~200-500ms)
│                  │  Output:
│                  │    category (1 of 9)
│                  │    extracted IDs (order, user, payment, fulfillment)
│                  │    confidence score
│                  │    corridor
│                  │    summary
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│     Router       │  Maps category → pipeline
└────────┬─────────┘
         │
         ├──► payment_error_diagnosis      → PaymentPipeline      (Tier 1)
         ├──► kyc_verification             → KYCPipeline           (Tier 2)
         ├──► db_lookup_status             → StatusLookupPipeline  (Tier 1)
         ├──► referral_promo               → ReferralPipeline      (Tier 2)
         ├──► bbps_partner                 → PartnerEscalation     (Tier 3)
         ├──► manual_backend_action        → ManualActionEscalation(Tier 3)
         ├──► rate_fx_investigation        → RatePipeline          (Tier 2)
         ├──► app_bug_engineering          → EngineeringEscalation (Tier 3)
         └──► other_needs_triage           → FallbackTriage        (Tier 3)
                                                    │
                                                    ▼
                                           ┌────────────────┐
                                           │ Slack Response  │  Thread reply
                                           │ + Dashboard     │  + metrics write
                                           │   Recorder      │
                                           └────────────────┘
```

### 4.2 Technology Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Language | Go 1.25 | Matches existing Cortex codebase; single binary; production-proven |
| LLM Provider | Anthropic Claude | Haiku for classification (~$0.0003/query), Sonnet for investigation (~$0.02/query) |
| Agent Framework | Custom (forked from Cortex) | Agentic loop with tool calling, per-tool circuit breakers, 429 failover |
| Slack Integration | slack-go SDK + polling | No Socket Mode / no Slack App needed; just a bot token |
| Data Lookup | AlphaDesk API, Metabase/Redshift | Existing internal tools — read-only access |
| Log Search | AWS CloudWatch via MCP | Reuse existing Cortex MCP server |
| Service Health | Datadog via MCP | Reuse existing Cortex MCP server |
| Knowledge Base | Structured SKILL.md files | CX-Tech Guide encoded as decision trees per category |
| Metrics Storage | SQLite | Lightweight, zero-ops, sufficient for dashboard reads |
| Dashboard | Retool / Metabase (or Go net/http) | Visual tracking of queries, resolutions, escalations |

### 4.3 Tool Inventory

| Tool | Purpose | Access Level |
|------|---------|-------------|
| `alphadesk_lookup` | Look up user/order details by user ID, order ID, or email | Read-only |
| `metabase_query` | Run pre-defined safe SQL queries (payment attempts, order status, rates, referrals) | Read-only, pre-defined queries only |
| `cloudwatch_search` | Search App-Server / service logs by device ID, user ID, or error pattern + time window | Read-only |
| `datadog_monitors` | Check service health, error rate monitors, latency dashboards | Read-only |
| `failure_reason_matcher` | Deterministic map: failure reason code → scripted CX response | No API call (in-memory lookup) |
| `escalation_router` | Deterministic map: issue type + corridor → correct contact person/team | No API call (in-memory lookup) |
| `rate_lookup` | Query rate service for historical FX rates at a given timestamp + corridor | Read-only |
| `dashboard_recorder` | Write query metrics (category, tier, resolution time, escalated_to) | Write to local SQLite |

### 4.4 Resilience Patterns (inherited from Cortex)

- **429 Rate-Limit Failover:** Pool of API keys; rotate instantly on 429, exponential backoff (30s→60s→120s) only when ALL keys exhausted
- **Per-Tool Circuit Breaker:** 3 consecutive failures on any single tool → tool auto-disabled for that run
- **All-Tools-Fail Early Exit:** If every tool fails for 3 consecutive iterations → agent exits early and escalates with partial context
- **Slack Reaction Dedup:** `AddReaction("eyes")` as distributed lock — Slack guarantees only one call succeeds; prevents duplicate processing
- **Atomic Dedup:** In-memory `tryAcquire()` combines duplicate check + processing mark in a single mutex lock
- **Graceful Degradation:** Classifier fails → keyword heuristic fallback. Tool fails → escalate with whatever context is available. Never silently drop a query.
- **Confidence Threshold:** If classifier confidence < 0.6 → route to "Other / Needs Triage" instead of risking misclassification

---

## 5. Escalation Contact Matrix

Encoded as a deterministic lookup table in the `escalation_router` tool:

| Issue Type | Primary Contact | Fallback |
|------------|----------------|----------|
| Payment error (unknown reason) | Backend Engineer | @Prajwal |
| KYC / Verification stuck | Compliance Team | Backend Engineer |
| Event flow issues (status sync) | @Ayush, @Raj | Backend engineer on-call |
| CNR / Await issues (especially US) | @Shyam Tayal | GOMS Team |
| Status not auto-updating (>10 days) | @Shyam Tayal | GOMS Team |
| Referral / Promo system failures | Growth Team | Backend Engineer |
| BBPS / Partner issues | Partner Ops Team | @Shyam Tayal |
| Manual backend actions (state/mobile change) | Backend Developer with DB access | Support Engineer with DB access |
| Rate / FX discrepancy | Rates / Treasury Team | Backend Engineer |
| App bug / Engineering issue | @Prajwal | Backend engineer on-call |
| Unknown / unclassified | CX-Tech Lead | @Prajwal |

---

## 6. Dashboard & Metrics

### 6.1 Tracked Metrics

| Metric | Description |
|--------|-------------|
| Total queries | All messages classified as non-noise |
| Auto-resolved (Tier 1) | Queries fully handled by bot with no human intervention |
| Investigated + escalated (Tier 2) | Queries where bot pulled data and tagged a person |
| Smart-escalated (Tier 3) | Queries routed straight to human with pre-built context |
| Fell to "Other" | Queries that couldn't be classified — candidates for guide expansion |
| Resolution time (P50, P95, P99) | Time from Slack message to bot response |
| Category distribution | Breakdown across all 9 categories (pie chart / bar chart) |
| Top failure reasons | Most common payment failure codes (trend over time) |
| Escalation load per person | Queries routed to each team member (for load balancing) |
| Unknown failure rate | % of queries where bot could not match any known pattern |
| Feedback score | Thumbs-up vs thumbs-down on bot responses |

### 6.2 Dashboard Views

- **Real-Time Panel:** Live feed of queries being processed, current pipeline status
- **Daily Slack Digest:** Auto-posted at end of day — total queries, auto-resolution rate, top categories, any new unknown patterns
- **Weekly Trend Report:** Is a specific failure reason trending up? Is a partner degrading? Is a category growing?
- **Escalation Heatmap:** Which team members are getting the most tags? (for on-call rotation planning)
- **Guide Coverage Score:** What % of queries are fully covered by the guide vs falling to "Other"?

---

## 7. Implementation Roadmap

### Phase 1 — Foundation + Classifier (Week 1-2)

**Goal:** Bot running in Slack, classifying all messages into 9 categories, logging results.

| Task | Days |
|------|------|
| Fork agent framework from Cortex (agent loop, tool registry, Slack poller, skill loader) | 2 |
| Build the 9-category classifier prompt with ID extraction | 2 |
| Encode the CX-Tech Guide into structured SKILL.md knowledge files | 1 |
| Wire Slack poller to #cx-tech-queries | 1 |
| Build keyword heuristic fallback for classifier | 1 |
| Deploy in **shadow mode** (classify + log, no Slack responses) | 1 |
| Validate classifier accuracy against historical messages | 2 |

**Deliverable:** Classifier running in shadow mode. Accuracy report across all 9 categories.

---

### Phase 2 — Tier 1 Auto-Resolve Pipelines (Week 3-4)

**Goal:** Payment errors and DB lookups handled end-to-end without humans.

| Task | Days |
|------|------|
| Build `alphadesk_lookup` tool (API integration) | 3 |
| Build `metabase_query` tool (pre-defined safe SQL queries) | 2 |
| Build `failure_reason_matcher` (deterministic table from guide) | 1 |
| Build PaymentPipeline: classify → lookup → match → respond | 2 |
| Build StatusLookupPipeline: order lookup + discrepancy detection | 2 |
| End-to-end testing with real queries | 1 |
| Enable **live mode** for Tier 1 categories only | 1 |

**Deliverable:** Payment errors + status lookups auto-resolved in <30 seconds.

---

### Phase 3 — Tier 2 Investigation Pipelines (Week 5-6)

**Goal:** KYC, referral, and rate queries investigated automatically, escalated with context.

| Task | Days |
|------|------|
| Build KYCPipeline (compliance-service log search + status classification) | 2 |
| Build ReferralPipeline (promo/referral service log search + root cause) | 2 |
| Build RatePipeline (rate service query + FX comparison logic) | 2 |
| Build `rate_lookup` tool (historical rate queries) | 1 |
| Integrate CloudWatch MCP (reuse from Cortex) | 1 |
| Integrate Datadog MCP (reuse from Cortex) | 1 |
| Build `escalation_router` tool with full contact matrix | 1 |
| Enable live mode for Tier 2 categories | 1 |

**Deliverable:** All Tier 2 queries return investigation findings + correct escalation.

---

### Phase 4 — Tier 3 Escalation Pipelines (Week 7)

**Goal:** BBPS/partner, manual actions, app bugs, and unknown queries routed instantly.

| Task | Days |
|------|------|
| Build PartnerEscalation pipeline (partner detection + health check + tagging) | 1 |
| Build ManualActionEscalation pipeline (request parsing + template generation) | 1 |
| Build EngineeringEscalation pipeline (log search + bug context packaging) | 2 |
| Build FallbackTriage pipeline (best-effort data pull + catch-all escalation) | 1 |
| Enable live mode for all categories | 0.5 |

**Deliverable:** All 9 categories live. Zero queries dropped.

---

### Phase 5 — Dashboard + Observability (Week 8)

**Goal:** Full visibility into bot performance and query trends.

| Task | Days |
|------|------|
| Build `dashboard_recorder` tool (SQLite writes on every query resolution) | 1 |
| Build dashboard UI (Retool/Metabase on SQLite, or custom Go web server) | 3 |
| Build daily Slack digest (auto-posted summary) | 1 |
| Build weekly trend report | 0.5 |

**Deliverable:** Live dashboard + daily/weekly automated digests in Slack.

---

### Phase 6 — Feedback Loop + Hardening (Week 9-10)

**Goal:** Bot improves over time. Edge cases handled. Production-hardened.

| Task | Days |
|------|------|
| Add thumbs-up / thumbs-down reaction tracking on bot responses | 1 |
| Track per-category accuracy and resolution quality scores | 1 |
| Build "unknown pattern accumulator" — alert when new failure reasons appear 3+ times | 1 |
| Prompt tuning based on real-world classification errors | 2 |
| Load testing + failure injection (simulate AlphaDesk down, rate limits, etc.) | 1 |
| Documentation + runbook for on-call handoff | 1 |

**Deliverable:** Self-improving system with accuracy tracking, production runbook.

---

### Timeline Summary

```
           Week 1   2   3   4   5   6   7   8   9  10
            ┌───┬───┬───┬───┬───┬───┬───┬───┬───┬───┐
Phase 1     │███████│   │   │   │   │   │   │   │   │  Foundation + Classifier
Phase 2     │   │   │███████│   │   │   │   │   │   │  Tier 1 (Auto-Resolve)
Phase 3     │   │   │   │   │███████│   │   │   │   │  Tier 2 (Investigate)
Phase 4     │   │   │   │   │   │   │███│   │   │   │  Tier 3 (Escalation)
Phase 5     │   │   │   │   │   │   │   │███│   │   │  Dashboard
Phase 6     │   │   │   │   │   │   │   │   │███████│  Feedback + Hardening
            └───┴───┴───┴───┴───┴───┴───┴───┴───┴───┘

MVP (Tier 1 live):      End of Week 4
Full bot (all 9 cats):  End of Week 7
Dashboard:              End of Week 8
Production-hardened:    End of Week 10
```

| Scenario | Total Duration |
|----------|---------------|
| 1 engineer, full scope | **10 weeks** (~50 working days) |
| 2 engineers, parallel | **6-7 weeks** (~33 working days) |
| MVP only (Tier 1: payments + status lookups) | **4 weeks** (~20 working days) |

---

## 8. Cost Estimate

### LLM Costs (per query)

| Query Tier | Models Used | Approx Cost |
|-----------|-------------|-------------|
| Tier 1 (Auto-Resolve) | Haiku (classify) + tool calls (no LLM) | ~$0.001 |
| Tier 2 (Investigate) | Haiku (classify) + Sonnet (investigate) | ~$0.025 |
| Tier 3 (Escalate) | Haiku (classify) only | ~$0.0003 |

**Projected monthly cost at 40 queries/day (mixed tiers):** ~$15-20/month.

### Engineering Effort

| Phase | Effort |
|-------|--------|
| Phase 1 — Foundation | 1 engineer, 2 weeks |
| Phase 2 — Tier 1 Pipelines | 1 engineer, 2 weeks |
| Phase 3 — Tier 2 Pipelines | 1-2 engineers, 2 weeks |
| Phase 4 — Tier 3 Pipelines | 1 engineer, 1 week |
| Phase 5 — Dashboard | 1 engineer, 1 week |
| Phase 6 — Feedback + Hardening | 1 engineer, 2 weeks |
| **Total** | **1-2 engineers, 10 weeks** |

---

## 9. Risk Mitigation

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Bot posts incorrect resolution | Customer gets wrong advice | Shadow mode for 2 weeks (Phase 1). Confidence threshold — if < 0.6, route to "Other" instead. Thumbs-down tracking. |
| Bot executes destructive action | Data corruption / unauthorized changes | Bot has **read-only** access everywhere. No write APIs. CURL templates are for human review only. |
| AlphaDesk / Metabase API is down | Bot can't look up data | Graceful fallback — post "I couldn't look up this order automatically" and escalate with whatever context is available. |
| Classifier puts query in wrong category | Wrong pipeline runs | 9-category classifier tested on historical data before launch. Low-confidence queries route to "Other". Weekly accuracy reviews. |
| New failure reason not in the guide | Bot can't match a resolution | Unknown reasons trigger escalation + get logged. Pattern accumulator surfaces recurring unknowns for guide updates. |
| Duplicate processing | Same query handled twice | Two-layer dedup: Slack reaction lock (distributed) + in-memory atomic tryAcquire (local). Proven in Cortex. |
| LLM rate limiting | Bot goes silent | Multi-key failover with exponential backoff. Haiku at 40 queries/day is far below rate limits. |
| Guide becomes stale | Bot gives outdated advice | "Other" category accumulator catches new patterns. Weekly review of "fell to Other" queries. Dashboard tracks guide coverage %. |

---

## 10. Success Metrics

| Metric | Target (3 months post-launch) |
|--------|-------------------------------|
| Classification accuracy (9 categories) | > 90% |
| Auto-resolution rate (Tier 1 queries) | > 60% of eligible queries |
| Median resolution time (Tier 1) | < 30 seconds |
| Median time-to-escalation (Tier 2 + 3) | < 60 seconds |
| Correct escalation routing | > 95% to the right person |
| Reduction in human response time | > 50% improvement |
| Guide coverage (queries NOT falling to "Other") | > 85% |
| Team satisfaction (quarterly survey) | Net positive |
| Monthly LLM cost | < $25 |

---

## 11. What We Reuse vs What We Build

| From Cortex (proven, copy directly) | Build New |
|-------------------------------------|-----------|
| Agent loop with tool calling + circuit breakers | 9-category classifier prompt |
| 429 multi-key failover + exponential backoff | 9 pipeline handlers (4 new tools + routing logic) |
| Slack poller with reaction dedup + cursor persistence | AlphaDesk API integration tool |
| Tool registry + FilteredRegistry + output truncation | Metabase safe-query tool |
| SkillStore + BuildRolePrompt (SKILL.md loading) | Failure reason matcher (deterministic) |
| CloudWatch MCP server | Escalation contact matrix |
| Datadog MCP server | Rate lookup tool |
| Message chunking (>40K char Slack limit) | Dashboard recorder + UI |
| | CX-Tech Guide structured knowledge files |
| | Daily/weekly Slack digest |

---

*Document prepared for internal review. This system builds on Aspora's existing Cortex agent framework, reusing the production-proven agent loop, tool registry, Slack integration, and resilience patterns while adding CX-Tech-specific 9-category classification, knowledge base, and tiered resolution pipelines.*
