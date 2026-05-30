import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, ReportDocument, StatementFact
from app.services.parsing.text_fallback_semantic_gate import (
    FALLBACK_EXTRACTION_METHOD,
    evaluate_text_fallback_table,
)
from app.services.parsing.text_normalization import normalize_financial_text, normalize_matching_text
from app.services.periods import period_or_year_in_range
from app.services.sectors.banking_policy import (
    BANKING_FACT_CODES,
    BANKING_KEY_FACT_CODES,
    is_banking_ticker,
    is_financial_non_bank_ticker,
)


@dataclass
class StatementFactCandidate:
    company_ticker: str
    period: str
    reporting_standard: str
    metric_code: str
    value: float
    currency: str | None
    unit_multiplier: float
    period_type: str
    source_document_id: int
    source_table_type: str
    source_table_index: int
    source_location: dict[str, Any]
    raw_label: str
    raw_value: str
    confidence_score: float
    quality_flag: str
    extraction_method: str = "dataframe_statement_parser"
    warnings: list[str] = field(default_factory=list)
    eligible_for_metric_engine: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RejectedStatementCandidate:
    document_id: int | None
    period: str | None
    statement_type: str | None
    table_index: int | None
    raw_label: str | None
    metric_code: str | None
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DataFrameStatementParseResult:
    company_ticker: str
    period_from: str
    period_to: str
    reporting_standard: str
    documents_processed: int
    tables_processed: int
    canonical_facts_created: int
    facts: list[StatementFactCandidate]
    missing_expected_facts: list[str]
    rejected_candidates: list[RejectedStatementCandidate]
    warnings: list[str]
    status: str
    text_fallback_semantic_gate_enabled: bool = False
    text_fallback_tables_considered: int = 0
    text_fallback_facts_created: int = 0
    banking_parser_quality: dict[str, Any] = field(default_factory=dict)
    structured_facts: list[dict[str, Any]] = field(default_factory=list)
    derived_safe_facts: list[dict[str, Any]] = field(default_factory=list)
    rejected_rows: list[dict[str, Any]] = field(default_factory=list)
    unmapped_numeric_evidence: list[dict[str, Any]] = field(default_factory=list)
    unmapped_table_evidence: list[dict[str, Any]] = field(default_factory=list)
    ratios_summary: dict[str, Any] = field(default_factory=dict)
    analysis_readiness_summary: dict[str, Any] = field(default_factory=dict)
    llm_ready_evidence_pack: dict[str, Any] = field(default_factory=dict)
    structural_blockers: dict[str, Any] = field(default_factory=dict)
    period_resolution: dict[str, Any] = field(default_factory=dict)
    period_blockers: list[str] = field(default_factory=list)
    row_parsing_diagnostics: dict[str, Any] = field(default_factory=dict)
    statement_family_summary: dict[str, Any] = field(default_factory=dict)
    fact_confidence_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["facts"] = [fact.to_dict() for fact in self.facts]
        payload["rejected_candidates"] = [item.to_dict() for item in self.rejected_candidates]
        return payload


