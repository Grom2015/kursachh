from pathlib import Path

from app.services.llm.analysis_service import LLMAnalysisService
from app.services.llm.client import LLMClient, LLMResponse


def test_llm_client_builds_anthropic_pdf_document_block(tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")

    content = LLMClient(api_key="test")._message_content(
        "Analyze the report.",
        [{"path": str(pdf_path), "source_document_id": 7}],
    )

    assert isinstance(content, list)
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["data"]
    assert content[1] == {"type": "text", "text": "Analyze the report."}


def test_llm_service_passes_source_pdf_to_fundamental_call(tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")

    class RecordingClient:
        available = True
        _model = "claude-test"

        def __init__(self):
            self.calls = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            return LLMResponse(
                text="HOLD",
                model="claude-test",
                input_tokens=1,
                output_tokens=1,
                stop_reason="end_turn",
            )

    client = RecordingClient()
    service = LLMAnalysisService(client)  # type: ignore[arg-type]

    service.generate_full_report(
        company={"ticker": "VTBR"},
        period={"from": "2025Q1", "to": "2025Q4"},
        financial_metrics=[],
        market_analysis={},
        peer_analysis={},
        source_documents=[{"id": 1, "storage_path": str(pdf_path), "file_name": "report.pdf"}],
        data_quality={},
        warnings=[],
    )

    assert len(client.calls) == 5
    assert client.calls[0]["pdf_attachments"][0]["path"] == str(pdf_path)
    assert "pdf_attachments" not in client.calls[1]
    assert client.calls[2]["pdf_attachments"][0]["path"] == str(pdf_path)
    assert client.calls[0]["max_tokens"] == 2200
    assert client.calls[1]["max_tokens"] == 1200
    assert client.calls[2]["max_tokens"] == 1600
    assert client.calls[3]["max_tokens"] == 1400
    assert client.calls[4]["pdf_attachments"][0]["path"] == str(pdf_path)
    assert client.calls[4]["max_tokens"] == 9000


def test_fundamental_prompt_instructs_llm_to_cross_check_pdf():
    from app.services.llm.prompts import fundamental_analysis_prompt

    system, user = fundamental_analysis_prompt(
        company={"ticker": "X5"},
        period={"from": "2023Q4", "to": "2023Q4"},
        financial_metrics=[],
        structured_facts=[],
        derived_safe_facts=[],
        analysis_readiness_summary={},
        top_blockers=[],
        unresolved_evidence_summary={},
        parser_risk_summary={"suspicious_fact_count": 2},
        source_documents={"source_pdf_attachments": [{"path": "report.pdf"}]},
        data_quality={},
        warnings=[],
    )

    merged = f"{system}\n{user}"
    assert "PDF" in merged
    assert "сверяй" in merged
    assert "structured_facts" in merged
    assert "не называй подтвержденным фактом" in merged
    assert "parser_risk_summary" in merged


def test_overall_prompt_requires_concise_output():
    from app.services.llm.prompts import overall_summary_prompt

    system, user = overall_summary_prompt(
        company={"ticker": "X5"},
        period={"from": "2023Q4", "to": "2023Q4"},
        fundamental_note="fund",
        technical_note="tech",
        peer_note="peer",
        source_documents={},
        data_quality={},
        warnings=[],
    )

    merged = f"{system}\n{user}".lower()
    assert "executive summary" in merged
    assert "не пиши длинный текст" in merged
    assert "до 350-500 слов" in merged


def test_detailed_memo_prompt_requires_full_markdown_report():
    from app.services.llm.prompts import detailed_memo_prompt

    system, user = detailed_memo_prompt(
        company={"ticker": "X5"},
        period={"from": "2023Q4", "to": "2023Q4"},
        financial_metrics=[],
        structured_facts=[],
        derived_safe_facts=[],
        analysis_readiness_summary={},
        top_blockers=[],
        unresolved_evidence_summary={},
        parser_risk_summary={},
        market_analysis={},
        source_documents={"source_pdf_attachments": [{"path": "report.pdf"}]},
        data_quality={},
        warnings=[],
    )

    merged = f"{system}\n{user}"
    assert "полноценный финальный аналитический отчет" in merged
    assert "1200-2200 слов" in merged
    assert "таблицы Markdown" in merged
    assert "не возвращай JSON" in merged


def test_llm_client_ignores_missing_or_non_pdf_attachments(tmp_path):
    txt_path = tmp_path / "report.txt"
    txt_path.write_text("not pdf", encoding="utf-8")

    content = LLMClient(api_key="test")._message_content(
        "Analyze the report.",
        [{"path": str(txt_path)}, {"path": str(Path(tmp_path) / "missing.pdf")}],
    )

    assert content == "Analyze the report."
