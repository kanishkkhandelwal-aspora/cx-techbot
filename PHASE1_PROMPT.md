# Phase 1 Build Prompt — CX-Tech Query Bot (Python)

Copy everything below the line and use it as your prompt to build Phase 1.

---

## Context

We are building a **CX-Tech Query Resolution Bot** — a Slack bot that monitors our `#cx-tech-queries` channel and helps route customer-facing technical queries to the right support engineer. The full project overview is in `CX-Tech-Bot-Overview.md` in this same directory — read it for the big picture. This prompt covers **Phase 1 only**.

## Phase 1 Scope — What We Are Building Today

A Python Slack bot that does exactly 3 things:

1. **Polls** `#cx-tech-queries` for new messages
2. **Classifies** each message into 1 of 9 issue categories using Claude Haiku
3. **Assigns** it to one of 3 support engineers — **Vatsal**, **Adarsh**, **Kanishk** — and posts a structured acknowledgment in the Slack thread

That's it. No investigation, no tool calling, no auto-resolution, no AlphaDesk lookups. Just classify, assign, respond, log. Later phases will add the investigation pipelines.

---

## Tech Stack

```
Python 3.11+
anthropic           — Claude API (pip install anthropic)
slack-sdk           — Slack API (pip install slack-sdk)
python-dotenv       — .env loading (pip install python-dotenv)
sqlite3             — metrics DB (stdlib, no install needed)
```

---

## Project Structure

Create this as a standalone project at `/Users/apple/Desktop/Cx-TechBot/`:

```
Cx-TechBot/
├── main.py                      ← entry point — wires everything, starts poller
├── config.py                    ← loads .env, returns typed config
├── classifier/
│   ├── __init__.py
│   ├── classifier.py            ← 9-category Haiku classifier (LLM call)
│   ├── fallback.py              ← keyword heuristic fallback when LLM fails
│   └── extractor.py             ← regex ID extraction (order, user, payment IDs)
├── assigner/
│   ├── __init__.py
│   └── assigner.py              ← round-robin + category affinity assignment
├── slack_bot/
│   ├── __init__.py
│   ├── poller.py                ← polls Slack channel, dedup, cursor persistence
│   └── formatter.py             ← builds Slack mrkdwn response messages
├── metrics/
│   ├── __init__.py
│   └── db.py                    ← SQLite schema + record writes
├── handler.py                   ← glue: classify → assign → format → post → record
├── requirements.txt
├── .env.example
├── .env                         ← actual secrets (gitignored)
├── .gitignore
├── Makefile
└── CX-Tech-Bot-Overview.md      ← project overview (already exists)
```

---

## File-by-File Specification

### 1. `.env.example`

```env
ANTHROPIC_API_KEY=sk-ant-your-key-here
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_CHANNEL_ID=C0XXXXXXXXX
CXBOT_ENV=stage
CXBOT_POLL_INTERVAL=15
CXBOT_CURSOR_FILE=.cxbot_cursor
CXBOT_DB_PATH=cxbot_metrics.db
CLASSIFIER_MODEL=claude-haiku-4-5-20251001
```

Slack bot token needs these OAuth scopes: `channels:history`, `channels:read`, `chat:write`, `reactions:read`, `reactions:write`.

### 2. `config.py`

Load config from `.env` using python-dotenv. Return a dataclass:

```python
@dataclass
class Config:
    anthropic_api_key: str
    slack_bot_token: str
    slack_channel_id: str
    env: str = "stage"                    # "stage" or "prod"
    poll_interval: int = 15               # seconds
    cursor_file: str = ".cxbot_cursor"
    db_path: str = "cxbot_metrics.db"
    classifier_model: str = "claude-haiku-4-5-20251001"
```

Validate that the 3 required vars (`ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`) are present. Raise a clear error if any are missing.

---

### 3. `classifier/extractor.py`

Deterministic regex extraction. Runs BEFORE the LLM call. Results are hints to the LLM and post-corrections.

