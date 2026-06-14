import json
from pathlib import Path

from sqlalchemy import select

from app.db.models import Company, ReportDocument
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser
from app.services.parsing.pdf_auto_parse_orchestrator import (
    augment_statement_table_report_with_engine_candidates,
)
from app.services.parsing.pdf_engine_adapters import (
    ENGINE_STATUS_SUCCESS,
    EngineArtifactPaths,
    EngineExtractionResult,
)
from app.services.parsing.statement_table_extractor import (
    ExtractedStatementTable,
    statement_tables_path,
)


def _base_report(doc, artifact):
    return {
        "document_id": doc.id,
        "reporting_standard": "IFRS",
        "period": "2021Q4",
        "default_extraction_surface": "normalized_statement_tables",
        "legacy_statement_tables_compatible": True,
        "period_resolution": {},
        "tables_found": 0,
        "tables_extracted": 0,
        "statement_tables_count": 0,
        "statement_coverage": {},
        "normalized_statement_tables": [],
        "table_structure_diagnostics": {},
        "degraded_primary_statement_count": 0,
        "extraction_coverage": {"pages_total": 1, "pages_processed_by_native_extractor": 1},
        "required_statement_tables_found": False,
        "required_statement_tables_missing": ["balance_sheet", "income_statement"],
        "facts_extracted": 0,
        "fact_parser_status": "not_invoked",
        "statement_tables": [],
        "warnings": [],
        "artifact_path": str(artifact),
    }


