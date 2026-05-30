import json
import shutil
from pathlib import Path

from app.db.models import MetricValue, StatementFact
from app.services.llm.llm_analysis_payload_builder import (
    COMPLIANCE_DISCLAIMER,
    LLMAnalysisPayloadBuilder,
    LLMAnalysisPayloadRequest,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _runtime_root() -> Path:
    root = Path("tests/runtime_llm_payload")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "data" / "validation" / "coverage" / "moex_ifrs_2021_universal_coverage_scan.json",
        {
            "company_items": [
                _coverage_item("LKOH", "FULL_STATEMENT_READY", None),
                _coverage_item("TATN", "SOURCE_BLOCKED", "missing_fy_report"),
                _coverage_item("GAZP", "PARSER_BLOCKED", "image_only_primary_statement_pages"),
            ]
        },
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_financial_ratios.json",
        {
            "summary": {"calculated_count": 1, "missing_count": 1, "unsupported_count": 1, "blocked_count": 1},
            "metrics": [
                {
                    "metric_code": "net_margin",
                    "metric_name": "Net Margin",
                    "period": "2021Q4",
                    "value": 0.12,
                    "display_value": "12.0%",
                    "unit": "ratio",
                    "status": "calculated",
                    "formula": "net_income / revenue",
                    "inputs": {
                        "net_income": {"value": 12, "source_fact_id": 1, "quality_flag": "exact"},
                        "revenue": {"value": 100, "source_fact_id": 2, "quality_flag": "exact"},
                    },
                    "warnings": [],
                    "methodology_notes": [],
                },
                _unavailable("roe", "missing", "missing_average_base_snapshot"),
                _unavailable("ebitda_margin", "unsupported_by_policy", "explicit_ebitda_missing_no_proxy_allowed"),
                _unavailable("revenue_growth", "blocked_by_period_semantics", "incomparable_revenue_period_type"),
            ],
        },
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_statement_table_extraction.json",
        {"documents_processed": 4, "statement_tables_count": 66, "facts_extracted": 0, "fact_parser_status": "not_invoked"},
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_dataframe_statement_fact_parse.json",
        {
            "canonical_fact_candidates": 40,
            "structured_facts": [
                {
                    "metric_code": "revenue",
                    "metric_name_original": "Revenue",
                    "period": "2021Q4",
                    "value": 100,
                    "source_document_id": 1,
                    "source_table_index": 0,
                    "page_number": 4,
                    "source_line": "Revenue | 100",
                    "source_trust_bucket": "validated_statement_table",
                    "extraction_method": "dataframe_statement_parser",
                }
            ],
            "derived_safe_facts": [
                {
                    "derived_metric_code": "customer_accounts",
                    "formula": "retail_customer_accounts + corporate_customer_accounts",
                    "value": 10,
                    "warning": "derived_safe_fact_not_original_statement_line",
                    "source_trust_bucket": "manual_upload_candidate_based",
                }
            ],
            "rejected_rows": [
                {
                    "raw_label": "Revenue per share",
                    "rejection_reason": "per_share_row_not_statement_fact",
                    "source_document_id": 1,
                    "source_table_index": 0,
                }
            ],
            "unmapped_numeric_evidence": [
                {
                    "raw_label": "Unknown line",
                    "numeric_values": [7],
                    "not_confirmed_fact": True,
                    "not_for_ratio_calculation": True,
                    "evidence_type": "unmapped_numeric_row",
                }
            ],
            "unmapped_table_evidence": [
                {
                    "table_title": "Note 12",
                    "not_confirmed_fact": True,
                    "not_for_ratio_calculation": True,
                    "evidence_type": "review_only_note_table",
                }
            ],
            "llm_ready_evidence_pack": {
                "normalized_facts": [
                    {
                        "metric_code": "revenue",
                        "metric_name_original": "Revenue",
                        "period": "2021Q4",
                        "value": 100,
                    }
                ],
                "derived_safe_facts": [
                    {
                        "derived_metric_code": "customer_accounts",
                        "value": 10,
                    }
                ],
                "rejected_rows": [{"raw_label": "Revenue per share"}],
                "unresolved_numeric_evidence": [{"raw_label": "Unknown line", "not_confirmed_fact": True}],
                "unresolved_table_evidence": [{"table_title": "Note 12", "not_confirmed_fact": True}],
                "evidence_warnings": ["unresolved_evidence_not_confirmed_fact"],
            },
        },
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_dataframe_vs_existing_fact_comparison.json",
        {"conflict_count": 0},
    )
    _write_json(
        root / "data" / "validation" / "LKOH" / "2021Q4_manual_report_ingestion.json",
        {
            "source_trust_bucket": "manual_upload_validated",
            "official_source_verified": False,
            "source_package_ready_contribution": False,
            "report_document_id": 237,
        },
    )
    _write_json(
        root / "data" / "validation" / "PEERS" / "LKOH_2021Q1_2021Q4_peer_analysis_report.json",
        {
            "peer_selection_status": "ready",
            "comparison_data_status": "partial",
            "comparison_readiness": "PARTIAL",
            "valuation_status": "unavailable",
        },
    )
    return root