Extract these ID types from raw message text:

```python
PATTERNS = {
    "order_id":            r'\b(AE|UK|US|EU|PK|PH|IN)[A-Z0-9]{8,12}\b',
    "user_id":             r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',   # UUID v4, case insensitive
    "payment_attempt_id":  r'\bPA-[A-Za-z0-9]{8,20}\b',
    "fulfillment_id":      r'\b[0-9a-f]{32}\b',                                                      # exactly 32 hex, no dashes
    "checkout_pay_id":     r'\bpay_[a-z0-9]{20,40}\b',
}
```

Return a dict like `{"order_id": ["AE13SNKS8O00"], "user_id": ["019e5135-..."], ...}`. The classifier merges these into the LLM result, fixing misclassifications (e.g., UUIDs that the LLM put in order_ids get moved to user_ids).

---

### 4. `classifier/fallback.py`

Keyword-based fallback when LLM is unavailable or confidence < 0.5.

Check message text (lowercased) against keyword lists. First match wins. No match → `other_needs_triage`.

```python
CATEGORY_KEYWORDS = {
    "payment_error_diagnosis": [
        "payment failed", "transaction declined", "3ds", "card not working",
        "debit card", "credit card", "payment attempt", "exceeds_daily_limit",
        "acquirer", "upi fail", "upi declined", "payment timeout", "card declined",
    ],
    "kyc_verification": [
        "kyc", "verification failed", "document rejected", "compliance",
        "identity verification", "onfido", "pep screening", "sanctions",
    ],
    "db_lookup_status": [
        "order status", "transfer status", "refund status", "cnr",
        "not syncing", "alphadesk", "falcon status", "fulfillment",
        "money deducted but", "where is my transfer", "status check",
    ],
    "referral_promo": [
        "referral", "promo code", "cashback", "reward not credited",
        "campaign", "offer not applied", "referral bonus",
    ],
    "bbps_partner_escalation": [
        "bbps", "bill payment", "checkout.com", "lulu", "partner",
        "webhook", "corridor down", "partner payout",
    ],
    "manual_backend_action": [
        "change state", "update mobile", "mobile number change",
        "unlock account", "db update", "manual fix", "curl",
        "state change", "number change",
    ],
    "rate_fx_investigation": [
        "exchange rate", "fx rate", "rate difference", "markup",
        "mid-market", "rate lock", "rate shown", "rate applied",
    ],
    "app_bug_engineering": [
        "app crash", "ui bug", "screen not loading", "button not working",
        "white screen", "app not opening", "after update", "api error",
    ],
}
```

Return a `CXClassification` with confidence 0.6 for keyword match, or `other_needs_triage` with confidence 0.3 if no match.

---

### 5. `classifier/classifier.py`

The core brain. Calls Claude Haiku to classify a message.

```python
@dataclass
class CXClassification:
    category: str
    confidence: float
    summary: str
    order_ids: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)
    payment_attempt_ids: list[str] = field(default_factory=list)
    fulfillment_ids: list[str] = field(default_factory=list)
    checkout_pay_ids: list[str] = field(default_factory=list)
    corridor: str = ""


class CXClassifier:
    def __init__(self, client: anthropic.Anthropic, model: str):
        self.client = client
        self.model = model

    def classify(self, message_text: str) -> CXClassification:
        # 1. Run regex extraction first (deterministic)
        # 2. Call Haiku with system prompt + message + extracted ID hints
        # 3. Parse JSON response
        # 4. If LLM fails or confidence < 0.5 → keyword fallback
        # 5. Merge regex-extracted IDs into result (post-correction)
        # 6. Return CXClassification
```

**LLM call details:**
- Model: `claude-haiku-4-5-20251001`
- Max tokens: 512
- Timeout: 10 seconds
- If 429 rate limit: retry once after 1 second, then fall back to keywords
- If any other error: fall back to keywords immediately

**Classifier System Prompt** (use as a constant string in the file):

