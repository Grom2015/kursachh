import json
import re
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.models import ReportDocument, StatementFact
from app.services.parsing.audit import write_parse_audit

WEAK_METRIC_PATTERNS = {
    "revenue": [r"revenue"],
    "ebitda": [r"ebitda"],
    "operating_profit": [r"operating profit", r"profit from operations"],
    "net_income": [r"net income", r"profit for the period"],
    "total_assets": [r"total assets"],
    "total_equity": [r"total equity"],
    "total_debt": [r"total debt", r"borrowings"],
    "cash_and_equivalents": [r"cash and cash equivalents"],
    "current_assets": [r"current assets"],
    "current_liabilities": [r"current liabilities"],
    "operating_cash_flow": [r"net cash provided by operating activities", r"operating cash flow"],
    "capex": [r"capital expenditures", r"purchase of property"],
}

STRONG_LABELS = {
    "revenue": [r"^sales \(including excise and export tariffs\)"],
    "net_income": [r"^profit \(loss\) for the period$", r"^profit for the year$"],
    "operating_profit": [r"^profit from operating activities$", r"^operating profit$"],
    "total_assets": [r"^total assets$"],
    "total_equity": [r"^total equity$"],
    "cash_and_equivalents": [r"^cash and cash equivalents$"],
    "current_assets": [r"^total current assets$"],
    "current_liabilities": [r"^total current liabilities$"],
    "operating_cash_flow": [r"^net cash provided by operating activities$"],
    "capex": [r"^capital expenditures$"],
    "short_term_borrowings": [r"^short-term borrowings and current portion of long-term debt$"],
    "long_term_borrowings": [r"^long-term debt$", r"^long-term borrowings$"],
    "total_debt": [r"^total debt$"],
}

BALANCE_SHEET_METRICS = {
    "total_assets",
    "total_equity",
    "cash_and_equivalents",
    "current_assets",
    "current_liabilities",
    "total_debt",
    "short_term_borrowings",
    "long_term_borrowings",
}


