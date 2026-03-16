---
name: kyc-resolution
description: >
  Diagnose and resolve KYC (Know Your Customer) verification issues for users across all corridors (UAE, UK, US, EU).
  Uses CloudWatch MCP to search verification and workflow service logs, and Alphadesk (when available) to look up users,
  check rejection reasons, and determine the right fix based on the KYC provider (Lulu for UAE, Persona for UK/US,
  Sumsub for EU). Posts findings and suggested solutions to the CX-Tech Slack channel.
  Trigger this skill whenever someone mentions: KYC issue, verification stuck, user can't verify, KYC rejected,
  face verification failed, document upload issue, EID scan not working, KYC pending, verification error,
  eKYC failure, "user unable to complete verification", or any variant of a user having trouble with identity verification.
  Even casual mentions like "this user can't do KYC" or "verification is broken for someone" should trigger this skill.
---

# KYC Issue Resolution

You are diagnosing and resolving KYC (identity verification) issues for Aspora users. Aspora is a cross-border remittance app operating in UAE, UK, US, and EU. Users must complete KYC before sending money.

## Architecture Overview

There are two backend services involved in KYC:

- **Verification Service** — Processes KYC data after submission, talks to external providers (Lulu, Persona, Sumsub), and updates KYC status. If a user's KYC is stuck in "rejected", "processing", or an unexpected status, the issue is likely here.
- **Workflow Service** — Manages the data collection screens (the sequence of pages the user sees). If the user can't get past a screen, sees a blank page, or the app isn't collecting their info properly, the issue is likely here. Track using device ID and workflow logs.

These two services work together: the Workflow Service collects info, then tells the Verification Service to process it. For SDK-based flows (Persona), the Workflow Service requests a token from the Verification Service, which opens the SDK on mobile.

## KYC Providers by Region

| Region | Provider | Flow Type | Notes |
|--------|----------|-----------|-------|
| UAE | Lulu (via EFR) | Native — Workflow Service collects data, Verification Service sends to Lulu | Common errors: NO_MATCH, document expired, no active residency visa |
| UK | Persona | SDK — Persona SDK handles collection, webhooks back to Verification Service | "Pending" = manual review by KYC team. Rejection reasons often unclear from Persona |
| US | Persona | SDK — same as UK | Same Persona flow |
| EU | Sumsub (migrating to Persona) | SDK | Check which provider the user is on |

## Step-by-Step Diagnosis Workflow

### Step 1: Identify the user

From the CX-Tech Slack message, extract any user identifiers: userId, email, phone number, or name.

<!-- PLACEHOLDER: Replace with actual MCP tool call once Alphadesk MCP is available -->
Search the user in Alphadesk using the identifier. Use `alphadesk.search_user` (or equivalent MCP tool) with the userId, email, or phone.

```
# Placeholder — actual MCP tool call TBD
alphadesk.search_user(query="<userId or email>")
```

### Step 2: Check KYC status and rejection reason

Once you've found the user in Alphadesk:

1. **Go to the User Verification section** — look at the KYC provider info popup (click the info icon next to "Vance User KYC" or equivalent). This shows:
   - Provider (VANCE, LULU, PERSONA, SUMSUB)
   - Product (REMITTANCE)
   - KYC Rejection Reasons (array — may be empty `[]`)
   - KYC Rejection Count (number)
   - Current status (VERIFIED, REJECTED, PROCESSING, PENDING, NEW, DRAFT, BLOCK)

2. **Navigate to Remittance > Visit** — click the "Visit" button next to the verification status. Check for rejection reasons displayed on this page.

3. **If no rejection reason found, check the Remarks popup** — there's a remarks button that may contain additional context about why the verification failed.

4. **If both are empty, check CloudWatch logs** — When Alphadesk doesn't surface the rejection reason, the raw error lives in the service logs on AWS CloudWatch. Use the CloudWatch MCP tool to search.

   **Log groups:**
   - Verification Service: `/ecs/vance-core/prod/london/01/verification-service-logs`
   - Workflow Service: `/ecs/vance-core/prod/london/01/workflow-service-logs`

   **Search sequence using CloudWatch MCP:**

   ```
   # Step 1: Search Verification Service logs by userId (start here — most KYC errors originate in this service)
   cloudwatch.filter_log_events(
     log_group="/ecs/vance-core/prod/london/01/verification-service-logs",
     filter_pattern="<userId>"
   )
   ```

   From the results, look for the `kyc_request_id` — this links to the specific KYC attempt.

   ```
   # Step 2: Narrow down with kyc_request_id to find the exact provider error
   cloudwatch.filter_log_events(
     log_group="/ecs/vance-core/prod/london/01/verification-service-logs",
     filter_pattern="<kyc_request_id>"
   )
   ```

   If the Verification Service logs don't show the issue, check the Workflow Service — particularly for screen-level errors, SDK token failures, or data collection issues:

   ```
   # Step 3: Fall back to Workflow Service logs
   cloudwatch.filter_log_events(
     log_group="/ecs/vance-core/prod/london/01/workflow-service-logs",
     filter_pattern="<userId>"
   )
   ```

   **Interpreting the error:** CloudWatch logs contain raw API responses from providers (Lulu, Persona, Sumsub) and internal exceptions. These can be cryptic. Analyze the error message, stack trace, and surrounding log context to determine the root cause. Cross-reference with `references/rejection_reasons.md` to see if it maps to a known pattern. If it's a new error, follow the unknown rejection reason workflow in Step 7.

