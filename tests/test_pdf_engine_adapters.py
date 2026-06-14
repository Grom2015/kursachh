import json
import sys
from pathlib import Path

from app.services.parsing.pdf_auto_parse_orchestrator import synthesize_engine_results
from app.services.parsing.pdf_engine_adapters import (
    ENGINE_STATUS_SUCCESS,
    ENGINE_STATUS_UNAVAILABLE,
    DoclingDocumentEngineAdapter,
    EngineArtifactPaths,
    EngineExtractionResult,
    EngineRunContext,
    NativePdfTableEngineAdapter,
    NativePdfTextEngineAdapter,
    OcrmypdfEngineAdapter,
    OcrTableStructureEngineAdapter,
    OcrTextEngineAdapter,
    PageRasterizationEngineAdapter,
    SecondaryTableEngineAdapter,
    WordLayoutRecoveryEngineAdapter,
    ensure_engine_runtime_environment,
    reconstruct_sequential_ocr_statement_text,
    resolved_binary_path,
    runtime_profiles,
)


def test_native_engine_adapters_produce_contract_fields():
    context = EngineRunContext(
        root=Path(".").resolve(),
        stored_document_path="data/raw/manual_uploads/LKOH/report.pdf",
        statement_table_report={
            "artifact_path": "data/parsed/LKOH/2021Q4/1_statement_tables.json",
            "statement_tables": [{"statement_type": "income_statement"}],
            "required_statement_tables_found": True,
        },
        extraction_coverage={
            "pages_total": 10,
            "pages_with_text_layer": 9,
            "pages_processed_by_native_extractor": 10,
        },
    )

    text_result = NativePdfTextEngineAdapter().run(context).to_dict()
    table_result = NativePdfTableEngineAdapter().run(context).to_dict()

    assert text_result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert table_result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert set(text_result["artifact_paths"]) == {
        "raw_output_path",
        "debug_output_path",
        "rendered_pages_path",
        "normalized_candidate_path",
    }
    assert set(table_result["artifact_paths"]) == {
        "raw_output_path",
        "debug_output_path",
        "rendered_pages_path",
        "normalized_candidate_path",
    }


def test_ensure_engine_runtime_environment_uses_workspace_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("PADDLE_PDX_CACHE_HOME", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    values = ensure_engine_runtime_environment(tmp_path)

    assert Path(values["PADDLE_PDX_CACHE_HOME"]).is_dir()
    assert Path(values["HF_HOME"]).is_dir()
    assert Path(values["TMP"]).is_dir()
    assert values["PADDLE_PDX_CACHE_HOME"].startswith(str(tmp_path))


def test_binary_resolution_checks_common_windows_install_paths(monkeypatch):
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.shutil.which", lambda _name: None)
    fake_tesseract = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")
    fake_qpdf = Path("C:/Program Files/qpdf 12.3.2/bin/qpdf.exe")
    monkeypatch.setattr(
        "app.services.parsing.pdf_engine_adapters.common_windows_binary_paths",
        lambda name: {"tesseract": [fake_tesseract], "qpdf": [fake_qpdf]}.get(name, []),
    )
    monkeypatch.setattr(Path, "exists", lambda self: self in {fake_tesseract, fake_qpdf})

    assert resolved_binary_path("tesseract") == "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
    assert resolved_binary_path("qpdf") == "C:\\Program Files\\qpdf 12.3.2\\bin\\qpdf.exe"


def test_reconstruct_sequential_ocr_statement_text_joins_label_note_values_and_skips_percent_lines():
    text = "\n".join(
        [
            "АКТИВЫ",
            "Итого активы",
            "36 884,3",
            "36 070,2",
            "2.3%",
            "Итого собственные средства",
            "2730.8",
            "2689.3",
        ]
    )

    reconstructed = reconstruct_sequential_ocr_statement_text(text)

    assert "Итого активы 36 884,3 36 070,2" in reconstructed
    assert "Итого собственные средства 2730.8 2689.3" in reconstructed
    assert "2.3% Итого" not in reconstructed


def test_optional_engines_become_unavailable_when_dependencies_missing(monkeypatch):
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda _name: False)
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda _name: False)

    context = EngineRunContext(root=Path(".").resolve())
    secondary = SecondaryTableEngineAdapter().run(context).to_dict()
    ocrmypdf = OcrmypdfEngineAdapter().run(context).to_dict()
    ocr = OcrTextEngineAdapter().run(context).to_dict()
    docling = DoclingDocumentEngineAdapter().run(context).to_dict()

    assert secondary["engine_status"] == ENGINE_STATUS_UNAVAILABLE
    assert secondary["blocker_reason"]
    assert secondary["recommended_setup_action"]
    assert ocrmypdf["engine_status"] == ENGINE_STATUS_UNAVAILABLE
    assert ocr["engine_status"] == ENGINE_STATUS_UNAVAILABLE
    assert ocr["blocker_reason"] == "missing_ocr_runtime:paddleocr_or_tesseract"
    assert docling["engine_status"] == ENGINE_STATUS_UNAVAILABLE


