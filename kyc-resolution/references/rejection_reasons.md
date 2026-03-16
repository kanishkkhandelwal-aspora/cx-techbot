# KYC Rejection Reasons — Knowledge Base

This file maps known rejection reasons to their resolutions. Organized by provider.

When you encounter a rejection reason, find it here and use the suggested resolution. If the reason is not listed, follow the "unknown rejection reason" workflow in the main SKILL.md.

---

## UAE — Lulu / EFR Errors

### NO_MATCH (EID and face not matching)
**What it means:** Lulu's facial recognition determined the user's selfie doesn't match the photo on their Emirates ID.
**Resolution:** Ask the user to retry with better lighting and ensure their face is clearly visible. If the user has retried multiple times with the same result, raise with Lulu using the `ekyc_request_id` — it may be a false negative on Lulu's end.
**Frequency:** High — one of the most common Lulu errors.

### DOCUMENT_EXPIRED / No active residency visa
**What it means:** The user's Emirates ID or residency visa has expired according to Lulu's records.
**Resolution:** Ask the user to check their Emirates ID expiry date. If the document is genuinely expired, they need to renew it first. If the user insists the document is valid, raise with Lulu — their database may not have the latest renewal.
**Status after fix:** Reject the current KYC so the user can retry with updated documents (RE_KYC_REQUIRED).

### Customer onboarding failed (suspicious)
**What it means:** Lulu flagged the user as suspicious during onboarding. This goes into Lulu's manual review queue.
**Resolution:** Raise with Lulu via email with the `ekyc_request_id`. Wait for Lulu's confirmation. If Lulu approves, reject the user internally so they can retry with a new ECRN.
**Important:** The user cannot resolve this themselves — it requires Lulu's manual review.

### EFR API Timeout (Connect timed out on GetTemporaryKey / OCR Detection API)
**What it means:** Network timeout when calling Lulu's EFR APIs. Usually intermittent.
**Resolution:** Ask the user to retry. If it keeps happening, check if there's a broader Lulu outage. These are transient and typically resolve on their own.
**Frequency:** Medium — occurs during Lulu service degradation.

### EFR SDK validation failure
**What it means:** The face detection/blink detection/lighting check failed on the mobile SDK side before data even reaches Lulu.
**Resolution:** Ask the user to:
- Ensure good lighting (avoid backlight)
- Hold phone at eye level
- Keep face centered in frame
- Blink naturally when prompted
If persistent, check the user's device/OS version — very old Android versions (8.0 and below) have known compatibility issues.

### Person does not have an active residency visa
**What it means:** Direct error from Lulu — the user's visa status in Lulu's system shows inactive.
**Resolution:** Document this error. If the user has a valid visa, raise with Lulu. This requires coordination with Lulu to update their records.

### Backend-mobile screen sync mismatch
**What it means:** Backend expects Emirates ID screen submission but mobile app sends face capture screen (or vice versa). A known synchronization bug.
**Resolution:** This is a bug — escalate to the mobile and backend teams. The user cannot fix this. As a workaround, rejecting the KYC and having the user restart sometimes resolves it.

---

## UK / US — Persona Errors

### Status: PENDING
**What it means:** Persona flagged the user for manual review. Not a rejection — the user is in a holding state.
**Resolution:** This needs the dedicated KYC review team to review via the Persona dashboard. They can approve or trigger a retry. The bot cannot resolve this. Inform CX to route to the KYC team.

### Status: PROCESSING (stuck)
**What it means:** The SDK submission was received but the webhook from Persona hasn't been processed. Should be brief.
**Resolution:** If stuck for more than ~10 minutes, it's a technical issue. Check verification service logs for webhook delivery failures. May need the engineering team to investigate.

### User cancelled inquiry
**What it means:** The user exited the Persona SDK before completing verification.
**Resolution:** Ask the user to retry and complete the full flow without cancelling. No backend action needed.

### Wrong country selected
**What it means:** User selected the wrong country during document type selection (e.g., selected US passport when they have a UK document, or vice versa). The app may lock into the wrong verification flow.
**Resolution:** Ask the user to carefully select the correct country from the dropdown. If the app is stuck in the wrong flow, they may need to logout/login or reinstall to reset.

