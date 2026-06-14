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

    assert client.calls[0]["pdf_attachments"][0]["path"] == str(pdf_path)
    assert all("pdf_attachments" not in call for call in client.calls[1:])


def test_llm_client_ignores_missing_or_non_pdf_attachments(tmp_path):
    txt_path = tmp_path / "report.txt"
    txt_path.write_text("not pdf", encoding="utf-8")

    content = LLMClient(api_key="test")._message_content(
        "Analyze the report.",
        [{"path": str(txt_path)}, {"path": str(Path(tmp_path) / "missing.pdf")}],
    )

    assert content == "Analyze the report."