```
You are a message classifier for Aspora's #cx-tech-queries Slack channel. Classify each message into exactly ONE of 9 categories.

## Categories

1. **payment_error_diagnosis** — Debit/credit card transaction failures, payment attempt errors, 3DS issues, UPI failures, acquirer rejections, payment timeouts, wallet failures.
   Signals: "payment failed", "transaction declined", "3DS", "card not working", "PA-" IDs, "EXCEEDS_DAILY_LIMIT", bank names + failure, "debit card", "credit card"

2. **kyc_verification** — KYC pending/stuck/rejected, document verification failures, compliance service errors, identity verification, PEP/sanctions screening.
   Signals: "KYC", "verification", "document rejected", "compliance", "identity", "Onfido", "PEP", "sanctions"

3. **db_lookup_status** — Order status lookups, transfer status discrepancies (AlphaDesk vs Falcon), refund status checks, CNR (Credit Not Received), fulfillment status.
   Signals: "status of order", "where is my transfer", "refund", "AlphaDesk", "Falcon status", "CNR", "not syncing", "money deducted but"

4. **referral_promo** — Referral rewards not credited, promo code not working, cashback missing, campaign/offer issues.
   Signals: "referral", "promo code", "cashback", "reward not credited", "offer", "campaign", "first transaction bonus"

5. **bbps_partner_escalation** — BBPS bill payment failures, partner issues (Checkout.com, LULU, banking partners), webhook failures, corridor-level outages.
   Signals: "BBPS", "bill payment", "Checkout.com", "LULU", "partner", "webhook", "corridor down", "all customers affected"

6. **manual_backend_action** — State change requests, mobile number updates, manual DB corrections, account unlocks — anything requiring production database write access.
   Signals: "change state", "update mobile", "mobile number change", "unlock account", "DB update", "CURL", "manual fix needed"

7. **rate_fx_investigation** — Exchange rate discrepancies, FX rate complaints, rate vs market rate, rate lock expiry, markup questions, corridor pricing.
   Signals: "exchange rate", "FX rate", "rate difference", "rate shown vs applied", "markup", "mid-market rate", "rate lock"

8. **app_bug_engineering** — App crashes, UI bugs, API errors suggesting code bugs, feature malfunctions, reproducible issues.
   Signals: "app crash", "UI bug", "screen not loading", "button not working", "error on app", "after update", "white screen", "API returning 500"

9. **other_needs_triage** — Anything that doesn't clearly fit above. Ambiguous, multi-issue, new patterns, greetings, thanks, noise.
   Signals: very short messages, "thanks", "got it", no technical content, unclear what's being asked

## Decision Rules

- If message mentions payment failure + specific order/card → **payment_error_diagnosis**
- If message is about checking an order/transfer/refund status → **db_lookup_status**
- If message says "KYC" or "verification" → **kyc_verification**
- If message mentions a partner (Checkout, LULU, BBPS) + failure → **bbps_partner_escalation**
- If message asks to change/update something in the DB → **manual_backend_action**
- If message is about rates/FX/exchange → **rate_fx_investigation**
- If message describes app misbehaviour or crashes → **app_bug_engineering**
- If message is about referral/promo/cashback → **referral_promo**
- When genuinely uncertain → **other_needs_triage**

## Identifier Formats

- **order_id**: Country prefix (AE/UK/US/EU/PK/PH/IN) + 8-12 alphanumeric. Example: AE13SNKS8O00
- **user_id**: UUID v4 (8-4-4-4-12 hex with dashes). Example: 019e5135-1ac8-468b-b00f-a7f257cb3dc4. NEVER put UUIDs in order_ids.
- **payment_attempt_id**: PA- prefix. Example: PA-XXXXXXXXXX
- **fulfillment_id**: 32 hex chars, no dashes. Example: 6f3b4de9f08144dab154ff9f9b98be70
- **checkout_pay_id**: pay_ prefix. Example: pay_e5xbsxab4cqifk2in4dj3ej2pa
- **corridor**: UAE-India, UAE-Pakistan, UAE-Philippines, UK-India, US-India, etc.

## Output Format

Return ONLY a JSON object — no markdown, no explanation, no wrapping:
{
  "category": "one_of_the_9_categories",
  "confidence": 0.0-1.0,
  "summary": "One sentence summary of what the query is about",
  "order_ids": ["AE..."],
  "user_ids": ["uuid-1"],
  "payment_attempt_ids": ["PA-..."],
  "fulfillment_ids": ["32hexchars"],
  "checkout_pay_ids": ["pay_..."],
  "corridor": "UAE-India or empty string"
}
```