def _coverage_item(ticker: str, level: str, blocker: str | None) -> dict:
    blockers = ["unavailable_valuation_inputs"]
    if blocker:
        blockers.insert(0, blocker)
    return {
        "ticker": ticker,
        "company_name": ticker,
        "coverage_level": level,
        "main_blocker": blocker,
        "blockers": blockers,
        "source_package_status": "READY" if ticker == "LKOH" else "PARTIAL",
    }


def _unavailable(metric_code: str, status: str, reason: str) -> dict:
    return {
        "metric_code": metric_code,
        "metric_name": metric_code,
        "period": "2021Q4",
        "value": None,
        "display_value": None,
        "unit": "ratio",
        "status": status,
        "reason": reason,
        "formula": "n/a",
        "inputs_missing": ["input"],
        "warnings": [],
        "methodology_notes": [],
    }


def test_builds_payload_from_existing_ratios_and_instructions():
    root = _runtime_root()
    payload = LLMAnalysisPayloadBuilder(root=root).build(
        LLMAnalysisPayloadRequest(company_ticker="LKOH", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()

    assert payload["company"]["coverage_level"] == "FULL_STATEMENT_READY"
    assert len(payload["financial_ratios"]["calculated"]) == 1
    assert len(payload["financial_ratios"]["missing"]) == 1
    assert len(payload["financial_ratios"]["unsupported"]) == 1
    assert len(payload["financial_ratios"]["blocked"]) == 1
    assert payload["unavailable_metrics"][0]["status"] == "missing"
    assert payload["compliance_disclaimer"] == COMPLIANCE_DISCLAIMER
    assert "Use only numbers present in this JSON." in payload["llm_instructions"]
    assert "Do not invent missing metrics." in payload["llm_instructions"]
    assert "Do not treat unresolved evidence as confirmed financial facts." in payload["llm_instructions"]
    assert payload["analysis_scope"]["peer_comparison"] is True
    assert payload["source_documents"]["peer_analysis_report"]["comparison_readiness"] == "PARTIAL"
    assert payload["normalized_facts"][0]["metric_code"] == "revenue"
    assert payload["derived_facts"][0]["derived_metric_code"] == "customer_accounts"
    assert payload["unresolved_numeric_evidence"][0]["not_confirmed_fact"] is True
    assert payload["evidence_pack_warnings"] == ["unresolved_evidence_not_confirmed_fact"]


def test_missing_ratios_report_is_unavailable_not_crash():
    root = _runtime_root()
    (root / "data" / "validation" / "LKOH" / "2021Q1_2021Q4_financial_ratios.json").unlink()

    payload = LLMAnalysisPayloadBuilder(root=root).build(
        LLMAnalysisPayloadRequest(company_ticker="LKOH", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()

    assert payload["financial_ratios"] == {"calculated": [], "missing": [], "unsupported": [], "blocked": []}
    assert payload["data_quality"]["ratios_report_available"] is False
    assert "financial_ratios_report_missing" in payload["warnings"]


def test_manual_upload_trust_and_company_blockers_are_preserved():
    root = _runtime_root()
    lkoh = LLMAnalysisPayloadBuilder(root=root).build(
        LLMAnalysisPayloadRequest(company_ticker="LKOH", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()
    tatn = LLMAnalysisPayloadBuilder(root=root).build(
        LLMAnalysisPayloadRequest(company_ticker="TATN", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()
    gazp = LLMAnalysisPayloadBuilder(root=root).build(
        LLMAnalysisPayloadRequest(company_ticker="GAZP", period_from="2021Q1", period_to="2021Q4")
    ).to_dict()

    assert lkoh["data_quality"]["manual_upload_used"] is True
    assert lkoh["data_quality"]["source_trust_bucket"] == "manual_upload_validated"
    assert lkoh["data_quality"]["manual_official_source_verified"] is False
    assert "manual_upload_lower_trust_fallback" in lkoh["data_quality"]["warnings"]
    assert "missing_fy_report" in tatn["blockers"]
    assert tatn["data_quality"]["main_status"] == "BLOCKED"
    assert "image_only_primary_statement_pages" in gazp["blockers"]


def test_builder_writes_json_and_does_not_mutate_db_or_manifests(db_session):
    root = _runtime_root()
    manifests = {path: path.read_bytes() for path in Path("data/manifests").glob("**/*") if path.is_file()}
    before_facts = db_session.query(StatementFact).count()
    before_metrics = db_session.query(MetricValue).count()
    builder = LLMAnalysisPayloadBuilder(root=root)
    payload = builder.build(LLMAnalysisPayloadRequest(company_ticker="LKOH", period_from="2021Q1", period_to="2021Q4"))
    path = builder.save_payload(payload)

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["company"]["ticker"] == "LKOH"
    assert db_session.query(StatementFact).count() == before_facts
    assert db_session.query(MetricValue).count() == before_metrics
    for path, content in manifests.items():
        assert path.read_bytes() == content


def teardown_module():
    shutil.rmtree("tests/runtime_llm_payload", ignore_errors=True)
