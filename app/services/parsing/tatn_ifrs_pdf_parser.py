import re

from app.db.models import ReportDocument, StatementFact
from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser

TATN_STRONG_LABELS = {
    "revenue": [
        r"^sales and other operating revenues on non-banking activities, net$",
        r"^sales and other operating revenues",
    ],
    "operating_profit": [r"^operating profit on non-banking activities$", r"^operating profit$"],
    "net_income": [r"^profit for the period$"],
    "operating_cash_flow": [r"^net cash provided by operating activities$"],
    "total_short_term_debt": [r"^total short-term debt$"],
    "current_portion_of_long_term_debt": [r"^current portion of long-term debt$"],
    "short_term_debt_including_current_portion": [
        r"^total short-term debt, including current portion of long-term debt$"
    ],
    "total_long_term_debt_gross": [r"^total long-term debt$"],
    "current_portion_deduction": [r"^less: current portion$"],
    "long_term_debt_net_of_current_portion": [r"^total long-term debt, net of current portion$"],
}

TATN_DEBT_COMPONENTS = {
    "total_short_term_debt",
    "current_portion_of_long_term_debt",
    "short_term_debt_including_current_portion",
    "total_long_term_debt_gross",
    "current_portion_deduction",
    "long_term_debt_net_of_current_portion",
}


class TATNIFRSPDFParser(LKOHIFRSPDFParser):
    def can_parse(self, document: ReportDocument) -> bool:
        return (
            document.company is not None
            and document.company.ticker.upper() == "TATN"
            and document.source_type in {"issuer_ir", "issuer_ir_manifest"}
            and document.reporting_standard.upper() == "IFRS"
            and bool(document.storage_path and document.storage_path.lower().endswith(".pdf"))
        )

    def _statement_title(self, text: str) -> str | None:
        for line in text.splitlines()[:12]:
            normalized = " ".join(line.casefold().split())
            if "consolidated" in normalized and (
                "statement" in normalized or "financial position" in normalized or "cash flows" in normalized
            ):
                return line.strip()
        return None

    def _extract_from_statement_text(self, document: ReportDocument, text: str, page_number: int) -> list[StatementFact]:
        context = self._tatn_context(text)
        if not context:
            return []
        out: list[StatementFact] = []
        lines = text.splitlines()
        for line_number, line in enumerate(lines, start=1):
            joined_line = self._join_wrapped_line(line, lines[line_number] if line_number < len(lines) else "")
            label, metric_code = self._strong_match(joined_line)
            if not metric_code or not self._metric_allowed_in_context(metric_code, context):
                continue
            value = self._current_period_value(joined_line)
            warnings = []
            if value is None:
                warnings.append("No numeric current-period value found")
            confidence = 0.85 if value is not None else 0.65
            self.candidate_rows.append(
                {
                    "table_index": "statement_text",
                    "page": page_number,
                    "raw_label": label or joined_line[:200],
                    "raw_values": [str(item) for item in self._extract_numbers(joined_line)],
                    "matched_metric_code": metric_code,
                    "match_reason": "tatn_context_strong_label_match",
                    "confidence_score": confidence,
                    "warnings": warnings,
                    "context": context,
                }
            )
            if value is None:
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
                        "table_title": context,
                        "period_type": period_type,
                        "ytd_months": ytd_months,
                    },
                    quality_flag="exact",
                    confidence_score=confidence,
                )
            )
        return out

    def _strong_match(self, line: str) -> tuple[str | None, str | None]:
        label = self._label_part(line)
        normalized = " ".join(label.casefold().split())
        for metric_code, patterns in TATN_STRONG_LABELS.items():
            if any(re.search(pattern, normalized) for pattern in patterns):
                return label, metric_code
        return None, None

    def _tatn_context(self, text: str) -> str | None:
        head = " ".join(text.splitlines()[:6]).casefold()
        if "statement of profit or loss" in head:
            return "consolidated_statement_of_profit_or_loss"
        if "statement of cash flows" in head:
            return "consolidated_statement_of_cash_flows"
        if "note 12: debt" in text.casefold():
            return "debt_note"
        return None

    def _metric_allowed_in_context(self, metric_code: str, context: str) -> bool:
        allowed = {
            "consolidated_statement_of_profit_or_loss": {"revenue", "operating_profit", "net_income"},
            "consolidated_statement_of_cash_flows": {"operating_cash_flow"},
            "debt_note": TATN_DEBT_COMPONENTS,
        }
        return metric_code in allowed.get(context, set())

    def _period_semantics(self, document: ReportDocument, metric_code: str) -> tuple[str, int | None]:
        if metric_code in TATN_DEBT_COMPONENTS:
            return "balance_sheet_snapshot", None
        return super()._period_semantics(document, metric_code)

    def _derive_total_debt(self, document: ReportDocument, facts: list[StatementFact]) -> list[StatementFact]:
        existing = [fact for fact in facts if fact.metric_code == "total_debt" and fact.value is not None]
        if existing:
            return []
        components = {fact.metric_code: fact for fact in facts if fact.value is not None}
        primary = (
            components.get("short_term_debt_including_current_portion"),
            components.get("long_term_debt_net_of_current_portion"),
            "short_term_debt_including_current_portion + long_term_debt_net_of_current_portion",
        )
        fallback = (
            components.get("total_short_term_debt"),
            components.get("total_long_term_debt_gross"),
            "total_short_term_debt + total_long_term_debt_gross",
        )
        left, right, formula = primary if primary[0] and primary[1] else fallback
        if not left or not right:
            return []
        inputs = {
            left.metric_code: {"value": left.value, "source_location": left.source_location},
            right.metric_code: {"value": right.value, "source_location": right.source_location},
        }
        return [
            StatementFact(
                company_id=document.company_id,
                report_document_id=document.id,
                period=document.report_period,
                reporting_standard=document.reporting_standard,
                statement_type="balance_sheet",
                metric_code="total_debt",
                metric_name_original="Derived total debt",
                value=(left.value or 0) + (right.value or 0),
                currency=left.currency,
                unit_multiplier=left.unit_multiplier,
                period_type="balance_sheet_snapshot",
                source_location={
                    "source_type": document.source_type,
                    "source_role": document.source_role,
                    "source_url": document.source_url,
                    "document_id": document.id,
                    "formula": formula,
                    "inputs": inputs,
                    "warnings": [
                        "total_debt derived from debt note components; methodology avoids double-counting current "
                        "portion of long-term debt."
                    ],
                },
                quality_flag="derived",
                confidence_score=min(left.confidence_score or 0.8, right.confidence_score or 0.8),
            )
        ]

    def _join_wrapped_line(self, line: str, next_line: str) -> str:
        if not line.strip():
            return line
        if self._extract_numbers(line):
            return line
        if line.rstrip().endswith("-") or "sales and other operating revenues" in line.casefold():
            return f"{line.rstrip('-').strip()} {next_line.strip()}"
        if "total short-term debt, including current portion of" in line.casefold():
            return f"{line.strip()} {next_line.strip()}"
        if "total long-term debt, net of" in line.casefold():
            return f"{line.strip()} {next_line.strip()}"
        return line
