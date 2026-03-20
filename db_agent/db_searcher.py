"""Databricks SQL searcher — Agent 2 in the multi-agent architecture.

Builds category-aware SELECT queries against prod.silver_schema.* tables
and returns structured results for Claude synthesis.

CRITICAL: Only SELECT queries. Never write/delete/drop — this is production data.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from databricks import sql as databricks_sql

logger = logging.getLogger(__name__)

# ─── Table definitions ──────────────────────────────────────────────────────
# Each table maps to its fully-qualified Unity Catalog name and key columns.

SCHEMA = "prod.silver_schema"

# ─── Category → queries mapping ─────────────────────────────────────────────
# For each category, define which tables to query and how to join on IDs.

INVESTIGATE_CATEGORIES = {
    "payment_error_diagnosis",
    "kyc_verification",
    "db_lookup_status",
    "rate_fx_investigation",
    "bbps_partner_escalation",
}


@dataclass
class DBQueryResult:
    """Result from a single Databricks query."""
    table: str = ""
    query: str = ""
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    error: str = ""


@dataclass
class DBInvestigationResult:
    """Full Databricks investigation across multiple queries."""
    category: str = ""
    queries_run: list[DBQueryResult] = field(default_factory=list)
    summary_text: str = ""  # Human-readable structured summary for Claude
    tables_searched: list[str] = field(default_factory=list)
    has_data: bool = False


class DatabricksSearcher:
    """Query Databricks SQL warehouse for structured CX data.

    All queries are READ-ONLY (SELECT). Never performs writes.
    """

    def __init__(self, server_hostname: str, http_path: str, access_token: str):
        self.server_hostname = server_hostname
        self.http_path = http_path
        self.access_token = access_token
        logger.info(f"Databricks searcher initialized (host={server_hostname[:30]}...)")

    def _get_connection(self):
        """Create a new Databricks SQL connection."""
        return databricks_sql.connect(
            server_hostname=self.server_hostname,
            http_path=self.http_path,
            access_token=self.access_token,
        )

    def _execute_query(self, query: str, table_name: str) -> DBQueryResult:
        """Execute a SELECT query and return structured results.

        Safety: Refuses to run anything that isn't a SELECT/SHOW/DESCRIBE.
        """
        result = DBQueryResult(table=table_name, query=query)

        # Safety check — only allow read operations
        normalized = query.strip().upper()
        if not any(normalized.startswith(kw) for kw in ("SELECT", "SHOW", "DESCRIBE")):
            result.error = f"BLOCKED: Only SELECT queries allowed. Got: {query[:50]}"
            logger.error(result.error)
            return result

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query)
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()

                    result.rows = [dict(zip(columns, row)) for row in rows]
                    result.row_count = len(result.rows)
                    logger.info(f"DB query [{table_name}]: {result.row_count} rows")

        except Exception as e:
            result.error = str(e)[:500]
            logger.error(f"Databricks query failed [{table_name}]: {e}")

        return result

    # ─── Public API ──────────────────────────────────────────────────────────

    def investigate(
        self,
        category: str,
        order_ids: list[str] = None,
        user_ids: list[str] = None,
        payment_attempt_ids: list[str] = None,
        fulfillment_ids: list[str] = None,
        checkout_pay_ids: list[str] = None,
    ) -> DBInvestigationResult:
        """Run category-specific queries and return structured results."""

        if category not in INVESTIGATE_CATEGORIES:
            inv = DBInvestigationResult(category=category)
            inv.summary_text = f"Databricks investigation not configured for '{category}'."
            return inv

        dispatch = {
            "payment_error_diagnosis": self._investigate_payment,
            "kyc_verification": self._investigate_kyc,
            "db_lookup_status": self._investigate_status_lookup,
            "rate_fx_investigation": self._investigate_rate_fx,
            "bbps_partner_escalation": self._investigate_payment,  # same tables
        }

        handler = dispatch[category]
        return handler(
            order_ids=order_ids or [],
            user_ids=user_ids or [],
            payment_attempt_ids=payment_attempt_ids or [],
            fulfillment_ids=fulfillment_ids or [],
            checkout_pay_ids=checkout_pay_ids or [],
        )

    # ─── Payment investigation ───────────────────────────────────────────────

    def _investigate_payment(
        self,
        order_ids: list[str],
        user_ids: list[str],
        payment_attempt_ids: list[str],
        fulfillment_ids: list[str],
        checkout_pay_ids: list[str],
    ) -> DBInvestigationResult:
        """Query payment-related tables for failure reasons."""
        investigation = DBInvestigationResult(category="payment_error_diagnosis")

        # ─── 1. Payment attempts — primary source of failure reasons ─────
        if payment_attempt_ids:
            pa_ids = ", ".join(f"'{pid}'" for pid in payment_attempt_ids[:5])
            query = f"""
                SELECT payment_attempt_id, status, reason, meta_failure_reason,
                       meta_response_summary, meta_acquirer, meta_user_id,
                       meta_order_id, year, month, day, hour
                FROM {SCHEMA}.goms_db_payment_attempts
                WHERE payment_attempt_id IN ({pa_ids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 20
            """
            result = self._execute_query(query, "goms_db_payment_attempts")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("payment_attempts")

        # ─── 2. Payment attempts by order_id ─────────────────────────────
        if order_ids:
            oids = ", ".join(f"'{oid}'" for oid in order_ids[:5])
            query = f"""
                SELECT payment_attempt_id, status, reason, meta_failure_reason,
                       meta_response_summary, meta_acquirer, meta_user_id,
                       meta_order_id, year, month, day, hour
                FROM {SCHEMA}.goms_db_payment_attempts
                WHERE meta_order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 20
            """
            result = self._execute_query(query, "goms_db_payment_attempts (by order)")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("payment_attempts")

        # ─── 3. Orders table — order status + amounts ────────────────────
        if order_ids:
            oids = ", ".join(f"'{oid}'" for oid in order_ids[:5])
            query = f"""
                SELECT order_id, status, sub_state, amount_val, amount_currency,
                       meta_postscript_pricing_info_send_amount,
                       meta_postscript_pricing_info_receive_amount,
                       meta_postscript_pricing_info_send_currency,
                       meta_postscript_pricing_info_receive_currency,
                       meta_postscript_pricing_info_fx_rate,
                       owner_id, year, month, day, hour
                FROM {SCHEMA}.goms_db_orders
                WHERE order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "goms_db_orders")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("orders")

        # ─── 4. Checkout payment data — response codes + risk ────────────
        if checkout_pay_ids:
            cpids = ", ".join(f"'{cpid}'" for cpid in checkout_pay_ids[:5])
            query = f"""
                SELECT checkout_payment_id, order_id, user_id, status,
                       response_code, response_summary, risk_flagged, risk_score,
                       year, month, day, hour
                FROM {SCHEMA}.appserver_db_checkout_payment_data
                WHERE checkout_payment_id IN ({cpids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "appserver_db_checkout_payment_data")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("checkout_payment_data")
        elif order_ids:
            oids = ", ".join(f"'{oid}'" for oid in order_ids[:5])
            query = f"""
                SELECT checkout_payment_id, order_id, user_id, status,
                       response_code, response_summary, risk_flagged, risk_score,
                       year, month, day, hour
                FROM {SCHEMA}.appserver_db_checkout_payment_data
                WHERE order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "appserver_db_checkout_payment_data (by order)")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("checkout_payment_data")

        # ─── 5. Appserver orders — rate, corridor, fulfillment provider ──
        if order_ids:
            oids = ", ".join(f"'{oid}'" for oid in order_ids[:5])
            query = f"""
                SELECT order_id, order_status, user_gid, send_amount,
                       receive_amount, currency_from, currency_to,
                       transfer_rate, payment_acquirer, fulfillment_provider,
                       year, month, day, hour
                FROM {SCHEMA}.appserver_db_orders
                WHERE order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "appserver_db_orders")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("appserver_orders")

        # ─── 6. Fulfillments — payout status ────────────────────────────
        if fulfillment_ids:
            fids = ", ".join(f"'{fid}'" for fid in fulfillment_ids[:5])
            query = f"""
                SELECT fulfillment_id, order_id, status, sub_status,
                       year, month, day, hour
                FROM {SCHEMA}.goms_db_fulfillments
                WHERE fulfillment_id IN ({fids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "goms_db_fulfillments")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("fulfillments")
        elif order_ids:
            oids = ", ".join(f"'{oid}'" for oid in order_ids[:5])
            query = f"""
                SELECT fulfillment_id, order_id, status, sub_status,
                       year, month, day, hour
                FROM {SCHEMA}.goms_db_fulfillments
                WHERE order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "goms_db_fulfillments (by order)")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("fulfillments")

        # ─── 7. Falcon transactions — partner payout status ──────────────
        if order_ids:
            oids = ", ".join(f"'{oid}'" for oid in order_ids[:5])
            query = f"""
                SELECT transaction_id, client_txn_id, status, error,
                       source_amount, payout_amount, exchange_rate,
                       year, month, day, hour
                FROM {SCHEMA}.falcondb_falcon_transactions_v2
                WHERE client_txn_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "falcondb_falcon_transactions_v2")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("falcon_transactions")

        # ─── 8. User-ID fallback: find recent orders → then query payment tables ─
        if user_ids and not order_ids and not payment_attempt_ids:
            uids = ", ".join(f"'{uid}'" for uid in user_ids[:3])
            cutoff = datetime.now(timezone.utc) - timedelta(days=14)

            # 8a. Find recent orders for this user via goms_db_orders (owner_id)
            query = f"""
                SELECT order_id, status, sub_state, amount_val, amount_currency,
                       owner_id, year, month, day, hour
                FROM {SCHEMA}.goms_db_orders
                WHERE owner_id IN ({uids})
                  AND year >= {cutoff.year}
                  AND month >= {cutoff.month}
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            orders_result = self._execute_query(query, "goms_db_orders (by user)")
            investigation.queries_run.append(orders_result)
            investigation.tables_searched.append("orders")

            # 8b. Also check appserver_db_orders (user_gid)
            query = f"""
                SELECT order_id, order_status, user_gid, send_amount,
                       receive_amount, currency_from, currency_to,
                       transfer_rate, payment_acquirer, fulfillment_provider,
                       year, month, day, hour
                FROM {SCHEMA}.appserver_db_orders
                WHERE user_gid IN ({uids})
                  AND year >= {cutoff.year}
                  AND month >= {cutoff.month}
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            app_orders_result = self._execute_query(query, "appserver_db_orders (by user)")
            investigation.queries_run.append(app_orders_result)
            investigation.tables_searched.append("appserver_orders")

            # 8c. Extract order_ids from results → query payment_attempts + fulfillments
            found_order_ids = set()
            for qr in [orders_result, app_orders_result]:
                for row in qr.rows:
                    oid = row.get("order_id")
                    if oid:
                        found_order_ids.add(oid)

            if found_order_ids:
                oids = ", ".join(f"'{oid}'" for oid in list(found_order_ids)[:5])

                # Payment attempts for found orders
                query = f"""
                    SELECT payment_attempt_id, status, reason, meta_failure_reason,
                           meta_response_summary, meta_acquirer, meta_user_id,
                           meta_order_id, year, month, day, hour
                    FROM {SCHEMA}.goms_db_payment_attempts
                    WHERE meta_order_id IN ({oids})
                    ORDER BY year DESC, month DESC, day DESC, hour DESC
                    LIMIT 20
                """
                result = self._execute_query(query, "goms_db_payment_attempts (by user→order)")
                investigation.queries_run.append(result)
                investigation.tables_searched.append("payment_attempts")

                # Checkout payment data for found orders
                query = f"""
                    SELECT checkout_payment_id, order_id, user_id, status,
                           response_code, response_summary, risk_flagged, risk_score,
                           year, month, day, hour
                    FROM {SCHEMA}.appserver_db_checkout_payment_data
                    WHERE order_id IN ({oids})
                    ORDER BY year DESC, month DESC, day DESC, hour DESC
                    LIMIT 10
                """
                result = self._execute_query(query, "appserver_db_checkout_payment_data (by user→order)")
                investigation.queries_run.append(result)
                investigation.tables_searched.append("checkout_payment_data")

                # Fulfillments for found orders
                query = f"""
                    SELECT fulfillment_id, order_id, status, sub_status,
                           year, month, day, hour
                    FROM {SCHEMA}.goms_db_fulfillments
                    WHERE order_id IN ({oids})
                    ORDER BY year DESC, month DESC, day DESC, hour DESC
                    LIMIT 10
                """
                result = self._execute_query(query, "goms_db_fulfillments (by user→order)")
                investigation.queries_run.append(result)
                investigation.tables_searched.append("fulfillments")
            else:
                # No orders found — try payment_attempts directly by meta_user_id
                query = f"""
                    SELECT payment_attempt_id, status, reason, meta_failure_reason,
                           meta_response_summary, meta_acquirer, meta_user_id,
                           meta_order_id, year, month, day, hour
                    FROM {SCHEMA}.goms_db_payment_attempts
                    WHERE meta_user_id IN ({uids})
                      AND year >= {cutoff.year}
                      AND month >= {cutoff.month}
                    ORDER BY year DESC, month DESC, day DESC, hour DESC
                    LIMIT 20
                """
                result = self._execute_query(query, "goms_db_payment_attempts (by user)")
                investigation.queries_run.append(result)
                investigation.tables_searched.append("payment_attempts")

        # Build summary
        investigation.summary_text = self._build_payment_summary(investigation)
        investigation.has_data = any(q.row_count > 0 for q in investigation.queries_run)
        return investigation

    # ─── KYC investigation ───────────────────────────────────────────────────

    def _investigate_kyc(
        self,
        order_ids: list[str],
        user_ids: list[str],
        payment_attempt_ids: list[str],
        fulfillment_ids: list[str],
        checkout_pay_ids: list[str],
    ) -> DBInvestigationResult:
        """Query KYC tables for verification status + rejection reasons."""
        investigation = DBInvestigationResult(category="kyc_verification")

        if not user_ids:
            investigation.summary_text = "No user IDs provided for KYC lookup."
            return investigation

        uids = ", ".join(f"'{uid}'" for uid in user_ids[:5])

        # ─── 1. Primary KYC table — status + rejection details ───────────
        query = f"""
            SELECT user_id, kyc_status, provider, rejection_reason,
                   rejection_reasons, rejection_count, provider_status,
                   previous_kyc_status, sub_provider, tags,
                   year, month, day, hour
            FROM {SCHEMA}.appserver_db_user_kyc
            WHERE user_id IN ({uids})
            ORDER BY year DESC, month DESC, day DESC, hour DESC
            LIMIT 20
        """
        result = self._execute_query(query, "appserver_db_user_kyc")
        investigation.queries_run.append(result)
        investigation.tables_searched.append("user_kyc")

        # ─── 2. Vance user KYC — additional status tracking ─────────────
        query = f"""
            SELECT user_id, kyc_status, rejection_reasons, rejection_count,
                   current_kyc_status, previous_kyc_status,
                   resolving_providers, resolving_sub_providers,
                   year, month, day, hour
            FROM {SCHEMA}.appserver_db_vance_user_kyc
            WHERE user_id IN ({uids})
            ORDER BY year DESC, month DESC, day DESC, hour DESC
            LIMIT 20
        """
        result = self._execute_query(query, "appserver_db_vance_user_kyc")
        investigation.queries_run.append(result)
        investigation.tables_searched.append("vance_user_kyc")

        # Build summary
        investigation.summary_text = self._build_kyc_summary(investigation)
        investigation.has_data = any(q.row_count > 0 for q in investigation.queries_run)
        return investigation

    # ─── Status lookup investigation ─────────────────────────────────────────

    def _investigate_status_lookup(
        self,
        order_ids: list[str],
        user_ids: list[str],
        payment_attempt_ids: list[str],
        fulfillment_ids: list[str],
        checkout_pay_ids: list[str],
    ) -> DBInvestigationResult:
        """Query for order/fulfillment/transaction status across systems."""
        investigation = DBInvestigationResult(category="db_lookup_status")

        # Orders
        if order_ids:
            oids = ", ".join(f"'{oid}'" for oid in order_ids[:5])

            # GOMS orders
            query = f"""
                SELECT order_id, status, sub_state, amount_val, amount_currency, owner_id,
                       year, month, day, hour
                FROM {SCHEMA}.goms_db_orders
                WHERE order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "goms_db_orders")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("goms_orders")

            # Appserver orders
            query = f"""
                SELECT order_id, order_status, send_amount, receive_amount,
                       currency_from, currency_to, transfer_rate,
                       fulfillment_provider, year, month, day, hour
                FROM {SCHEMA}.appserver_db_orders
                WHERE order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "appserver_db_orders")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("appserver_orders")

            # Fulfillments by order
            query = f"""
                SELECT fulfillment_id, order_id, status, sub_status,
                       year, month, day, hour
                FROM {SCHEMA}.goms_db_fulfillments
                WHERE order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "goms_db_fulfillments")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("fulfillments")

            # Falcon transactions
            query = f"""
                SELECT transaction_id, client_txn_id, status, error,
                       source_amount, payout_amount, exchange_rate,
                       year, month, day, hour
                FROM {SCHEMA}.falcondb_falcon_transactions_v2
                WHERE client_txn_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "falcondb_falcon_transactions_v2")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("falcon_transactions")

        # Fulfillments by ID
        if fulfillment_ids:
            fids = ", ".join(f"'{fid}'" for fid in fulfillment_ids[:5])
            query = f"""
                SELECT fulfillment_id, order_id, status, sub_status,
                       year, month, day, hour
                FROM {SCHEMA}.goms_db_fulfillments
                WHERE fulfillment_id IN ({fids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "goms_db_fulfillments (by id)")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("fulfillments")

        investigation.summary_text = self._build_status_summary(investigation)
        investigation.has_data = any(q.row_count > 0 for q in investigation.queries_run)
        return investigation

    # ─── Rate/FX investigation ───────────────────────────────────────────────

    def _investigate_rate_fx(
        self,
        order_ids: list[str],
        user_ids: list[str],
        payment_attempt_ids: list[str],
        fulfillment_ids: list[str],
        checkout_pay_ids: list[str],
    ) -> DBInvestigationResult:
        """Query for rate/FX data from orders and transactions."""
        investigation = DBInvestigationResult(category="rate_fx_investigation")

        if order_ids:
            oids = ", ".join(f"'{oid}'" for oid in order_ids[:5])

            # Appserver orders — has transfer_rate
            query = f"""
                SELECT order_id, order_status, send_amount, receive_amount,
                       currency_from, currency_to, transfer_rate,
                       payment_acquirer, year, month, day, hour
                FROM {SCHEMA}.appserver_db_orders
                WHERE order_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "appserver_db_orders")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("appserver_orders")

            # Falcon transactions — has exchange_rate
            query = f"""
                SELECT transaction_id, client_txn_id, status, error,
                       source_amount, payout_amount, exchange_rate,
                       year, month, day, hour
                FROM {SCHEMA}.falcondb_falcon_transactions_v2
                WHERE client_txn_id IN ({oids})
                ORDER BY year DESC, month DESC, day DESC, hour DESC
                LIMIT 10
            """
            result = self._execute_query(query, "falcondb_falcon_transactions_v2")
            investigation.queries_run.append(result)
            investigation.tables_searched.append("falcon_transactions")

        investigation.summary_text = self._build_rate_summary(investigation)
        investigation.has_data = any(q.row_count > 0 for q in investigation.queries_run)
        return investigation

    # ─── Summary builders ────────────────────────────────────────────────────

    def _build_payment_summary(self, inv: DBInvestigationResult) -> str:
        """Build a structured text summary from payment query results."""
        lines = ["## Databricks — Payment Data\n"]

        for qr in inv.queries_run:
            if qr.error:
                lines.append(f"**{qr.table}**: Query error — {qr.error[:200]}\n")
                continue
            if qr.row_count == 0:
                continue

            lines.append(f"**{qr.table}** ({qr.row_count} rows):")
            for row in qr.rows[:10]:  # Cap at 10 rows per table
                row_parts = []
                for key, val in row.items():
                    if key in ("year", "month", "day", "hour"):
                        continue  # skip partition columns
                    if val is not None and str(val).strip():
                        row_parts.append(f"{key}={val}")
                if row_parts:
                    lines.append(f"  • {' | '.join(row_parts)}")
            lines.append("")

        if len(lines) == 1:
            lines.append("No payment data found in Databricks for the provided IDs.")

        return "\n".join(lines)

    def _build_kyc_summary(self, inv: DBInvestigationResult) -> str:
        """Build a structured text summary from KYC query results."""
        lines = ["## Databricks — KYC Data\n"]

        for qr in inv.queries_run:
            if qr.error:
                lines.append(f"**{qr.table}**: Query error — {qr.error[:200]}\n")
                continue
            if qr.row_count == 0:
                continue

            lines.append(f"**{qr.table}** ({qr.row_count} rows):")
            for row in qr.rows[:10]:
                row_parts = []
                for key, val in row.items():
                    if key in ("year", "month", "day", "hour"):
                        continue
                    if val is not None and str(val).strip():
                        row_parts.append(f"{key}={val}")
                if row_parts:
                    lines.append(f"  • {' | '.join(row_parts)}")
            lines.append("")

        if len(lines) == 1:
            lines.append("No KYC data found in Databricks for the provided user IDs.")

        return "\n".join(lines)

    def _build_status_summary(self, inv: DBInvestigationResult) -> str:
        """Build a structured text summary from status lookup results."""
        lines = ["## Databricks — Status Lookup Data\n"]

        for qr in inv.queries_run:
            if qr.error:
                lines.append(f"**{qr.table}**: Query error — {qr.error[:200]}\n")
                continue
            if qr.row_count == 0:
                continue

            lines.append(f"**{qr.table}** ({qr.row_count} rows):")
            for row in qr.rows[:10]:
                row_parts = []
                for key, val in row.items():
                    if key in ("year", "month", "day", "hour"):
                        continue
                    if val is not None and str(val).strip():
                        row_parts.append(f"{key}={val}")
                if row_parts:
                    lines.append(f"  • {' | '.join(row_parts)}")
            lines.append("")

        if len(lines) == 1:
            lines.append("No status data found in Databricks for the provided IDs.")

        return "\n".join(lines)

    def _build_rate_summary(self, inv: DBInvestigationResult) -> str:
        """Build a structured text summary from rate/FX results."""
        lines = ["## Databricks — Rate / FX Data\n"]

        for qr in inv.queries_run:
            if qr.error:
                lines.append(f"**{qr.table}**: Query error — {qr.error[:200]}\n")
                continue
            if qr.row_count == 0:
                continue

            lines.append(f"**{qr.table}** ({qr.row_count} rows):")
            for row in qr.rows[:10]:
                row_parts = []
                for key, val in row.items():
                    if key in ("year", "month", "day", "hour"):
                        continue
                    if val is not None and str(val).strip():
                        row_parts.append(f"{key}={val}")
                if row_parts:
                    lines.append(f"  • {' | '.join(row_parts)}")
            lines.append("")

        if len(lines) == 1:
            lines.append("No rate/FX data found in Databricks for the provided IDs.")

        return "\n".join(lines)