class LKOHIFRSPDFParser:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.candidate_rows: list[dict[str, Any]] = []

    def can_parse(self, document: ReportDocument) -> bool:
        return (
            document.source_type in {"issuer_ir", "issuer_ir_manifest"}
            and document.reporting_standard.upper() == "IFRS"
            and bool(document.storage_path and document.storage_path.lower().endswith(".pdf"))
        )

    def parse(self, document: ReportDocument) -> list[StatementFact]:
        self.warnings = []
        self.candidate_rows = []
        try:
            import pdfplumber  # type: ignore
        except ImportError:
            self.warnings.append("pdfplumber is not installed; LKOH PDF facts not extracted")
            write_parse_audit(document, self.__class__.__name__, 0, [], self.warnings)
            return []

        facts: list[StatementFact] = []
        tables_found = 0
        try:
            with pdfplumber.open(document.storage_path) as pdf:
                for page_number, page in enumerate(pdf.pages, start=1):
                    tables = page.extract_tables() or []
                    tables_found += len(tables)
                    text = page.extract_text() or ""
                    if document.source_role == "financial_statements":
                        facts.extend(self._extract_from_statement_text(document, text, page_number))
                    elif document.source_role == "press_release":
                        self.warnings.append(
                            f"Press release document {document.id} excluded from canonical LKOH financial statement extraction"
                        )
                    else:
                        facts.extend(self._extract_weak_from_text(document, text, page_number))
        except Exception as exc:
            self.warnings.append(f"LKOH PDF parsing failed: {exc}")
        facts = self._dedupe(facts)
        facts.extend(self._derive_total_debt(document, facts))
        if not facts:
            self.warnings.append("No LKOH IFRS facts extracted; no values invented")
        write_parse_audit(document, self.__class__.__name__, tables_found, facts, self.warnings)
        self._write_table_debug(document, tables_found)
        return facts

    def _extract_from_statement_text(self, document: ReportDocument, text: str, page_number: int) -> list[StatementFact]:
        out: list[StatementFact] = []
        table_title = self._statement_title(text)
        for line_number, line in enumerate(text.splitlines(), start=1):
            label, metric_code = self._strong_match(line)
            if not metric_code:
                continue
            value = self._current_period_value(line)
            warnings = []
            if value is None:
                warnings.append("No numeric current-period value found")
            if not table_title:
                warnings.append("Line matched outside a primary consolidated statement page")
            confidence = 0.85 if value is not None and table_title else 0.65
            self.candidate_rows.append(
                {
                    "table_index": "statement_text",
                    "page": page_number,
                    "raw_label": label or line[:200],
                    "raw_values": [str(item) for item in self._extract_numbers(line)],
                    "matched_metric_code": metric_code,
                    "match_reason": "exact_label_match" if label else "no_match",
                    "confidence_score": confidence,
                    "warnings": warnings,
                }
            )
            if value is None or not table_title:
                continue
            period_type, ytd_months = self._period_semantics(document, metric_code)
            out.append(
                StatementFact(
                    company_id=document.company_id,
                    report_document_id=document.id,
                    period=document.report_period,
                    reporting_standard=document.reporting_standard,
                    statement_type=self._statement_type(metric_code),
                    metric_code=metric_code,
                    metric_name_original=label,
                    value=value,
                    currency="RUB",
                    unit_multiplier=1_000_000,
                    period_type=period_type,
                    source_location={
                        "source_type": document.source_type,
                        "source_role": document.source_role,
                        "source_url": document.source_url,
                        "document_id": document.id,
                        "page": page_number,
                        "table": "statement_text",
                        "line": line_number,
                        "raw_label": label,
                        "table_title": table_title,
                        "period_type": period_type,
                        "ytd_months": ytd_months,
                    },
                    quality_flag="exact" if confidence >= 0.8 else "low_confidence_parse",
                    confidence_score=confidence,
                )
            )
        return out

    def _extract_weak_from_text(
        self, document: ReportDocument, text: str, page_number: int, table_index: int | None = None
    ) -> list[StatementFact]:
        out: list[StatementFact] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            normalized = " ".join(line.casefold().split())
            for metric_code, patterns in WEAK_METRIC_PATTERNS.items():
                if any(re.search(pattern, normalized) for pattern in patterns):
                    value = self._current_period_value(line)
                    if value is None:
                        continue
                    out.append(
                        StatementFact(
                            company_id=document.company_id,
                            report_document_id=document.id,
                            period=document.report_period,
                            reporting_standard=document.reporting_standard,
                            statement_type=self._statement_type(metric_code),
                            metric_code=metric_code,
                            metric_name_original=line[:512],
                            value=value,
                            currency="RUB",
                            unit_multiplier=1_000_000,
                            period_type="quarter",
                            source_location={
                                "source_type": document.source_type,
                                "source_role": document.source_role,
                                "source_url": document.source_url,
                                "document_id": document.id,
                                "page": page_number,
                                "table": table_index,
                                "line": line_number,
                            },
                            quality_flag="low_confidence_parse",
                            confidence_score=0.6,
                        )
                    )
        return out

    def _extract_from_text(
        self, document: ReportDocument, text: str, page_number: int, table_index: int | None = None
    ) -> list[StatementFact]:
        return self._extract_weak_from_text(document, text, page_number, table_index)

    def _strong_match(self, line: str) -> tuple[str | None, str | None]:
        label = self._label_part(line)
        normalized = " ".join(label.casefold().split())
        for metric_code, patterns in STRONG_LABELS.items():
            if any(re.search(pattern, normalized) for pattern in patterns):
                return label, metric_code
        return None, None

    def _label_part(self, line: str) -> str:
        return re.split(r"\s[-(]?\d", line, maxsplit=1)[0].strip()

    def _extract_numbers(self, line: str) -> list[float]:
        matches = re.findall(r"\(\d{1,3}(?:,\d{3})+(?:\.\d+)?\)|-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?", line)
        values = []
        for match in matches:
            raw = match.replace(" ", "").replace(",", "")
            negative = raw.startswith("(") and raw.endswith(")")
            raw = raw.strip("()")
            try:
                value = float(raw)
            except ValueError:
                continue
            values.append(-value if negative else value)
        return values

    def _current_period_value(self, line: str) -> float | None:
        values = self._extract_numbers(line)
        if len(values) >= 2:
            return values[-2]
        if values:
            return values[0]
        return None

    def _statement_title(self, text: str) -> str | None:
        for line in text.splitlines()[:8]:
            if line.startswith("Consolidated Statement"):
                return line.strip()
        return None

    def _period_semantics(self, document: ReportDocument, metric_code: str) -> tuple[str, int | None]:
        if metric_code in BALANCE_SHEET_METRICS:
            return "balance_sheet_snapshot", None
        quarter = int(document.report_period[-1])
        months = quarter * 3
        if months == 12:
            return "annual", 12
        return "ytd", months

    def _extract_number(self, line: str) -> float | None:
        values = self._extract_numbers(line)
        if not values:
            return None
        return values[-1]

    def _write_table_debug(self, document: ReportDocument, tables_found: int) -> Path:
        settings = get_settings()
        root = settings.root_dir / settings.report_parsed_dir / document.company.ticker.upper() / document.report_period
        path = root / f"{document.id}_table_debug.json"
        parsed_root = (settings.root_dir / settings.report_parsed_dir).resolve()
        path = path.resolve()
        if not path.is_relative_to(parsed_root):
            raise ValueError("Table debug path escapes parsed directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "document_id": document.id,
            "period": document.report_period,
            "source_role": document.source_role,
            "tables_found": tables_found,
            "candidate_rows": self.candidate_rows,
            "warnings": self.warnings,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _legacy_extract_number(self, line: str) -> float | None:
        matches = re.findall(r"[-(]?\d[\d\s,]*(?:\.\d+)?\)?", line)
        if not matches:
            return None
        raw = matches[-1].replace(" ", "").replace(",", "")
        negative = raw.startswith("(") and raw.endswith(")")
        raw = raw.strip("()")
        try:
            value = float(raw)
        except ValueError:
            return None
        return -value if negative else value

    def _statement_type(self, metric_code: str) -> str:
        if metric_code in {"revenue", "ebitda", "operating_profit", "net_income"}:
            return "income_statement"
        if metric_code in {"operating_cash_flow", "capex"}:
            return "cash_flow"
        return "balance_sheet"

    def _derive_total_debt(self, document: ReportDocument, facts: list[StatementFact]) -> list[StatementFact]:
        existing = [fact for fact in facts if fact.metric_code == "total_debt" and fact.value is not None]
        if existing:
            return []
        short = next((fact for fact in facts if fact.metric_code == "short_term_borrowings" and fact.value is not None), None)
        long = next((fact for fact in facts if fact.metric_code == "long_term_borrowings" and fact.value is not None), None)
        if not short or not long:
            return []
        return [
            StatementFact(
                company_id=document.company_id,
                report_document_id=document.id,
                period=document.report_period,
                reporting_standard=document.reporting_standard,
                statement_type="balance_sheet",
                metric_code="total_debt",
                metric_name_original="Derived total debt",
                value=(short.value or 0) + (long.value or 0),
                currency=short.currency,
                unit_multiplier=short.unit_multiplier,
                period_type="balance_sheet_snapshot",
                source_location={
                    "source_type": document.source_type,
                    "source_role": document.source_role,
                    "source_url": document.source_url,
                    "document_id": document.id,
                    "formula": "short_term_borrowings + long_term_borrowings",
                    "inputs": {
                        "short_term_borrowings": {
                            "value": short.value,
                            "source_location": short.source_location,
                        },
                        "long_term_borrowings": {
                            "value": long.value,
                            "source_location": long.source_location,
                        },
                    },
                    "warnings": [
                        "total_debt derived from borrowings components; methodology may differ from issuer-defined debt."
                    ],
                },
                quality_flag="derived",
                confidence_score=min(short.confidence_score or 0.8, long.confidence_score or 0.8),
            )
        ]

    def _dedupe(self, facts: list[StatementFact]) -> list[StatementFact]:
        seen: set[tuple[str, float | None]] = set()
        out: list[StatementFact] = []
        for fact in facts:
            key = (fact.metric_code, fact.value)
            if key not in seen:
                seen.add(key)
                out.append(fact)
        return out
