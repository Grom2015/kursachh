from app.db.models import AnalysisJob, AnalysisResult, Company, ReportDocument
from app.services.analysis.pdf_insight_service import _artifact_relative_path, generate_pdf_insight


class _DummyClient:
    def __init__(self, api_key=None, model=None):
        self.available = True
        self._model = model or "claude-test"


class _DummyReport:
    def __init__(self):
        self.fundamental_note = "fundamental"
        self.technical_note = "technical"
        self.peer_note = None
        self.overall_summary = "overall"
        self.recommendation = "HOLD"
        self.full_markdown = "# Demo report"
        self.structured_summary = {"status": "mixed", "executive_summary": ["one"]}
        self.memo_markdown = "# Full memo"
        self.token_usage = {"total_tokens": 42}
        self.warnings = []
        self.llm_model = "claude-test"


def test_pdf_insight_passes_structured_context_and_pdf_attachment(monkeypatch, db_session, tmp_path):
    company = Company(
        ticker="X5",
        board="TQBR",
        short_name="X5",
        full_name="X5 Group",
        aliases_json=[],
        sector="consumer",
        subsector="retail",
    )
    db_session.add(company)
    db_session.flush()

    pdf_path = tmp_path / "x5_report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")
    document = ReportDocument(
        company_id=company.id,
        report_period="2023Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        storage_path=str(pdf_path),
        file_name=pdf_path.name,
        status="validated",
    )
    db_session.add(document)
    db_session.flush()

    job = AnalysisJob(
        company_id=company.id,
        company_query="X5",
        period_from="2023Q4",
        period_to="2023Q4",
        reporting_standard="IFRS",
        data_mode="manual_upload",
        status="succeeded",
    )
    db_session.add(job)
    db_session.flush()

    result = AnalysisResult(
        job_id=job.id,
        company_id=company.id,
        period_from="2023Q4",
        period_to="2023Q4",
        reporting_standard="IFRS",
        data_snapshot_json={"document_ids": [document.id]},
        result_json={
            "company": {"ticker": "X5", "name": "X5 Group", "sector": "consumer"},
            "period_from": "2023Q4",
            "period_to": "2023Q4",
            "periods": ["2023Q4"],
            "reporting_standard": "IFRS",
            "ratios": [
                {
                    "metric_code": "net_margin",
                    "metric_name": "Net margin",
                    "period": "2023Q4",
                    "value": 0.05,
                    "display_value": "5.0%",
                    "formula": "net_income / revenue",
                    "status": "calculated",
                }
            ],
            "structured_facts": [
                {
                    "metric_code": "revenue",
                    "metric_name_original": "Revenue",
                    "period": "2023Q4",
                    "value": 100.0,
                    "raw_value": "24",
                },
                {
                    "metric_code": "net_income",
                    "metric_name_original": "Profit for the year",
                    "period": "2023Q4",
                    "value": 5.0,
                    "raw_value": "5",
                },
            ],
            "derived_safe_facts": [
                {"derived_metric_code": "customer_accounts", "value": 10.0}
            ],
            "rejected_rows": [{"raw_label": "Ambiguous line", "rejection_reason": "period_header_ambiguous"}],
            "unmapped_numeric_evidence": [{"raw_label": "Unknown line", "numeric_values": [123]}],
            "unmapped_table_evidence": [{"table_title": "Note 12"}],
            "analysis_readiness_summary": {"coverage_grade": "partial", "facts_ready": True},
            "top_blockers": ["period_header_ambiguous"],
            "documents": [{"id": document.id, "file_name": pdf_path.name, "period": "2023Q4"}],
            "facts_count": 2,
            "document_validation_status": "validated",
            "warnings": ["manual_upload_lower_trust"],
        },
        llm_payload_json={},
        warnings_json=[],
        disclaimer="demo",
    )
    db_session.add(result)
    db_session.commit()

    captured: dict[str, object] = {}

    class _DummyService:
        def __init__(self, client):
            captured["client_model"] = client._model

        def generate_full_report(self, **kwargs):
            captured.update(kwargs)
            return _DummyReport()

    monkeypatch.setattr("app.services.analysis.pdf_insight_service.LLMClient", _DummyClient)
    monkeypatch.setattr("app.services.analysis.pdf_insight_service.LLMAnalysisService", _DummyService)

    llm_report = generate_pdf_insight(db_session, result.id)
    db_session.refresh(result)

    assert captured["structured_facts"][0]["metric_code"] == "revenue"
    assert captured["derived_safe_facts"][0]["derived_metric_code"] == "customer_accounts"
    assert captured["analysis_readiness_summary"]["coverage_grade"] == "partial"
    assert captured["top_blockers"] == ["period_header_ambiguous"]
    assert captured["unresolved_evidence_summary"]["unmapped_numeric_evidence_count"] == 1
    assert captured["parser_risk_summary"]["suspicious_fact_count"] >= 1
    assert captured["source_documents"]["source_pdf_attachments"][0]["path"] == str(pdf_path)
    assert any(item["entry_type"] == "structured_fact" for item in captured["financial_metrics"])
    assert result.llm_payload_json["structured_facts"][0]["metric_code"] == "revenue"
    assert result.llm_payload_json["parser_risk_summary"]["suspicious_fact_count"] >= 1
    assert result.llm_payload_json["source_documents"]["source_pdf_attachments"][0]["path"] == str(pdf_path)
    assert llm_report["prompt_source"] == "app/services/llm/prompts.py"
    assert llm_report["structured_summary"]["status"] == "mixed"
    assert llm_report["memo_markdown_path"].endswith("_llm_memo.md")
    assert not llm_report["memo_markdown_path"].startswith(str(tmp_path))
    assert result.report_markdown == "# Full memo"


def test_artifact_relative_path_converts_absolute_root_path(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.analysis.pdf_insight_service.get_settings", lambda: type("S", (), {"root_dir": tmp_path})())
    artifact = tmp_path / "data" / "validation" / "X5" / "2023Q4_2023Q4_llm_memo.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# memo", encoding="utf-8")
    assert _artifact_relative_path(artifact) == "data/validation/X5/2023Q4_2023Q4_llm_memo.md"
