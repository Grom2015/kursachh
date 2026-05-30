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
from app.services.parsing.text_normalization import contains_normalized_marker, normalize_financial_text, normalize_matching_text


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
    period_warnings: list[str] = field(default_factory=list)
    statement_family: str = "unknown"
    table_role: str = "narrative"
    header_columns: list[str] = field(default_factory=list)
    row_blocks: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StatementTableExtractor:
    def __init__(self, root: Path | None = None):
        self.root = root or get_settings().root_dir
        self.warnings: list[str] = []

    def extract(self, document: ReportDocument) -> dict[str, Any]:
        self.warnings = []
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
                for page_index, page in enumerate(pdf.pages, start=1):
                    page_text = page.extract_text() or ""
                    raw_tables = page.extract_tables() or []
                    expected_primary_pages.update(extract_expected_primary_pages_from_toc(page_text, document.reporting_standard))
                    for raw_table in raw_tables:
                        table = self._table_from_rows(
                            document,
                            raw_table,
                            len(tables),
                            page_index,
                            page_text,
                            extraction_method="pdf_table",
                        )
                        tables.append(table)
                    page_statement_tables = [table.to_dict() for table in tables if table.page_number == page_index]
                    if raw_tables and should_add_primary_income_text_fallback(
                        page_text,
                        page_statement_tables,
                        document.reporting_standard,
                    ):
                        fallback = self._text_fallback_table(document, page_text, len(tables), page_index)
                        if fallback:
                            tables.append(fallback)
                    if not raw_tables:
                        fallback = self._text_fallback_table(document, page_text, len(tables), page_index)
                        if fallback:
                            tables.append(fallback)
                        elif page_index in expected_primary_pages and not page_has_extractable_content(page):
                            self.warnings.append(
                                "primary_statement_page_image_only_or_no_extractable_text:"
                                f"page={page_index}:statement_type={expected_primary_pages[page_index]}"
                            )
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
        table_title: str | None = None,
        extraction_method: str = "unknown",
    ) -> ExtractedStatementTable:
        clean_rows = [[_clean_cell(cell) for cell in row] for row in raw_rows if any(_clean_cell(cell) for cell in row)]
        if extraction_method == "pdf_table":
            columns, records, diagnostics = normalize_pdf_table_rows(clean_rows, nearby_text)
            active_columns = columns
        else:
            columns = clean_rows[0] if clean_rows else []
            active_columns = unique_column_names(columns)
            data_rows = clean_rows[1:] if len(clean_rows) > 1 else []
            records = [dict(zip(active_columns, row, strict=False)) for row in data_rows]
            diagnostics = {
                "header_row_count": 1 if columns else 0,
                "normalized_columns": active_columns,
                "repeated_column_names_detected": False,
                "label_column_present": bool(records and "line" in records[0]),
                "label_column_reconstructed": False,
                "table_structure_quality": "standard",
            }
        text = " ".join(
            [normalize_financial_text(" ".join(columns))]
            + [" ".join(str(value or "") for value in row.values()) for row in records[:5]]
            + [nearby_text, table_title or ""]
        )
        statement_family = classify_statement_family(text, document.reporting_standard)
        statement_type = statement_family
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
                and statement_family in {"balance_sheet", "income_statement", "cash_flow"}
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
            source_location={"page": page_number, "table_index": index},
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
            period_warnings=list(period_resolution.period_warnings),
            statement_family=statement_family,
            table_role=table_role,
            header_columns=active_columns,
            row_blocks=row_blocks,
            diagnostics=combined_diagnostics,
        )

    def _text_fallback_table(
        self,
        document: ReportDocument,
        page_text: str,
        index: int,
        page_number: int,
    ) -> ExtractedStatementTable | None:
        statement_family = classify_primary_statement_page(page_text, document.reporting_standard)
        statement_type = statement_family
        if statement_type == "unknown":
            return None
        rows = text_lines_to_rows(page_text)
        if len(rows) < 2:
            return None
        title = guess_title(page_text)
        records = [{"line": row[0]} for row in rows[1:]]
        period_resolution = resolve_report_period(
            title_text="\n".join(filter(None, [page_text, title or ""])),
            headers=[],
            report_period=document.report_period,
            filename_hint=document.file_name,
        )
        effective_period = period_resolution.effective_report_period or document.report_period
        row_blocks, row_diagnostics = build_normalized_row_blocks(
            records=records,
            columns=["line"],
            statement_family=statement_family,
        )
        table_role = classify_table_role(statement_family, "degraded", "text_table_fallback")
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
            unit=detect_unit(page_text),
            currency=detect_currency(page_text),
            unit_multiplier=detect_unit_multiplier(page_text),
            columns=["line"],
            rows=records,
            dataframe_json={"orientation": "records", "data": records},
            source_location={"page": page_number, "table_index": index, "extraction_method": "text_table_fallback"},
            confidence_score=0.65,
            extraction_method="text_table_fallback",
            quality_flag="raw_text_table",
            eligible_for_fact_normalization=False,
            warnings=["text_table_fallback_used"],
            effective_period=period_resolution.effective_report_period,
            comparative_period=period_resolution.comparative_period,
            period_source=period_resolution.period_source,
            period_confidence=round(period_resolution.period_confidence, 4),
            period_warnings=list(period_resolution.period_warnings),
            statement_family=statement_family,
            table_role=table_role,
            header_columns=["line"],
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
                **row_diagnostics,
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
    return "unknown"