### Step 3: Determine the user's region and provider

Based on the Alphadesk data, identify which KYC provider is handling this user. This matters because the resolution path differs by provider.

### Step 4: Look up the rejection reason

Once you have the rejection reason, check `references/rejection_reasons.md` for the known resolution. That file contains a mapping of common rejection reasons to their fixes, organized by provider.

### Step 5: Check rejection count

The rejection count is important context:

- **1-4 rejections** — Likely a user-side issue (bad photo, expired doc, wrong country selected). Follow the standard resolution for that rejection reason.
- **5-7+ rejections** — This is a red flag. The user has tried multiple times and keeps failing. This suggests a systemic issue (provider bug, data mismatch, backend problem). **Escalate to the respective KYC provider:**
  - Lulu issues → raise via email to Lulu with the `ekyc_request_id`
  - Persona issues → raise with Persona team (Pwell has dashboard access)
  - Sumsub issues → raise with Sumsub support

### Step 6: Post findings to Slack

Write a message to the CX-Tech Slack channel with:

1. **User identifier** (userId)
2. **KYC Provider** (Lulu / Persona / Sumsub)
3. **Rejection Reason** found (exact text from Alphadesk)
4. **Suggested Resolution** from the rejection reasons knowledge base
5. **Rejection Count** and whether escalation is needed

Format example:
```
*KYC Diagnosis for user `<userId>`*

*Provider:* Lulu (UAE)
*Status:* REJECTED
*Rejection Reason:* Document expired
*Rejection Count:* 2

*Suggested Resolution:* Ask the user to check their Emirates ID expiry date and upload a valid, non-expired document. Then retry KYC verification.
```

### Step 7: Handle unknown rejection reasons

If the rejection reason is not in `references/rejection_reasons.md`:

1. Search the web for context about the error (provider documentation, known issues)
2. Draft a proposed addition to the rejection reasons knowledge base
3. Post the proposed update to the approval channel for team review
4. **Do not update the skill until the team approves** — wait for explicit approval in the channel before making changes

## Special Scenarios

### KYC stuck in DRAFT status
The user started KYC but never completed it. The KYC session may have expired or the app crashed mid-flow. Resolution: reject the current KYC so the user can re-initiate and get a new session (new ECRN for UAE users).

### KYC stuck in PROCESSING
This is supposed to be a brief intermediate state. If a user has been in "processing" for more than a few minutes, it's a technical issue. Check verification service logs for errors. Common cause: webhook from provider didn't arrive or wasn't processed.

### PENDING status (UK/EU only)
Persona flagged the user as suspicious. This requires manual review by the dedicated KYC team — it's not something the bot can resolve. Inform CX that this needs to be handled by the KYC review team via the Persona dashboard.

### UAE "customer onboarding failed"
Lulu flagged as suspicious. This goes into Lulu's manual queue. Resolution: raise with Lulu via email. If Lulu approves, reject the user internally so they can retry with a new ECRN.

### Missing DOB or address (old users)
Users who onboarded in 2023-2024 before DOB/address collection was mandatory may have missing data. The current app cannot collect this info retroactively. Resolution: ask the user for their DOB/address, then patch it in Alphadesk backend manually. After patching, the user can retry.

### App version issues
Some KYC bugs are fixed in newer app versions. If the issue matches a known app-version bug (blank screen, stuck page, camera not opening), ask the user what app version they're on and suggest updating to the latest version.

### Backend-mobile screen sync mismatch (UAE)
A known issue where the backend expects one screen (e.g., Emirates ID) but the mobile app submits a different screen (e.g., face capture). This is a bug that needs mobile team investigation. Flag it and escalate — the user can't fix this on their end.

## Available Tools

| Tool | Status | Used For |
|------|--------|----------|
| CloudWatch MCP | Ready | Searching verification-service-logs and workflow-service-logs by userId / kyc_request_id |
| Slack MCP | Ready | Reading CX-Tech queries, posting diagnosis results |
| Alphadesk MCP | Pending (waiting on devs) | User lookup, KYC status, rejection reasons, remarks |

When Alphadesk MCP is not yet available, go directly to CloudWatch logs (Step 2.4) after getting the userId from the Slack message. You can still diagnose most issues from CloudWatch alone — the logs contain provider responses, error codes, and status transitions.

## Important Notes

- Always check the Slack thread first for any prior investigation by team members before starting diagnosis
- The CX-Tech Slack channel is: `C052230PWSC`
- For Lulu escalations, always include the `ekyc_request_id` in the email
- For Persona issues, Pwell has dashboard access and can review stuck users directly
- The UK Share Code is a 9-digit number for digital verification (like US SSN) — if a user mentions share code issues, it's a Persona flow