---

### 6. `assigner/assigner.py`

Round-robin assignment across 3 engineers with category affinity.

```python
ENGINEERS = ["Vatsal", "Adarsh", "Kanishk"]

CATEGORY_AFFINITY = {
    "payment_error_diagnosis":  "Vatsal",
    "rate_fx_investigation":    "Vatsal",
    "kyc_verification":         "Adarsh",
    "db_lookup_status":         "Adarsh",
    "referral_promo":           "Adarsh",
    "bbps_partner_escalation":  "Kanishk",
    "manual_backend_action":    "Kanishk",
    "app_bug_engineering":      "Kanishk",
    "other_needs_triage":       "",          # pure round-robin
}
```

**Logic:**

1. Look up preferred engineer for this category
2. If preferred exists AND their count is not more than 2 above the least-loaded → assign to preferred
3. Otherwise → assign to least-loaded (tie-break: round-robin order)
4. Increment count
5. Persist state to a JSON file (`cxbot_assigner_state.json`) so counts survive restarts within the same day
6. Auto-reset counts at midnight (compare stored date vs current date)

```python
@dataclass
class Assignment:
    engineer: str
    category: str
    message_ts: str
    assigned_at: str      # ISO format timestamp


class Assigner:
    def __init__(self, state_file: str = "cxbot_assigner_state.json"):
        self.state_file = state_file
        self.counts: dict[str, int] = {e: 0 for e in ENGINEERS}
        self.last_reset: str = ""   # date string "2026-03-11"
        self._load_state()

    def assign(self, category: str, message_ts: str = "") -> Assignment:
        self._maybe_reset()
        # ... affinity + balance logic ...
        self._save_state()
        return Assignment(...)
```

---

### 7. `slack_bot/poller.py`

Polls `#cx-tech-queries` using `conversations.history` from the Slack SDK.

```python
@dataclass
class SlackMessage:
    text: str
    user: str
    timestamp: str
    thread_ts: str
    channel: str


class Poller:
    def __init__(self, client: WebClient, channel_id: str, interval: int,
                 cursor_file: str, bot_user_id: str, on_message: Callable):
        ...
        self.processing: set[str] = set()
        self.completed: dict[str, float] = {}   # ts → completion unix time

    def run(self):
        """Blocking loop. Polls until KeyboardInterrupt / SIGTERM."""
        self._init_cursor()
        while True:
            self._poll()
            self._cleanup_completed()
            time.sleep(self.interval)

    def _poll(self):
        """Fetch new messages since cursor, dedup, dispatch."""
        # 1. Call conversations.history(channel, oldest=last_ts, limit=50)
        # 2. Filter out bot messages, self-messages
        # 3. For each new message:
        #    a. try_acquire(ts) — skip if already processing/completed
        #    b. Add "eyes" reaction — if already_reacted or fails, skip
        #    c. Check for existing "white_check_mark" — skip if present
        #    d. Call on_message callback
        # 4. Update cursor, persist to file

    def try_acquire(self, ts: str) -> bool:
        """Atomic check-and-mark. Returns True if we claimed it."""
        if ts in self.processing or ts in self.completed:
            return False
        self.processing.add(ts)
        return True

    def mark_done(self, ts: str):
        """Move from processing to completed."""
        self.processing.discard(ts)
        self.completed[ts] = time.time()

    def post_message(self, channel: str, text: str, thread_ts: str = ""):
        """Post to Slack. Chunk if > 39000 chars."""
        ...

    def ack_done(self, channel: str, ts: str):
        """Swap eyes → white_check_mark."""
        ...

    def add_reaction(self, channel: str, ts: str, emoji: str) -> bool:
        """Add reaction. Returns False if already_reacted or error."""
        ...
```

