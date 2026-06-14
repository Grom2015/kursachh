import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pandas as pd

from app.core.config import get_settings
from app.db.models import ReportDocument
from app.services.parsing.ifrs.period_detector import resolve_report_period
from app.services.parsing.text_normalization import (
    contains_normalized_marker,
    normalize_financial_text,
    normalize_matching_text,
)

PRIMARY_STATEMENT_FAMILIES = {"balance_sheet", "income_statement", "cash_flow", "changes_in_equity"}
REQUIRED_PRIMARY_STATEMENT_FAMILIES = {"balance_sheet", "income_statement"}


@dataclass
class ExtractedStatementTable:
    document_id: int
    company_ticker: str
    period: str
    reporting_standard: str
    statement_type: str
    period_type: str
    table_index: int
    page_number: int | None
    table_title: str | None
    unit: str | None
    currency: str | None
    unit_multiplier: int | None
    columns: list[str]
    rows: list[dict[str, Any]]
    dataframe_json: dict[str, Any]
    source_location: dict[str, Any]
    confidence_score: float
    extraction_method: str = "unknown"
    source_engine: str | None = None
    source_table_id: str | None = None
    quality_flag: str = "raw_table"
    eligible_for_fact_normalization: bool = False
    warnings: list[str] = field(default_factory=list)
    header_row_count: int = 1
    normalized_columns: list[str] = field(default_factory=list)
    repeated_column_names_detected: bool = False
    label_column_present: bool = False
    label_column_reconstructed: bool = False
    table_structure_quality: str = "standard"
    effective_period: str | None = None
    comparative_period: str | None = None
    period_source: str | None = None
    period_confidence: float | None = None
    period_resolution_source_detail: str | None = None
    period_conflict_sources: list[str] = field(default_factory=list)
    period_warnings: list[str] = field(default_factory=list)
    statement_family: str = "unknown"
    table_role: str = "narrative"
    header_columns: list[str] = field(default_factory=list)
    row_blocks: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    source_traceability: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StatementTableExtractor:
    def __init__(self, root: Path | None = None):
        self.root = root or get_settings().root_dir
        self.warnings: list[str] = []
        self.extraction_coverage: dict[str, int] = {}

    def extract(self, document: ReportDocument) -> dict[str, Any]:
        self.warnings = []
        self.extraction_coverage = {
            "pages_total": 0,
            "pages_with_text_layer": 0,
            "pages_with_table_candidates": 0,
            "pages_processed_by_native_extractor": 0,
            "pages_requiring_ocr": 0,
            "pages_ocr_skipped": 0,
            "pages_with_no_usable_extraction": 0,
        }
        if document.status not in {"downloaded", "parsed", "validated", "evidence_only"}:
            return self._report(document, [], [f"Document status is not cached/validated for extraction: {document.status}"])
        path = self._storage_path(document)
        if not path or not path.exists():
            return self._report(document, [], ["Cached document file not found."])
        suffix = path.suffix.casefold()
        tables: list[ExtractedStatementTable]
        if suffix == ".pdf":
            tables = self._extract_pdf(document, path)
        elif suffix in {".xlsx", ".xls"}:
            tables = self._extract_xlsx(document, path)
        elif suffix in {".html", ".htm"}:
            tables = self._extract_html(document, path)
        elif suffix == ".zip":
            tables = self._extract_zip(document, path)
        else:
            tables = []
            self.warnings.append(f"Unsupported file type for statement table extraction: {suffix}")
        report = self._report(document, tables, self.warnings)
        self.save_artifact(document, report)
        return report

    def save_artifact(self, document: ReportDocument, report: dict[str, Any]) -> Path:
        path = statement_tables_path(document)
        parsed_root = (self.root / get_settings().report_parsed_dir).resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(parsed_root):
            raise ValueError("Statement tables path escapes parsed directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _extract_pdf(self, document: ReportDocument, path: Path) -> list[ExtractedStatementTable]:
        try:
            import pdfplumber  # type: ignore
        except ImportError:
            self.warnings.append("pdfplumber is not installed; PDF statement tables not extracted")
            return []
        tables = []
        try:
            with pdfplumber.open(path) as pdf:
                expected_primary_pages: dict[int, str] = {}
                self.extraction_coverage["pages_total"] = len(pdf.pages)
                for page_index, page in enumerate(pdf.pages, start=1):
                    page_text = page.extract_text() or ""
                    recovery_text = page_text_with_word_layout(page_text, page)
                    self.extraction_coverage["pages_processed_by_native_extractor"] += 1
                    if page_text.strip():
                        self.extraction_coverage["pages_with_text_layer"] += 1
                    raw_tables = page.extract_tables() or []
                    if raw_tables:
                        self.extraction_coverage["pages_with_table_candidates"] += 1
                    expected_primary_pages.update(extract_expected_primary_pages_from_toc(page_text, document.reporting_standard))
                    tables_before_page = len(tables)
                    for raw_table in raw_tables:
                        table = self._table_from_rows(
                            document,
                            raw_table,
                            len(tables),
                            page_index,
                            page_text,
                            page=page,
                            extraction_method="pdf_table",
                        )
                        tables.append(table)
                    page_statement_tables = [table.to_dict() for table in tables if table.page_number == page_index]
                    if should_add_primary_statement_text_recovery(
                        recovery_text,
                        page_statement_tables,
                        document.reporting_standard,
                    ):
                        fallback = self._text_fallback_table(document, page_text, len(tables), page_index, page=page)
                        if fallback and not page_has_text_fallback_for_statement(
                            page_statement_tables,
                            fallback.statement_type,
                        ):
                            tables.append(fallback)
                    if not raw_tables and page_index in expected_primary_pages and not page_has_extractable_content(page):
                            self.warnings.append(
                                "primary_statement_page_image_only_or_no_extractable_text:"
                                f"page={page_index}:statement_type={expected_primary_pages[page_index]}"
                            )
                            self.extraction_coverage["pages_requiring_ocr"] += 1
                    if len(tables) == tables_before_page:
                        self.extraction_coverage["pages_with_no_usable_extraction"] += 1
        except Exception as exc:
            self.warnings.append(f"PDF table extraction failed for document {document.id}: {exc}")
        return tables

    def _extract_xlsx(self, document: ReportDocument, path: Path) -> list[ExtractedStatementTable]:
        tables = []
        try:
            sheets = pd.read_excel(path, sheet_name=None, header=None)
            for sheet_name, df in sheets.items():
                rows = df.fillna("").astype(str).values.tolist()
                table = self._table_from_rows(
                    document,
                    rows,
                    len(tables),
                    None,
                    str(sheet_name),
                    table_title=str(sheet_name),
                    extraction_method="xlsx_table",
                )
                tables.append(table)
        except Exception as exc:
            self.warnings.append(f"XLSX table extraction failed for document {document.id}: {exc}")
        return tables

    def _extract_html(self, document: ReportDocument, path: Path) -> list[ExtractedStatementTable]:
        tables = []
        html = path.read_text(encoding="utf-8", errors="ignore")
        try:
            dfs = pd.read_html(StringIO(html))
            for df in dfs:
                rows = [list(df.columns.astype(str))] + df.fillna("").astype(str).values.tolist()
                table = self._table_from_rows(document, rows, len(tables), None, "html", extraction_method="html_table")
                tables.append(table)
        except Exception as exc:
            parsed_tables = parse_html_tables(html)
            if parsed_tables:
                for raw_rows in parsed_tables:
                    table = self._table_from_rows(document, raw_rows, len(tables), None, "html", extraction_method="html_table")
                    tables.append(table)
            else:
                self.warnings.append(f"HTML table extraction failed for document {document.id}: {exc}")
        return tables

    def _extract_zip(self, document: ReportDocument, path: Path) -> list[ExtractedStatementTable]:
        tables = []
        try:
            tmp_parent = self.root / get_settings().report_parsed_dir / "_tmp"
            tmp_parent.mkdir(parents=True, exist_ok=True)
            tmp_root = tmp_parent / f"zip_{document.id}_{uuid.uuid4().hex}"
            tmp_root.mkdir(parents=True, exist_ok=True)
            with ZipFile(path) as archive:
                for member in archive.infolist():
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        self.warnings.append(f"Skipped unsafe zip member: {member.filename}")
                        continue
                    if member_path.suffix.casefold() not in {".pdf", ".xlsx", ".xls", ".html", ".htm"}:
                        continue
                    target = (tmp_root / member_path.name).resolve()
                    if not target.is_relative_to(tmp_root.resolve()):
                        self.warnings.append(f"Skipped unsafe zip member: {member.filename}")
                        continue
                    target.write_bytes(archive.read(member))
                    proxy = self._proxy_document(document, str(target))
                    suffix = target.suffix.casefold()
                    if suffix == ".pdf":
                        tables.extend(self._extract_pdf(proxy, target))
                    elif suffix in {".xlsx", ".xls"}:
                        tables.extend(self._extract_xlsx(proxy, target))
                    elif suffix in {".html", ".htm"}:
                        tables.extend(self._extract_html(proxy, target))
        except Exception as exc:
            self.warnings.append(f"ZIP statement table extraction failed for document {document.id}: {exc}")
        return tables

    def _table_from_rows(
        self,
        document: ReportDocument,
        raw_rows: list[list[Any]],
        index: int,
        page_number: int | None,
        nearby_text: str,
        page: Any | None = None,
        table_title: str | None = None,
        extraction_method: str = "unknown",
    ) -> ExtractedStatementTable:
        clean_rows = [[_clean_cell(cell) for cell in row] for row in raw_rows if any(_clean_cell(cell) for cell in row)]
        if extraction_method == "pdf_table":
            columns, records, diagnostics = normalize_pdf_table_rows(clean_rows, nearby_text)
            active_columns = columns
        else:
            columns = clean_rows[0] if clean_rows else []
            data_rows = clean_rows[1:] if len(clean_rows) > 1 else []
            active_columns, records, diagnostics = normalize_generic_table_rows(columns, data_rows)
        text = " ".join(
            [normalize_financial_text(" ".join(columns))]
            + [" ".join(str(value or "") for value in row.values()) for row in records[:5]]
            + [nearby_text, table_title or ""]
        )
        statement_family = classify_statement_family(text, document.reporting_standard)
        statement_type = statement_family
        if extraction_method == "pdf_table":
            records, diagnostics = recover_numeric_fragment_statement_rows(
                records,
                active_columns,
                page=page,
                statement_family=statement_family,
                diagnostics=diagnostics,
            )
        period_resolution = resolve_report_period(
            title_text="\n".join(filter(None, [nearby_text, table_title or ""])),
            headers=columns,
            report_period=document.report_period,
            filename_hint=document.file_name,
        )
        effective_period = period_resolution.effective_report_period or document.report_period
        row_blocks, row_diagnostics = build_normalized_row_blocks(
            records=records,
            columns=active_columns,
            statement_family=statement_family,
            source_engine=extraction_method,
            source_page=page_number,
            source_table_id=f"{document.id}:{page_number or 'na'}:{index}",
        )
        table_role = classify_table_role(statement_family, diagnostics["table_structure_quality"], extraction_method)
        combined_diagnostics = {
            **diagnostics,
            **row_diagnostics,
            "statement_family": statement_family,
            "table_role": table_role,
            "header_collapsed": diagnostics["header_row_count"] > 1,
            "duplicate_date_headers": diagnostics["repeated_column_names_detected"],
            "label_column_missing": not diagnostics["label_column_present"],
            "period_header_ambiguous": (
                period_resolution.period_source in {"upload_default", "filename"}
                and statement_family in PRIMARY_STATEMENT_FAMILIES
            ),
        }
        return ExtractedStatementTable(
            document_id=document.id,
            company_ticker=document.company.ticker if document.company else "",
            period=effective_period,
            reporting_standard=document.reporting_standard,
            statement_type=statement_type,
            period_type=period_type_for_statement(statement_type, effective_period or document.report_period),
            table_index=index,
            page_number=page_number,
            table_title=table_title or guess_title(nearby_text),
            unit=detect_unit(text),
            currency=detect_currency(text),
            unit_multiplier=detect_unit_multiplier(text),
            columns=columns,
            rows=records,
            dataframe_json={"orientation": "records", "data": records},
            source_location={
                "page": page_number,
                "table_index": index,
                "source_engine": extraction_method,
                "source_table_id": f"{document.id}:{page_number or 'na'}:{index}",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": [extraction_method],
            },
            source_engine=extraction_method,
            source_table_id=f"{document.id}:{page_number or 'na'}:{index}",
            confidence_score=0.85 if statement_type != "unknown" else 0.3,
            extraction_method=extraction_method,
            quality_flag="degraded_pdf_table"
            if extraction_method == "pdf_table" and diagnostics["table_structure_quality"] != "standard"
            else "raw_table",
            warnings=[] if statement_type != "unknown" else ["Table could not be classified as a primary statement."],
            header_row_count=diagnostics["header_row_count"],
            normalized_columns=diagnostics["normalized_columns"],
            repeated_column_names_detected=diagnostics["repeated_column_names_detected"],
            label_column_present=diagnostics["label_column_present"],
            label_column_reconstructed=diagnostics["label_column_reconstructed"],
            table_structure_quality=diagnostics["table_structure_quality"],
            effective_period=period_resolution.effective_report_period,
            comparative_period=period_resolution.comparative_period,
            period_source=period_resolution.period_source,
            period_confidence=round(period_resolution.period_confidence, 4),
            period_resolution_source_detail=period_resolution.period_resolution_source_detail,
            period_conflict_sources=list(period_resolution.period_conflict_sources),
            period_warnings=list(period_resolution.period_warnings),
            statement_family=statement_family,
            table_role=table_role,
            header_columns=active_columns,
            row_blocks=row_blocks,
            diagnostics=combined_diagnostics,
            source_traceability={
                "source_engine": extraction_method,
                "source_page": page_number,
                "source_table_id": f"{document.id}:{page_number or 'na'}:{index}",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": [extraction_method],
            },
        )

    def _text_fallback_table(
        self,
        document: ReportDocument,
        page_text: str,
        index: int,
        page_number: int,
        page: Any | None = None,
    ) -> ExtractedStatementTable | None:
        recovery_text = page_text_with_word_layout(page_text, page)
        rows, recovery_source = best_text_recovery_rows(page_text, page)
        if len(rows) < 2:
            return None
        statement_family = resolve_text_fallback_statement_family(
            reporting_standard=document.reporting_standard,
            recovery_text=recovery_text,
            page_text=page_text,
            rows=rows,
        )
        statement_type = statement_family
        if statement_type == "unknown":
            return None
        title = guess_title(recovery_text)
        period_resolution = resolve_report_period(
            title_text="\n".join(filter(None, [recovery_text, title or ""])),
            headers=[],
            report_period=document.report_period,
            filename_hint=document.file_name,
        )
        effective_period = period_resolution.effective_report_period or document.report_period
        if recovery_source == "extract_words":
            columns, records = build_word_layout_fallback_records(
                page,
                current_period=period_resolution.effective_report_period,
                comparative_period=period_resolution.comparative_period,
            )
            if not records:
                columns, records = build_text_fallback_records(
                    rows,
                    current_period=period_resolution.effective_report_period,
                    comparative_period=period_resolution.comparative_period,
                )
        else:
            columns, records = build_text_fallback_records(
                rows,
                current_period=period_resolution.effective_report_period,
                comparative_period=period_resolution.comparative_period,
            )
        row_blocks, row_diagnostics = build_normalized_row_blocks(
            records=records,
            columns=columns,
            statement_family=statement_family,
            source_engine="text_table_fallback_words" if recovery_source == "extract_words" else "text_table_fallback",
            source_page=page_number,
            source_table_id=f"{document.id}:{page_number}:{index}",
        )
        table_role = classify_table_role(statement_family, "degraded", "text_table_fallback")
        warnings = ["text_table_fallback_used"]
        if recovery_source == "extract_words":
            warnings.append("word_layout_recovery_used")
        return ExtractedStatementTable(
            document_id=document.id,
            company_ticker=document.company.ticker if document.company else "",
            period=effective_period,
            reporting_standard=document.reporting_standard,
            statement_type=statement_type,
            period_type=period_type_for_statement(statement_type, effective_period or document.report_period),
            table_index=index,
            page_number=page_number,
            table_title=title,
            unit=detect_unit(recovery_text),
            currency=detect_currency(recovery_text),
            unit_multiplier=detect_unit_multiplier(recovery_text),
            columns=columns,
            rows=records,
            dataframe_json={"orientation": "records", "data": records},
            source_location={
                "page": page_number,
                "table_index": index,
                "extraction_method": "text_table_fallback",
                "source_engine": "text_table_fallback_words" if recovery_source == "extract_words" else "text_table_fallback",
                "source_table_id": f"{document.id}:{page_number}:{index}",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": [
                    "text_table_fallback_words" if recovery_source == "extract_words" else "text_table_fallback"
                ],
            },
            source_engine="text_table_fallback_words" if recovery_source == "extract_words" else "text_table_fallback",
            source_table_id=f"{document.id}:{page_number}:{index}",
            confidence_score=0.65,
            extraction_method="text_table_fallback",
            quality_flag="raw_text_table",
            eligible_for_fact_normalization=False,
            warnings=warnings,
            effective_period=period_resolution.effective_report_period,
            comparative_period=period_resolution.comparative_period,
            period_source=period_resolution.period_source,
            period_confidence=round(period_resolution.period_confidence, 4),
            period_resolution_source_detail=period_resolution.period_resolution_source_detail,
            period_conflict_sources=list(period_resolution.period_conflict_sources),
            period_warnings=list(period_resolution.period_warnings),
            statement_family=statement_family,
            table_role=table_role,
            header_columns=columns,
            row_blocks=row_blocks,
            diagnostics={
                "header_collapsed": False,
                "duplicate_date_headers": False,
                "label_column_missing": False,
                "label_column_reconstructed": False,
                "stitched_rows_count": 0,
                "numeric_only_rows_count": row_diagnostics["numeric_only_rows_count"],
                "subtotal_rows_count": row_diagnostics["subtotal_rows_count"],
                "period_header_ambiguous": period_resolution.period_source in {"upload_default", "filename"},
                "text_recovery_source": recovery_source,
                **row_diagnostics,
            },
            source_traceability={
                "source_engine": "text_table_fallback_words" if recovery_source == "extract_words" else "text_table_fallback",
                "source_page": page_number,
                "source_table_id": f"{document.id}:{page_number}:{index}",
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": [
                    "text_table_fallback_words" if recovery_source == "extract_words" else "text_table_fallback"
                ],
            },
        )

    def _report(self, document: ReportDocument, tables: list[ExtractedStatementTable], warnings: list[str]) -> dict[str, Any]:
        statement_tables = [table.to_dict() for table in tables]
        coverage = statement_coverage(statement_tables)
        missing = required_statement_tables_missing(coverage)
        return {
            "document_id": document.id,
            "source_url": document.source_url,
            "reporting_standard": document.reporting_standard,
            "period": document.report_period,
            "default_extraction_surface": "normalized_statement_tables",
            "legacy_statement_tables_compatible": True,
            "period_resolution": summarize_table_period_resolution(statement_tables, document.report_period),
            "tables_found": len(tables),
            "tables_extracted": len(tables),
            "statement_tables_count": sum(1 for table in tables if table.statement_type != "unknown"),
            "statement_coverage": coverage,
            "normalized_statement_tables": statement_tables,
            "table_structure_diagnostics": summarize_table_structure_diagnostics(statement_tables),
            "degraded_primary_statement_count": sum(
                1
                for table in statement_tables
                if table.get("table_role") in {"primary_statement_degraded_but_usable", "primary_statement_degraded_unusable"}
            ),
            "extraction_coverage": self.extraction_coverage,
            "required_statement_tables_found": not missing,
            "required_statement_tables_missing": missing,
            "facts_extracted": 0,
            "fact_parser_status": "not_invoked",
            "statement_tables": statement_tables,
            "warnings": warnings,
            "artifact_path": str(statement_tables_path(document)),
        }

    def _storage_path(self, document: ReportDocument) -> Path | None:
        if not document.storage_path:
            return None
        path = Path(document.storage_path)
        return path if path.is_absolute() else self.root / path

    def _proxy_document(self, document: ReportDocument, storage_path: str) -> ReportDocument:
        proxy = ReportDocument(
            id=document.id,
            company=document.company,
            company_id=document.company_id,
            report_period=document.report_period,
            reporting_standard=document.reporting_standard,
            document_type=document.document_type,
            source_role=document.source_role,
            source_type=document.source_type,
            source_url=document.source_url,
            storage_path=storage_path,
            file_name=Path(storage_path).name,
            status=document.status,
        )
        return proxy