def test_word_layout_recovery_engine_reports_recovered_tables():
    context = EngineRunContext(
        root=Path(".").resolve(),
        statement_table_report={
            "artifact_path": "data/parsed/LKOH/2021Q4/1_statement_tables.json",
            "normalized_statement_tables": [
                {
                    "page_number": 11,
                    "warnings": ["text_table_fallback_used", "word_layout_recovery_used"],
                    "diagnostics": {"text_recovery_source": "extract_words"},
                    "rows": [{"line": "Revenue", "2021Q4": "100", "2020Q4": "90", "word_layout_column_recovered": True}],
                }
            ],
        },
    )

    result = WordLayoutRecoveryEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert result["table_candidates"] == 1
    assert result["pages_attempted"] == 1


def test_secondary_table_engine_executes_when_camelot_available(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    class FakeCamelot:
        @staticmethod
        def read_pdf(_path, pages="all"):
            assert pages == "all"
            return [object(), object()]

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "camelot")
    monkeypatch.setitem(sys.modules, "camelot", FakeCamelot)

    context = EngineRunContext(root=tmp_path, stored_document_path=str(pdf_path), pages_total=3)
    result = SecondaryTableEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert result["table_candidates"] == 2
    assert result["artifact_paths"]["debug_output_path"]
    payload = json.loads((tmp_path / result["artifact_paths"]["debug_output_path"]).read_text(encoding="utf-8"))
    assert payload["engine_name"] == "secondary_table_engine"
    assert payload["candidate_contract_version"] == "1.0"


def test_page_rasterization_engine_executes_when_pdfium_available(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    class FakeRendered:
        def save(self, target):
            Path(target).write_bytes(b"png")

    class FakePage:
        def render(self, scale=1):
            assert scale == 1
            return FakeRendered()

    class FakeDocument:
        def __init__(self, _path):
            self.pages = [FakePage(), FakePage(), FakePage()]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

    class FakePdfiumModule:
        PdfDocument = FakeDocument

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "pypdfium2")
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda _name: False)
    monkeypatch.setitem(sys.modules, "pypdfium2", FakePdfiumModule)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(pdf_path),
        extraction_coverage={"pages_requiring_ocr": 2},
        pages_total=3,
    )
    result = PageRasterizationEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert result["pages_attempted"] == 2
    assert result["artifact_paths"]["rendered_pages_path"]


def test_page_rasterization_engine_executes_when_only_poppler_binary_available(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda _name: False)
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda name: name == "pdftoppm")
    calls = []

    def fake_run(command, check=False, capture_output=True, text=True):
        calls.append(command)
        output_prefix = Path(command[-1])
        output_prefix.parent.mkdir(parents=True, exist_ok=True)
        (output_prefix.parent / "page-1.png").write_bytes(b"png")
        (output_prefix.parent / "page-2.png").write_bytes(b"png")

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.subprocess.run", fake_run)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(pdf_path),
        extraction_coverage={"pages_requiring_ocr": 2},
        pages_total=3,
    )
    result = PageRasterizationEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert result["pages_attempted"] == 2
    assert result["artifact_paths"]["rendered_pages_path"]
    assert calls
    assert calls[0][0] == "pdftoppm"