**Retry on Slack 429:** 3 attempts with 1s → 2s → 4s backoff. Non-429 errors fail immediately.

**Cursor persistence:** Write `last_ts` to a file after each poll. Atomic write (write to `.tmp`, rename). On startup, load from file. If no file, fetch latest message from Slack.

---

### 8. `slack_bot/formatter.py`

Builds Slack mrkdwn response strings.

```python
CATEGORY_DISPLAY_NAMES = {
    "payment_error_diagnosis":  "Payment Error Diagnosis",
    "kyc_verification":         "KYC / Verification Service Check",
    "db_lookup_status":         "DB Lookup & Status Check",
    "referral_promo":           "Referral / Promo System Check",
    "bbps_partner_escalation":  "BBPS / Partner Escalation",
    "manual_backend_action":    "Manual Backend Action",
    "rate_fx_investigation":    "Rate / FX Investigation",
    "app_bug_engineering":      "App Bug / Engineering Escalation",
    "other_needs_triage":       "Other / Needs Triage",
}


def format_response(classification: CXClassification, assignment: Assignment) -> str:
    ...
```

**For normal classifications (confidence >= 0.5):**

```
:mag: *CX-Tech Bot — Query Classified*

*Category:* Payment Error Diagnosis
*Assigned to:* Vatsal
*Confidence:* 92%

*Summary:* Customer reporting debit card transaction failure with FAB bank for order AE13SNKS8O00

*Extracted IDs:*
• Order: `AE13SNKS8O00`
• User: `019e5135-1ac8-468b-b00f-a7f257cb3dc4`

_Vatsal, please pick this up._
```

**For `other_needs_triage` or low confidence (< 0.5):**

```
:warning: *CX-Tech Bot — Needs Manual Triage*

*Category:* Other / Needs Triage
*Assigned to:* Kanishk
*Confidence:* 45%

*Summary:* Query doesn't match known patterns clearly.

_Kanishk, this needs manual triage. Bot couldn't confidently classify it._
```

**Rules:**
- Use Slack mrkdwn: `*bold*`, `_italic_`, `` `code` ``
- `:mag:` emoji for normal, `:warning:` for triage/low-confidence
- Only show "Extracted IDs" section if there are IDs to show
- Keep it concise

---

### 9. `metrics/db.py`

SQLite database for logging every classification + assignment.

```sql
CREATE TABLE IF NOT EXISTS cx_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_ts TEXT UNIQUE NOT NULL,
    channel_id TEXT NOT NULL,
    slack_user TEXT,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    summary TEXT,
    assigned_to TEXT NOT NULL,
    extracted_order_ids TEXT,              -- JSON array string
    extracted_user_ids TEXT,               -- JSON array string
    corridor TEXT,
    classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

```python
class MetricsDB:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self._create_tables()

    def record(self, msg: SlackMessage, classification: CXClassification, assignment: Assignment):
        """Insert one row. Ignore duplicates (UNIQUE on message_ts)."""
        ...

    def close(self):
        self.conn.close()