def statement_tables_path(document: ReportDocument) -> Path:
    settings = get_settings()
    ticker = document.company.ticker.upper() if document.company else str(document.company_id)
    return (
        settings.root_dir
        / settings.report_parsed_dir
        / ticker
        / document.report_period
        / f"{document.id}_statement_tables.json"
    )


def classify_statement_family(text: str, reporting_standard: str) -> str:
    normalized = normalize_matching_text(text)
    if is_toc_page(normalized) or is_auditor_page(normalized):
        return "unknown"
    if is_notes_page(normalized):
        return "notes"
    if is_cash_flow_text(normalized, reporting_standard):
        return "cash_flow"
    if reporting_standard.upper() == "RAS":
        if contains_normalized_marker(normalized, "бухгалтерский баланс"):
            return "balance_sheet"
        if contains_normalized_marker(normalized, "отчет о финансовых результатах"):
            return "income_statement"
        if contains_normalized_marker(normalized, "отчет о движении денежных средств"):
            return "cash_flow"
    if reporting_standard.upper() == "RAS":
        if contains_normalized_marker(normalized, "бухгалтерский баланс"):
            return "balance_sheet"
        if contains_normalized_marker(normalized, "отчет о финансовых результатах"):
            return "income_statement"
        if contains_normalized_marker(normalized, "отчет о движении денежных средств"):
            return "cash_flow"
    if has_balance_sheet_title(normalized):
        return "balance_sheet"
    if has_income_statement_title(normalized):
        return "income_statement"
    if has_balance_sheet_rows(normalized) and contains_explicit_year_or_date(normalized):
        return "balance_sheet"
    if has_income_statement_rows(normalized) and contains_explicit_year_or_date(normalized):
        return "income_statement"
    if "statement of cash flows" in normalized:
        return "cash_flow"
    if "statement of changes in equity" in normalized:
        return "changes_in_equity"
    if "notes" in normalized or "note " in normalized:
        return "notes"
    return "unknown"


def classify_statement_table(text: str, reporting_standard: str) -> str:
    return classify_statement_family(text, reporting_standard)