### Persona SDK not opening
**What it means:** The backend successfully generated the SDK token but the mobile app failed to open the Persona SDK.
**Resolution:** Backend looks fine in this case. Raise with Persona team — likely an SDK integration issue. Check the app version as well.

### Share Code issues (UK only)
**What it means:** The UK Share Code is a 9-digit government-issued code for digital identity verification. Issues may include: code not accepted, flow not showing, or verification failing after code entry.
**Resolution:** Ensure the user is entering a valid 9-digit share code. If the share code flow isn't appearing as an option, check if the user's workflow is correctly routing to the share code path (known issue where default POI flow shows instead).

---

## EU — Sumsub Errors

### Sumsub rejection with Goms hold
**What it means:** Sumsub rejected the verification and the order is stuck in Goms compliance hold.
**Resolution:** Release the order from Goms hold. Check if the Sumsub rejection is legitimate — if so, the user needs to re-verify. If it's a false rejection, escalate to Sumsub.

---

## Cross-Provider Issues (Any Region)

### Missing DOB (Date of Birth)
**What it means:** Old users (onboarded 2023-2024) may not have DOB in the system because it wasn't mandatory at the time.
**Resolution:** Check Alphadesk for the user's profile. If DOB is missing, ask the user to provide it. Patch the DOB in Alphadesk backend. After patching, user can retry verification.
**Note:** The current app cannot collect DOB retroactively — it must be patched manually.

### Missing address / street field
**What it means:** Similar to missing DOB — address data may be incomplete for older users. Can also occur due to mobile SDK failing to process address fields correctly.
**Resolution:** Quick fix: get the address from the user and patch in Alphadesk (can sometimes derive from postcode). Long-term: app update needed for address collection.

### Wrong state selected during registration
**What it means:** User selected the wrong state/region during initial registration, causing mismatches downstream.
**Resolution:** Update the correct state in the database via Alphadesk.

### Camera / blank screen issues
**What it means:** User sees a blank page or black screen when trying to scan documents or take a selfie.
**Resolution:**
1. Check app version — several blank screen bugs were fixed in v6.5.0 and v7.3.1
2. Check if camera permissions are granted
3. Ask user to update to latest app version
4. If on latest version, escalate as a new bug

### Face verification stuck on same page
**What it means:** After completing face capture, the app doesn't proceed to the next step.
**Resolution:** Ask user to update to latest app version (fix often in newer releases). If already on latest, escalate to mobile team.

### OTP not received
**What it means:** User isn't receiving the OTP for phone verification during KYC.
**Resolution:** Check with OTP provider (Prelude) if OTPs were sent successfully. If sends show as successful, the issue is on the telecom/carrier side. Escalate to Prelude if sends are failing.

### iOS-specific: Documents submitted but not visible in backend
**What it means:** Known iOS bug where the mobile app shows successful document submission but the backend receives empty data.
**Resolution:** Documents may be stored somewhere in backend — investigate. This is a known bug with a fix expected in upcoming release. Inform user and track the fix.

### App not redirecting after verification
**What it means:** User completed verification (face/document capture) but the app stays on the same screen instead of moving forward.
**Resolution:** Ask user to close and reopen the app, then retry. If that doesn't work, ask them to update to the latest version.

### NullPointerException in EFR SDK config
**What it means:** Internal error — `sdkConfig is null in EFRSdkConfig.getSdkSecretKey()`. The SDK configuration wasn't properly initialized.
**Resolution:** This is a backend bug. Escalate to the engineering team. The user cannot fix this.

### JSON deserialization error / internal server error on /kyc endpoint
**What it means:** Backend error during KYC processing. Example: `FeatureWhitelistDto type resolution failure`.
**Resolution:** Backend bug — escalate to engineering. Not a user-side issue.

---

## Rejection Reason Not Found?

If you encounter a rejection reason not listed here:
1. Search the web for context about the error message
2. Check the KYC provider's documentation if accessible
3. Draft a proposed entry for this file with the format above
4. Post the proposed addition to the team approval channel
5. Wait for approval before updating this file