```

Store list fields (`order_ids`, `user_ids`) as JSON strings using `json.dumps()`.

---

### 10. `handler.py`

Glues everything together. This is the callback that the poller calls for each new message.

```python
class Handler:
    def __init__(self, classifier, assigner, metrics_db, poller):
        self.classifier = classifier
        self.assigner = assigner
        self.metrics = metrics_db
        self.poller = poller

    def handle(self, msg: SlackMessage):
        """Classify → Assign → Format → Post → Record → AckDone."""
        try:
            # 1. Classify
            result = self.classifier.classify(msg.text)

            # 2. Assign
            assignment = self.assigner.assign(result.category, msg.timestamp)

            # 3. Record to DB
            self.metrics.record(msg, result, assignment)

            # 4. Format + Post to Slack thread
            response = format_response(result, assignment)
            self.poller.post_message(msg.channel, response, msg.timestamp)

            # 5. Mark done (eyes → checkmark)
            self.poller.ack_done(msg.channel, msg.timestamp)
            self.poller.mark_done(msg.timestamp)

            logging.info("Handled message",
                extra={"ts": msg.timestamp, "category": result.category,
                       "assigned_to": assignment.engineer, "confidence": result.confidence})

        except Exception as e:
            # NEVER crash. Log the error, try to escalate.
            logging.error(f"Handler error for {msg.timestamp}: {e}")
            # Still try to post something helpful
            try:
                fallback_assignment = self.assigner.assign("other_needs_triage", msg.timestamp)
                self.poller.post_message(
                    msg.channel,
                    f":warning: *CX-Tech Bot — Error*\n\nBot encountered an error classifying this query.\n*Assigned to:* {fallback_assignment.engineer}\n\n_{fallback_assignment.engineer}, please triage this manually._",
                    msg.timestamp
                )
                self.poller.ack_done(msg.channel, msg.timestamp)
                self.poller.mark_done(msg.timestamp)
            except Exception:
                logging.error(f"Failed to post fallback for {msg.timestamp}")
```

---

### 11. `main.py`

Entry point. Wires everything.

```python
import signal, sys, logging
from config import load_config
from classifier.classifier import CXClassifier
from assigner.assigner import Assigner
from slack_bot.poller import Poller, SlackMessage
from metrics.db import MetricsDB
from handler import Handler
import anthropic
from slack_sdk import WebClient


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config()

    # Anthropic client
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    # Classifier
    classifier = CXClassifier(client, cfg.classifier_model)

    # Assigner
    assigner = Assigner(state_file="cxbot_assigner_state.json")

    # Metrics DB
    metrics_db = MetricsDB(cfg.db_path)

    # Slack client
    slack_client = WebClient(token=cfg.slack_bot_token)

    # Get bot's own user ID (for filtering self-messages)
    auth_resp = slack_client.auth_test()
    bot_user_id = auth_resp["user_id"]

    # Handler (poller set after creation)
    handler = Handler(classifier, assigner, metrics_db, poller=None)

    # Poller
    poller = Poller(
        client=slack_client,
        channel_id=cfg.slack_channel_id,
        interval=cfg.poll_interval,
        cursor_file=cfg.cursor_file,
        bot_user_id=bot_user_id,
        on_message=handler.handle,
    )
    handler.poller = poller

    # Graceful shutdown
    def shutdown(sig, frame):
        logging.info("Shutting down...")
        metrics_db.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start
    logging.info(f"CX-Tech Bot starting | channel={cfg.slack_channel_id} | interval={cfg.poll_interval}s | env={cfg.env}")
    poller.run()


if __name__ == "__main__":
    main()
```

---

### 12. `requirements.txt`

```
anthropic>=0.40.0
slack-sdk>=3.33.0
python-dotenv>=1.0.0
```

### 13. `.gitignore`

```
.env
__pycache__/
*.pyc
.cxbot_cursor
cxbot_metrics.db
cxbot_assigner_state.json
*.db-journal
.venv/
```

### 14. `Makefile`

```makefile
.PHONY: run install test clean

install:
	pip install -r requirements.txt

run:
	python main.py

test:
	python -m pytest tests/ -v

clean:
	rm -f cxbot_metrics.db .cxbot_cursor cxbot_assigner_state.json