def classify_table_role(statement_family: str, structure_quality: str, extraction_method: str) -> str:
    if statement_family == "notes":
        return "review_only_note"
    if statement_family == "unknown":
        return "narrative"
    if extraction_method == "text_table_fallback":
        return "primary_statement_degraded_but_usable"
    if structure_quality == "standard":
        return "primary_statement"
    if structure_quality == "degraded":
        return "primary_statement_degraded_but_usable"
    return "primary_statement_degraded_unusable"


def classify_primary_statement_page(text: str, reporting_standard: str) -> str:
    normalized = normalize_matching_text(text)
    if not normalized or is_toc_page(normalized) or is_notes_page(normalized) or is_auditor_page(normalized):
        return "unknown"
    if reporting_standard.upper() == "RAS":
        if contains_normalized_marker(normalized, "бухгалтерский баланс"):
            return "balance_sheet"
        if contains_normalized_marker(normalized, "отчет о финансовых результатах"):
            return "income_statement"
    if has_balance_sheet_title(normalized) and has_balance_sheet_rows(normalized):
        return "balance_sheet"
    if has_income_statement_title(normalized) and has_income_statement_rows(normalized):
        return "income_statement"
    if is_cash_flow_text(normalized, reporting_standard):
        return "cash_flow"
    if has_changes_in_equity_title(normalized) and has_changes_in_equity_rows(normalized):
        return "changes_in_equity"
    return "unknown"


def classify_primary_statement_title_only(text: str, reporting_standard: str) -> str:
    normalized = normalize_matching_text(text)
    if not normalized or is_toc_page(normalized) or is_notes_page(normalized) or is_auditor_page(normalized):
        return "unknown"
    if reporting_standard.upper() == "RAS":
        if contains_normalized_marker(normalized, "бухгалтерский баланс"):
            return "balance_sheet"
        if contains_normalized_marker(normalized, "отчет о финансовых результатах"):
            return "income_statement"
    if has_balance_sheet_title(normalized):
        return "balance_sheet"
    if has_income_statement_title(normalized):
        return "income_statement"
    if contains_normalized_marker(normalized, "statement of cash flows", "отчет о движении денежных средств"):
        return "cash_flow"
    if has_changes_in_equity_title(normalized):
        return "changes_in_equity"
    return "unknown"


def resolve_text_fallback_statement_family(
    *,
    reporting_standard: str,
    recovery_text: str,
    page_text: str,
    rows: list[list[str]],
) -> str:
    row_text = "\n".join(" ".join(str(cell or "").strip() for cell in row if str(cell or "").strip()) for row in rows)
    candidates = [
        recovery_text,
        page_text,
        row_text,
        "\n".join(filter(None, [page_text, row_text])),
    ]
    for candidate in candidates:
        if not candidate.strip():
            continue
        for classifier in (
            classify_primary_statement_page,
            classify_primary_statement_title_only,
            classify_statement_table,
        ):
            statement_family = classifier(candidate, reporting_standard)
            if statement_family != "unknown":
                return statement_family
    return "unknown"


def should_add_primary_income_text_fallback(
    page_text: str,
    page_tables: list[dict[str, Any]],
    reporting_standard: str,
) -> bool:
    return should_add_primary_statement_text_recovery(page_text, page_tables, reporting_standard)


def should_add_primary_statement_text_recovery(
    page_text: str,
    page_tables: list[dict[str, Any]],
    reporting_standard: str,
) -> bool:
    if reporting_standard.upper() != "IFRS":
        return False
    normalized = normalize_matching_text(page_text)
    if is_toc_page(normalized) or is_notes_page(normalized) or is_auditor_page(normalized):
        return False
    statement_family = classify_primary_statement_page(page_text, reporting_standard)
    if statement_family == "unknown" and page_tables and has_statement_like_numeric_line(page_text):
        return any(not _table_has_strong_primary_rows(table) for table in page_tables)
    if statement_family == "unknown":
        return False
    family_tables = [table for table in page_tables if table.get("statement_type") == statement_family]
    if not family_tables:
        return True
    if any(_table_has_strong_primary_rows(table) for table in family_tables):
        return False
    if has_statement_like_numeric_line(page_text):
        return True
    return any(
        marker in normalized
        for marker in PRIMARY_INCOME_STATEMENT_MARKERS
        + PRIMARY_BALANCE_SHEET_MARKERS
        + PRIMARY_CASH_FLOW_MARKERS
        + PRIMARY_CHANGES_IN_EQUITY_MARKERS
    )


PRIMARY_INCOME_STATEMENT_MARKERS = [
    "consolidated statement of profit or loss",
    "consolidated statement of profit or loss and other comprehensive income",
    "interim condensed consolidated statement of profit or loss",
    "statement of comprehensive income",
    "обобщенный консолидированный отчет о прибылях и убытках",
    "консолидированный отчет о прибылях и убытках",
    "отчет о прибылях и убытках",
]


PRIMARY_BALANCE_SHEET_MARKERS = [
    "statement of financial position",
    "balance sheet",
    "consolidated balance sheet",
    "consolidated statement of financial position",
]

PRIMARY_CASH_FLOW_MARKERS = [
    "statement of cash flows",
    "consolidated statement of cash flows",
    "cash flows from operating activities",
]

PRIMARY_CHANGES_IN_EQUITY_MARKERS = [
    "statement of changes in equity",
    "consolidated statement of changes in equity",
]


def page_has_text_fallback_for_statement(page_tables: list[dict[str, Any]], statement_type: str) -> bool:
    return any(
        table.get("statement_type") == statement_type and table.get("extraction_method") == "text_table_fallback"
        for table in page_tables
    )


def _table_has_income_fact_rows(table: dict[str, Any]) -> bool:
    if table.get("statement_type") != "income_statement":
        return False
    rows = table.get("rows") or []
    text = " ".join(
        " ".join(str(value or "") for value in row.values()) if isinstance(row, dict) else str(row or "")
        for row in rows
    ).casefold()
    markers = [
        "sales",
        "revenue",
        "operating profit",
        "profit from operating activities",
        "profit for the period",
        "profit for the year",
        "net income",
    ]
    return any(marker in text for marker in markers)


def _table_has_strong_primary_rows(table: dict[str, Any]) -> bool:
    row_blocks = table.get("row_blocks") or []
    for block in row_blocks:
        row_kind = str(block.get("row_kind") or "")
        if row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
            continue
        if not block.get("label_text"):
            continue
        if not (block.get("value_cells") or {}):
            continue
        return True
    return False


def has_statement_like_numeric_line(text: str) -> bool:
    for raw_line in str(text or "").splitlines():
        line = normalize_financial_text(raw_line)
        if not line or is_text_fallback_header_or_title_line(line):
            continue
        split = split_text_fallback_line_and_values(line)
        if not split:
            continue
        label, values = split
        if len(label.split()) >= 2 and len(values) == 2:
            return True
    return False


def is_toc_page(normalized: str) -> bool:
    if normalized.startswith("contents ") or normalized == "contents":
        return True
    toc_markers = [
        "report on review",
        "independent auditor",
        "consolidated balance sheet",
        "consolidated statement of comprehensive income",
        "consolidated statement of cash flows",
        "notes to the consolidated",
    ]
    return "contents" in normalized and sum(1 for marker in toc_markers if marker in normalized) >= 3


def is_notes_page(normalized: str) -> bool:
    notes_markers = [
        "notes to consolidated financial statements",
        "notes to the consolidated financial statements",
        "notes to the consolidated interim condensed financial information",
        "notes to the consolidated interim condensed financial statements",
        "notes to financial statements",
        "notes to the financial statements",
        "примечания к обобщенной консолидированной финансовой отчетности",
        "примечания к консолидированной финансовой отчетности",
        "примечания к финансовой отчетности",
    ]
    return any(contains_normalized_marker(normalized, marker) for marker in notes_markers)


def is_auditor_page(normalized: str) -> bool:
    auditor_markers = [
        "independent auditor",
        "auditor's report",
        "report on review",
        "we have audited",
        "we have reviewed",
        "auditor’s responsibilities",
    ]
    return any(contains_normalized_marker(normalized, marker) for marker in auditor_markers)


def has_balance_sheet_title(normalized: str) -> bool:
    return contains_normalized_marker(
        normalized,
        "statement of financial position",
        "balance sheet",
        "consolidated balance sheet",
        "consolidated interim condensed balance sheet",
        "отчет о финансовом положении",
        "консолидированный отчет о финансовом положении",
        "обобщенный консолидированный отчет о финансовом положении",
    )


def has_balance_sheet_rows(normalized: str) -> bool:
    markers = [
        "total assets",
        "current assets",
        "non-current assets",
        "total equity",
        "total equity and liabilities",
        "current liabilities",
        "non-current liabilities",
        "итого активов",
        "обязательства",
        "собственных средств",
    ]
    return sum(1 for marker in markers if contains_normalized_marker(normalized, marker)) >= 2


def has_income_statement_title(normalized: str) -> bool:
    return contains_normalized_marker(
        normalized,
        "statement of profit or loss",
        "statement of comprehensive income",
        "statement of income",
        "отчет о прибылях и убытках",
        "консолидированный отчет о прибылях и убытках",
        "обобщенный консолидированный отчет о прибылях и убытках",
    )


def has_income_statement_rows(normalized: str) -> bool:
    markers = [
        "sales",
        "revenue",
        "operating profit",
        "profit for the period",
        "profit for the year",
        "profit before profit tax",
        "total comprehensive income",
        "прибыль за год",
        "процентные доходы",
        "чистые процентные доходы",
        "комиссионные доходы",
    ]
    return sum(1 for marker in markers if contains_normalized_marker(normalized, marker)) >= 1


def has_changes_in_equity_title(normalized: str) -> bool:
    return contains_normalized_marker(
        normalized,
        "statement of changes in equity",
        "consolidated statement of changes in equity",
        "interim condensed consolidated statement of changes in equity",
    )


def has_changes_in_equity_rows(normalized: str) -> bool:
    markers = [
        "share capital",
        "issued capital",
        "share premium",
        "retained earnings",
        "other reserves",
        "non-controlling interests",
        "non controlling interests",
        "total equity",
        "balance at 31 december",
    ]
    return sum(1 for marker in markers if contains_normalized_marker(normalized, marker)) >= 2


def is_cash_flow_text(normalized: str, reporting_standard: str) -> bool:
    if reporting_standard.upper() == "RAS":
        strong_ras_title = contains_normalized_marker(normalized, "отчет о движении денежных средств")
        ras_markers = [
            "денежные потоки от операционной деятельности",
            "денежные потоки от инвестиционной деятельности",
            "денежные потоки от финансовой деятельности",
            "чистое увеличение денежных средств",
            "чистое уменьшение денежных средств",
        ]
        return strong_ras_title or sum(1 for marker in ras_markers if contains_normalized_marker(normalized, marker)) >= 2
    strong_titles = [
        "consolidated statement of cash flows",
        "interim condensed consolidated statement of cash flows",
        "statement of cash flows",
        "отчет о движении денежных средств",
        "отчёт о движении денежных средств",
        "консолидированный отчет о движении денежных средств",
        "консолидированный отчёт о движении денежных средств",
        "обобщенный консолидированный отчет о движении денежных средств",
        "обобщённый консолидированный отчёт о движении денежных средств",
    ]
    if any(contains_normalized_marker(normalized, title) for title in strong_titles):
        return True
    row_markers = [
        "operating activities",
        "investing activities",
        "financing activities",
        "net increase in cash",
        "net decrease in cash",
        "cash and cash equivalents at end of period",
        "cash and cash equivalents at the end of the period",
    ]
    return sum(1 for marker in row_markers if contains_normalized_marker(normalized, marker)) >= 3