class DataFrameStatementParser:
    def __init__(
        self,
        db: Session | None = None,
        root: Path | None = None,
        allow_text_fallback_facts: bool = False,
        allow_text_fallback_semantic_gate: bool = False,
    ):
        self.db = db
        self.root = root or get_settings().root_dir
        self.allow_text_fallback_semantic_gate = allow_text_fallback_semantic_gate or allow_text_fallback_facts

    def parse(
        self,
        company_ticker: str,
        period_from: str,
        period_to: str,
        reporting_standard: str = "IFRS",
        document_ids: list[int] | None = None,
    ) -> DataFrameStatementParseResult:
        company_ticker = company_ticker.upper()
        artifacts = self._artifact_paths(company_ticker, period_from, period_to, document_ids)
        if not artifacts:
            return DataFrameStatementParseResult(
                company_ticker=company_ticker,
                period_from=period_from,
                period_to=period_to,
                reporting_standard=reporting_standard.upper(),
                documents_processed=0,
                tables_processed=0,
                canonical_facts_created=0,
                facts=[],
                missing_expected_facts=EXPECTED_FACTS.copy(),
                rejected_candidates=[],
                warnings=["No statement table artifacts found."],
                status="NO_TABLES",
                text_fallback_semantic_gate_enabled=self.allow_text_fallback_semantic_gate,
                banking_parser_quality=self._banking_parser_quality(company_ticker, [], []),
            )
        facts: list[StatementFactCandidate] = []
        rejected: list[RejectedStatementCandidate] = []
        unmapped_numeric_evidence: list[dict[str, Any]] = []
        unmapped_table_evidence: list[dict[str, Any]] = []
        warnings: list[str] = []
        documents_seen: set[int] = set()
        tables_processed = 0
        text_fallback_tables_considered = 0
        text_fallback_facts_created = 0
        period_resolutions: list[dict[str, Any]] = []
        for artifact in artifacts:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            document_id = int(payload.get("document_id") or 0)
            if document_id:
                documents_seen.add(document_id)
            for table in payload.get("statement_tables", []):
                if (table.get("reporting_standard") or reporting_standard).upper() != reporting_standard.upper():
                    continue
                table_period = table.get("effective_period") or table.get("period")
                if not period_or_year_in_range(table_period, period_from, period_to):
                    continue
                period_resolutions.append(
                    {
                        "effective_period": table.get("effective_period") or table.get("period"),
                        "comparative_period": table.get("comparative_period"),
                        "period_source": table.get("period_source"),
                        "period_confidence": table.get("period_confidence"),
                        "period_warnings": table.get("period_warnings") or [],
                    }
                )
                tables_processed += 1
                if table.get("extraction_method") == "text_table_fallback":
                    text_fallback_tables_considered += 1
                table_facts, table_rejected, table_numeric_evidence, table_unmapped_evidence = self._parse_table(
                    company_ticker,
                    table,
                    document_id,
                    reporting_standard,
                )
                facts.extend(table_facts)
                rejected.extend(table_rejected)
                unmapped_numeric_evidence.extend(table_numeric_evidence)
                unmapped_table_evidence.extend(table_unmapped_evidence)
                text_fallback_facts_created += sum(
                    1 for fact in table_facts if fact.extraction_method == FALLBACK_EXTRACTION_METHOD
                )
        facts = self._dedupe_facts([*facts, *self._derived_banking_facts(facts)])
        missing = missing_expected_facts(facts)
        status = self._status(artifacts, facts, rejected)
        structured_facts = [self._structured_fact_payload(company_ticker, fact) for fact in facts]
        derived_safe_facts = self._derived_safe_facts_payload(company_ticker, facts)
        rejected_rows = [self._rejected_row_payload(company_ticker, item) for item in rejected]
        ratios_summary = self._ratios_summary(company_ticker, period_from, period_to)
        analysis_readiness_summary = self._analysis_readiness_summary(
            company_ticker=company_ticker,
            structured_facts=structured_facts,
            derived_safe_facts=derived_safe_facts,
            rejected_rows=rejected_rows,
            unmapped_numeric_evidence=unmapped_numeric_evidence,
            unmapped_table_evidence=unmapped_table_evidence,
            ratios_summary=ratios_summary,
            missing_expected_facts=missing,
            banking_parser_quality=self._banking_parser_quality(company_ticker, facts, rejected),
            status=status,
        )
        structural_blockers = self._structural_blockers(
            rejected_rows=rejected_rows,
            unmapped_numeric_evidence=unmapped_numeric_evidence,
            unmapped_table_evidence=unmapped_table_evidence,
        )
        llm_ready_evidence_pack = self._llm_ready_evidence_pack(
            structured_facts=structured_facts,
            derived_safe_facts=derived_safe_facts,
            rejected_rows=rejected_rows,
            unmapped_numeric_evidence=unmapped_numeric_evidence,
            unmapped_table_evidence=unmapped_table_evidence,
        )
        period_resolution = self._period_resolution(period_from, period_to, period_resolutions)
        period_blockers = self._period_blockers(period_resolution, rejected_rows)
        row_parsing_diagnostics = self._row_parsing_diagnostics(artifacts)
        statement_family_summary = self._statement_family_summary(artifacts)
        fact_confidence_summary = self._fact_confidence_summary(facts)
        return DataFrameStatementParseResult(
            company_ticker=company_ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard=reporting_standard.upper(),
            documents_processed=len(documents_seen),
            tables_processed=tables_processed,
            canonical_facts_created=len(facts),
            facts=facts,
            missing_expected_facts=missing,
            rejected_candidates=rejected,
            warnings=sorted(set(warnings)),
            status=status,
            text_fallback_semantic_gate_enabled=self.allow_text_fallback_semantic_gate,
            text_fallback_tables_considered=text_fallback_tables_considered,
            text_fallback_facts_created=text_fallback_facts_created,
            banking_parser_quality=self._banking_parser_quality(company_ticker, facts, rejected),
            structured_facts=structured_facts,
            derived_safe_facts=derived_safe_facts,
            rejected_rows=rejected_rows,
            unmapped_numeric_evidence=unmapped_numeric_evidence,
            unmapped_table_evidence=unmapped_table_evidence,
            ratios_summary=ratios_summary,
            analysis_readiness_summary=analysis_readiness_summary,
            llm_ready_evidence_pack=llm_ready_evidence_pack,
            structural_blockers=structural_blockers,
            period_resolution=period_resolution,
            period_blockers=period_blockers,
            row_parsing_diagnostics=row_parsing_diagnostics,
            statement_family_summary=statement_family_summary,
            fact_confidence_summary=fact_confidence_summary,
        )

    def persist(self, result: DataFrameStatementParseResult) -> int:
        if not self.db:
            raise RuntimeError("A database session is required to persist DataFrame statement facts.")
        company = self.db.scalar(select(Company).where(Company.ticker == result.company_ticker))
        if not company:
            raise RuntimeError(f"Company not found: {result.company_ticker}")
        inserted = 0
        for fact in result.facts:
            existing = self.db.scalar(
                select(StatementFact).where(
                    StatementFact.company_id == company.id,
                    StatementFact.report_document_id == fact.source_document_id,
                    StatementFact.period == fact.period,
                    StatementFact.metric_code == fact.metric_code,
                    StatementFact.reporting_standard == fact.reporting_standard,
                )
            )
            if existing and not _is_dataframe_fact(existing):
                continue
            if existing:
                existing.value = fact.value
                existing.metric_name_original = fact.raw_label
                existing.currency = fact.currency
                existing.unit_multiplier = fact.unit_multiplier
                existing.period_type = fact.period_type
                existing.source_location = fact.source_location
                existing.quality_flag = fact.quality_flag
                existing.confidence_score = fact.confidence_score
            else:
                self.db.add(
                    StatementFact(
                        company_id=company.id,
                        report_document_id=fact.source_document_id,
                        period=fact.period,
                        reporting_standard=fact.reporting_standard,
                        statement_type=fact.source_table_type,
                        metric_code=fact.metric_code,
                        metric_name_original=fact.raw_label,
                        value=fact.value,
                        currency=fact.currency,
                        unit_multiplier=fact.unit_multiplier,
                        period_type=fact.period_type,
                        source_location=fact.source_location,
                        quality_flag=fact.quality_flag,
                        confidence_score=fact.confidence_score,
                    )
                )
                inserted += 1
        self.db.commit()
        return inserted

    def _parse_table(
        self,
        company_ticker: str,
        table: dict[str, Any],
        document_id: int,
        reporting_standard: str,
    ) -> tuple[
        list[StatementFactCandidate],
        list[RejectedStatementCandidate],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        facts: list[StatementFactCandidate] = []
        rejected: list[RejectedStatementCandidate] = []
        unmapped_numeric_evidence: list[dict[str, Any]] = []
        unmapped_table_evidence: list[dict[str, Any]] = []
        statement_type = table.get("statement_type")
        table_index = table.get("table_index")
        period = table.get("effective_period") or table.get("period")
        if statement_type not in ALLOWED_BY_STATEMENT:
            classification = classify_note_table(table)
            if classification in {"review_only_note", "narrative_or_non_numeric", "statement_like_note"}:
                unmapped_table_evidence.append(
                    self._table_evidence_payload(
                        table,
                        document_id,
                        evidence_category=table_evidence_category(classification),
                        evidence_type=table_evidence_type(classification),
                        reasons=[f"table_classified_as_{classification}"],
                    )
                )
            return facts, rejected, unmapped_numeric_evidence, unmapped_table_evidence
        if table.get("extraction_method") == "text_table_fallback":
            if not self.allow_text_fallback_semantic_gate:
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        None,
                        None,
                        "text_table_fallback_not_eligible_for_fact_normalization",
                    )
                )
                return facts, rejected, unmapped_numeric_evidence, unmapped_table_evidence
            return self._parse_text_fallback_table(company_ticker, table, document_id, reporting_standard)
        if not traceability_ok(table, document_id):
            rejected.append(
                RejectedStatementCandidate(
                    document_id,
                    period,
                    statement_type,
                    table_index,
                    None,
                    None,
                    "missing_traceability",
                )
            )
            return facts, rejected, unmapped_numeric_evidence, unmapped_table_evidence
        rows = table.get("rows", []) or []
        row_blocks = table.get("row_blocks") or []
        for row_index, row in enumerate(rows):
            row_block = row_blocks[row_index] if row_index < len(row_blocks) else normalized_row_block_fallback(row)
            row_kind = str(row_block.get("row_kind") or "unknown")
            raw_label = first_text_cell(row)
            source_line = row_to_source_line(row)
            if row_kind in {"header", "footnote", "note_reference_only"}:
                continue
            if not raw_label:
                if row_has_numeric_values(row):
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=row,
                            raw_label=None,
                            evidence_category="unknown_concept",
                            evidence_type="unmapped_numeric_row",
                            reasons=["label_column_missing_after_pdf_extraction"],
                        )
                    )
                continue
            if row_kind == "numeric_fragment":
                unmapped_numeric_evidence.append(
                    self._numeric_evidence_payload(
                        table=table,
                        document_id=document_id,
                        row=row,
                        raw_label=raw_label,
                        evidence_category="multi_line_parse_uncertain",
                        evidence_type="ambiguous_numeric_fragment",
                        reasons=["row_kind_numeric_fragment"],
                    )
                )
                continue
            if row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
                if row_has_numeric_values(row):
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=row,
                            raw_label=raw_label,
                            evidence_category="ambiguous_mapping",
                            evidence_type="unmapped_numeric_row",
                            reasons=[f"row_kind_not_statement_line_item:{row_kind}"],
                        )
                    )
                continue
            metric_code, row_policy_reason = self._interpret_statement_row(
                company_ticker=company_ticker,
                statement_type=statement_type,
                row=row,
                row_block=row_block,
                raw_label=raw_label,
            )
            if row_policy_reason:
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        raw_label,
                        metric_code,
                        row_policy_reason,
                        {
                            "row": row,
                            "source_line": source_line,
                            "row_kind": row_kind,
                            "warnings": list(table.get("warnings") or []),
                        },
                    )
                )
                continue
            if not metric_code:
                if row_has_numeric_values(row):
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=row,
                            raw_label=raw_label,
                            evidence_category="unknown_concept",
                            evidence_type="unmapped_numeric_row",
                            reasons=["no_metric_match"],
                        )
                    )
                continue
            sector_rejection = self._sector_policy_rejection_reason(company_ticker, metric_code)
            if sector_rejection:
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        raw_label,
                        metric_code,
                        sector_rejection,
                        {"row": row, "source_line": source_line, "row_kind": row_kind},
                    )
                )
                continue
            raw_value, column_name, reason = select_current_value(row, period, statement_type)
            if reason:
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        raw_label,
                        metric_code,
                        reason,
                        {"row": row, "source_line": source_line, "row_kind": row_kind},
                    )
                )
                continue
            value = parse_number(raw_value)
            if value is None:
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        raw_label,
                        metric_code,
                        "numeric_value_not_detected",
                        {"raw_value": raw_value, "column_name": column_name, "source_line": source_line},
                    )
                )
                continue
            if metric_code == "total_debt" and is_component_debt_label(raw_label):
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        raw_label,
                        metric_code,
                        "debt_component_not_total_debt",
                        {"row": row, "source_line": source_line},
                    )
                )
                continue
            unit_multiplier = table_unit_multiplier(table)
            fact_period = normalized_fact_period(period)
            source_location = {
                "source_document_id": document_id,
                "source_table_type": statement_type,
                "source_table_index": table_index,
                "source_location": table.get("source_location"),
                "column_name": column_name,
                "raw_label": raw_label,
                "raw_value": raw_value,
                "extraction_method": "dataframe_statement_parser",
                "source_table_extraction_method": table.get("extraction_method"),
            }
            facts.append(
                StatementFactCandidate(
                    company_ticker=company_ticker,
                    period=fact_period,
                    reporting_standard=reporting_standard.upper(),
                    metric_code=metric_code,
                    value=value * unit_multiplier,
                    currency=table.get("currency"),
                    unit_multiplier=unit_multiplier,
                    period_type=period_type(statement_type, fact_period),
                    source_document_id=document_id,
                    source_table_type=statement_type,
                    source_table_index=int(table_index or 0),
                    source_location=source_location,
                    raw_label=raw_label,
                    raw_value=str(raw_value),
                    confidence_score=min(
                        0.95,
                        max(
                            0.62,
                            float(row_block.get("row_confidence") or 0.82),
                        ),
                    ),
                    quality_flag="dataframe_exact",
                    warnings=[],
                    eligible_for_metric_engine=True,
                )
            )
        return facts, rejected, unmapped_numeric_evidence, unmapped_table_evidence

    def _parse_text_fallback_table(
        self,
        company_ticker: str,
        table: dict[str, Any],
        document_id: int,
        reporting_standard: str,
    ) -> tuple[
        list[StatementFactCandidate],
        list[RejectedStatementCandidate],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        facts: list[StatementFactCandidate] = []
        rejected: list[RejectedStatementCandidate] = []
        unmapped_numeric_evidence: list[dict[str, Any]] = []
        unmapped_table_evidence: list[dict[str, Any]] = []
        statement_type = table.get("statement_type")
        table_index = table.get("table_index")
        period = table.get("period")
        if document_id and not table.get("document_id"):
            table = {**table, "document_id": document_id}
        result = evaluate_text_fallback_table(table, reporting_standard, company_ticker, period, statement_type)
        for row in result.accepted_rows:
            sector_rejection = self._sector_policy_rejection_reason(company_ticker, row.metric_code)
            if sector_rejection:
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        row.raw_label,
                        row.metric_code,
                        sector_rejection,
                        {
                            "source_line": row.source_location.get("source_line"),
                            "row_text": row.source_location.get("source_text"),
                        },
                    )
                )
                continue
            fact_period = normalized_fact_period(row.period or period)
            facts.append(
                StatementFactCandidate(
                    company_ticker=company_ticker,
                    period=fact_period,
                    reporting_standard=reporting_standard.upper(),
                    metric_code=row.metric_code,
                    value=row.value,
                    currency=row.currency,
                    unit_multiplier=row.unit_multiplier,
                    period_type=period_type(statement_type, fact_period),
                    source_document_id=document_id,
                    source_table_type=statement_type,
                    source_table_index=int(table_index or 0),
                    source_location=row.source_location,
                    raw_label=row.raw_label,
                    raw_value=row.raw_value,
                    confidence_score=row.confidence_score,
                    quality_flag=row.quality_flag,
                    extraction_method=FALLBACK_EXTRACTION_METHOD,
                    warnings=row.warnings,
                    eligible_for_metric_engine=True,
                )
            )
        for item in result.rejected_rows:
            rejected.append(
                RejectedStatementCandidate(
                    document_id,
                    period,
                    statement_type,
                    table_index,
                    item.raw_label,
                    item.metric_code,
                    item.reason,
                    item.details,
                )
            )
        classification = classify_note_table(table)
        if classification in {"review_only_note", "narrative_or_non_numeric", "statement_like_note"} and not facts:
            unmapped_table_evidence.append(
                self._table_evidence_payload(
                    table,
                    document_id,
                    evidence_category=table_evidence_category(classification),
                    evidence_type=table_evidence_type(classification),
                    reasons=[f"table_classified_as_{classification}"],
                )
            )
        return facts, rejected, unmapped_numeric_evidence, unmapped_table_evidence

    def _artifact_paths(
        self,
        company_ticker: str,
        period_from: str,
        period_to: str,
        document_ids: list[int] | None,
    ) -> list[Path]:
        root = self.root / get_settings().report_parsed_dir / company_ticker.upper()
        if not root.exists():
            return []
        paths_by_key: dict[tuple[str, str | None, str | None], Path] = {}
        for path in root.glob("*/*_statement_tables.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            document_id = int(payload.get("document_id") or 0)
            document = self._artifact_document(document_id, company_ticker)
            if not document:
                continue
            if document_ids and document_id not in document_ids:
                continue
            payload_period = (
                (payload.get("period_resolution") or {}).get("effective_report_period")
                or first_table_period(payload)
                or payload.get("period")
            )
            if payload_period and period_or_year_in_range(payload_period, period_from, period_to):
                key = (document.report_period, document.source_url, document.file_hash)
                existing_path = paths_by_key.get(key)
                existing_document_id = 0
                if existing_path:
                    existing_payload = json.loads(existing_path.read_text(encoding="utf-8"))
                    existing_document_id = int(existing_payload.get("document_id") or 0)
                if existing_path is None or document_id > existing_document_id:
                    paths_by_key[key] = path
        return sorted(paths_by_key.values())

    def _artifact_document(self, document_id: int, company_ticker: str) -> ReportDocument | None:
        if not self.db:
            return ReportDocument(id=document_id, report_period="", source_url=None, file_hash=None, source_type="unknown")
        document = self.db.get(ReportDocument, document_id)
        if (
            document
            and document.company
            and document.company.ticker == company_ticker.upper()
            and document.source_type != "fixture"
        ):
            return document
        return None

    def _dedupe_facts(self, facts: list[StatementFactCandidate]) -> list[StatementFactCandidate]:
        selected: dict[tuple[str, str, int], StatementFactCandidate] = {}
        for fact in sorted(facts, key=lambda item: item.confidence_score, reverse=True):
            key = (fact.period, fact.metric_code, fact.source_document_id)
            selected.setdefault(key, fact)
        return sorted(selected.values(), key=lambda item: (item.period, item.metric_code, item.source_document_id))

    def _derived_banking_facts(self, facts: list[StatementFactCandidate]) -> list[StatementFactCandidate]:
        derived: list[StatementFactCandidate] = []
        grouped: dict[tuple[str, int], dict[str, StatementFactCandidate]] = {}
        for fact in facts:
            if fact.metric_code not in BANKING_FACT_CODES:
                continue
            grouped.setdefault((fact.period, fact.source_document_id), {})[fact.metric_code] = fact
        for (period, _document_id), by_code in grouped.items():
            if "customer_accounts" in by_code:
                continue
            retail = by_code.get("retail_customer_accounts")
            corporate = by_code.get("corporate_customer_accounts")
            if not retail or not corporate:
                continue
            source_location = {
                "extraction_method": "dataframe_statement_parser_derived",
                "formula": "retail_customer_accounts + corporate_customer_accounts",
                "inputs": {
                    "retail_customer_accounts": retail.source_location,
                    "corporate_customer_accounts": corporate.source_location,
                },
                "fact_source_kind": "banking_safe_derived_fact",
                "requires_downstream_quality_gate": True,
            }
            derived.append(
                StatementFactCandidate(
                    company_ticker=retail.company_ticker,
                    period=period,
                    reporting_standard=retail.reporting_standard,
                    metric_code="customer_accounts",
                    value=retail.value + corporate.value,
                    currency=retail.currency or corporate.currency,
                    unit_multiplier=1.0,
                    period_type="balance_sheet_snapshot",
                    source_document_id=retail.source_document_id,
                    source_table_type=retail.source_table_type,
                    source_table_index=retail.source_table_index,
                    source_location=source_location,
                    raw_label="Derived customer accounts",
                    raw_value=f"{retail.raw_value} + {corporate.raw_value}",
                    confidence_score=min(retail.confidence_score, corporate.confidence_score, 0.9),
                    quality_flag="high_confidence_derived",
                    extraction_method="dataframe_statement_parser_derived",
                    warnings=["derived_customer_accounts_from_retail_and_corporate_customer_accounts"],
                    eligible_for_metric_engine=True,
                )
            )
        return derived

    def _banking_parser_quality(
        self,
        company_ticker: str,
        facts: list[StatementFactCandidate],
        rejected: list[RejectedStatementCandidate],
    ) -> dict[str, Any]:
        if not is_banking_ticker(company_ticker):
            return {}
        bank_facts = [fact for fact in facts if fact.metric_code in BANKING_FACT_CODES]
        statement_types = {fact.source_table_type for fact in bank_facts}
        periods = {fact.period for fact in bank_facts}
        accepted = [
            {
                "metric_code": fact.metric_code,
                "period": fact.period,
                "value": fact.value,
                "page_number": (fact.source_location or {}).get("page_number"),
                "raw_label": fact.raw_label,
                "raw_value": fact.raw_value,
                "source_line": (fact.source_location or {}).get("source_line"),
                "quality_flag": fact.quality_flag,
                "extraction_method": fact.extraction_method,
                "trust_warning": "manual_upload_candidate_based"
                if fact.extraction_method == FALLBACK_EXTRACTION_METHOD
                else None,
            }
            for fact in bank_facts
        ]
        rejected_bank_rows = [
            item
            for item in rejected
            if item.reason in BANKING_REJECTION_REASONS
            or (item.metric_code in BANKING_FACT_CODES if item.metric_code else False)
        ]
        missing_key_facts = sorted(BANKING_KEY_FACT_CODES - {fact.metric_code for fact in bank_facts})
        primary_balance_sheet_found = "balance_sheet" in statement_types
        primary_income_statement_found = "income_statement" in statement_types
        primary_cash_flow_found = "cash_flow" in statement_types
        current_and_comparative_columns_found = len(periods) >= 2 and any(str(period).endswith("Q4") for period in periods)
        notes_used = any("note" in str(fact.source_table_type or "").casefold() for fact in bank_facts)
        if (
            primary_balance_sheet_found
            and primary_income_statement_found
            and primary_cash_flow_found
            and current_and_comparative_columns_found
            and (len(missing_key_facts) <= 4 or len(bank_facts) >= 10)
        ):
            coverage_grade = "high"
        elif primary_balance_sheet_found and primary_income_statement_found and len(bank_facts) >= 6:
            coverage_grade = "medium"
        else:
            coverage_grade = "low"
        return {
            "sector_profile": "banking",
            "primary_balance_sheet_found": primary_balance_sheet_found,
            "primary_income_statement_found": primary_income_statement_found,
            "primary_cash_flow_found": primary_cash_flow_found,
            "current_and_comparative_columns_found": current_and_comparative_columns_found,
            "notes_used": notes_used,
            "coverage_grade": coverage_grade,
            "accepted_bank_facts_count": len(bank_facts),
            "accepted_bank_facts": sorted(accepted, key=lambda item: (item["period"], item["metric_code"])),
            "rejected_bank_rows_count": len(rejected_bank_rows),
            "rejected_bank_rows": [
                {
                    "reason": item.reason,
                    "metric_code": item.metric_code,
                    "raw_label": item.raw_label,
                    "source_line": item.details.get("source_line"),
                }
                for item in rejected_bank_rows
            ],
            "missing_key_bank_facts": missing_key_facts,
        }

    def _status(
        self,
        artifacts: list[Path],
        facts: list[StatementFactCandidate],
        rejected: list[RejectedStatementCandidate],
    ) -> str:
        if not artifacts:
            return "NO_TABLES"
        if not facts:
            return "NO_FACTS"
        if len(facts) < 5 or len(rejected) > len(facts) * 3:
            return "PARTIAL"
        return "SUCCESS"

    def _structured_fact_payload(self, company_ticker: str, fact: StatementFactCandidate) -> dict[str, Any]:
        location = fact.source_location or {}
        trust_warning = None
        if fact.extraction_method == FALLBACK_EXTRACTION_METHOD:
            trust_warning = "manual_upload_candidate_based"
        elif fact.extraction_method == "dataframe_statement_parser_derived":
            trust_warning = "derived_safe_fact_not_original_statement_line"
        return {
            "metric_code": fact.metric_code,
            "metric_name_original": fact.raw_label,
            "sector_policy": self._sector_policy(company_ticker),
            "statement_type": fact.source_table_type,
            "period": fact.period,
            "period_type": fact.period_type,
            "value": fact.value,
            "currency": fact.currency,
            "unit_multiplier": fact.unit_multiplier,
            "source_document_id": fact.source_document_id,
            "source_table_index": fact.source_table_index,
            "page_number": location.get("page_number") or location.get("page"),
            "source_line": location.get("source_line"),
            "raw_label": fact.raw_label,
            "raw_value": fact.raw_value,
            "confidence_score": fact.confidence_score,
            "quality_flag": fact.quality_flag,
            "source_trust_bucket": self._fact_source_trust_bucket(fact),
            "trust_warning": trust_warning,
            "extraction_method": fact.extraction_method,
        }

    def _derived_safe_facts_payload(
        self,
        company_ticker: str,
        facts: list[StatementFactCandidate],
    ) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for fact in facts:
            if fact.extraction_method != "dataframe_statement_parser_derived":
                continue
            location = fact.source_location or {}
            payload.append(
                {
                    "derived_metric_code": fact.metric_code,
                    "formula": location.get("formula"),
                    "input_facts": location.get("inputs") or {},
                    "value": fact.value,
                    "confidence_score": fact.confidence_score,
                    "derivation_policy": "explicit_allowlisted_safe_derivation",
                    "warning": "derived_safe_fact_not_original_statement_line",
                    "sector_policy": self._sector_policy(company_ticker),
                    "source_trust_bucket": self._fact_source_trust_bucket(fact),
                }
            )
        return payload

    def _rejected_row_payload(self, company_ticker: str, item: RejectedStatementCandidate) -> dict[str, Any]:
        details = item.details or {}
        reason = item.reason
        return {
            "raw_label": item.raw_label,
            "raw_value": details.get("raw_value") or row_to_raw_value(details.get("row")),
            "source_line": details.get("source_line") or row_to_source_line(details.get("row")),
            "statement_type": item.statement_type,
            "page_number": details.get("page_number") or details.get("page"),
            "period_context": item.period,
            "rejection_reason": normalize_rejection_reason(reason),
            "policy_reason": rejection_policy_reason(reason),
            "candidate_metric_code": item.metric_code,
            "source_document_id": item.document_id,
            "source_table_index": item.table_index,
            "extraction_method": details.get("extraction_method") or infer_rejection_extraction_method(reason),
            "source_trust_bucket": self._rejection_source_trust_bucket(reason, details),
            "warnings": details.get("warnings") or [],
        }

    def _numeric_evidence_payload(
        self,
        table: dict[str, Any],
        document_id: int,
        row: dict[str, Any],
        raw_label: str | None,
        evidence_category: str,
        evidence_type: str,
        reasons: list[str],
    ) -> dict[str, Any]:
        return {
            "raw_label": raw_label,
            "raw_values": {str(key): value for key, value in row.items()},
            "numeric_values": extract_numeric_values(row),
            "source_line": row_to_source_line(row),
            "statement_type": table.get("statement_type"),
            "page_number": table.get("page_number"),
            "period_context": table.get("period"),
            "evidence_category": evidence_category,
            "evidence_type": evidence_type,
            "source_document_id": document_id,
            "source_table_index": table.get("table_index"),
            "extraction_method": table.get("extraction_method"),
            "source_trust_bucket": evidence_source_trust_bucket(table),
            "not_confirmed_fact": True,
            "not_for_ratio_calculation": True,
            "warnings": list(table.get("warnings") or []),
            "reasons": reasons,
        }

    def _table_evidence_payload(
        self,
        table: dict[str, Any],
        document_id: int,
        evidence_category: str,
        evidence_type: str,
        reasons: list[str],
    ) -> dict[str, Any]:
        rows = table.get("rows") or []
        classification = classify_note_table(table)
        return {
            "statement_type": table.get("statement_type"),
            "table_title": table.get("table_title"),
            "page_number": table.get("page_number"),
            "rows_sample": rows,
            "columns": table.get("columns") or [],
            "dataframe_json_compact": table.get("dataframe_json"),
            "note_classification": classification,
            "evidence_category": evidence_category,
            "evidence_type": evidence_type,
            "source_document_id": document_id,
            "source_table_index": table.get("table_index"),
            "extraction_method": table.get("extraction_method"),
            "source_trust_bucket": evidence_source_trust_bucket(table),
            "not_confirmed_fact": True,
            "not_for_ratio_calculation": True,
            "warnings": list(table.get("warnings") or []),
            "reasons": reasons,
        }

    def _ratios_summary(self, company_ticker: str, period_from: str, period_to: str) -> dict[str, Any]:
        path = self.root / "data" / "validation" / company_ticker.upper() / f"{period_from}_{period_to}_financial_ratios.json"
        if not path.exists():
            return {"ratios_ready": "none", "ratios_report_available": False, "report_path": None}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"ratios_ready": "none", "ratios_report_available": False, "report_path": None}
        summary = payload.get("summary") or {}
        calculated_count = int(summary.get("calculated_count") or 0)
        missing_count = int(summary.get("missing_count") or 0)
        unsupported_count = int(summary.get("unsupported_count") or 0)
        blocked_count = int(summary.get("blocked_count") or 0)
        if calculated_count and not (missing_count or blocked_count):
            ratios_ready = "full"
        elif calculated_count:
            ratios_ready = "partial"
        else:
            ratios_ready = "none"
        return {
            "calculated_count": calculated_count,
            "missing_count": missing_count,
            "unsupported_count": unsupported_count,
            "blocked_count": blocked_count,
            "report_path": str(path.relative_to(self.root)),
            "ratios_ready": ratios_ready,
            "ratios_report_available": True,
        }

    def _analysis_readiness_summary(
        self,
        *,
        company_ticker: str,
        structured_facts: list[dict[str, Any]],
        derived_safe_facts: list[dict[str, Any]],
        rejected_rows: list[dict[str, Any]],
        unmapped_numeric_evidence: list[dict[str, Any]],
        unmapped_table_evidence: list[dict[str, Any]],
        ratios_summary: dict[str, Any],
        missing_expected_facts: list[str],
        banking_parser_quality: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        missing_key_facts = (
            list(banking_parser_quality.get("missing_key_bank_facts") or [])
            if is_banking_ticker(company_ticker)
            else list(missing_expected_facts)
        )
        facts_ready = bool(structured_facts)
        llm_analysis_ready = (
            True
            if structured_facts
            else "limited_evidence_only"
            if (unmapped_numeric_evidence or unmapped_table_evidence)
            else False
        )
        blocked_areas = sorted(
            {
                item.get("rejection_reason")
                for item in rejected_rows
                if item.get("rejection_reason") in HARD_BLOCKING_REJECTION_REASONS
            }
        )
        if status == "NO_TABLES":
            coverage_grade = "blocked"
        elif facts_ready and len(missing_key_facts) <= max(2, len(structured_facts) // 6):
            coverage_grade = "strong"
        elif facts_ready:
            coverage_grade = "partial"
        elif unmapped_numeric_evidence or unmapped_table_evidence:
            coverage_grade = "weak"
        else:
            coverage_grade = "blocked"
        return {
            "facts_ready": facts_ready,
            "ratios_ready": ratios_summary.get("ratios_ready", "none"),
            "llm_analysis_ready": llm_analysis_ready,
            "structured_facts_count": len(structured_facts),
            "derived_safe_facts_count": len(derived_safe_facts),
            "rejected_rows_count": len(rejected_rows),
            "unmapped_numeric_evidence_count": len(unmapped_numeric_evidence),
            "unmapped_table_evidence_count": len(unmapped_table_evidence),
            "missing_key_facts": missing_key_facts,
            "blocked_areas": blocked_areas,
            "coverage_grade": coverage_grade,
        }

    def _structural_blockers(
        self,
        *,
        rejected_rows: list[dict[str, Any]],
        unmapped_numeric_evidence: list[dict[str, Any]],
        unmapped_table_evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rejected_by_reason: dict[str, int] = {}
        for item in rejected_rows:
            reason = str(item.get("rejection_reason") or "")
            if reason:
                rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1
        numeric_by_reason: dict[str, int] = {}
        for item in unmapped_numeric_evidence:
            for reason in item.get("reasons") or []:
                numeric_by_reason[reason] = numeric_by_reason.get(reason, 0) + 1
        table_by_reason: dict[str, int] = {}
        for item in unmapped_table_evidence:
            for reason in item.get("reasons") or []:
                table_by_reason[reason] = table_by_reason.get(reason, 0) + 1
        return {
            "rejected_rows_by_reason": rejected_by_reason,
            "numeric_evidence_by_reason": numeric_by_reason,
            "table_evidence_by_reason": table_by_reason,
        }

    def _period_resolution(
        self,
        period_from: str,
        period_to: str,
        resolutions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not resolutions:
            return {
                "requested_period_from": period_from,
                "requested_period_to": period_to,
                "effective_report_period": None,
                "comparative_period": None,
                "period_source": None,
                "period_confidence": 0.0,
                "period_warnings": ["no_table_period_resolution_available"],
            }
        best = max(resolutions, key=lambda item: float(item.get("period_confidence") or 0.0))
        return {
            "requested_period_from": period_from,
            "requested_period_to": period_to,
            "effective_report_period": best.get("effective_period"),
            "comparative_period": best.get("comparative_period"),
            "period_source": best.get("period_source"),
            "period_confidence": best.get("period_confidence"),
            "period_warnings": best.get("period_warnings") or [],
            "resolved_periods_seen": sorted(
                {item.get("effective_period") for item in resolutions if item.get("effective_period")}
            ),
        }

    def _period_blockers(
        self,
        period_resolution: dict[str, Any],
        rejected_rows: list[dict[str, Any]],
    ) -> list[str]:
        blockers: set[str] = set()
        if period_resolution.get("period_source") == "upload_default":
            blockers.add("period_defaulted_without_document_evidence")
        if any(item.get("rejection_reason") == "ambiguous_period_mapping" for item in rejected_rows):
            blockers.add("period_header_ambiguous")
        effective_period = str(period_resolution.get("effective_report_period") or "")
        requested_to = str(period_resolution.get("requested_period_to") or "")
        if effective_period and requested_to and effective_period != requested_to:
            blockers.add("current_year_conflicts_with_document_headers")
        return sorted(blockers)

    def _row_parsing_diagnostics(self, artifacts: list[Path]) -> dict[str, Any]:
        diagnostics = {
            "row_kind_counts": {},
            "degraded_primary_statement_tables": 0,
            "primary_statement_clean_tables": 0,
        }
        for artifact in artifacts:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            for table in payload.get("statement_tables", []):
                role = table.get("table_role")
                if role == "primary_statement":
                    diagnostics["primary_statement_clean_tables"] += 1
                elif role in {"primary_statement_degraded_but_usable", "primary_statement_degraded_unusable"}:
                    diagnostics["degraded_primary_statement_tables"] += 1
                for block in table.get("row_blocks") or []:
                    kind = str(block.get("row_kind") or "unknown")
                    diagnostics["row_kind_counts"][kind] = diagnostics["row_kind_counts"].get(kind, 0) + 1
        diagnostics["row_kind_counts"] = dict(sorted(diagnostics["row_kind_counts"].items()))
        return diagnostics

    def _statement_family_summary(self, artifacts: list[Path]) -> dict[str, Any]:
        summary: dict[str, int] = {}
        for artifact in artifacts:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            for table in payload.get("statement_tables", []):
                family = str(table.get("statement_family") or table.get("statement_type") or "unknown")
                summary[family] = summary.get(family, 0) + 1
        return dict(sorted(summary.items()))

    def _fact_confidence_summary(self, facts: list[StatementFactCandidate]) -> dict[str, Any]:
        if not facts:
            return {"count": 0, "min": None, "max": None, "average": None}
        values = [float(fact.confidence_score) for fact in facts]
        return {
            "count": len(values),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "average": round(sum(values) / len(values), 4),
        }

    def _llm_ready_evidence_pack(
        self,
        *,
        structured_facts: list[dict[str, Any]],
        derived_safe_facts: list[dict[str, Any]],
        rejected_rows: list[dict[str, Any]],
        unmapped_numeric_evidence: list[dict[str, Any]],
        unmapped_table_evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "normalized_facts": structured_facts,
            "derived_safe_facts": derived_safe_facts,
            "rejected_rows": rejected_rows,
            "unresolved_numeric_evidence": unmapped_numeric_evidence,
            "unresolved_table_evidence": unmapped_table_evidence,
            "evidence_warnings": sorted(
                {
                    "unresolved_evidence_not_confirmed_fact" if (unmapped_numeric_evidence or unmapped_table_evidence) else None,
                    "derived_facts_require_caution" if derived_safe_facts else None,
                }
                - {None}
            ),
            "instructions": [
                "Use structured_facts as normalized facts.",
                "Use derived_safe_facts only with caution.",
                "Unresolved evidence may be mentioned only as unverified supporting material.",
                "Do not calculate ratios from unresolved evidence.",
                "Do not treat unresolved evidence as confirmed financial facts.",
                "Do not invent missing values.",
            ],
        }

    def _sector_policy(self, company_ticker: str) -> str:
        if is_banking_ticker(company_ticker):
            return "banking"
        if is_financial_non_bank_ticker(company_ticker):
            return "financial_non_bank"
        return "industrial"

    def _issuer_class(self, company_ticker: str) -> str:
        ticker = company_ticker.upper()
        if is_banking_ticker(ticker):
            return "bank_ifrs"
        if is_financial_non_bank_ticker(ticker):
            return "financial_non_bank_ifrs"
        if ticker in {"MGNT", "X5", "FIVE"}:
            return "industrial_ifrs_retail"
        if ticker in {"LKOH", "GAZP", "ROSN", "SIBN", "TATN", "NVTK"}:
            return "industrial_ifrs_oil_gas"
        if ticker in {"GMKN", "PLZL", "CHMF", "MAGN", "PHOR"}:
            return "industrial_ifrs_metals_mining"
        return "industrial_ifrs_retail"

    def _sector_policy_rejection_reason(self, company_ticker: str, metric_code: str) -> str | None:
        if is_banking_ticker(company_ticker) and metric_code in INDUSTRIAL_ONLY_FACT_CODES:
            return "industrial_concept_not_banking_metric"
        if not is_banking_ticker(company_ticker) and metric_code in BANKING_ONLY_FACT_CODES:
            return "banking_concept_not_industrial_metric"
        return None

    def _interpret_statement_row(
        self,
        company_ticker: str,
        statement_type: str,
        row: dict[str, Any],
        row_block: dict[str, Any],
        raw_label: str,
    ) -> tuple[str | None, str | None]:
        row_kind = str(row_block.get("row_kind") or "unknown")
        if statement_type == "balance_sheet":
            return interpret_balance_sheet_row(raw_label, row_kind)
        if statement_type == "income_statement":
            return interpret_income_statement_row(raw_label, row_kind, self._issuer_class(company_ticker))
        if statement_type == "cash_flow":
            return interpret_cash_flow_row(raw_label, row_kind)
        return None, None

    def _fact_source_trust_bucket(self, fact: StatementFactCandidate) -> str:
        if fact.extraction_method in {FALLBACK_EXTRACTION_METHOD, "dataframe_statement_parser_derived"}:
            return "manual_upload_candidate_based"
        return "validated_statement_table"

    def _rejection_source_trust_bucket(self, reason: str, details: dict[str, Any]) -> str:
        extraction_method = details.get("extraction_method") or infer_rejection_extraction_method(reason)
        if extraction_method in {FALLBACK_EXTRACTION_METHOD, "text_table_fallback"}:
            return "manual_upload_candidate_based"
        return "validated_statement_table"


EXPECTED_FACTS = [
    "revenue",
    "operating_profit",
    "net_income",
    "total_assets",
    "total_equity",
    "current_assets",
    "current_liabilities",
    "cash_and_equivalents",
    "operating_cash_flow",
    "capex",
]

BANKING_REJECTION_REASONS = {
    "banking_income_not_industrial_revenue",
    "non_core_revenue_not_statement_revenue",
    "insurance_revenue_not_total_revenue",
}

HARD_BLOCKING_REJECTION_REASONS = {
    "missing_traceability",
    "ambiguous_period_mapping",
    "ambiguous_period_column",
    "text_table_fallback_not_eligible_for_fact_normalization",
}

SHARED_SECTOR_FACT_CODES = {
    "net_income",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "cash_and_equivalents",
    "operating_cash_flow",
}

BANKING_ONLY_FACT_CODES = BANKING_FACT_CODES - SHARED_SECTOR_FACT_CODES

INDUSTRIAL_ONLY_FACT_CODES = {
    "revenue",
    "operating_profit",
    "current_assets",
    "current_liabilities",
    "capex",
    "total_debt",
    "dividends_paid",
    "declared_dividends",
}

ALLOWED_BY_STATEMENT = {
    "income_statement": {
        "revenue",
        "operating_profit",
        "net_income",
        "interest_income",
        "interest_expense",
        "fee_and_commission_income",
        "fee_and_commission_expense",
        "net_interest_income",
        "net_fee_commission_income",
        "net_trading_income",
        "operating_income",
        "operating_expenses",
        "impairment_charge",
        "profit_before_tax",
    },
    "balance_sheet": {
        "total_assets",
        "total_liabilities",
        "total_equity",
        "current_assets",
        "current_liabilities",
        "cash_and_equivalents",
        "total_debt",
        "loans_to_customers",
        "retail_customer_accounts",
        "corporate_customer_accounts",
        "customer_accounts",
    },
    "cash_flow": {"operating_cash_flow", "capex"},
}

LABELS = {
    "income_statement": {
        "revenue": ["revenue", "revenues", "sales", "sales and other operating revenues", "выручка"],
        "operating_profit": [
            "operating profit",
            "profit from operating activities",
            "прибыль от операционной деятельности",
        ],
        "net_income": [
            "profit for the period",
            "profit for the year",
            "net income",
            "чистая прибыль",
            "прибыль за год",
            "прибыль за период",
        ],
        "interest_income": ["interest income", "процентные доходы"],
        "interest_expense": ["interest expense", "interest expenses", "процентные расходы"],
        "fee_and_commission_income": ["fee and commission income", "fee commission income", "комиссионные доходы"],
        "fee_and_commission_expense": [
            "fee and commission expense",
            "fee and commission expenses",
            "комиссионные расходы",
        ],
        "net_interest_income": [
            "net interest income",
            "чистый процентный доход",
            "чистые процентные доходы",
        ],
        "net_fee_commission_income": [
            "net fee and commission income",
            "net fee commission income",
            "чистый комиссионный доход",
            "чистые комиссионные доходы",
        ],
        "net_trading_income": [
            "net trading income",
            "net gains from trading",
            "чистые доходы от операций с финансовыми инструментами",
            "чистые доходы от торговых операций",
        ],
        "operating_income": [
            "operating income",
            "total operating income",
            "операционные доходы",
            "итого операционные доходы",
        ],
        "operating_expenses": [
            "operating expenses",
            "administrative and other operating expenses",
            "staff costs and administrative expenses",
            "personnel and administrative expenses",
            "операционные расходы",
            "административные и прочие операционные расходы",
            "расходы на содержание персонала и административные расходы",
        ],
        "impairment_charge": ["impairment losses", "credit loss allowance", "расходы по кредитным убыткам"],
        "profit_before_tax": ["profit before tax", "profit before income tax", "прибыль до налогообложения"],
    },
    "balance_sheet": {
        "total_assets": ["total assets", "итого активы", "итого активов", "баланс"],
        "total_liabilities": ["total liabilities", "итого обязательств"],
        "total_equity": [
            "total equity",
            "total shareholders equity",
            "equity attributable to shareholders",
            "итого собственных средств",
            "итого капитала",
            "итого капитал",
            "итого собственных средств",
        ],
        "current_assets": ["current assets", "total current assets", "оборотные активы", "итого оборотные активы"],
        "current_liabilities": [
            "current liabilities",
            "total current liabilities",
            "краткосрочные обязательства",
            "итого краткосрочные обязательства",
        ],
        "cash_and_equivalents": [
            "cash and cash equivalents",
            "денежные средства и денежные эквиваленты",
            "денежные средства и их эквиваленты",
        ],
        "total_debt": ["total debt", "total borrowings", "loans and borrowings", "borrowings"],
        "loans_to_customers": ["loans and advances to customers", "loans to customers", "кредиты и авансы клиентам"],
        "retail_customer_accounts": ["amounts due to individuals", "retail customer accounts", "средства физических лиц"],
        "corporate_customer_accounts": [
            "amounts due to corporate customers",
            "corporate customer accounts",
            "средства корпоративных клиентов",
        ],
        "customer_accounts": ["amounts due to customers", "customer accounts", "due to customers", "средства клиентов"],
    },
    "cash_flow": {
        "operating_cash_flow": [
            "net cash provided by operating activities",
            "net cash generated from operating activities",
            "net cash from operating activities",
            "cash flows from operating activities",
            "денежные потоки от операционной деятельности",
            "чистые денежные средства полученные от операционной деятельности",
        ],
        "capex": [
            "capital expenditures",
            "purchase of property plant and equipment",
            "purchases of property plant and equipment",
            "acquisition of property plant and equipment",
            "приобретение основных средств",
            "капитальные вложения",
        ],
    },
}


def normalize_label(value: str) -> str:
    return normalize_matching_text(value)


def match_metric(raw_label: str, statement_type: str) -> str | None:
    normalized = normalize_label(raw_label)
    if (
        statement_type == "balance_sheet"
        and "капитал" in normalized
        and "обязател" in normalized
        and "итого" in normalized
    ):
        return None
    for metric_code, labels in LABELS.get(statement_type, {}).items():
        for label in labels:
            if normalized == normalize_label(label):
                return metric_code
    best: tuple[str | None, int] = (None, 0)
    for metric_code, labels in LABELS.get(statement_type, {}).items():
        for label in labels:
            candidate = normalize_label(label)
            if metric_code in {"current_assets", "current_liabilities"}:
                continue
            if metric_code == "current_assets" and contains_non_current_assets_marker(normalized):
                continue
            if metric_code == "current_liabilities" and contains_non_current_liabilities_marker(normalized):
                continue
            if candidate in normalized:
                best = max(best, (metric_code, len(candidate)), key=lambda item: item[1])
    return best[0]


def contains_non_current_assets_marker(normalized_label: str) -> bool:
    markers = (
        "non current assets",
        "non-current assets",
        "внеоборотные активы",
        "необоротные активы",
    )
    return any(normalize_label(marker) in normalized_label for marker in markers)


def contains_non_current_liabilities_marker(normalized_label: str) -> bool:
    markers = (
        "non current liabilities",
        "non-current liabilities",
        "долгосрочные обязательства",
    )
    return any(normalize_label(marker) in normalized_label for marker in markers)


def normalized_row_block_fallback(row: dict[str, Any]) -> dict[str, Any]:
    raw_label = first_text_cell(row)
    normalized = normalize_label(raw_label or "")
    row_kind = "unknown"
    if raw_label and row_has_numeric_values(row):
        if any(marker in normalized for marker in ("итого", "total")):
            row_kind = "grand_total"
        elif any(
            marker in normalized
            for marker in (
                "current assets",
                "current liabilities",
                "оборотные активы",
                "краткосрочные обязательства",
            )
        ):
            row_kind = "subtotal"
        else:
            row_kind = "statement_line_item"
    return {
        "label_text": raw_label,
        "row_kind": row_kind,
        "row_confidence": 0.7,
        "diagnostics": {},
    }


def interpret_balance_sheet_row(raw_label: str, row_kind: str) -> tuple[str | None, str | None]:
    normalized = normalize_label(raw_label)
    metric_code = match_metric(raw_label, "balance_sheet")
    if metric_code in {"current_assets", "current_liabilities"}:
        if any(marker in normalized for marker in ("total current", "итого", "total", "current assets", "current liabilities")):
            return metric_code, None
        return None, "component_row_not_total_metric"
    if metric_code in {"total_assets", "total_equity", "total_liabilities"} and row_kind not in {"subtotal", "grand_total"}:
        return None, "subtotal_without_statement_context"
    if row_kind == "grand_total" and not any(
        marker in normalized
        for marker in ("актив", "asset", "капитал", "equity", "обяз", "liabilit")
    ):
        return None, "component_row_not_total_metric"
    return metric_code, None


def interpret_income_statement_row(
    raw_label: str,
    row_kind: str,
    issuer_class: str,
) -> tuple[str | None, str | None]:
    metric_code = match_metric(raw_label, "income_statement")
    normalized = normalize_label(raw_label)
    if issuer_class.startswith("industrial") and metric_code == "revenue":
        if any(marker in normalized for marker in ("segment", "сегмент", "прочие", "other")):
            return None, "component_row_not_total_metric"
    if row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
        return None, "row_kind_not_statement_line_item"
    return metric_code, None


def interpret_cash_flow_row(raw_label: str, row_kind: str) -> tuple[str | None, str | None]:
    if row_kind not in {"statement_line_item", "subtotal"}:
        return None, "row_kind_not_statement_line_item"
    return match_metric(raw_label, "cash_flow"), None


def first_text_cell(row: dict[str, Any]) -> str | None:
    preferred = normalize_label(row.get("line")) if isinstance(row, dict) and row.get("line") else ""
    if preferred:
        return row.get("line")
    for value in row.values():
        text = str(value or "").strip()
        if text and parse_number(text) is None:
            return text
    return None


def row_to_source_line(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    parts = [str(value).strip() for value in row.values() if str(value or "").strip()]
    return " | ".join(parts) if parts else None


def row_to_raw_value(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    numeric = [str(value).strip() for value in row.values() if parse_number(value) is not None]
    return " | ".join(numeric) if numeric else None


def row_has_numeric_values(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    return any(parse_number(value) is not None for value in row.values())


def extract_numeric_values(row: dict[str, Any] | None) -> list[float]:
    if not isinstance(row, dict):
        return []
    values: list[float] = []
    for value in row.values():
        parsed = parse_number(value)
        if parsed is not None:
            values.append(parsed)
    return values


def classify_note_table(table: dict[str, Any]) -> str:
    statement_type = str(table.get("statement_type") or "")
    title = normalize_label(table.get("table_title") or "")
    extraction_method = normalize_label(table.get("extraction_method") or "")
    if statement_type in ALLOWED_BY_STATEMENT:
        return "safe_structured_note"
    if "note" in title or "примеч" in title:
        return "review_only_note"
    first_row = (table.get("rows") or [None])[0]
    if "text table fallback" in extraction_method and row_has_numeric_values(first_row):
        return "statement_like_note"
    if any(row_has_numeric_values(row) for row in table.get("rows") or []):
        return "unknown_table"
    return "narrative_or_non_numeric"


def table_evidence_category(classification: str) -> str:
    return {
        "review_only_note": "review_only_note",
        "statement_like_note": "notes_table",
        "unknown_table": "unknown_table",
        "narrative_or_non_numeric": "narrative_or_non_numeric",
    }.get(classification, "parser_unsupported_structure")


def table_evidence_type(classification: str) -> str:
    return {
        "review_only_note": "review_only_note_table",
        "statement_like_note": "unsupported_structure_table",
        "unknown_table": "unmapped_table",
        "narrative_or_non_numeric": "unmapped_table",
    }.get(classification, "unsupported_structure_table")


def evidence_source_trust_bucket(table: dict[str, Any]) -> str:
    extraction_method = table.get("extraction_method")
    if extraction_method in {"text_table_fallback", FALLBACK_EXTRACTION_METHOD}:
        return "manual_upload_candidate_based"
    return "validated_statement_table"


def normalize_rejection_reason(reason: str) -> str:
    mapping = {
        "text_table_fallback_not_eligible_for_fact_normalization": "unsupported_metric_for_text_fallback",
        "ambiguous_period_column": "ambiguous_period_mapping",
        "missing_traceability": "policy_blocked",
        "numeric_value_not_detected": "multi_line_parse_uncertain",
        "debt_component_not_derived_in_dataframe_parser": "debt_component_not_total_debt",
    }
    return mapping.get(reason, reason)


def rejection_policy_reason(reason: str) -> str | None:
    normalized = normalize_rejection_reason(reason)
    if normalized in {
        "unsupported_metric_for_text_fallback",
        "notes_fallback_not_eligible_for_fact_normalization",
        "policy_blocked",
        "ebitda_proxy_not_allowed",
        "debt_component_not_total_debt",
        "banking_concept_not_industrial_metric",
        "industrial_concept_not_banking_metric",
    }:
        return normalized
    if normalized == "ambiguous_period_mapping":
        return "period_semantics_unclear"
    if normalized == "unit_or_currency_basis_missing":
        return "unit_or_currency_basis_missing"
    return None


def infer_rejection_extraction_method(reason: str) -> str:
    if "text_fallback" in reason:
        return FALLBACK_EXTRACTION_METHOD
    return "dataframe_statement_parser"


def select_current_value(row: dict[str, Any], period: str, statement_type: str) -> tuple[Any, str | None, str | None]:
    numeric_cells = [(key, value) for key, value in row.items() if parse_number(value) is not None]
    if not numeric_cells:
        return None, None, "numeric_value_not_detected"
    current_keys = [key for key, value in numeric_cells if is_current_period_column(str(key), period, statement_type)]
    if len(current_keys) == 1:
        key = current_keys[0]
        return row[key], str(key), None
    if len(current_keys) > 1:
        return None, None, "ambiguous_period_column"
    if len(numeric_cells) == 1:
        key, value = numeric_cells[0]
        return value, str(key), None
    return None, None, "ambiguous_period_column"


def is_current_period_column(column: str, period: str, statement_type: str) -> bool:
    normalized = normalize_label(column)
    year = period_year(period)
    if not year:
        return False
    if period.endswith("Q1"):
        return column_mentions_year(normalized, year) and (
            "march" in normalized
            or "марта" in normalized
            or "3 months" in normalized
            or "three months" in normalized
            or "3 мес" in normalized
            or re.fullmatch(rf"{year}\s*г", normalized) is not None
            or normalized == str(year)
            or statement_type == "balance_sheet"
        )
    if period.endswith("Q2"):
        return column_mentions_year(normalized, year) and (
            "june" in normalized
            or "июня" in normalized
            or "6 months" in normalized
            or "six months" in normalized
            or "6 мес" in normalized
            or re.fullmatch(rf"{year}\s*г", normalized) is not None
            or normalized == str(year)
            or statement_type == "balance_sheet"
        )
    if period.endswith("Q3"):
        return column_mentions_year(normalized, year) and (
            "september" in normalized
            or "сентября" in normalized
            or "9 months" in normalized
            or "nine months" in normalized
            or "9 мес" in normalized
            or re.fullmatch(rf"{year}\s*г", normalized) is not None
            or normalized == str(year)
            or statement_type == "balance_sheet"
        )
    if period.endswith("Q4"):
        return column_mentions_year(normalized, year) and (
            "31 december" in normalized
            or "31 декабря" in normalized
            or "fy" in normalized
            or normalized.startswith(f"for {year}")
            or normalized.startswith(f"за {year}")
            or "year" in normalized
            or "год" in normalized
            or re.fullmatch(rf"{year}\s*г", normalized) is not None
            or normalized == str(year)
            or statement_type == "balance_sheet"
        )
    return False


def column_mentions_year(normalized: str, year: int) -> bool:
    return re.search(rf"\b{year}\b", normalized) is not None


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = normalize_financial_text(text)
    if not text or text in {"-", "—"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("\u00a0", " ")
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


def unit_multiplier_for(unit: str | None) -> float:
    normalized = normalize_label(unit or "")
    if "billion" in normalized or "миллиард" in normalized:
        return 1_000_000_000.0
    if "million" in normalized or "млн" in normalized:
        return 1_000_000.0
    if "thousand" in normalized or "тыс" in normalized:
        return 1_000.0
    return 1.0


def table_unit_multiplier(table: dict[str, Any]) -> float:
    raw = table.get("unit_multiplier")
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return unit_multiplier_for(table.get("unit"))


def normalized_fact_period(period: str | None) -> str:
    text = str(period or "").strip().upper()
    if text.isdigit() and len(text) == 4:
        return f"{text}Q4"
    return text


def period_year(period: str | None) -> int | None:
    match = re.match(r"^(\d{4})Q[1-4]$", str(period or "").upper())
    return int(match.group(1)) if match else None


def period_type(statement_type: str, period: str) -> str:
    if statement_type == "balance_sheet":
        return "balance_sheet_snapshot"
    if period.endswith("Q4"):
        return "annual"
    return "ytd"


def traceability_ok(table: dict[str, Any], document_id: int) -> bool:
    return bool(document_id and table.get("source_location") and table.get("table_index") is not None)


def is_component_debt_label(raw_label: str) -> bool:
    normalized = normalize_label(raw_label)
    return any(token in normalized for token in ["short term", "long term", "current portion"])


def missing_expected_facts(facts: list[StatementFactCandidate]) -> list[str]:
    present = {fact.metric_code for fact in facts}
    return [fact for fact in EXPECTED_FACTS if fact not in present]


def _is_dataframe_fact(existing: StatementFact) -> bool:
    location = existing.source_location or {}
    return location.get("extraction_method") in {"dataframe_statement_parser", FALLBACK_EXTRACTION_METHOD}


def first_table_period(payload: dict[str, Any]) -> str | None:
    for table in payload.get("statement_tables", []) or []:
        if table.get("effective_period") or table.get("period"):
            return table.get("effective_period") or table["period"]
    return None