```

---

## Test Messages for Validation

After building, test the classifier with these messages:

```python
TEST_CASES = [
    # payment_error_diagnosis
    ("Customer unable to make debit card transaction with FAB bank. Order AE13SNKS8O00", "payment_error_diagnosis"),
    ("Payment failed with 3DS error for user 019e5135-1ac8-468b-b00f-a7f257cb3dc4", "payment_error_diagnosis"),
    ("Getting EXCEEDS_DAILY_LIMIT on UPI payment for order AE98XYZ12345", "payment_error_diagnosis"),
    ("PA-ABC123XYZ payment attempt expired after 15 minutes", "payment_error_diagnosis"),

    # kyc_verification
    ("User KYC stuck in pending for 3 days, user id 019e5135-1ac8-468b-b00f-a7f257cb3dc4", "kyc_verification"),
    ("Compliance verification failed for customer, document rejected", "kyc_verification"),
    ("Identity verification timed out for new user signup", "kyc_verification"),

    # db_lookup_status
    ("What is the status of order AE13SNKS8O00? Customer says money deducted but shows failed", "db_lookup_status"),
    ("Transfer status not syncing in AlphaDesk, Falcon shows completed but AD shows pending", "db_lookup_status"),
    ("Refund for order UK88ABC12345 — customer asking when they'll get it", "db_lookup_status"),
    ("CNR for order AE99TEST001, bank returned funds after 12 days", "db_lookup_status"),

    # referral_promo
    ("Referral reward not credited for user who referred 3 friends", "referral_promo"),
    ("Promo code FIRST50 not applying for customer's first transaction", "referral_promo"),
    ("Cashback was promised but not reflected in wallet after 48 hours", "referral_promo"),

    # bbps_partner_escalation
    ("BBPS bill payment failing for Jio postpaid, all customers affected", "bbps_partner_escalation"),
    ("Checkout.com webhooks not being acknowledged for UAE corridor", "bbps_partner_escalation"),
    ("LULU partner payout stuck for last 6 hours", "bbps_partner_escalation"),

    # manual_backend_action
    ("Customer needs state changed from Maharashtra to Uttar Pradesh, user id 8a8b1234-abcd-1234-ef56-789012345678", "manual_backend_action"),
    ("Mobile number update needed for user, old number not accessible", "manual_backend_action"),
    ("Need to manually unlock this user's account", "manual_backend_action"),

    # rate_fx_investigation
    ("Customer complaining rate shown was different from what was applied on order AE55RATE001", "rate_fx_investigation"),
    ("FX rate for UAE-India corridor seems off compared to market rate", "rate_fx_investigation"),
    ("Rate lock expired before customer could complete the transfer", "rate_fx_investigation"),

    # app_bug_engineering
    ("App crashing when user opens transfer history on Android", "app_bug_engineering"),
    ("UI showing wrong currency symbol after latest app update", "app_bug_engineering"),
    ("White screen after login on iOS 17, multiple users reporting", "app_bug_engineering"),

    # other_needs_triage
    ("Something weird happening with this user's account, not sure what", "other_needs_triage"),
    ("thanks team!", "other_needs_triage"),
    ("hey can someone look into this", "other_needs_triage"),
]
```

---

## Critical Rules

1. **NEVER ignore a message.** Every non-bot message gets classified. If classification completely fails (LLM down + keyword fallback fails), assign to `other_needs_triage` and tag the next engineer in rotation.

2. **NEVER crash.** Wrap everything in try/except. Log errors, post a fallback response, keep running. The bot must survive API failures, rate limits, bad responses, malformed messages.

3. **Read-only.** The bot reads Slack and posts responses. It calls no external APIs except Anthropic (for classification) and Slack (for reading/posting). No AlphaDesk, no Metabase, no CloudWatch — those come in later phases.

4. **Use Python logging everywhere.** `logging.info()` for every classification, assignment, and Slack post. `logging.error()` for failures. Include `message_ts` in all log lines.

5. **Keep it simple.** No async, no threading, no multiprocessing. A simple synchronous polling loop is fine for Phase 1 at ~40 queries/day. We'll optimize later if needed.

Now build this. Create every file, make sure it runs, and include proper error handling throughout.
