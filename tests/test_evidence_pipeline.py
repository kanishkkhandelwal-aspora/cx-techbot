import json
import unittest
from pathlib import Path

from cloudwatch.log_searcher import InvestigationResult, LogSearchResult
from db_agent.db_searcher import DBInvestigationResult, DBQueryResult
from knowledge_base.case_facts import build_case_facts
from knowledge_base.cx_response_playbook import find_playbook_match
from knowledge_base.response_engine import decide_response_mode
from slack_bot.formatter import format_full_response


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "evidence_cases.json"


class DummyClassification:
    category = "payment_error_diagnosis"
    summary = "summary"


class DummyAssignment:
    slack_tag = "<@U123>"


def _build_db_inv(category: str, db_rows: list[dict]) -> DBInvestigationResult:
    inv = DBInvestigationResult(category=category)
    for entry in db_rows:
        inv.queries_run.append(
            DBQueryResult(
                table=entry["table"],
                rows=entry["rows"],
                row_count=len(entry["rows"]),
            )
        )
    inv.has_data = bool(inv.queries_run)
    return inv


def _build_cw_inv(category: str, log_lines: list[str]) -> InvestigationResult:
    if not log_lines:
        return InvestigationResult(category=category)
    return InvestigationResult(
        category=category,
        search_steps=[
            LogSearchResult(
                total_results=len(log_lines),
                all_lines=log_lines,
                error_lines=log_lines,
                has_errors=True,
            )
        ],
        services_searched=["test-service"],
        error_found=True,
    )


class EvidencePipelineTests(unittest.TestCase):
    def test_fixture_regressions(self):
        cases = json.loads(FIXTURE_PATH.read_text())
        for case in cases:
            with self.subTest(case["name"]):
                db_inv = _build_db_inv(case["category"], case.get("db_rows", []))
                cw_inv = _build_cw_inv(case["category"], case.get("log_lines", []))
                facts = build_case_facts(case["category"], db_inv, cw_inv)
                match = find_playbook_match(facts)
                mode = decide_response_mode(
                    category=case["category"],
                    classifier_confidence=0.95,
                    facts=facts,
                    playbook_match=match,
                )

                self.assertEqual(match.issue if match else None, case["expected_playbook"])
                self.assertEqual(mode, case["expected_mode"])

    def test_formatter_keeps_playbook_under_cx_advice(self):
        analysis = {
            "root_cause": "• Root cause here",
            "cx_advice": "• First advice line",
            "playbook_guidance": "• Extra approved guidance",
        }
        rendered = format_full_response(
            DummyClassification(),
            DummyAssignment(),
            analysis=analysis,
            poster_user_id="U999",
            services_searched=[],
            data_sources=[],
        )
        self.assertIn("*2. CX Advice*", rendered)
        self.assertIn("• First advice line\n• Extra approved guidance", rendered)
        self.assertNotIn("Playbook Guidance", rendered)


if __name__ == "__main__":
    unittest.main()