def test_page_rasterization_engine_prefers_ocr_layer_pdf_when_available(monkeypatch, tmp_path):
    original_pdf = tmp_path / "report.pdf"
    original_pdf.write_bytes(b"%PDF-1.4 original")
    ocr_layer_pdf = tmp_path / "ocr_layer.pdf"
    ocr_layer_pdf.write_bytes(b"%PDF-1.4 ocr")

    class FakeRendered:
        def save(self, target):
            Path(target).write_bytes(b"png")

    seen_paths: list[str] = []

    class FakePage:
        def render(self, scale=1):
            assert scale == 1
            return FakeRendered()

    class FakeDocument:
        def __init__(self, path):
            seen_paths.append(path)
            self.pages = [FakePage()]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

    class FakePdfiumModule:
        PdfDocument = FakeDocument

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "pypdfium2")
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda _name: False)
    monkeypatch.setitem(sys.modules, "pypdfium2", FakePdfiumModule)

    ocr_result = OcrmypdfEngineAdapter().run(EngineRunContext(root=tmp_path))
    ocr_result.artifact_paths.raw_output_path = str(ocr_layer_pdf)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(original_pdf),
        extraction_coverage={"pages_requiring_ocr": 1},
        prior_engine_results=[ocr_result],
    )
    result = PageRasterizationEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert seen_paths == [str(ocr_layer_pdf)]


def test_ocrmypdf_engine_creates_ocr_layer_artifact_when_hook_available(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    class FakeOcrmypdf:
        @staticmethod
        def ocr(input_pdf, output_pdf, **_kwargs):
            assert str(pdf_path) == input_pdf
            Path(output_pdf).write_bytes(b"%PDF-1.4 ocr-layer")

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "ocrmypdf")
    monkeypatch.setitem(sys.modules, "ocrmypdf", FakeOcrmypdf)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(pdf_path),
        extraction_coverage={"pages_requiring_ocr": 2},
        pages_total=3,
    )
    result = OcrmypdfEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert result["artifact_paths"]["raw_output_path"]
    assert (tmp_path / result["artifact_paths"]["raw_output_path"]).exists()


def test_page_rasterization_targets_ocr_required_warning_page(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    rendered_pages: list[int] = []

    class FakeRendered:
        def save(self, target):
            Path(target).write_bytes(b"png")

    class FakePage:
        def __init__(self, page_number):
            self.page_number = page_number

        def render(self, scale=1):
            assert scale == 1
            rendered_pages.append(self.page_number)
            return FakeRendered()

    class FakeDocument:
        def __init__(self, _path):
            self.pages = [FakePage(index + 1) for index in range(10)]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

    class FakePdfiumModule:
        PdfDocument = FakeDocument

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "pypdfium2")
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda _name: False)
    monkeypatch.setitem(sys.modules, "pypdfium2", FakePdfiumModule)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(pdf_path),
        statement_table_report={
            "warnings": [
                "primary_statement_page_image_only_or_no_extractable_text:page=7:statement_type=balance_sheet"
            ]
        },
        extraction_coverage={"pages_total": 10, "pages_requiring_ocr": 1},
        pages_total=10,
    )
    result = PageRasterizationEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert rendered_pages == [7]
    rendered_dir = tmp_path / result["artifact_paths"]["rendered_pages_path"]
    assert (rendered_dir / "page_7.png").exists()
    assert any("targeted_ocr_pages:7" == warning for warning in result["warnings"])


def test_docling_engine_emits_candidate_artifact_when_hook_available(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    class FakeDocling:
        @staticmethod
        def extract_statement_candidates(document_path, engine_name, effective_period, comparative_period):
            assert document_path == str(pdf_path)
            assert engine_name == "docling_document_engine"
            return [
                {
                    "page_number": 4,
                    "statement_family": "income_statement",
                    "statement_type": "income_statement",
                    "effective_period": effective_period or "2025Q4",
                    "comparative_period": comparative_period or "2024Q4",
                    "table_title": "Consolidated statement of profit or loss",
                    "header_columns": ["line", "2025", "2024"],
                    "column_boundaries": [0, 180, 280],
                    "page_bbox": [0, 0, 595, 842],
                    "table_bbox": [24, 120, 560, 410],
                    "row_blocks": [
                        {
                            "label_text": "Revenue",
                            "label_bbox": [24, 150, 220, 168],
                            "value_cells": {"2025": "100", "2024": "90"},
                            "value_bboxes": {"2025": [240, 150, 300, 168], "2024": [320, 150, 380, 168]},
                            "row_kind": "statement_line_item",
                            "row_confidence": 0.92,
                            "source_bbox": [24, 150, 380, 168],
                            "diagnostics": {"source_line": "Revenue | 100 | 90"},
                        }
                    ],
                }
            ]

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "docling")
    monkeypatch.setitem(sys.modules, "docling", FakeDocling)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(pdf_path),
        statement_table_report={"period_resolution": {"effective_report_period": "2025Q4", "comparative_period": "2024Q4"}},
        extraction_coverage={"pages_total": 5},
        pages_total=5,
    )
    result = DoclingDocumentEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    payload = json.loads((tmp_path / result["artifact_paths"]["debug_output_path"]).read_text(encoding="utf-8"))
    assert payload["tables"][0]["statement_type"] == "income_statement"
    assert payload["tables"][0]["column_boundaries"] == [0, 180, 280]