def should_add_primary_income_text_fallback(
    page_text: str,
    page_tables: list[dict[str, Any]],
    reporting_standard: str,
) -> bool:
    if reporting_standard.upper() != "IFRS":
        return False
    normalized = normalize_matching_text(page_text)
    if is_toc_page(normalized) or is_notes_page(normalized) or is_auditor_page(normalized):
        return False
    if classify_primary_statement_page(page_text, reporting_standard) != "income_statement":
        return False
    if any(_table_has_income_fact_rows(table) for table in page_tables):
        return False
    return any(marker in normalized for marker in PRIMARY_INCOME_STATEMENT_MARKERS)


PRIMARY_INCOME_STATEMENT_MARKERS = [
    "consolidated statement of profit or loss",
    "consolidated statement of profit or loss and other comprehensive income",
    "interim condensed consolidated statement of profit or loss",
    "statement of comprehensive income",
    "обобщенный консолидированный отчет о прибылях и убытках",
    "консолидированный отчет о прибылях и убытках",
    "отчет о прибылях и убытках",
]


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


def extract_expected_primary_pages_from_toc(text: str, reporting_standard: str) -> dict[int, str]:
    if reporting_standard.upper() != "IFRS":
        return {}
    normalized = normalize_matching_text(text)
    if not is_toc_page(normalized):
        return {}
    expected: dict[int, str] = {}
    for line in (text or "").splitlines():
        line_normalized = " ".join(line.casefold().split())
        match = re.search(r"(\d{1,3})\s*$", line_normalized)
        if not match:
            continue
        page_number = int(match.group(1))
        if contains_normalized_marker(line_normalized, "balance sheet", "statement of financial position"):
            expected[page_number] = "balance_sheet"
        elif contains_normalized_marker(line_normalized, "statement of comprehensive income", "statement of profit or loss"):
            expected[page_number] = "income_statement"
        elif contains_normalized_marker(line_normalized, "statement of cash flows"):
            expected[page_number] = "cash_flow"
    return expected


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
    for statement_type in ["balance_sheet", "income_statement", "cash_flow"]:
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
    return [statement_type for statement_type in ["balance_sheet", "income_statement"] if not coverage[statement_type]["found"]]


def period_type_for_statement(statement_type: str, period: str) -> str:
    if statement_type == "balance_sheet":
        return "balance_sheet_snapshot"
    if period.endswith("Q4"):
        return "annual"
    if statement_type in {"income_statement", "cash_flow"}:
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
        label_parts = [
            normalize_financial_text(cell)
            for index, cell in enumerate(row[:first_data_index])
            if index not in note_indices and cell and parseable_number(cell) is None
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
        note_ref = next((normalize_financial_text(row[index]) for index in sorted(note_indices) if row[index]), None)
        if note_ref:
            record["note_ref"] = note_ref
        if record:
            records.append(record)
    records = stitch_primary_statement_rows(records, active_columns)
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


def detect_pdf_header_row_count(rows: list[list[str]]) -> int:
    max_headers = min(3, len(rows))
    header_count = 0
    for row in rows[:max_headers]:
        text_cells = [cell for cell in row if cell and parseable_number(cell) is None]
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
                "note_ref": row.get("note_ref"),
                "value_cells": value_cells,
                "row_kind": row_kind,
                "stitched_from_rows": row.get("stitched_from_rows") or [],
                "row_confidence": row_confidence_for_kind(row_kind, row),
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
    if normalized.startswith(("примечание", "note ")) or normalized.startswith("*"):
        return "footnote"
    if row.get("note_ref") and not value_cells:
        return "note_reference_only"
    if any(marker in normalized for marker in ("итого", "total")):
        if any(marker in normalized for marker in ("актив", "обяз", "капитал", "equity", "liabilit", "assets")):
            return "grand_total"
        return "subtotal"
    if statement_family == "cash_flow" and any(
        marker in normalized
        for marker in (
            "операционной деятельности",
            "инвестиционной деятельности",
            "финансовой деятельности",
            "operating activities",
            "investing activities",
            "financing activities",
        )
    ):
        return "section_title" if not value_cells else "statement_line_item"
    if value_cells:
        return "statement_line_item"
    if any(marker in normalized for marker in ("активы", "обязательства", "капитал", "equity", "assets", "liabilities")):
        return "section_title"
    return "unknown"


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