def text_lines_to_rows(text: str) -> list[list[str]]:
    return [[line.strip()] for line in (text or "").splitlines() if line.strip()]


def build_text_fallback_records(
    rows: list[list[str]],
    *,
    current_period: str | None,
    comparative_period: str | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    value_columns = fallback_value_columns(current_period, comparative_period)
    normalized_lines = [normalize_financial_text(row[0] if row else "") for row in rows]
    stitched_rows = stitch_text_fallback_rows(normalized_lines, value_columns)
    records: list[dict[str, Any]] = []
    for row in stitched_rows:
        if not row:
            continue
        if isinstance(row, dict):
            records.extend(split_structured_text_fallback_record(row, value_columns))
            continue
        record = structured_text_fallback_record(str(row), value_columns)
        records.extend(split_structured_text_fallback_record(record, value_columns))
    return ["line", *value_columns], records


def build_word_layout_fallback_records(
    page: Any | None,
    *,
    current_period: str | None,
    comparative_period: str | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    value_columns = fallback_value_columns(current_period, comparative_period)
    records = word_layout_structured_records(page, value_columns)
    stitched_records = stitch_word_layout_records(records, value_columns)
    normalized_records: list[dict[str, Any]] = []
    for record in stitched_records:
        normalized_records.extend(split_structured_text_fallback_record(record, value_columns))
    return ["line", *value_columns], normalized_records


def word_layout_line_groups(page: Any | None) -> list[list[dict[str, Any]]]:
    if page is None:
        return []
    try:
        words = page.extract_words() or []
    except Exception:
        return []
    cleaned_words = [word for word in words if str((word or {}).get("text") or "").strip()]
    if not cleaned_words:
        return []
    sorted_words = sorted(
        cleaned_words,
        key=lambda item: (
            round(float(item.get("top", item.get("doctop", 0.0))) / 3.0),
            float(item.get("x0", 0.0)),
        ),
    )
    lines: list[list[dict[str, Any]]] = []
    current_line: list[dict[str, Any]] = []
    current_top: float | None = None
    for word in sorted_words:
        top = float(word.get("top", word.get("doctop", 0.0)))
        if current_top is None or abs(top - current_top) <= 3.0:
            current_line.append(word)
            current_top = top if current_top is None else min(current_top, top)
            continue
        lines.append(current_line)
        current_line = [word]
        current_top = top
    if current_line:
        lines.append(current_line)
    return lines


def render_word_layout_line(line_words: list[dict[str, Any]]) -> str:
    ordered = sorted(line_words, key=lambda item: float(item.get("x0", 0.0)))
    return normalize_financial_text(" ".join(str(item.get("text") or "").strip() for item in ordered))


def word_layout_lines(page: Any | None) -> list[str]:
    return [line for line in (render_word_layout_line(words) for words in word_layout_line_groups(page)) if line]


def word_layout_structured_records(page: Any | None, value_columns: list[str]) -> list[dict[str, Any]]:
    line_groups = word_layout_line_groups(page)
    if not line_groups:
        return []
    numeric_clusters, note_ref_cluster = detect_word_numeric_clusters(line_groups, value_columns)
    records: list[dict[str, Any]] = []
    for line_words in line_groups:
        record = word_layout_record_from_line(line_words, numeric_clusters, value_columns, note_ref_cluster=note_ref_cluster)
        if record:
            records.append(record)
    return records


def detect_word_numeric_clusters(
    line_groups: list[list[dict[str, Any]]],
    value_columns: list[str],
) -> tuple[list[float], float | None]:
    numeric_words: list[dict[str, Any]] = []
    for line_words in line_groups:
        rendered = render_word_layout_line(line_words)
        if not rendered or is_text_fallback_header_or_title_line(rendered):
            continue
        numeric_word_count = sum(1 for word in line_words if parseable_number(word.get("text")) is not None)
        if numeric_word_count < 2:
            continue
        for word in line_words:
            if parseable_number(word.get("text")) is None:
                continue
            numeric_words.append(word)
    if not numeric_words:
        return [], None
    sorted_words = sorted(numeric_words, key=lambda item: float(item.get("x0", 0.0)))
    clusters: list[list[dict[str, Any]]] = []
    for word in sorted_words:
        position = float(word.get("x0", 0.0))
        if not clusters or abs(position - float(clusters[-1][-1].get("x0", 0.0))) > 42.0:
            clusters.append([word])
        else:
            clusters[-1].append(word)
    eligible_clusters = [cluster for cluster in clusters if len(cluster) >= 2]
    if not eligible_clusters:
        return [], None
    note_ref_cluster: float | None = None
    if len(eligible_clusters) > len(value_columns) and is_note_ref_word_cluster(eligible_clusters[0]):
        note_ref_cluster = cluster_center(eligible_clusters[0])
        eligible_clusters = eligible_clusters[1:]
    value_clusters = eligible_clusters[: len(value_columns)]
    return [cluster_center(cluster) for cluster in value_clusters], note_ref_cluster


def cluster_center(cluster: list[dict[str, Any]]) -> float:
    return sum(float(word.get("x0", 0.0)) for word in cluster) / len(cluster)


def is_note_ref_word_cluster(cluster: list[dict[str, Any]]) -> bool:
    return all(re.fullmatch(r"\d{1,2}", normalize_financial_text(word.get("text"))) for word in cluster)


def word_layout_record_from_line(
    line_words: list[dict[str, Any]],
    numeric_clusters: list[float],
    value_columns: list[str],
    *,
    note_ref_cluster: float | None = None,
) -> dict[str, Any] | None:
    rendered = render_word_layout_line(line_words)
    if not rendered:
        return None
    bbox = line_bbox(line_words)
    if not numeric_clusters or is_text_fallback_header_or_title_line(rendered):
        return {"line": rendered, "source_line": rendered, "source_bbox": bbox}
    ordered = sorted(line_words, key=lambda item: float(item.get("x0", 0.0)))
    cluster_tokens: list[list[str]] = [[] for _ in numeric_clusters]
    label_tokens: list[str] = []
    note_ref_tokens: list[str] = []
    first_numeric_boundary = min(numeric_clusters) - 18.0
    for word in ordered:
        text = normalize_financial_text(word.get("text"))
        if not text:
            continue
        x0 = float(word.get("x0", 0.0))
        if note_ref_cluster is not None and abs(x0 - note_ref_cluster) <= 35.0 and parseable_number(text) is not None:
            note_ref_tokens.append(text)
            continue
        if parseable_number(text) is None:
            if x0 <= first_numeric_boundary:
                label_tokens.append(text)
            continue
        cluster_index = nearest_numeric_cluster_index(x0, numeric_clusters)
        if cluster_index is None:
            continue
        cluster_tokens[cluster_index].append(text)
    label = normalize_financial_text(" ".join(label_tokens))
    numeric_values = [
        " ".join(tokens).strip()
        for tokens in cluster_tokens[: len(value_columns)]
        if tokens and parseable_number(" ".join(tokens).strip()) is not None
    ]
    if not label or not numeric_values:
        return {"line": rendered, "source_line": rendered, "source_bbox": bbox}
    record: dict[str, Any] = {
        "line": label,
        "source_line": rendered,
        "source_bbox": bbox,
        "inline_value_recovered": True,
        "inline_value_recovered_from": rendered,
        "word_layout_column_recovered": True,
    }
    for index, value in enumerate(numeric_values[: len(value_columns)]):
        record[value_columns[index]] = value
    if note_ref_tokens:
        record["note_ref"] = " ".join(note_ref_tokens)
        record["word_layout_note_ref_recovered"] = True
    percent_values = period_values_from_percent_change_tokens(rendered)
    if percent_values and len(value_columns) >= 2:
        note_ref, current_value, comparative_value = percent_values
        record[value_columns[0]] = current_value
        record[value_columns[1]] = comparative_value
        if note_ref:
            record["note_ref"] = note_ref
            record["word_layout_note_ref_recovered"] = True
        record["word_layout_percent_change_columns_recovered"] = True
    return record


def period_values_from_percent_change_tokens(text: str) -> tuple[str | None, str, str] | None:
    tokens = str(text or "").split()
    if len(tokens) < 4 or not any(token.endswith("%") for token in tokens):
        return None
    percent_index = next((index for index in range(len(tokens) - 1, -1, -1) if tokens[index].endswith("%")), None)
    if percent_index is None or percent_index < 2:
        return None
    comparative_group, cursor = numeric_group_before(tokens, percent_index - 1)
    if not comparative_group or cursor < 0:
        return None
    current_group, cursor = numeric_group_before(tokens, cursor)
    if not current_group:
        return None
    note_ref = tokens[cursor] if cursor >= 0 and re.fullmatch(r"\d{1,2}", tokens[cursor]) else None
    return note_ref, current_group, comparative_group


def numeric_group_before(tokens: list[str], index: int) -> tuple[str | None, int]:
    if index < 0:
        return None, index
    token = tokens[index]
    if parseable_number(token) is None:
        return None, index
    group = [token]
    cursor = index - 1
    if cursor >= 0 and re.fullmatch(r"\d{1,3}", tokens[cursor]) and parseable_number(token) is not None:
        previous_is_numeric = cursor - 1 >= 0 and parseable_number(tokens[cursor - 1]) is not None
        if previous_is_numeric:
            group.insert(0, tokens[cursor])
            cursor -= 1
    return " ".join(group), cursor


def nearest_numeric_cluster_index(position: float, clusters: list[float]) -> int | None:
    best_index: int | None = None
    best_distance: float | None = None
    for index, center in enumerate(clusters):
        distance = abs(position - center)
        if distance > 55.0:
            continue
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def line_bbox(line_words: list[dict[str, Any]]) -> dict[str, float] | None:
    if not line_words:
        return None
    try:
        return {
            "x0": min(float(word.get("x0", 0.0)) for word in line_words),
            "x1": max(float(word.get("x1", word.get("x0", 0.0))) for word in line_words),
            "top": min(float(word.get("top", word.get("doctop", 0.0))) for word in line_words),
            "bottom": max(float(word.get("bottom", word.get("top", word.get("doctop", 0.0)))) for word in line_words),
        }
    except Exception:
        return None


def stitch_word_layout_records(records: list[dict[str, Any]], value_columns: list[str]) -> list[dict[str, Any]]:
    stitched: list[dict[str, Any]] = []
    index = 0
    while index < len(records):
        current = dict(records[index])
        if index + 1 >= len(records):
            stitched.append(current)
            index += 1
            continue
        following = dict(records[index + 1])
        merged = maybe_stitch_word_layout_record_pair(current, following, index, value_columns)
        if merged is not None:
            stitched.append(merged)
            index += 2
            continue
        stitched.append(current)
        index += 1
    return stitched


def maybe_stitch_word_layout_record_pair(
    current: dict[str, Any],
    following: dict[str, Any],
    index: int,
    value_columns: list[str],
) -> dict[str, Any] | None:
    current_line = normalize_financial_text(current.get("line"))
    following_line = normalize_financial_text(following.get("line"))
    if not current_line or not following_line:
        return None
    if is_text_fallback_header_or_title_line(current_line) or is_text_fallback_header_or_title_line(following_line):
        return None
    current_has_values = any(parseable_number(current.get(column)) is not None for column in value_columns)
    following_has_values = any(parseable_number(following.get(column)) is not None for column in value_columns)
    if current_has_values or not following_has_values:
        return None
    combined_source = normalize_financial_text(
        " ".join(
            filter(
                None,
                [
                    str(current.get("source_line") or current_line).strip(),
                    str(following.get("source_line") or following_line).strip(),
                ],
            )
        )
    )
    recovered = split_text_fallback_line_and_values(combined_source)
    if not recovered:
        return None
    label, values = recovered
    if len(values) > len(value_columns):
        return None
    merged = dict(following)
    merged["line"] = label
    merged["source_line"] = combined_source
    merged["stitched_from_rows"] = [index, index + 1]
    merged["stitch_warning"] = "word_layout_adjacent_label_value_rows_stitched"
    merged["stitch_confidence"] = 0.83
    merged["source_bbox"] = merge_bboxes(current.get("source_bbox"), following.get("source_bbox"))
    return merged


def merge_bboxes(first: dict[str, float] | None, second: dict[str, float] | None) -> dict[str, float] | None:
    if not first and not second:
        return None
    if not first:
        return second
    if not second:
        return first
    return {
        "x0": min(float(first.get("x0", 0.0)), float(second.get("x0", 0.0))),
        "x1": max(float(first.get("x1", 0.0)), float(second.get("x1", 0.0))),
        "top": min(float(first.get("top", 0.0)), float(second.get("top", 0.0))),
        "bottom": max(float(first.get("bottom", 0.0)), float(second.get("bottom", 0.0))),
    }


def page_text_with_word_layout(text: str, page: Any | None) -> str:
    word_lines = word_layout_lines(page)
    if not word_lines:
        return text
    base_lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    merged_lines = list(base_lines)
    seen = {normalize_financial_text(line) for line in merged_lines}
    for line in word_lines:
        normalized = normalize_financial_text(line)
        if normalized and normalized not in seen:
            merged_lines.append(line)
            seen.add(normalized)
    return "\n".join(merged_lines)


def numeric_line_count(rows: list[list[str]]) -> int:
    count = 0
    for row in rows:
        line = str(row[0] if row else "").strip()
        if line and parseable_number(line) is None and re.search(r"\d", line):
            count += 1
    return count


def best_text_recovery_rows(text: str, page: Any | None) -> tuple[list[list[str]], str]:
    text_rows = text_lines_to_rows(text)
    word_rows = [[line] for line in word_layout_lines(page)]
    if not word_rows:
        return text_rows, "extract_text"
    text_numeric = numeric_line_count(text_rows)
    word_numeric = numeric_line_count(word_rows)
    if word_numeric >= 2 and word_layout_has_value_columns(page):
        return word_rows, "extract_words"
    if len(word_rows) > len(text_rows) and word_numeric >= text_numeric:
        return word_rows, "extract_words"
    if word_numeric > text_numeric:
        return word_rows, "extract_words"
    return text_rows, "extract_text"


def word_layout_has_value_columns(page: Any | None) -> bool:
    clusters, _note_ref_cluster = detect_word_numeric_clusters(word_layout_line_groups(page), ["current", "comparative"])
    return len(clusters) >= 2


def extract_expected_primary_pages_from_toc(text: str, reporting_standard: str) -> dict[int, str]:
    if reporting_standard.upper() != "IFRS":
        return {}
    normalized = normalize_matching_text(text)
    if not is_toc_page(normalized) and not looks_like_toc_text(normalized):
        return {}
    expected: dict[int, str] = {}
    for line in (text or "").splitlines():
        line_normalized = normalize_matching_text(" ".join(line.casefold().split()))
        match = re.search(r"(\d{1,3})\s*$", line_normalized)
        if not match:
            continue
        page_number = int(match.group(1))
        statement_family = classify_primary_statement_title_only(line_normalized, reporting_standard)
        if statement_family in PRIMARY_STATEMENT_FAMILIES:
            expected[page_number] = statement_family
        elif contains_normalized_marker(line_normalized, "balance sheet", "statement of financial position"):
            expected[page_number] = "balance_sheet"
        elif contains_normalized_marker(line_normalized, "statement of comprehensive income", "statement of profit or loss"):
            expected[page_number] = "income_statement"
        elif contains_normalized_marker(line_normalized, "statement of cash flows"):
            expected[page_number] = "cash_flow"
    return expected


def looks_like_toc_text(normalized: str) -> bool:
    return contains_normalized_marker(normalized, "contents", "содержание", "РЎРћР”Р•Р Р–РђРќРР•") or normalized.count("...") >= 3


def page_has_extractable_content(page: Any) -> bool:
    text = ""
    try:
        text = page.extract_text() or ""
    except Exception:
        text = ""
    if text.strip():
        return True
    try:
        if page.extract_tables() or []:
            return True
    except Exception:
        pass
    try:
        if page.extract_words() or []:
            return True
    except Exception:
        pass
    return False


def statement_coverage(statement_tables: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    coverage: dict[str, dict[str, Any]] = {}
    for statement_type in sorted(PRIMARY_STATEMENT_FAMILIES):
        periods = sorted(
            {
                table.get("period")
                for table in statement_tables
                if table.get("statement_type") == statement_type and table.get("period")
            }
        )
        coverage[statement_type] = {
            "found": bool(periods),
            "count": sum(1 for table in statement_tables if table.get("statement_type") == statement_type),
            "periods": periods,
        }
    return coverage


def required_statement_tables_missing(coverage: dict[str, dict[str, Any]]) -> list[str]:
    return [
        statement_type
        for statement_type in sorted(REQUIRED_PRIMARY_STATEMENT_FAMILIES)
        if not coverage[statement_type]["found"]
    ]


def period_type_for_statement(statement_type: str, period: str) -> str:
    if statement_type == "balance_sheet":
        return "balance_sheet_snapshot"
    if period.endswith("Q4"):
        return "annual"
    if statement_type in {"income_statement", "cash_flow", "changes_in_equity"}:
        return "ytd"
    return "unknown"


def guess_title(text: str) -> str | None:
    for line in (text or "").splitlines():
        if classify_statement_table(line, "IFRS") != "unknown" or classify_statement_table(line, "RAS") != "unknown":
            return normalize_financial_text(line)
    return None


def detect_unit(text: str) -> str | None:
    normalized = normalize_matching_text(text)
    if contains_normalized_marker(normalized, "billion", "billions", "миллиард"):
        return "billion"
    if contains_normalized_marker(normalized, "million", "mln", "млн"):
        return "million"
    if contains_normalized_marker(normalized, "thousand", "тыс"):
        return "thousand"
    return None


def detect_unit_multiplier(text: str) -> int | None:
    unit = detect_unit(text)
    if unit == "billion":
        return 1_000_000_000
    if unit == "million":
        return 1_000_000
    if unit == "thousand":
        return 1_000
    return None


def detect_currency(text: str) -> str | None:
    normalized = normalize_matching_text(text)
    if contains_normalized_marker(normalized, "rub", "rouble", "ruble", "руб"):
        return "RUB"
    if contains_normalized_marker(normalized, "usd"):
        return "USD"
    return None


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return normalize_financial_text(" ".join(str(value).split()))


def unique_column_names(columns: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    unique: list[str] = []
    for index, column in enumerate(columns):
        base = normalize_financial_text(column) or f"col_{index}"
        count = counts.get(base, 0)
        counts[base] = count + 1
        unique.append(base if count == 0 else f"{base}__{count + 1}")
    return unique


def normalize_pdf_table_rows(
    raw_rows: list[list[str]],
    nearby_text: str,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    if not raw_rows:
        return [], [], _empty_pdf_diagnostics()
    row_width = max(len(row) for row in raw_rows)
    padded_rows = [row + [""] * (row_width - len(row)) for row in raw_rows]
    header_row_count = detect_pdf_header_row_count(padded_rows)
    header_rows = padded_rows[:header_row_count]
    data_rows = padded_rows[header_row_count:]
    columns, collisions = build_pdf_columns(header_rows)
    date_indices = [index for index, column in enumerate(columns) if is_date_like_column(column)]
    first_data_index = min(date_indices) if date_indices else infer_first_numeric_column(data_rows)
    note_indices = {index for index, column in enumerate(columns) if contains_normalized_marker(column, "прим", "note")}
    records: list[dict[str, Any]] = []
    label_column_present = False
    label_column_reconstructed = False
    active_columns = ["line"]
    for index in range(first_data_index, len(columns)):
        if index not in note_indices:
            active_columns.append(columns[index])
    for row in data_rows:
        record: dict[str, Any] = {}
        inline_recovery: tuple[str, list[str]] | None = None
        inline_recovery_source: str | None = None
        for index, cell in enumerate(row[:first_data_index]):
            if index in note_indices or not cell:
                continue
            candidate = split_inline_label_and_values(cell)
            if candidate:
                inline_recovery = candidate
                inline_recovery_source = normalize_financial_text(cell)
                break
        label_parts = [
            cell_label_fragment(cell)
            for index, cell in enumerate(row[:first_data_index])
            if index not in note_indices and cell and cell_label_fragment(cell)
        ]
        label = " ".join(part for part in label_parts if part).strip()
        if label:
            record["line"] = label
            label_column_present = True
            if len(label_parts) > 1 or (first_data_index and not normalize_financial_text(row[0])):
                label_column_reconstructed = True
        for index in range(first_data_index, min(len(row), len(columns))):
            if index in note_indices:
                continue
            value = normalize_financial_text(row[index])
            if value:
                record[columns[index]] = value
        existing_numeric_columns = [
            column
            for column in active_columns
            if column != "line" and parseable_number(record.get(column)) is not None
        ]
        if inline_recovery and not existing_numeric_columns:
            recovered_label, recovered_values = inline_recovery
            record["line"] = recovered_label
            target_columns = [column for column in active_columns if column != "line"]
            for value_index, value in enumerate(recovered_values):
                if value_index >= len(target_columns):
                    break
                record[target_columns[value_index]] = value
            record["inline_value_recovered"] = True
            record["inline_value_recovered_from"] = inline_recovery_source
        note_ref = next((normalize_financial_text(row[index]) for index in sorted(note_indices) if row[index]), None)
        if note_ref:
            record["note_ref"] = note_ref
        if record:
            records.append(record)
    records = stitch_primary_statement_rows(records, active_columns)
    records = recover_inline_statement_values(records, active_columns)
    quality = "standard"
    if collisions or label_column_reconstructed or not label_column_present:
        quality = "degraded"
    return active_columns, records, {
        "header_row_count": header_row_count,
        "normalized_columns": active_columns,
        "repeated_column_names_detected": collisions,
        "label_column_present": label_column_present,
        "label_column_reconstructed": label_column_reconstructed,
        "table_structure_quality": quality,
    }


def normalize_generic_table_rows(
    header_row: list[str],
    data_rows: list[list[str]],
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    columns = unique_column_names(header_row)
    date_indices = [index for index, column in enumerate(columns) if is_date_like_column(column)]
    first_data_index = min(date_indices) if date_indices else infer_first_numeric_column(data_rows)
    note_indices = {index for index, column in enumerate(columns) if contains_normalized_marker(column, "прим", "note")}
    active_columns = ["line"]
    for index in range(first_data_index, len(columns)):
        if index not in note_indices:
            active_columns.append(columns[index])
    records: list[dict[str, Any]] = []
    label_column_present = False
    label_column_reconstructed = False
    for row in data_rows:
        padded_row = row + [""] * (len(columns) - len(row))
        record: dict[str, Any] = {}
        label_parts = [
            cell_label_fragment(cell)
            for index, cell in enumerate(padded_row[:first_data_index])
            if index not in note_indices and cell and cell_label_fragment(cell)
        ]
        label = " ".join(part for part in label_parts if part).strip()
        if label:
            record["line"] = label
            label_column_present = True
            if len(label_parts) > 1 or (first_data_index and not normalize_financial_text(padded_row[0])):
                label_column_reconstructed = True
        for index in range(first_data_index, len(columns)):
            if index in note_indices:
                continue
            value = normalize_financial_text(padded_row[index])
            if value:
                record[columns[index]] = value
        note_ref = next(
            (normalize_financial_text(padded_row[index]) for index in sorted(note_indices) if padded_row[index]),
            None,
        )
        if note_ref:
            record["note_ref"] = note_ref
        if record:
            records.append(record)
    records = stitch_primary_statement_rows(records, active_columns)
    records = recover_inline_statement_values(records, active_columns)
    quality = "degraded" if label_column_reconstructed or not label_column_present else "standard"
    return active_columns, records, {
        "header_row_count": 1 if header_row else 0,
        "normalized_columns": active_columns,
        "repeated_column_names_detected": False,
        "label_column_present": label_column_present,
        "label_column_reconstructed": label_column_reconstructed,
        "table_structure_quality": quality,
    }


def recover_numeric_fragment_statement_rows(
    records: list[dict[str, Any]],
    columns: list[str],
    *,
    page: Any | None,
    statement_family: str,
    diagnostics: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    value_columns = [column for column in columns if column != "line"]
    primary_family = statement_family in PRIMARY_STATEMENT_FAMILIES
    needs_recovery = primary_family and not diagnostics.get("label_column_present") and bool(value_columns)
    updated = dict(diagnostics)
    updated["label_recovery_attempted"] = bool(needs_recovery)
    updated["label_recovery_succeeded_count"] = 0
    updated["label_recovery_failed_count"] = 0
    updated["numeric_fragment_rows_recovered"] = 0
    updated["numeric_fragment_rows_unresolved"] = 0
    if not needs_recovery or page is None:
        if needs_recovery:
            updated["numeric_fragment_rows_unresolved"] = sum(
                1
                for record in records
                if not normalize_financial_text(record.get("line"))
                and any(record.get(column) for column in value_columns)
            )
        return records, updated
    word_records = word_layout_structured_records(page, value_columns)
    signature_map: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for candidate in word_records:
        label = normalize_financial_text(candidate.get("line"))
        if not label:
            continue
        signature = numeric_signature(candidate, value_columns)
        if not signature or not any(signature):
            continue
        signature_map.setdefault(signature, []).append(candidate)
    recovered_records: list[dict[str, Any]] = []
    for record in records:
        if normalize_financial_text(record.get("line")):
            recovered_records.append(record)
            continue
        signature = numeric_signature(record, value_columns)
        candidates = signature_map.get(signature, [])
        if len(candidates) == 1:
            matched = candidates[0]
            recovered = dict(record)
            recovered["line"] = normalize_financial_text(matched.get("line"))
            recovered["source_bbox"] = matched.get("source_bbox")
            recovered["label_recovered_from_word_layout"] = True
            recovered["recovery_mode"] = "word_layout_numeric_signature_match"
            recovered["alignment_confidence"] = 0.92
            recovered["ownership_confidence"] = 0.92
            recovered["label_confidence"] = 0.9
            recovered["value_confidence"] = 0.86
            recovered["anchor_hints"] = [f"numeric_signature:{'|'.join(signature)}"]
            recovered_records.append(recovered)
            updated["label_recovery_succeeded_count"] += 1
            updated["numeric_fragment_rows_recovered"] += 1
            continue
        unresolved = dict(record)
        unresolved["recovery_mode"] = "word_layout_numeric_signature_match"
        unresolved["label_recovery_reason"] = "label_ownership_unresolved_after_recovery"
        unresolved["anchor_hints"] = [f"numeric_signature:{'|'.join(signature)}"] if signature else []
        recovered_records.append(unresolved)
        updated["label_recovery_failed_count"] += 1
        updated["numeric_fragment_rows_unresolved"] += 1
    if updated["label_recovery_succeeded_count"] > 0:
        updated["label_column_reconstructed"] = True
        updated["label_column_present"] = True
    return recovered_records, updated


def numeric_signature(record: dict[str, Any], value_columns: list[str]) -> tuple[str, ...]:
    signature_values: list[float] = []
    for column in value_columns:
        value = normalize_financial_text(record.get(column))
        parsed = primary_cell_numeric_value(value)
        if parsed is None:
            continue
        signature_values.append(parsed)
    if len(signature_values) < 2:
        source_numbers = trailing_numeric_values(record.get("source_line"), limit=max(2, len(signature_values) or 2))
        if source_numbers:
            signature_values = source_numbers[-max(2, len(signature_values) or 2) :]
    return tuple(format(value, ".6f") for value in signature_values)


def primary_cell_numeric_value(value: Any) -> float | None:
    text = normalize_financial_text(str(value or ""))
    if not text:
        return None
    direct = parseable_number(text)
    token_matches = list(re.finditer(r"\(?-?\d[\d,]*(?:\.\d+)?\)?", text))
    if len(token_matches) <= 1:
        return direct
    for match in reversed(token_matches):
        parsed = parseable_number(match.group(0))
        if parsed is not None:
            return parsed
    return direct


def trailing_numeric_values(value: Any, limit: int = 2) -> list[float]:
    text = normalize_financial_text(str(value or ""))
    if not text:
        return []
    parsed_values: list[float] = []
    for match in re.finditer(r"\(?-?\d[\d,]*(?:\.\d+)?\)?", text):
        parsed = parseable_number(match.group(0))
        if parsed is None:
            continue
        parsed_values.append(parsed)
    if not parsed_values:
        return []
    return parsed_values[-limit:]


def detect_pdf_header_row_count(rows: list[list[str]]) -> int:
    max_headers = min(3, len(rows))
    header_count = 0
    for row in rows[:max_headers]:
        if any(split_inline_label_and_values(cell) for cell in row if cell):
            break
        text_cells = [cell for cell in row if cell and cell_label_fragment(cell)]
        numeric_cells = [cell for cell in row if parseable_number(cell) is not None]
        row_text = " ".join(str(cell or "") for cell in row)
        if contains_normalized_marker(
            row_text,
            "31 декабря",
            "30 сентября",
            "30 июня",
            "31 марта",
            "прим",
            "note",
            "2020",
            "2021",
            "2022",
            "2023",
            "2024",
            "2025",
            "2026",
            "2027",
        ):
            header_count += 1
            continue
        if text_cells and len(numeric_cells) <= 1:
            header_count += 1
            continue
        break
    return max(header_count, 1)


def build_pdf_columns(header_rows: list[list[str]]) -> tuple[list[str], bool]:
    width = max(len(row) for row in header_rows)
    merged: list[str] = []
    collisions = False
    raw_counts: dict[str, int] = {}
    visible_header_counts: dict[str, int] = {}
    for index in range(width):
        parts: list[str] = []
        for row in header_rows:
            value = normalize_financial_text(row[index] if index < len(row) else "")
            if value and value not in parts:
                parts.append(value)
        merged_value = " ".join(parts).strip() or f"col_{index}"
        visible_header = normalize_financial_text(header_rows[0][index] if index < len(header_rows[0]) else "")
        if visible_header:
            visible_header_counts[visible_header] = visible_header_counts.get(visible_header, 0) + 1
            if visible_header_counts[visible_header] > 1:
                collisions = True
        merged.append(merged_value)
        raw_counts[merged_value] = raw_counts.get(merged_value, 0) + 1
        if raw_counts[merged_value] > 1:
            collisions = True
    return unique_column_names(merged), collisions


def is_date_like_column(column: str) -> bool:
    return contains_normalized_marker(
        column,
        "31 декабря",
        "30 сентября",
        "30 июня",
        "31 марта",
        "2020",
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
        "2026",
        "2027",
        "fy 2020",
        "fy 2021",
        "fy 2022",
        "fy 2023",
        "fy 2024",
        "fy 2025",
        "fy 2026",
        "fy 2027",
    )


def contains_explicit_year_or_date(text: str) -> bool:
    return bool(re.search(r"\b20\d{2}\b", normalize_matching_text(text))) or is_date_like_column(text)


def infer_first_numeric_column(rows: list[list[str]]) -> int:
    if not rows:
        return 0
    width = max(len(row) for row in rows)
    for index in range(width):
        if any(index < len(row) and parseable_number(row[index]) is not None for row in rows):
            return index
    return width


def parseable_number(value: Any) -> float | None:
    text = normalize_financial_text(str(value or ""))
    if not text or text in {"-", "—"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()")
    cleaned = re.sub(r"[^0-9,.\-\s]", "", cleaned).strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        parts = cleaned.split(",")
        cleaned = "".join(parts) if all(len(part) == 3 for part in parts[1:]) else cleaned.replace(",", ".")
    cleaned = cleaned.replace(" ", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def cell_label_fragment(value: Any) -> str:
    text = normalize_financial_text(str(value or ""))
    if not text:
        return ""
    recovered = split_inline_label_and_values(text)
    if recovered:
        return recovered[0]
    return text if parseable_number(text) is None else ""


def _empty_pdf_diagnostics() -> dict[str, Any]:
    return {
        "header_row_count": 0,
        "normalized_columns": [],
        "repeated_column_names_detected": False,
        "label_column_present": False,
        "label_column_reconstructed": False,
        "table_structure_quality": "degraded",
    }


def stitch_primary_statement_rows(records: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    if not records:
        return records
    date_columns = [column for column in columns if column != "line" and is_date_like_column(column)]
    stitched: list[dict[str, Any]] = []
    index = 0
    while index < len(records):
        current = dict(records[index])
        if index + 1 < len(records):
            candidate = records[index + 1]
            if should_stitch_rows(current, candidate, date_columns):
                merged = dict(candidate)
                merged["line"] = " ".join(
                    filter(
                        None,
                        [
                            str(current.get("line") or "").strip(),
                            str(candidate.get("line") or "").strip(),
                        ],
                    )
                ).strip()
                merged["stitched_from_rows"] = [index, index + 1]
                merged["stitch_confidence"] = 0.86
                merged["stitch_warning"] = "row_label_stitched_from_adjacent_pdf_rows"
                if current.get("note_ref") and not merged.get("note_ref"):
                    merged["note_ref"] = current["note_ref"]
                current_numeric = [
                    parseable_number(current.get(column))
                    for column in current
                    if column not in {"line", "note_ref"}
                ]
                next_numeric = [
                    parseable_number(candidate.get(column))
                    for column in candidate
                    if column not in {"line", "note_ref"}
                ]
                if any(value is not None for value in current_numeric) and any(value is not None for value in next_numeric):
                    merged.setdefault("stitch_warning", "row_label_and_value_stitched_from_adjacent_pdf_rows")
                stitched.append(merged)
                index += 2
                continue
        stitched.append(current)
        index += 1
    return stitched


def recover_inline_statement_values(records: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    if not records:
        return records
    value_columns = [column for column in columns if column != "line"]
    if not value_columns:
        return records
    recovered_rows: list[dict[str, Any]] = []
    for row in records:
        if any(parseable_number(row.get(column)) is not None for column in value_columns):
            recovered_rows.append(row)
            continue
        line = normalize_financial_text(row.get("line"))
        if not line:
            recovered_rows.append(row)
            continue
        split = split_inline_label_and_values(line)
        if not split:
            recovered_rows.append(row)
            continue
        label, values = split
        if not values:
            recovered_rows.append(row)
            continue
        recovered = dict(row)
        recovered["line"] = label
        for index, value in enumerate(values):
            if index >= len(value_columns):
                break
            recovered[value_columns[index]] = value
        recovered["inline_value_recovered"] = True
        recovered["inline_value_recovered_from"] = line
        recovered_rows.append(recovered)
    return recovered_rows


def should_stitch_rows(current: dict[str, Any], following: dict[str, Any], date_columns: list[str]) -> bool:
    current_line = str(current.get("line") or "").strip()
    next_line = str(following.get("line") or "").strip()
    if not current_line or not next_line:
        return False
    if current_line.lower().startswith("итого") or next_line.lower().startswith("итого"):
        return False
    current_date_values = [current.get(column) for column in date_columns if current.get(column)]
    next_date_values = [following.get(column) for column in date_columns if following.get(column)]
    if current_date_values:
        return False
    if not next_date_values:
        return False
    if parseable_number(current_line) is not None or parseable_number(next_line) is not None:
        return False
    current_non_date_numeric = [
        parseable_number(value)
        for key, value in current.items()
        if key not in {"line", "note_ref"} and key not in date_columns
    ]
    current_non_date_numeric = [value for value in current_non_date_numeric if value is not None]
    if len(current_non_date_numeric) > 1:
        return False
    return True


def split_inline_label_and_values(text: str) -> tuple[str, list[str]] | None:
    tokens = str(text or "").strip().split()
    if len(tokens) < 2:
        return None
    trailing_values: list[str] = []
    for token in reversed(tokens):
        if parseable_number(token) is None:
            break
        trailing_values.append(token)
        if len(trailing_values) == 2:
            break
    if not trailing_values:
        return None
    trailing_values.reverse()
    label_tokens = tokens[: len(tokens) - len(trailing_values)]
    label = " ".join(label_tokens).strip()
    if not label:
        return None
    return label, trailing_values


def fallback_value_columns(current_period: str | None, comparative_period: str | None) -> list[str]:
    columns: list[str] = []
    if current_period:
        columns.append(current_period)
    if comparative_period:
        columns.append(comparative_period)
    if not columns:
        columns.append("current_period_value")
    if len(columns) == 1:
        columns.append("comparative_period_value")
    return columns[:2]


def structured_text_fallback_record(line: str, value_columns: list[str]) -> dict[str, Any]:
    recovered = split_text_fallback_line_and_values(line)
    if not recovered:
        return {"line": line}
    label, values = recovered
    record: dict[str, Any] = {"line": label}
    for index, value in enumerate(values[: len(value_columns)]):
        record[value_columns[index]] = value
    record["source_line"] = line
    record["inline_value_recovered"] = True
    record["inline_value_recovered_from"] = line
    return record


def split_structured_text_fallback_record(record: dict[str, Any], value_columns: list[str]) -> list[dict[str, Any]]:
    working_record = dict(record)
    line = normalize_financial_text(working_record.get("line"))
    if not line or len(value_columns) < 2:
        return [working_record]
    if not any(parseable_number(working_record.get(column)) is not None for column in value_columns):
        source_line = normalize_financial_text(working_record.get("source_line"))
        if not source_line:
            return [working_record]
        recovered_record = structured_text_fallback_record(source_line, value_columns)
        if not any(parseable_number(recovered_record.get(column)) is not None for column in value_columns):
            return [working_record]
        merged_record = dict(working_record)
        merged_record.update(recovered_record)
        working_record = merged_record
        line = normalize_financial_text(working_record.get("line"))
    tokens = [token for token in line.split() if token]
    if len(tokens) < 6:
        return [working_record]

    segments = extract_embedded_fallback_segments(tokens)
    if not segments:
        return [working_record]
    consumed_tokens = segments[-1]["next_cursor"]
    tail_label_tokens = tokens[consumed_tokens:]
    if len(tail_label_tokens) >= 2 and is_short_note_reference(tail_label_tokens[-1]):
        tail_label_tokens = tail_label_tokens[:-1]
    tail_label = " ".join(tail_label_tokens).strip()
    if not tail_label:
        return [working_record]

    split_records: list[dict[str, Any]] = []
    total_segments = len(segments) + 1
    base_source_line = str(working_record.get("source_line") or line)
    base_stitched_rows = list(working_record.get("stitched_from_rows") or [])
    for segment_index, segment in enumerate(segments):
        split_record = {
            "line": segment["label"],
            value_columns[0]: segment["values"][0],
            value_columns[1]: segment["values"][1],
            "source_line": base_source_line,
            "source_bbox": working_record.get("source_bbox"),
            "inline_value_recovered": True,
            "inline_value_recovered_from": base_source_line,
            "recovery_mode": "embedded_statement_value_split",
            "merged_line_split": True,
            "merged_statement_segment_index": segment_index,
            "merged_statement_segment_count": total_segments,
        }
        if base_stitched_rows:
            split_record["stitched_from_rows"] = base_stitched_rows
        split_records.append(split_record)

    final_record = dict(working_record)
    final_record["line"] = tail_label
    final_record["source_line"] = base_source_line
    final_record["recovery_mode"] = "embedded_statement_value_split_follow_up"
    final_record["merged_line_split"] = True
    final_record["merged_statement_segment_index"] = total_segments - 1
    final_record["merged_statement_segment_count"] = total_segments
    split_records.append(final_record)
    return split_records


def extract_embedded_fallback_segments(tokens: list[str]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(tokens):
        segment = next_embedded_fallback_segment(tokens[cursor:])
        if not segment:
            break
        absolute_next_cursor = cursor + int(segment["next_cursor"])
        segments.append(
            {
                "label": segment["label"],
                "values": list(segment["values"]),
                "next_cursor": absolute_next_cursor,
            }
        )
        cursor = absolute_next_cursor
    return segments


def next_embedded_fallback_segment(tokens: list[str]) -> dict[str, Any] | None:
    if len(tokens) < 6:
        return None
    candidates: list[dict[str, Any]] = []
    for first_numeric_index, token in enumerate(tokens):
        if first_numeric_index <= 0 or parseable_number(token) is None:
            continue
        label_tokens = tokens[:first_numeric_index]
        if len(label_tokens) >= 2 and is_short_note_reference(label_tokens[-1]):
            label_tokens = label_tokens[:-1]
        label = " ".join(label_tokens).strip()
        if len(label.split()) < 2:
            continue
        remaining_tokens = tokens[first_numeric_index:]
        note_offset = 1 if remaining_tokens and is_short_note_reference(remaining_tokens[0]) else 0
        if len(remaining_tokens) < note_offset + 3:
            continue
        embedded_value_tokens = remaining_tokens[note_offset : note_offset + 2]
        if len(embedded_value_tokens) != 2 or any(parseable_number(item) is None for item in embedded_value_tokens):
            continue
        tail_tokens = remaining_tokens[note_offset + 2 :]
        if len(tail_tokens) < 2:
            continue
        if not any(any(char.isalpha() for char in token) for token in tail_tokens):
            continue
        candidates.append(
            {
                "label": label,
                "values": embedded_value_tokens,
                "next_cursor": first_numeric_index + note_offset + 2,
            }
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(str(item["label"])))


def stitch_text_fallback_rows(lines: list[str], value_columns: list[str]) -> list[str | dict[str, Any]]:
    stitched: list[str | dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = normalize_financial_text(lines[index])
        if not line:
            index += 1
            continue
        stitched_record, consumed = stitch_text_fallback_cluster(lines, index, value_columns)
        if stitched_record is not None:
            stitched.append(stitched_record)
            index += consumed
            continue
        stitched.append(line)
        index += 1
    return stitched


def stitch_text_fallback_cluster(
    lines: list[str],
    index: int,
    value_columns: list[str],
) -> tuple[dict[str, Any] | None, int]:
    current = normalize_financial_text(lines[index])
    if not current or is_text_fallback_header_or_title_line(current) or split_text_fallback_line_and_values(current):
        return None, 1
    for width in (3, 2):
        group = [normalize_financial_text(item) for item in lines[index : index + width]]
        if len(group) != width or not all(group):
            continue
        if any(is_text_fallback_header_or_title_line(item) for item in group[1:]):
            continue
        tail = group[-1]
        if not split_text_fallback_line_and_values(tail):
            continue
        combined = normalize_financial_text(" ".join(group))
        combined_recovered = split_text_fallback_line_and_values(combined)
        if not combined_recovered:
            continue
        label, values = combined_recovered
        if len(values) > len(value_columns):
            continue
        tail_recovered = split_text_fallback_line_and_values(tail)
        if tail_recovered and normalize_financial_text(tail_recovered[0]) == normalize_financial_text(label):
            continue
        record = structured_text_fallback_record(combined, value_columns)
        record["source_line"] = combined
        record["stitched_from_rows"] = list(range(index, index + width))
        record["stitch_warning"] = "text_fallback_adjacent_line_cluster_stitched"
        record["stitch_confidence"] = 0.84 if width == 2 else 0.8
        return record, width
    return None, 1


def split_text_fallback_line_and_values(text: str) -> tuple[str, list[str]] | None:
    matches = trailing_number_matches(text)
    raw_tokens = grouped_trailing_number_tokens([match.group(0).strip() for match in matches])
    if raw_tokens:
        label = text[: matches[0].start()] if matches else text
        label = normalize_financial_text(" ".join(label.split(" -:")))
        if label:
            values = [token for token in raw_tokens if parseable_number(token) is not None]
            if values and len(values) <= 2:
                return label, values
    simple = split_inline_label_and_values(text)
    if simple and len(simple[1]) <= 2:
        return simple
    return None


def is_text_fallback_header_or_title_line(line: str) -> bool:
    normalized = normalize_matching_text(line)
    if classify_statement_table(line, "IFRS") != "unknown" or classify_statement_table(line, "RAS") != "unknown":
        return True
    if normalized.startswith("note"):
        return True
    if any(marker in normalized for marker in ("million", "billion", "thousand", "russian rubles", "руб", "млн", "млрд")):
        return True
    return False


def trailing_number_matches(line: str) -> list[re.Match[str]]:
    token_pattern = r"\(?-?\d[\d,]*(?:\.\d+)?\)?"
    matches = list(re.finditer(token_pattern, line))
    if not matches:
        return []
    trailing: list[re.Match[str]] = []
    cursor = len(line)
    for match in reversed(matches):
        between = line[match.end() : cursor]
        if between.strip():
            break
        trailing.append(match)
        cursor = match.start()
    return list(reversed(trailing))


def grouped_trailing_number_tokens(tokens: list[str]) -> list[str]:
    if len(tokens) <= 2:
        return tokens
    start = 1 if is_short_note_reference(tokens[0]) and len(tokens) % 2 == 1 else 0
    value_tokens = tokens[start:]
    if len(value_tokens) <= 2 or len(value_tokens) % 2:
        return tokens
    decimal_pairs = group_decimal_pairs(value_tokens)
    if decimal_pairs:
        return ([tokens[0]] if start else []) + decimal_pairs
    if any("," in token for token in tokens):
        return tokens
    half = len(value_tokens) // 2
    first = value_tokens[:half]
    second = value_tokens[half:]
    if valid_spaced_group(first) and valid_spaced_group(second):
        grouped = [" ".join(first), " ".join(second)]
        return ([tokens[0]] if start else []) + grouped
    return tokens


def group_decimal_pairs(tokens: list[str]) -> list[str] | None:
    if len(tokens) % 2:
        return None
    grouped: list[str] = []
    for index in range(0, len(tokens), 2):
        first = tokens[index].strip("()").lstrip("-")
        second = tokens[index + 1].strip("()").lstrip("-")
        if len(first) > 3 or not re.match(r"^\d{3}(?:[,.]\d+)$", second):
            return None
        left_paren = "(" if tokens[index].startswith("(") else ""
        right_paren = ")" if tokens[index + 1].endswith(")") else ""
        grouped.append(f"{left_paren}{tokens[index].strip('()')} {tokens[index + 1].strip('()')}{right_paren}")
    return grouped


def valid_spaced_group(tokens: list[str]) -> bool:
    if len(tokens) < 2:
        return False
    cleaned = [token.strip("()").lstrip("-") for token in tokens]
    return bool(cleaned[0]) and len(cleaned[0]) <= 3 and all(len(token) == 3 for token in cleaned[1:])


def is_short_note_reference(raw: str) -> bool:
    cleaned = raw.strip("()").replace(" ", "").replace(",", "")
    return cleaned.isdigit() and len(cleaned) <= 2


def summarize_table_period_resolution(statement_tables: list[dict[str, Any]], fallback_period: str) -> dict[str, Any]:
    candidates = [
        table
        for table in statement_tables
        if table.get("effective_period")
    ]
    if not candidates:
        return {
            "effective_report_period": fallback_period,
            "comparative_period": None,
            "period_source": "upload_default",
            "period_confidence": 0.1,
            "period_warnings": ["period_defaulted_without_table_evidence"],
        }
    best = max(candidates, key=lambda item: float(item.get("period_confidence") or 0.0))
    return {
        "effective_report_period": best.get("effective_period"),
        "comparative_period": best.get("comparative_period"),
        "period_source": best.get("period_source"),
        "period_confidence": best.get("period_confidence"),
        "period_warnings": best.get("period_warnings") or [],
    }


def summarize_table_structure_diagnostics(statement_tables: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics = {
        "header_collapsed_count": 0,
        "duplicate_date_headers_count": 0,
        "label_column_missing_count": 0,
        "label_column_reconstructed_count": 0,
        "stitched_rows_count": 0,
        "numeric_only_rows_count": 0,
        "subtotal_rows_count": 0,
        "period_header_ambiguous_count": 0,
    }
    for table in statement_tables:
        table_diagnostics = table.get("diagnostics") or {}
        diagnostics["header_collapsed_count"] += int(bool(table_diagnostics.get("header_collapsed")))
        diagnostics["duplicate_date_headers_count"] += int(bool(table_diagnostics.get("duplicate_date_headers")))
        diagnostics["label_column_missing_count"] += int(bool(table_diagnostics.get("label_column_missing")))
        diagnostics["label_column_reconstructed_count"] += int(bool(table_diagnostics.get("label_column_reconstructed")))
        diagnostics["stitched_rows_count"] += int(table_diagnostics.get("stitched_rows_count") or 0)
        diagnostics["numeric_only_rows_count"] += int(table_diagnostics.get("numeric_only_rows_count") or 0)
        diagnostics["subtotal_rows_count"] += int(table_diagnostics.get("subtotal_rows_count") or 0)
        diagnostics["period_header_ambiguous_count"] += int(bool(table_diagnostics.get("period_header_ambiguous")))
    return diagnostics


def build_normalized_row_blocks(
    records: list[dict[str, Any]],
    columns: list[str],
    statement_family: str,
    source_engine: str | None = None,
    source_page: int | None = None,
    source_table_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    value_columns = [column for column in columns if column != "line"]
    row_blocks: list[dict[str, Any]] = []
    stitched_rows_count = 0
    numeric_only_rows_count = 0
    subtotal_rows_count = 0
    for row in records:
        label_text = normalize_financial_text(row.get("line"))
        value_cells = {
            column: row.get(column)
            for column in value_columns
            if normalize_financial_text(row.get(column))
        }
        label_confidence = (
            float(row.get("label_confidence"))
            if row.get("label_confidence") is not None
            else (0.92 if label_text else 0.0)
        )
        value_confidence = (
            float(row.get("value_confidence"))
            if row.get("value_confidence") is not None
            else (0.86 if value_cells else 0.0)
        )
        ownership_confidence = (
            float(row.get("ownership_confidence"))
            if row.get("ownership_confidence") is not None
            else min(label_confidence, value_confidence) if label_text and value_cells else 0.0
        )
        fragment_role = str(row.get("fragment_role") or _infer_fragment_role(label_text, value_cells))
        anchor_hints = list(row.get("anchor_hints") or [])
        row_kind = classify_row_kind(label_text, value_cells, row, statement_family)
        if row_kind == "numeric_fragment":
            numeric_only_rows_count += 1
        if row_kind in {"subtotal", "grand_total"}:
            subtotal_rows_count += 1
        if row.get("stitched_from_rows"):
            stitched_rows_count += 1
        row_blocks.append(
            {
                "label_text": label_text or None,
                "label_bbox": row.get("label_bbox"),
                "label_tokens": [token for token in re.split(r"\s+", label_text or "") if token],
                "note_ref": row.get("note_ref"),
                "value_cells": value_cells,
                "value_bboxes": row.get("value_bboxes") or {},
                "row_kind": row_kind,
                "fragment_role": fragment_role,
                "anchor_hints": anchor_hints,
                "stitched_from_rows": row.get("stitched_from_rows") or [],
                "row_confidence": row_confidence_for_kind(row_kind, row),
                "label_confidence": label_confidence,
                "value_confidence": value_confidence,
                "ownership_confidence": ownership_confidence,
                "fact_period_confidence": row.get("fact_period_confidence"),
                "source_engine": source_engine,
                "source_page": source_page,
                "source_table_id": source_table_id,
                "source_bbox": row.get("source_bbox"),
                "fusion_status": "single_engine",
                "source_engines_involved": [source_engine] if source_engine else [],
                "source_traceability": {
                    "source_engine": source_engine,
                    "source_page": source_page,
                    "source_table_id": source_table_id,
                    "source_bbox": row.get("source_bbox"),
                    "fusion_status": "single_engine",
                    "source_engines_involved": [source_engine] if source_engine else [],
                },
                "diagnostics": {
                    "source_line": " | ".join(
                        str(value).strip()
                        for value in row.values()
                        if str(value or "").strip()
                    ),
                    "stitch_warning": row.get("stitch_warning"),
                    "stitch_mode": infer_stitch_mode(row),
                    "label_missing": not bool(label_text),
                    "numeric_cell_count": len(value_cells),
                    "inline_value_recovered": bool(row.get("inline_value_recovered")),
                    "inline_value_recovered_from": row.get("inline_value_recovered_from"),
                    "fragment_role": fragment_role,
                    "anchor_hints": anchor_hints,
                    "label_recovery_reason": row.get("label_recovery_reason"),
                    "recovery_mode": row.get("recovery_mode"),
                },
            }
        )
    return row_blocks, {
        "stitched_rows_count": stitched_rows_count,
        "numeric_only_rows_count": numeric_only_rows_count,
        "subtotal_rows_count": subtotal_rows_count,
        "row_kinds_present": sorted({block["row_kind"] for block in row_blocks}),
    }


def classify_row_kind(
    label_text: str,
    value_cells: dict[str, Any],
    row: dict[str, Any],
    statement_family: str,
) -> str:
    normalized = normalize_matching_text(label_text)
    if not label_text and value_cells:
        return "numeric_fragment"
    if not label_text:
        return "unknown"
    if is_statement_title_like_label(normalized):
        return "header"
    if is_statement_signature_like_label(normalized) or is_statement_footer_like_label(normalized):
        return "footnote"
    if contains_normalized_marker(normalized, "примечание", "note") or normalized.startswith("*"):
        return "footnote"
    if row.get("note_ref") and not value_cells:
        return "note_reference_only"
    if contains_normalized_marker(normalized, "итого", "total"):
        if contains_normalized_marker(normalized, "актив", "обяз", "капитал", "equity", "liabilit", "assets"):
            return "grand_total"
        return "subtotal"
    if statement_family == "cash_flow" and contains_normalized_marker(
        normalized,
        "операционной деятельности",
        "инвестиционной деятельности",
        "финансовой деятельности",
        "operating activities",
        "investing activities",
        "financing activities",
    ):
        return "section_title" if not value_cells else "statement_line_item"
    if value_cells:
        return "statement_line_item"
    if contains_normalized_marker(normalized, "активы", "обязательства", "капитал", "equity", "assets", "liabilities"):
        return "section_title"
    return "unknown"


def is_statement_title_like_label(normalized: str) -> bool:
    return contains_normalized_marker(
        normalized,
        "statement of financial position",
        "statement of profit or loss",
        "statement of cash flows",
        "statement of comprehensive income",
        "consolidated statement of financial position",
        "consolidated statement of profit or loss",
        "consolidated statement of cash flows",
        "consolidated statement of comprehensive income",
        "for the year ended",
        "for the period ended",
        "at 31 december",
        "по состоянию на",
        "за год, закончившийся",
        "за период, закончившийся",
    )


def is_statement_signature_like_label(normalized: str) -> bool:
    return contains_normalized_marker(
        normalized,
        "chief executive officer",
        "chief financial officer",
        "general director",
        "ceo",
        "cfo",
        "генеральный директор",
        "главный исполнительный директор",
        "финансовый директор",
    )


def is_statement_footer_like_label(normalized: str) -> bool:
    return contains_normalized_marker(
        normalized,
        "the accompanying notes are the integral part",
        "accompanying notes are an integral part",
        "integral part of these consolidated financial statements",
        "integral part of these financial statements",
        "прилагаемые примечания являются неотъемлемой частью",
    )


def row_confidence_for_kind(row_kind: str, row: dict[str, Any]) -> float:
    if row_kind == "grand_total":
        return 0.95
    if row_kind == "subtotal":
        return 0.88
    if row_kind == "statement_line_item":
        return 0.9 if row.get("stitched_from_rows") else 0.82
    if row_kind == "section_title":
        return 0.55
    if row_kind == "numeric_fragment":
        return 0.35
    return 0.4


def infer_stitch_mode(row: dict[str, Any]) -> str | None:
    if not row.get("stitched_from_rows"):
        return None
    warning = str(row.get("stitch_warning") or "")
    if "label_and_value" in warning:
        return "label_plus_value_continuation_stitch"
    if "section" in warning:
        return "multi_line_statement_section_stitch"
    return "label_continuation_stitch"


def _infer_fragment_role(label_text: str, value_cells: dict[str, Any]) -> str:
    if not label_text and value_cells:
        return "numeric_values_only"
    if label_text and value_cells:
        return "statement_row"
    return "text_only"


class _SimpleTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_cell = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif self._in_table and tag == "tr":
            self._current_row = []
        elif self._in_table and tag in {"td", "th"}:
            self._in_cell = True
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._in_table and tag in {"td", "th"}:
            self._current_row.append(_clean_cell(" ".join(self._current_cell)))
            self._current_cell = []
            self._in_cell = False
        elif self._in_table and tag == "tr":
            if any(self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = []
        elif tag == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._current_table = []
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_cell.append(data)


def parse_html_tables(html: str) -> list[list[list[str]]]:
    parser = _SimpleTableParser()
    parser.feed(html or "")
    return parser.tables