def test_ocr_text_engine_emits_candidate_artifact_from_rendered_pages(monkeypatch, tmp_path):
    rendered_dir = tmp_path / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    (rendered_dir / "page_1.png").write_bytes(b"png")

    class FakePytesseract:
        @staticmethod
        def image_to_string(path, lang="rus+eng"):
            assert "page_1.png" in str(path)
            assert lang == "rus+eng"
            return "Statement of profit or loss\nRevenue 100 90\nOperating profit 20 10"

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "pytesseract")
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda name: name == "tesseract")
    monkeypatch.setitem(sys.modules, "pytesseract", FakePytesseract)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(tmp_path / "report.pdf"),
        statement_table_report={"period_resolution": {"effective_report_period": "2021Q4", "comparative_period": "2020Q4"}},
        extraction_coverage={"pages_requiring_ocr": 1},
        prior_engine_results=[
            PageRasterizationEngineAdapter().run(
                EngineRunContext(
                    root=tmp_path,
                    stored_document_path=str(tmp_path / "report.pdf"),
                    extraction_coverage={},
                )
            )
        ],
    )
    context.prior_engine_results[0].artifact_paths.rendered_pages_path = str(rendered_dir)

    result = OcrTextEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert result["table_candidates"] == 1
    payload = json.loads((tmp_path / result["artifact_paths"]["debug_output_path"]).read_text(encoding="utf-8"))
    assert payload["tables"][0]["statement_type"] == "income_statement"
    assert payload["tables"][0]["warnings"] == ["ocr_only_lower_trust"]


def test_ocr_text_engine_uses_official_paddleocr_api_when_hook_absent(monkeypatch, tmp_path):
    rendered_dir = tmp_path / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    (rendered_dir / "page_1.png").write_bytes(b"png")

    class FakePaddleOCR:
        def __init__(self, **_kwargs):
            pass

        def predict(self, path):
            assert "page_1.png" in str(path)
            return [
                {
                    "rec_text": "Statement of profit or loss",
                    "children": [
                        {"text": "Revenue 100 90"},
                        {"text": "Operating profit 20 10"},
                    ],
                }
            ]

    fake_module = type("FakePaddleModule", (), {"PaddleOCR": FakePaddleOCR})
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "paddleocr")
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda _name: False)
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(tmp_path / "report.pdf"),
        statement_table_report={"period_resolution": {"effective_report_period": "2021Q4", "comparative_period": "2020Q4"}},
        extraction_coverage={"pages_requiring_ocr": 1},
        prior_engine_results=[
            EngineExtractionResult(
                engine_name="page_rasterization_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=60,
                pages_attempted=1,
                table_candidates=0,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(rendered_pages_path=str(rendered_dir)),
            )
        ],
    )

    result = OcrTextEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    payload = json.loads((tmp_path / result["artifact_paths"]["debug_output_path"]).read_text(encoding="utf-8"))
    assert payload["tables"][0]["statement_type"] == "income_statement"
    assert payload["tables"][0]["warnings"] == ["ocr_only_lower_trust"]