def test_secondary_engine_candidates_are_appended_and_become_parseable(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "secondary_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 3,
                        "table_title": "Consolidated Statement of Profit or Loss and Other Comprehensive Income",
                        "nearby_text": "Amounts in millions of RUB",
                        "rows": [
                            ["Consolidated Statement of Profit or Loss and Other Comprehensive Income", "2021", "2020"],
                            ["Revenue", "100", "90"],
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="secondary_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=70,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            )
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    assert updated["statement_tables_count"] == 1
    assert updated["normalized_statement_tables"][0]["statement_type"] == "income_statement"
    assert "non_native_engine_candidates_processed" in updated["warnings"]

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    assert revenue.value == 100_000_000.0
    assert revenue.source_location["source_engine"] == "secondary_table_engine"


def test_ocr_candidate_row_blocks_are_imported_into_parse_path(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "ocr_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 4,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "table_headers",
                        "period_confidence": 0.95,
                        "unit": "million",
                        "currency": "RUB",
                        "unit_multiplier": 1000000,
                        "row_blocks": [
                            {
                                "label_text": "Revenue",
                                "label_tokens": ["Revenue"],
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.8,
                                "diagnostics": {"source_line": "Revenue | 100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="ocr_table_structure_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=40,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            )
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    assert revenue.value == 100_000_000.0
    assert revenue.source_location["source_engine"] == "ocr_table_structure_engine"
    assert revenue.source_location["fusion_status"] == "single_engine"
    assert "ocr_only_lower_trust" in revenue.warnings


def test_ocr_layer_pdf_is_reparsed_by_native_extractor_and_appended(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    ocr_layer_path = tmp_path / "ocr_layer.pdf"
    ocr_layer_path.write_bytes(b"%PDF-1.4 ocr")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["extraction_coverage"]["pages_requiring_ocr"] = 1
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="ocrmypdf_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=65,
                pages_attempted=1,
                table_candidates=0,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(raw_output_path=str(ocr_layer_path)),
            )
        ]

    def fake_extract_pdf(self, document_arg, path_arg):
        assert str(path_arg) == str(ocr_layer_path)
        return [
            ExtractedStatementTable(
                document_id=doc.id,
                company_ticker="LKOH",
                period="2021Q4",
                reporting_standard="IFRS",
                statement_type="income_statement",
                period_type="annual",
                table_index=0,
                page_number=2,
                table_title="Statement of Profit or Loss",
                unit="million",
                currency="RUB",
                unit_multiplier=1000000,
                columns=["line", "2021", "2020"],
                rows=[{"line": "Revenue", "2021": "100", "2020": "90"}],
                dataframe_json={"orientation": "records", "data": [{"line": "Revenue", "2021": "100", "2020": "90"}]},
                source_location={"page": 2, "table_index": 0, "source_engine": "native_pdf_table_engine"},
                confidence_score=0.85,
                extraction_method="pdf_table",
                source_engine="native_pdf_table_engine",
                source_table_id=f"{doc.id}:2:0",
                row_blocks=[
                    {
                        "label_text": "Revenue",
                        "label_tokens": ["Revenue"],
                        "value_cells": {"2021": "100", "2020": "90"},
                        "row_kind": "statement_line_item",
                        "row_confidence": 0.85,
                        "diagnostics": {"source_line": "Revenue | 100 | 90"},
                    }
                ],
                statement_family="income_statement",
                table_role="primary_statement_degraded_but_usable",
            )
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )
    monkeypatch.setattr(
        "app.services.parsing.statement_table_extractor.StatementTableExtractor._extract_pdf",
        fake_extract_pdf,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    table = updated["normalized_statement_tables"][0]
    assert table["statement_type"] == "income_statement"
    assert table["source_engine"] == "native_pdf_table_engine_ocr_layer"
    assert "ocr_layer_native_reextract_used" in table["warnings"]
    assert table["source_location"]["ocr_layer_pdf_used"] is True

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    assert revenue.value == 100_000_000.0
    assert revenue.source_location["source_engine"] == "native_pdf_table_engine_ocr_layer"


def test_candidate_page_layout_surface_is_materialized_into_report_and_table(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "page_layout_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 4,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "table_headers",
                        "period_confidence": 0.95,
                        "unit": "million",
                        "currency": "RUB",
                        "unit_multiplier": 1000000,
                        "page_bbox": [0, 0, 595, 842],
                        "table_bbox": [24, 120, 560, 410],
                        "column_boundaries": [0, 220, 320],
                        "statement_title_candidates": ["Statement of Profit or Loss", "Consolidated profit and loss"],
                        "layout_diagnostics": {"line_deslur_attempted": True},
                        "row_blocks": [
                            {
                                "label_text": "Revenue",
                                "label_bbox": [24, 150, 220, 168],
                                "label_tokens": ["Revenue"],
                                "value_cells": {"2021": "100", "2020": "90"},
                                "value_bboxes": {"2021": [240, 150, 300, 168], "2020": [320, 150, 380, 168]},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.8,
                                "diagnostics": {"source_line": "Revenue | 100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="docling_document_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=35,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            )
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    table = updated["normalized_statement_tables"][0]
    assert table["column_boundaries"] == [0, 220, 320]
    assert table["page_layout"]["page_bbox"] == [0, 0, 595, 842]
    assert table["statement_title_candidates"] == [
        "Statement of Profit or Loss",
        "Consolidated profit and loss",
    ]
    assert table["row_blocks"][0]["label_bbox"] == [24, 150, 220, 168]
    assert table["row_blocks"][0]["value_bboxes"]["2021"] == [240, 150, 300, 168]
    assert updated["normalized_page_layouts"][0]["table_regions"][0]["table_bbox"] == [24, 120, 560, 410]
    assert updated["normalized_page_layouts"][0]["lines"][0]["text"] == "Revenue | 100 | 90"


def test_page_layout_merged_line_recovers_multiple_statement_rows_in_parser(db_session, tmp_path):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    report = _base_report(doc, artifact)
    report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 4,
            "table_title": "Statement of Profit or Loss",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Revenue"}, {"line": "Operating profit"}],
            "row_blocks": [
                {
                    "label_text": "Revenue",
                    "label_tokens": ["Revenue"],
                    "value_cells": {},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.76,
                    "source_engine": "ocr_text_engine",
                    "source_page": 4,
                    "source_table_id": "ocr:4:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["ocr_text_engine"],
                    "source_traceability": {
                        "source_engine": "ocr_text_engine",
                        "source_page": 4,
                        "source_table_id": "ocr:4:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["ocr_text_engine"],
                    },
                    "diagnostics": {"source_line": "Revenue"},
                },
                {
                    "label_text": "Operating profit",
                    "label_tokens": ["Operating", "profit"],
                    "value_cells": {},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.76,
                    "source_engine": "ocr_text_engine",
                    "source_page": 4,
                    "source_table_id": "ocr:4:1",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["ocr_text_engine"],
                    "source_traceability": {
                        "source_engine": "ocr_text_engine",
                        "source_page": 4,
                        "source_table_id": "ocr:4:1",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["ocr_text_engine"],
                    },
                    "diagnostics": {"source_line": "Operating profit"},
                },
            ],
            "page_layout": {
                "page_number": 4,
                "page_bbox": [0, 0, 595, 842],
                "tokens": [],
                "lines": [
                    {
                        "text": "Revenue 100 90 Operating profit 20 10",
                        "bbox": [24, 150, 380, 180],
                        "confidence": 0.83,
                        "source_engine": "ocr_text_engine",
                    }
                ],
                "table_regions": [{"table_title": "Statement of Profit or Loss", "table_bbox": [24, 120, 560, 410]}],
                "statement_title_candidates": ["Statement of Profit or Loss"],
                "layout_diagnostics": {"merged_line_detected": True},
                "source_engine": "ocr_text_engine",
            },
            "dataframe_json": {"orientation": "records", "data": [{"line": "Revenue"}, {"line": "Operating profit"}]},
            "source_traceability": {
                "source_engine": "ocr_text_engine",
                "source_page": 4,
                "source_table_id": "ocr:4",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["ocr_text_engine"],
            },
            "source_location": {"page": 4, "table_index": 0},
            "confidence_score": 0.8,
            "extraction_method": "ocr_text_engine",
            "quality_flag": "ocr_candidate_table",
            "warnings": ["ocr_only_lower_trust"],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    report["statement_tables"] = report["normalized_statement_tables"]
    report["statement_tables_count"] = 1
    report["tables_extracted"] = 1
    report["required_statement_tables_found"] = True
    artifact.write_text(json.dumps(report), encoding="utf-8")

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    by_metric = {fact.metric_code: fact for fact in result.facts}

    assert by_metric["revenue"].value == 100_000_000.0
    assert by_metric["operating_profit"].value == 20_000_000.0
    assert by_metric["revenue"].source_location["source_line"] == "Revenue 100 90 Operating profit 20 10"
    assert by_metric["operating_profit"].source_location["source_line"] == "Revenue 100 90 Operating profit 20 10"


def test_native_and_ocr_candidates_merge_before_parse(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 1,
            "table_title": "income_statement",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Revenue"}],
            "row_blocks": [
                {
                    "label_text": "Revenue",
                    "label_tokens": ["Revenue"],
                    "value_cells": {},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.85,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Revenue"},
                }
            ],
            "dataframe_json": {"orientation": "records", "data": [{"line": "Revenue"}]},
            "source_traceability": {
                "source_engine": "native_pdf_table_engine",
                "source_page": 1,
                "source_table_id": "1:1:0",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["native_pdf_table_engine"],
            },
            "source_location": {"page": 1, "table_index": 0},
            "confidence_score": 0.85,
            "extraction_method": "pdf_table",
            "quality_flag": "degraded_pdf_table",
            "warnings": [],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    base_report["statement_tables"] = []
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "ocr_merge_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 1,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "table_headers",
                        "period_confidence": 0.95,
                        "row_blocks": [
                            {
                                "label_text": "Revenue",
                                "label_tokens": ["Revenue"],
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.8,
                                "diagnostics": {"source_line": "Revenue | 100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="native_pdf_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=90,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(),
            ),
            EngineExtractionResult(
                engine_name="ocr_table_structure_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=40,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    assert updated["engine_fusion_diagnostics"][0]["fusion_status"] == "merged_engines"
    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    assert revenue.value == 100_000_000.0
    assert revenue.source_location["fusion_status"] == "merged_engines"
    assert set(revenue.source_location["source_engines_involved"]) == {
        "native_pdf_table_engine",
        "ocr_table_structure_engine",
    }


def test_multi_engine_continuation_candidate_is_inserted_adjacent_for_parser_recovery(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 1,
            "table_title": "income_statement",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Sales and other operating"}, {"line": "Operating profit", "2021": "20", "2020": "10"}],
            "row_blocks": [
                {
                    "label_text": "Sales and other operating",
                    "label_tokens": ["Sales", "and", "other", "operating"],
                    "value_cells": {},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.72,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Sales and other operating"},
                },
                {
                    "label_text": "Operating profit",
                    "label_tokens": ["Operating", "profit"],
                    "value_cells": {"2021": "20", "2020": "10"},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.85,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:1",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:1",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Operating profit | 20 | 10"},
                },
            ],
            "dataframe_json": {
                "orientation": "records",
                "data": [{"line": "Sales and other operating"}, {"line": "Operating profit", "2021": "20", "2020": "10"}],
            },
            "source_traceability": {
                "source_engine": "native_pdf_table_engine",
                "source_page": 1,
                "source_table_id": "1:1:0",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["native_pdf_table_engine"],
            },
            "source_location": {"page": 1, "table_index": 0},
            "confidence_score": 0.85,
            "extraction_method": "pdf_table",
            "quality_flag": "degraded_pdf_table",
            "warnings": [],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    base_report["statement_tables"] = []
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "ocr_continuation_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 1,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "table_headers",
                        "period_confidence": 0.95,
                        "row_blocks": [
                            {
                                "label_text": "revenues",
                                "label_tokens": ["revenues"],
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.8,
                                "diagnostics": {"source_line": "revenues | 100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="native_pdf_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=90,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(),
            ),
            EngineExtractionResult(
                engine_name="ocr_table_structure_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=40,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    labels = [block.get("label_text") for block in updated["normalized_statement_tables"][0]["row_blocks"]]
    assert labels == ["Sales and other operating revenues", "Operating profit"]
    assert updated["engine_fusion_diagnostics"][0]["fusion_mode"] == "continuation_row_merge"

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    operating_profit = next(fact for fact in result.facts if fact.metric_code == "operating_profit")

    assert revenue.value == 100_000_000.0
    assert revenue.raw_label == "Sales and other operating revenues"
    assert revenue.source_location["fusion_status"] == "merged_engines"
    assert set(revenue.source_location["source_engines_involved"]) == {
        "native_pdf_table_engine",
        "ocr_table_structure_engine",
    }
    assert operating_profit.value == 20_000_000.0


def test_multi_engine_semantic_metric_identity_merges_label_variants(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 1,
            "table_title": "income_statement",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Sales"}],
            "row_blocks": [
                {
                    "label_text": "Sales",
                    "label_tokens": ["Sales"],
                    "value_cells": {"2021": "100", "2020": "90"},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.82,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Sales | 100 | 90"},
                }
            ],
            "dataframe_json": {"orientation": "records", "data": [{"line": "Sales", "2021": "100", "2020": "90"}]},
            "source_traceability": {
                "source_engine": "native_pdf_table_engine",
                "source_page": 1,
                "source_table_id": "1:1:0",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["native_pdf_table_engine"],
            },
            "source_location": {"page": 1, "table_index": 0},
            "confidence_score": 0.85,
            "extraction_method": "pdf_table",
            "quality_flag": "degraded_pdf_table",
            "warnings": [],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    base_report["statement_tables"] = []
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "ocr_semantic_identity_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 1,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "table_headers",
                        "period_confidence": 0.95,
                        "row_blocks": [
                            {
                                "label_text": "Revenue",
                                "label_tokens": ["Revenue"],
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.8,
                                "diagnostics": {"source_line": "Revenue | 100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="native_pdf_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=90,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(),
            ),
            EngineExtractionResult(
                engine_name="ocr_table_structure_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=40,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    row_blocks = updated["normalized_statement_tables"][0]["row_blocks"]
    assert len(row_blocks) == 1
    assert row_blocks[0]["fusion_status"] == "merged_engines"

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")
    assert revenue.value == 100_000_000.0
    assert revenue.source_location["fusion_status"] == "merged_engines"


def test_multi_engine_note_reference_value_tail_is_inserted_adjacent_for_parser_recovery(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 1,
            "table_title": "income_statement",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Revenue"}, {"line": "Operating profit", "2021": "20", "2020": "10"}],
            "row_blocks": [
                {
                    "label_text": "Revenue",
                    "label_tokens": ["Revenue"],
                    "value_cells": {},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.72,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Revenue"},
                },
                {
                    "label_text": "Operating profit",
                    "label_tokens": ["Operating", "profit"],
                    "value_cells": {"2021": "20", "2020": "10"},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.85,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:1",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:1",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Operating profit | 20 | 10"},
                },
            ],
            "dataframe_json": {
                "orientation": "records",
                "data": [{"line": "Revenue"}, {"line": "Operating profit", "2021": "20", "2020": "10"}],
            },
            "source_traceability": {
                "source_engine": "native_pdf_table_engine",
                "source_page": 1,
                "source_table_id": "1:1:0",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["native_pdf_table_engine"],
            },
            "source_location": {"page": 1, "table_index": 0},
            "confidence_score": 0.85,
            "extraction_method": "pdf_table",
            "quality_flag": "degraded_pdf_table",
            "warnings": [],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    base_report["statement_tables"] = []
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "ocr_note_ref_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 1,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "table_headers",
                        "period_confidence": 0.95,
                        "row_blocks": [
                            {
                                "label_text": "Note 2",
                                "label_tokens": ["Note", "2"],
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "note_reference_only",
                                "row_confidence": 0.8,
                                "diagnostics": {"source_line": "Note 2 | 100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="native_pdf_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=90,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(),
            ),
            EngineExtractionResult(
                engine_name="ocr_table_structure_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=40,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    row_blocks = updated["normalized_statement_tables"][0]["row_blocks"]
    labels = [block.get("label_text") for block in row_blocks]
    assert labels == ["Revenue", "Note 2", "Operating profit"]
    assert row_blocks[1]["diagnostics"]["fragment_role"] == "mixed_fragment"
    assert "treat_label_as_note_reference" in row_blocks[1]["diagnostics"]["anchor_hints"]

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")

    assert revenue.value == 100_000_000.0
    assert revenue.raw_label == "Revenue"
    assert revenue.source_location["fusion_status"] == "merged_engines"
    assert set(revenue.source_location["source_engines_involved"]) == {
        "native_pdf_table_engine",
        "ocr_table_structure_engine",
    }


def test_unknown_family_candidate_can_attach_to_unique_primary_statement_surface(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 1,
            "table_title": "income_statement",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Revenue"}],
            "row_blocks": [
                {
                    "label_text": "Revenue",
                    "label_tokens": ["Revenue"],
                    "value_cells": {},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.72,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Revenue"},
                }
            ],
            "dataframe_json": {"orientation": "records", "data": [{"line": "Revenue"}]},
            "source_traceability": {
                "source_engine": "native_pdf_table_engine",
                "source_page": 1,
                "source_table_id": "1:1:0",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["native_pdf_table_engine"],
            },
            "source_location": {"page": 1, "table_index": 0},
            "confidence_score": 0.85,
            "extraction_method": "pdf_table",
            "quality_flag": "degraded_pdf_table",
            "warnings": [],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    base_report["statement_tables"] = []
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "unknown_family_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 1,
                        "statement_family": "unknown",
                        "statement_type": "unknown",
                        "table_title": "",
                        "nearby_text": "100 90",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "ocr_text_fragment",
                        "period_confidence": 0.7,
                        "row_blocks": [
                            {
                                "label_text": "Revenue",
                                "label_tokens": ["Revenue"],
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.76,
                                "diagnostics": {"source_line": "Revenue | 100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="native_pdf_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=90,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(),
            ),
            EngineExtractionResult(
                engine_name="ocr_text_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=35,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    assert len(updated["normalized_statement_tables"]) == 1
    assert updated["normalized_statement_tables"][0]["statement_type"] == "income_statement"

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")

    assert revenue.value == 100_000_000.0
    assert revenue.source_location["fusion_status"] == "merged_engines"
    assert set(revenue.source_location["source_engines_involved"]) == {
        "native_pdf_table_engine",
        "ocr_text_engine",
    }


def test_values_only_numeric_fragment_is_routed_to_adjacent_recovery_not_false_conflict(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 1,
            "table_title": "income_statement",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Revenue"}, {"line": "Operating profit", "2021": "20", "2020": "10"}],
            "row_blocks": [
                {
                    "label_text": "Revenue",
                    "label_tokens": ["Revenue"],
                    "value_cells": {},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.72,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Revenue"},
                },
                {
                    "label_text": "Operating profit",
                    "label_tokens": ["Operating", "profit"],
                    "value_cells": {"2021": "20", "2020": "10"},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.85,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:1",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:1",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Operating profit | 20 | 10"},
                },
            ],
            "dataframe_json": {
                "orientation": "records",
                "data": [{"line": "Revenue"}, {"line": "Operating profit", "2021": "20", "2020": "10"}],
            },
            "source_traceability": {
                "source_engine": "native_pdf_table_engine",
                "source_page": 1,
                "source_table_id": "1:1:0",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["native_pdf_table_engine"],
            },
            "source_location": {"page": 1, "table_index": 0},
            "confidence_score": 0.85,
            "extraction_method": "pdf_table",
            "quality_flag": "degraded_pdf_table",
            "warnings": [],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    base_report["statement_tables"] = []
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "values_only_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 1,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "100 90",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "ocr_text_fragment",
                        "period_confidence": 0.7,
                        "row_blocks": [
                            {
                                "label_text": "",
                                "label_tokens": [],
                                "value_cells": {"2021": "100", "2020": "90"},
                                "row_kind": "numeric_fragment",
                                "row_confidence": 0.74,
                                "diagnostics": {"source_line": "100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="native_pdf_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=90,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(),
            ),
            EngineExtractionResult(
                engine_name="ocr_text_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=35,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    row_blocks = updated["normalized_statement_tables"][0]["row_blocks"]
    labels = [block.get("label_text") for block in row_blocks]
    assert labels == ["Revenue", "", "Operating profit"]
    assert row_blocks[1]["diagnostics"]["fragment_role"] == "value_only"
    assert "attach_to_previous_label_only_row" in row_blocks[1]["diagnostics"]["anchor_hints"]

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")

    assert revenue.value == 100_000_000.0
    assert revenue.source_location["fusion_status"] == "merged_engines"
    assert set(revenue.source_location["source_engines_involved"]) == {
        "native_pdf_table_engine",
        "ocr_text_engine",
    }
    assert not result.unmapped_numeric_evidence


def test_single_row_candidate_without_prebuilt_row_blocks_is_imported_into_parse_path(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "single_row_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 2,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "ocr_text_fragment",
                        "period_confidence": 0.7,
                        "unit": "million",
                        "currency": "RUB",
                        "unit_multiplier": 1000000,
                        "columns": ["line", "2021", "2020"],
                        "rows": [["Revenue", "100", "90"]],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="ocr_text_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=35,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            )
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    assert updated["statement_tables_count"] == 1
    assert updated["normalized_statement_tables"][0]["statement_type"] == "income_statement"

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    revenue = next(fact for fact in result.facts if fact.metric_code == "revenue")

    assert revenue.value == 100_000_000.0
    assert revenue.source_location["source_engine"] == "ocr_text_engine"


def test_geometry_split_is_materialized_into_normalized_statement_tables(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "geometry_split_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 2,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "ocr_text_fragment",
                        "period_confidence": 0.9,
                        "row_blocks": [
                            {
                                "label_text": "Revenue 100 90 Operating profit 20 10",
                                "value_cells": {},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.78,
                                "diagnostics": {"source_line": "Revenue 100 90 Operating profit 20 10"},
                            }
                        ],
                        "page_layout": {
                            "page_number": 2,
                            "tokens": [
                                {
                                    "text": "Revenue",
                                    "bbox": [24, 140, 90, 160],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                                {
                                    "text": "100",
                                    "bbox": [240, 140, 280, 160],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                                {
                                    "text": "90",
                                    "bbox": [320, 140, 350, 160],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                                {
                                    "text": "Operating",
                                    "bbox": [28, 168, 120, 188],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                                {
                                    "text": "profit",
                                    "bbox": [128, 168, 180, 188],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                                {
                                    "text": "20",
                                    "bbox": [240, 168, 270, 188],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                                {
                                    "text": "10",
                                    "bbox": [320, 168, 350, 188],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                            ],
                            "lines": [
                                {
                                    "text": "Revenue 100 90 Operating profit 20 10",
                                    "bbox": [24, 140, 420, 188],
                                    "confidence": 0.84,
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                }
                            ],
                            "layout_diagnostics": {"merged_line_detected": True, "token_geometry_available": True},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="ocr_text_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=35,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            )
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    row_blocks = updated["normalized_statement_tables"][0]["row_blocks"]
    labels = [block.get("label_text") for block in row_blocks]
    assert labels == ["Revenue", "Operating profit"]
    assert row_blocks[0]["value_cells"] == {"2021": "100", "2020": "90"}
    assert row_blocks[1]["value_cells"] == {"2021": "20", "2020": "10"}
    assert row_blocks[0]["diagnostics"]["merged_statement_line_split"] is True


def test_layout_token_alignment_recovers_missing_label_for_numeric_row_block(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "layout_alignment_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 2,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2021Q4",
                        "comparative_period": "2020Q4",
                        "period_source": "ocr_text_fragment",
                        "period_confidence": 0.9,
                        "row_blocks": [
                            {
                                "label_text": None,
                                "value_cells": {"2021": "100", "2020": "90"},
                                "value_bboxes": {"2021": [240, 140, 280, 160], "2020": [320, 140, 350, 160]},
                                "row_kind": "numeric_fragment",
                                "row_confidence": 0.72,
                                "diagnostics": {"source_line": "100 | 90"},
                            }
                        ],
                        "page_layout": {
                            "page_number": 2,
                            "tokens": [
                                {
                                    "text": "Revenue",
                                    "bbox": [24, 140, 90, 160],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                                {
                                    "text": "100",
                                    "bbox": [240, 140, 280, 160],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                                {
                                    "text": "90",
                                    "bbox": [320, 140, 350, 160],
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                },
                            ],
                            "lines": [
                                {
                                    "text": "Revenue 100 90",
                                    "bbox": [24, 140, 350, 160],
                                    "confidence": 0.84,
                                    "source_engine": "ocr_text_engine",
                                    "row_index": 0,
                                }
                            ],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="ocr_text_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=35,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            )
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    row_block = updated["normalized_statement_tables"][0]["row_blocks"][0]
    assert row_block["label_text"] == "Revenue"
    assert row_block["diagnostics"]["label_recovered_from_layout_tokens"] is True
    assert "layout_token_label_alignment" in row_block["diagnostics"]["anchor_hints"]


def test_engine_conflict_is_retained_as_evidence_not_fact(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 1,
            "table_title": "income_statement",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Revenue"}],
            "row_blocks": [
                {
                    "label_text": "Revenue",
                    "label_tokens": ["Revenue"],
                    "value_cells": {},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.85,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Revenue"},
                }
            ],
            "dataframe_json": {"orientation": "records", "data": [{"line": "Revenue"}]},
            "source_traceability": {
                "source_engine": "native_pdf_table_engine",
                "source_page": 1,
                "source_table_id": "1:1:0",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["native_pdf_table_engine"],
            },
            "source_location": {"page": 1, "table_index": 0},
            "confidence_score": 0.85,
            "extraction_method": "pdf_table",
            "quality_flag": "degraded_pdf_table",
            "warnings": [],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    base_report["statement_tables"] = []
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "ocr_conflict_candidates.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 1,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2020Q4",
                        "comparative_period": "2019Q4",
                        "period_source": "table_headers",
                        "period_confidence": 0.95,
                        "row_blocks": [
                            {
                                "label_text": "Revenue",
                                "label_tokens": ["Revenue"],
                                "value_cells": {"2020": "100", "2019": "90"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.8,
                                "diagnostics": {"source_line": "Revenue | 100 | 90"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="native_pdf_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=90,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(),
            ),
            EngineExtractionResult(
                engine_name="ocr_table_structure_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=40,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    assert updated["engine_fusion_diagnostics"][0]["fusion_status"] == "conflict_retained_as_evidence"
    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    assert not any(fact.metric_code == "revenue" for fact in result.facts)
    assert result.unmapped_numeric_evidence
    assert result.unmapped_numeric_evidence[0]["fusion_status"] == "conflict_retained_as_evidence"
    assert "period_conflict" in result.unmapped_numeric_evidence[0]["reasons"]


def test_table_level_period_conflict_retains_candidate_rows_as_evidence(db_session, tmp_path, monkeypatch):
    company = db_session.scalar(select(Company).where(Company.ticker == "LKOH"))
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="manual_upload",
        source_url="https://example.com/report.pdf",
        storage_path=str(tmp_path / "report.pdf"),
        file_name="report.pdf",
        status="validated",
    )
    Path(doc.storage_path).write_bytes(b"%PDF-1.4 fake")
    db_session.add(doc)
    db_session.flush()

    artifact = statement_tables_path(doc)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    base_report = _base_report(doc, artifact)
    base_report["normalized_statement_tables"] = [
        {
            "document_id": doc.id,
            "company_ticker": "LKOH",
            "period": "2021Q4",
            "effective_period": "2021Q4",
            "comparative_period": "2020Q4",
            "period_source": "table_headers",
            "period_confidence": 0.95,
            "reporting_standard": "IFRS",
            "statement_type": "income_statement",
            "statement_family": "income_statement",
            "period_type": "annual",
            "table_index": 0,
            "page_number": 1,
            "table_title": "income_statement",
            "unit": "million",
            "currency": "RUB",
            "unit_multiplier": 1000000,
            "columns": ["line", "2021", "2020"],
            "header_columns": ["line", "2021", "2020"],
            "rows": [{"line": "Revenue", "2021": "100", "2020": "90"}],
            "row_blocks": [
                {
                    "label_text": "Revenue",
                    "label_tokens": ["Revenue"],
                    "value_cells": {"2021": "100", "2020": "90"},
                    "row_kind": "statement_line_item",
                    "row_confidence": 0.85,
                    "source_engine": "native_pdf_table_engine",
                    "source_page": 1,
                    "source_table_id": "1:1:0",
                    "source_bbox": None,
                    "fusion_status": "single_engine",
                    "source_engines_involved": ["native_pdf_table_engine"],
                    "source_traceability": {
                        "source_engine": "native_pdf_table_engine",
                        "source_page": 1,
                        "source_table_id": "1:1:0",
                        "source_bbox": None,
                        "fusion_status": "single_engine",
                        "source_engines_involved": ["native_pdf_table_engine"],
                    },
                    "diagnostics": {"source_line": "Revenue | 100 | 90"},
                }
            ],
            "dataframe_json": {"orientation": "records", "data": [{"line": "Revenue", "2021": "100", "2020": "90"}]},
            "source_traceability": {
                "source_engine": "native_pdf_table_engine",
                "source_page": 1,
                "source_table_id": "1:1:0",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": ["native_pdf_table_engine"],
            },
            "source_location": {"page": 1, "table_index": 0},
            "confidence_score": 0.85,
            "extraction_method": "pdf_table",
            "quality_flag": "degraded_pdf_table",
            "warnings": [],
            "table_role": "primary_statement_degraded_but_usable",
            "diagnostics": {},
        }
    ]
    base_report["statement_tables"] = []
    artifact.write_text(json.dumps(base_report), encoding="utf-8")

    candidate_path = tmp_path / "ocr_table_period_conflict.json"
    candidate_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "page_number": 1,
                        "statement_family": "income_statement",
                        "statement_type": "income_statement",
                        "table_title": "Statement of Profit or Loss",
                        "nearby_text": "Amounts in millions of RUB",
                        "effective_period": "2020Q4",
                        "comparative_period": "2019Q4",
                        "period_source": "ocr_table_headers",
                        "period_confidence": 0.95,
                        "row_blocks": [
                            {
                                "label_text": "Operating profit",
                                "label_tokens": ["Operating", "profit"],
                                "value_cells": {"2020": "20", "2019": "10"},
                                "row_kind": "statement_line_item",
                                "row_confidence": 0.8,
                                "diagnostics": {"source_line": "Operating profit | 20 | 10"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_engine_cascade(_context):
        return [
            EngineExtractionResult(
                engine_name="native_pdf_table_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=90,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=0,
                artifact_paths=EngineArtifactPaths(),
            ),
            EngineExtractionResult(
                engine_name="ocr_table_structure_engine",
                engine_status=ENGINE_STATUS_SUCCESS,
                engine_priority=40,
                pages_attempted=1,
                table_candidates=1,
                text_blocks=1,
                artifact_paths=EngineArtifactPaths(
                    normalized_candidate_path=str(candidate_path),
                    debug_output_path=str(candidate_path),
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.services.parsing.pdf_auto_parse_orchestrator.run_engine_cascade",
        fake_run_engine_cascade,
    )

    updated = augment_statement_table_report_with_engine_candidates(
        root=tmp_path,
        document=doc,
        statement_table_report=base_report,
    )

    assert updated["engine_fusion_diagnostics"][0]["conflict_scope"] == "table_context"
    assert updated["engine_fusion_diagnostics"][0]["conflict_reason"] == "period_conflict"

    result = DataFrameStatementParser(db_session).parse("LKOH", "2021Q1", "2021Q4", document_ids=[doc.id])
    metric_codes = {fact.metric_code for fact in result.facts}
    assert "revenue" in metric_codes
    assert "operating_profit" not in metric_codes
    assert any(item["fusion_status"] == "conflict_retained_as_evidence" for item in result.unmapped_numeric_evidence)
    assert any("period_conflict" in item["reasons"] for item in result.unmapped_numeric_evidence)
