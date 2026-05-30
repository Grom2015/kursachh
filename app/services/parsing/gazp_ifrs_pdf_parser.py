import re

from app.db.models import ReportDocument, StatementFact
from app.services.parsing.ifrs.candidate_scorer import score_candidate
from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser

GAZP_LABELS = {
    "revenue": [r"^total sales in the consolidated(?: interim condensed)? statement of comprehensive income$"],
    "total_assets": [r"^total assets in the consolidated(?: interim condensed)? balance sheet$"],
    "cash_and_equivalents": [r"^total cash and cash equivalents$"],
    "capex": [r"^capital expenditures1$"],
    "short_term_borrowings": [
        r"^short-term borrowings, promissory notes and current portion of long-term borrowings$"
    ],
    "long_term_borrowings": [r"^long-term borrowings, promissory notes$"],
    "operating_cash_flow": [r"^net cash from operating activities$"],
    "total_debt": [r"^total debt$"],
}

GAZP_BALANCE_SHEET_METRICS = {
    "total_assets",
    "cash_and_equivalents",
    "short_term_borrowings",
    "long_term_borrowings",
    "total_debt",
}


class GAZPIFRSPDFParser(LKOHIFRSPDFParser):
    def can_parse(self, document: ReportDocument) -> bool:
        return (
            document.company is not None
            and document.company.ticker.upper() == "GAZP"
            and document.source_type in {"issuer_ir", "issuer_ir_manifest"}
            and document.reporting_standard.upper() == "IFRS"
            and bool(document.storage_path and document.storage_path.lower().endswith(".pdf"))
        )

    def _extract_from_statement_text(self, document: ReportDocument, text: str, page_number: int) -> list[StatementFact]:
        context = self._gazp_context(text)
        if not context:
            return []
        out: list[StatementFact] = []
        lines = text.splitlines()
        for index, line in enumerate(lines):
            joined_line = self._join_gazp_line(line, lines[index : index + 3])
            label, metric_code = self._strong_match(joined_line)
            if not metric_code or not self._metric_allowed_in_context(metric_code, context, joined_line):
                continue
            value = self._gazp_value(metric_code, joined_line)
            warnings = []
            if value is None:
                warnings.append("No numeric current-period value found")
            confidence = 0.9 if value is not None else 0.65
            period_type, ytd_months = self._period_semantics(document, metric_code)
            semantic_score = score_candidate(
                label or joined_line[:200],
                context,
                period_type,
                unit_detected=True,
                currency_detected=True,
                source_location_exists=True,
                ticker="GAZP",
            )
            self.candidate_rows.append(
                {
                    "table_index": "statement_text",
                    "page": page_number,
                    "raw_label": label or joined_line[:200],
                    "raw_values": [str(item) for item in self._extract_numbers(joined_line)],
                    "matched_metric_code": metric_code,
                    "ifrs_concept_code": semantic_score.concept_code,
                    "match_reason": "gazp_context_strong_label_match",
                    "statement_context": context,
                    "period_column_detected": True,
                    "period_coverage": "FY" if ytd_months == 12 else f"{ytd_months}M" if ytd_months else "as_at_date",
                    "candidate_score_breakdown": semantic_score.score_breakdown,
                    "applied_issuer_override": semantic_score.applied_issuer_override,
                    "rejection_reason": semantic_score.rejection_reason,
                    "confidence_score": min(confidence, semantic_score.confidence_score) if semantic_score.concept_code else 0.65,
                    "warnings": warnings,
                    "context": context,
                }
            )
            if value is None:
                continue
            confidence = min(confidence, semantic_score.confidence_score) if semantic_score.concept_code else confidence
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
                        "line": index + 1,
                        "raw_label": label,
                        "ifrs_concept_code": semantic_score.concept_code,
                        "table_title": context,
                        "period_type": period_type,
                        "ytd_months": ytd_months,
                        "candidate_score_breakdown": semantic_score.score_breakdown,
                        "applied_issuer_override": semantic_score.applied_issuer_override,
                    },
                    quality_flag="exact",
                    confidence_score=confidence,
                )
            )
        return out

    def _strong_match(self, line: str) -> tuple[str | None, str | None]:
        label = self._label_part(line)
        normalized = " ".join(label.casefold().split())
        for metric_code, patterns in GAZP_LABELS.items():
            if any(re.search(pattern, normalized) for pattern in patterns):
                return label, metric_code
        return None, None

    def _label_part(self, line: str) -> str:
        line = re.sub(r"^\s*\d{1,2}\s+", "", line)
        return super()._label_part(line)

    def _gazp_context(self, text: str) -> str | None:
        normalized = " ".join(text.casefold().split())
        if "segment information" in normalized:
            return "segment_information_note"
        if "cash and cash equivalents" in normalized:
            return "cash_and_cash_equivalents_note"
        if "long-term borrowings, promissory notes" in normalized:
            return "long_term_borrowings_note"
        if "net cash from operating activities" in normalized:
            return "net_cash_from_operating_activities_note"
        if "financial risk factors" in normalized and "total debt" in normalized:
            return "financial_risk_factors_note"
        return None

    def _metric_allowed_in_context(self, metric_code: str, context: str, line: str) -> bool:
        allowed = {
            "segment_information_note": {
                "revenue",
                "capex",
                "total_assets",
                "short_term_borrowings",
                "long_term_borrowings",
            },
            "cash_and_cash_equivalents_note": {"cash_and_equivalents"},
            "long_term_borrowings_note": set(),
            "net_cash_from_operating_activities_note": {"operating_cash_flow"},
            "financial_risk_factors_note": {"total_debt"},
        }
        if metric_code == "capex" and "capital expenditures2" in line.casefold():
            return False
        return metric_code in allowed.get(context, set())

    def _period_semantics(self, document: ReportDocument, metric_code: str) -> tuple[str, int | None]:
        if metric_code in GAZP_BALANCE_SHEET_METRICS:
            return "balance_sheet_snapshot", None
        quarter = int(document.report_period[-1])
        months = quarter * 3
        if months == 12:
            return "annual", 12
        return "ytd", months

    def _join_gazp_line(self, line: str, lines: list[str]) -> str:
        normalized = " ".join(line.casefold().split())
        if self._extract_numbers(line):
            return line
        if (
            normalized.startswith("total sales in the consolidated")
            or normalized.startswith("total assets in the consolidated")
            or normalized.startswith("total short-term borrowings")
        ):
            selected = [line.strip()]
            for next_line in lines[1:]:
                if next_line.strip():
                    selected.append(next_line.strip())
                if self._extract_numbers(next_line):
                    break
            return " ".join(selected)
        return line

    def _gazp_value(self, metric_code: str, line: str) -> float | None:
        values = self._extract_numbers(line)
        if not values:
            return None
        if metric_code == "capex":
            return values[-1]
        if len(values) < 2:
            return None
        return self._current_period_value(line)

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
                        "total_debt derived from Gazprom borrowings components; methodology excludes lease liabilities."
                    ],
                },
                quality_flag="derived",
                confidence_score=min(short.confidence_score or 0.8, long.confidence_score or 0.8),
            )
        ]