def test_ocr_text_engine_infers_income_statement_without_clean_title(monkeypatch, tmp_path):
    rendered_dir = tmp_path / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    (rendered_dir / "page_3.png").write_bytes(b"png")

    class FakePytesseract:
        @staticmethod
        def image_to_string(path, lang="rus+eng"):
            assert "page_3.png" in str(path)
            return "random scan noise\n\nRevenue 100 90\nOperating profit 20 10\nNet income 15 8"

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "pytesseract")
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda name: name == "tesseract")
    monkeypatch.setitem(sys.modules, "pytesseract", FakePytesseract)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(tmp_path / "report.pdf"),
        statement_table_report={"period_resolution": {"effective_report_period": "2021Q4", "comparative_period": "2020Q4"}},
        extraction_coverage={"pages_requiring_ocr": 1},
        prior_engine_results=[
            PageRasterizationEngineAdapter().run(
                EngineRunContext(
                    root=tmp_path,
                    stored_document_path=str(tmp_path / "report.pdf"),
                    extraction_coverage={},
                )
            )
        ],
    )
    context.prior_engine_results[0].artifact_paths.rendered_pages_path = str(rendered_dir)

    result = OcrTextEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    payload = json.loads((tmp_path / result["artifact_paths"]["debug_output_path"]).read_text(encoding="utf-8"))
    assert payload["tables"][0]["statement_type"] == "income_statement"
    assert len(payload["tables"][0]["row_blocks"]) >= 2


def test_ocr_table_structure_engine_uses_structured_candidate_hook(monkeypatch, tmp_path):
    rendered_dir = tmp_path / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    (rendered_dir / "page_2.png").write_bytes(b"png")

    class FakePaddleModule:
        @staticmethod
        def extract_statement_candidates(document_path, rendered_pages_path, engine_name):
            assert engine_name == "ocr_table_structure_engine"
            return [
                {
                    "page_number": 2,
                    "statement_family": "income_statement",
                    "statement_type": "income_statement",
                    "table_title": "Statement of profit or loss",
                    "nearby_text": "Amounts in millions of RUB",
                    "effective_period": "2021Q4",
                    "comparative_period": "2020Q4",
                    "period_source": "ocr_table_headers",
                    "period_confidence": 0.9,
                    "unit": "million",
                    "currency": "RUB",
                    "unit_multiplier": 1000000,
                    "row_blocks": [
                        {
                            "label_text": "Revenue",
                            "value_cells": {"2021": "100", "2020": "90"},
                            "row_kind": "statement_line_item",
                            "row_confidence": 0.8,
                            "diagnostics": {"source_line": "Revenue | 100 | 90"},
                        }
                    ],
                }
            ]

    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "paddleocr")
    monkeypatch.setitem(sys.modules, "paddleocr", FakePaddleModule)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(tmp_path / "report.pdf"),
        extraction_coverage={"pages_requiring_ocr": 1},
        prior_engine_results=[
            PageRasterizationEngineAdapter().run(
                EngineRunContext(
                    root=tmp_path,
                    stored_document_path=str(tmp_path / "report.pdf"),
                    extraction_coverage={},
                )
            )
        ],
    )
    context.prior_engine_results[0].artifact_paths.rendered_pages_path = str(rendered_dir)

    result = OcrTableStructureEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    assert result["table_candidates"] == 1
    payload = json.loads((tmp_path / result["artifact_paths"]["debug_output_path"]).read_text(encoding="utf-8"))
    assert payload["tables"][0]["statement_type"] == "income_statement"


def test_ocr_table_structure_engine_uses_official_ppstructure_api_when_hook_absent(monkeypatch, tmp_path):
    rendered_dir = tmp_path / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    (rendered_dir / "page_2.png").write_bytes(b"png")

    class FakePPStructureV3:
        def __init__(self, **_kwargs):
            pass

        def predict(self, path):
            assert "page_2.png" in str(path)
            return [
                {
                    "page_number": 2,
                    "page_bbox": [0, 0, 595, 842],
                    "table_bbox": [24, 120, 560, 410],
                    "column_boundaries": [0, 220, 320],
                    "table_title": "Statement of profit or loss",
                    "table_html": (
                        "<table><tr><th>Statement of profit or loss</th><th>2021</th><th>2020</th></tr>"
                        "<tr><td>Revenue</td><td>100</td><td>90</td></tr></table>"
                    )
                }
            ]

    fake_module = type("FakePaddleModule", (), {"PPStructureV3": FakePPStructureV3})
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "paddleocr")
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(tmp_path / "report.pdf"),
        statement_table_report={"period_resolution": {"effective_report_period": "2021Q4", "comparative_period": "2020Q4"}},
        extraction_coverage={"pages_requiring_ocr": 1},
        prior_engine_results=[
            EngineExtractionResult(
                engine_name="page_rasterization_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=60,
                pages_attempted=1,
                table_candidates=0,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(rendered_pages_path=str(rendered_dir)),
            )
        ],
    )

    result = OcrTableStructureEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    payload = json.loads((tmp_path / result["artifact_paths"]["debug_output_path"]).read_text(encoding="utf-8"))
    assert payload["tables"][0]["statement_type"] == "income_statement"
    assert payload["tables"][0]["warnings"] == ["ocr_only_lower_trust"]
    assert payload["tables"][0]["page_number"] == 2
    assert payload["tables"][0]["page_bbox"] == [0, 0, 595, 842]
    assert payload["tables"][0]["table_bbox"] == [24, 120, 560, 410]
    assert payload["tables"][0]["column_boundaries"] == [0, 220, 320]


def test_docling_engine_uses_official_document_converter_when_hook_absent(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    class FakeDoclingDocument:
        def export_to_dict(self):
            return {
                "pages": [
                    {
                        "page_number": 1,
                        "items": [
                            {"text": "Consolidated statement of financial position"},
                            {
                                "table": {
                                    "table_title": "Statement of financial position",
                                    "page_bbox": [0, 0, 595, 842],
                                    "table_bbox": [20, 100, 560, 430],
                                    "html": (
                                        "<table><tr><th>Statement of financial position</th><th>2025</th><th>2024</th></tr>"
                                        "<tr><td>Total assets</td><td>150</td><td>120</td></tr></table>"
                                    )
                                }
                            }
                        ]
                    }
                ]
            }

    class FakeConversionResult:
        def __init__(self):
            self.document = FakeDoclingDocument()

    class FakeDocumentConverter:
        def convert(self, source, raises_on_error=False, max_num_pages=8):
            assert source == str(pdf_path)
            assert raises_on_error is False
            assert max_num_pages == 8
            return FakeConversionResult()

    fake_converter_module = type("FakeDoclingConverterModule", (), {"DocumentConverter": FakeDocumentConverter})
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.dependency_available", lambda name: name == "docling")
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_converter_module)

    context = EngineRunContext(
        root=tmp_path,
        stored_document_path=str(pdf_path),
        statement_table_report={"period_resolution": {"effective_report_period": "2025Q4", "comparative_period": "2024Q4"}},
        extraction_coverage={"pages_total": 1},
        pages_total=1,
    )
    result = DoclingDocumentEngineAdapter().run(context).to_dict()

    assert result["engine_status"] == ENGINE_STATUS_SUCCESS
    payload = json.loads((tmp_path / result["artifact_paths"]["debug_output_path"]).read_text(encoding="utf-8"))
    assert payload["tables"][0]["statement_type"] == "balance_sheet"
    assert payload["tables"][0]["page_number"] == 1
    assert "Consolidated statement of financial position" in payload["tables"][0]["nearby_text"]
    assert payload["tables"][0]["page_bbox"] == [0, 0, 595, 842]
    assert payload["tables"][0]["table_bbox"] == [20, 100, 560, 430]


def test_runtime_profiles_expose_pdf_heavy_optional_engine_readiness(monkeypatch):
    monkeypatch.setattr(
        "app.services.parsing.pdf_engine_adapters.dependency_available",
        lambda name: name in {"pdfplumber", "camelot", "ocrmypdf", "docling"},
    )
    monkeypatch.setattr("app.services.parsing.pdf_engine_adapters.binary_available", lambda _name: False)

    payload = runtime_profiles()

    assert "ocrmypdf_ready" in payload["pdf_heavy_runtime"]
    assert "docling_document_engine_ready" in payload["pdf_heavy_runtime"]


def test_engine_synthesis_reports_runtime_profiles_and_engine_results():
    payload = synthesize_engine_results(
        root=Path(".").resolve(),
        stored_document_path="data/raw/manual_uploads/LKOH/report.pdf",
        statement_table_report={"artifact_path": "data/parsed/LKOH/2021Q4/1_statement_tables.json", "statement_tables": []},
        parse_report={},
        extraction_coverage={"pages_total": 1, "pages_processed_by_native_extractor": 1},
    )

    assert "runtime_profiles" in payload
    assert "engine_results" in payload
    assert payload["engine_cascade_order"][0:3] == [
        "native_pdf_text_engine",
        "native_pdf_table_engine",
        "word_layout_recovery_engine",
    ]
    assert "ocrmypdf_engine" in payload["engine_cascade_order"]
    assert "docling_document_engine" in payload["engine_cascade_order"]
    assert any(item["engine_name"] == "native_pdf_text_engine" for item in payload["engine_results"])
