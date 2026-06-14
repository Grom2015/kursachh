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
from app.services.parsing.text_normalization import contains_normalized_marker, normalize_financial_text, normalize_matching_text
from app.services.periods import period_or_year_in_range
from app.services.sectors.banking_policy import (
    BANKING_FACT_CODES,
    BANKING_KEY_FACT_CODES,
    issuer_class_is_banking,
    issuer_class_is_financial_non_bank,
    resolve_issuer_class,
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
        self._issuer_class_cache: dict[str, str] = {}

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
            for table in statement_tables_from_payload(payload):
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
        unmapped_numeric_evidence = [
            item
            for item in unmapped_numeric_evidence
            if not is_statement_context_only_label(str(item.get("raw_label") or ""))
        ]
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
        if document_id and not table.get("document_id"):
            table = {**table, "document_id": document_id}
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
            if text_fallback_notes_like(table):
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        None,
                        None,
                        "notes_fallback_not_eligible_for_fact_normalization",
                    )
                )
                return facts, rejected, unmapped_numeric_evidence, unmapped_table_evidence
            if issuer_class_is_banking(self._issuer_class(company_ticker)) and not table.get("row_blocks"):
                return self._parse_text_fallback_table(company_ticker, table, document_id, reporting_standard)
            table = normalized_text_fallback_table_for_parser(table)
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
        row_blocks = list(table.get("row_blocks") or [])
        rows = normalized_table_rows(table)
        rows, row_blocks = expand_merged_statement_rows(
            table=table,
            rows=rows,
            row_blocks=row_blocks,
            statement_type=statement_type,
        )
        row_index = 0
        while row_index < len(rows):
            row = rows[row_index]
            row_block = row_blocks[row_index] if row_index < len(row_blocks) else normalized_row_block_fallback(row)
            effective_row, row_block, consumed_next_row = recover_adjacent_statement_row(
                table=table,
                row_index=row_index,
                rows=rows,
                row_blocks=row_blocks,
            )
            effective_row, row_block = recover_statement_numeric_fragment_label(
                table=table,
                row=effective_row,
                row_block=row_block,
                row_index=row_index,
                statement_type=statement_type,
            )
            row_kind = str(row_block.get("row_kind") or "unknown")
            raw_label = preferred_row_label(effective_row, row_block)
            source_line = row_to_source_line(effective_row)
            row_source_engine = source_engine_for_row_block(row_block, table)
            row_source_table_id = source_table_id_for_row_block(row_block, table)
            row_fusion_status = fusion_status_for_row_block(row_block, table)
            row_source_engines = source_engines_for_row_block(row_block, table)
            advance = 2 if consumed_next_row else 1
            if raw_label and is_statement_context_only_label(raw_label):
                row_index += advance
                continue
            if row_kind in {"header", "footnote", "note_reference_only"}:
                row_index += advance
                continue
            if row_fusion_status == "conflict_retained_as_evidence":
                if row_has_numeric_values(effective_row):
                    conflict_reason = str(
                        (row_block.get("diagnostics") or {}).get("fusion_conflict_reason")
                        or "engine_conflict_retained_as_evidence"
                    )
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=effective_row,
                            row_block=row_block,
                            raw_label=raw_label,
                            evidence_category="ambiguous_mapping",
                            evidence_type="ambiguous_numeric_fragment",
                            reasons=[conflict_reason],
                        )
                    )
                row_index += advance
                continue
            if not raw_label:
                if row_has_numeric_values(effective_row):
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=effective_row,
                            row_block=row_block,
                            raw_label=None,
                            evidence_category="unknown_concept",
                            evidence_type="unmapped_numeric_row",
                            reasons=["label_column_missing_after_pdf_extraction"],
                        )
                    )
                row_index += advance
                continue
            if row_kind == "numeric_fragment":
                promotable_row_kind = promotable_numeric_fragment_row_kind(
                    table=table,
                    row=effective_row,
                    row_block=row_block,
                    raw_label=raw_label,
                    statement_type=statement_type,
                )
                if promotable_row_kind:
                    row_kind = promotable_row_kind
                    row_block = dict(row_block or {})
                    row_block["row_kind"] = promotable_row_kind
                    row_block["row_confidence"] = max(float(row_block.get("row_confidence") or 0.0), 0.84)
                    diagnostics = dict(row_block.get("diagnostics") or {})
                    diagnostics["promoted_from_numeric_fragment"] = True
                    row_block["diagnostics"] = diagnostics
                else:
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=effective_row,
                            row_block=row_block,
                            raw_label=raw_label,
                            evidence_category="multi_line_parse_uncertain",
                            evidence_type="ambiguous_numeric_fragment",
                            reasons=["row_kind_numeric_fragment"],
                        )
                    )
                    row_index += advance
                    continue
            if row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
                if row_has_numeric_values(effective_row):
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=effective_row,
                            row_block=row_block,
                            raw_label=raw_label,
                            evidence_category="ambiguous_mapping",
                            evidence_type="unmapped_numeric_row",
                            reasons=[f"row_kind_not_statement_line_item:{row_kind}"],
                        )
                    )
                row_index += advance
                continue
            metric_code, row_policy_reason = self._interpret_statement_row(
                company_ticker=company_ticker,
                statement_type=statement_type,
                row=effective_row,
                row_block=row_block,
                raw_label=raw_label,
            )
            if row_policy_reason:
                evidence_only_reasons = {"component_row_not_total_metric", "subtotal_without_statement_context"}
                if row_policy_reason in evidence_only_reasons and row_has_numeric_values(effective_row):
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=effective_row,
                            row_block=row_block,
                            raw_label=raw_label,
                            evidence_category="policy_blocked",
                            evidence_type="unmapped_numeric_row",
                            reasons=[row_policy_reason],
                        )
                    )
                    row_index += advance
                    continue
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
                            "row": effective_row,
                            "source_line": source_line,
                            "row_kind": row_kind,
                            "warnings": list(table.get("warnings") or []),
                            "source_page": table.get("page_number"),
                            "source_engine": row_source_engine,
                            "source_table_id": row_source_table_id,
                            "source_bbox": source_bbox_for_row_block(row_block, table),
                            "fusion_status": row_fusion_status,
                            "source_engines_involved": row_source_engines,
                        },
                    )
                )
                row_index += advance
                continue
            if not metric_code:
                if row_has_numeric_values(effective_row):
                    unmapped_numeric_evidence.append(
                        self._numeric_evidence_payload(
                            table=table,
                            document_id=document_id,
                            row=effective_row,
                            row_block=row_block,
                            raw_label=raw_label,
                            evidence_category="unknown_concept",
                            evidence_type="unmapped_numeric_row",
                            reasons=["no_metric_match"],
                        )
                    )
                row_index += advance
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
                        {
                            "row": effective_row,
                            "source_line": source_line,
                            "row_kind": row_kind,
                            "source_page": table.get("page_number"),
                            "source_engine": row_source_engine,
                            "source_table_id": row_source_table_id,
                            "source_bbox": source_bbox_for_row_block(row_block, table),
                            "fusion_status": row_fusion_status,
                            "source_engines_involved": row_source_engines,
                        },
                    )
                )
                row_index += advance
                continue
            promotion_blocker = self._safe_fact_promotion_gate(
                table=table,
                row=effective_row,
                row_block=row_block,
                statement_type=statement_type,
                raw_label=raw_label,
            )
            if promotion_blocker:
                rejected.append(
                    RejectedStatementCandidate(
                        document_id,
                        period,
                        statement_type,
                        table_index,
                        raw_label,
                        metric_code,
                        promotion_blocker,
                        {
                            "row": effective_row,
                            "source_line": source_line,
                            "row_kind": row_kind,
                            "source_page": table.get("page_number"),
                            "source_engine": row_source_engine,
                            "source_table_id": row_source_table_id,
                            "source_bbox": source_bbox_for_row_block(row_block, table),
                            "fusion_status": row_fusion_status,
                            "source_engines_involved": row_source_engines,
                        },
                    )
                )
                row_index += advance
                continue
            raw_value, column_name, reason = select_current_value(
                effective_row,
                period,
                statement_type,
                table=table,
                row_block=row_block,
            )
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
                        {
                            "row": effective_row,
                            "source_line": source_line,
                            "row_kind": row_kind,
                            "source_page": table.get("page_number"),
                            "source_engine": source_engine_for_table(table),
                            "source_table_id": source_table_id_for_table(table),
                            "source_bbox": source_bbox_for_row_block(row_block, table),
                            "fusion_status": fusion_status_for_table(table),
                            "source_engines_involved": source_engines_for_table(table),
                        },
                    )
                )
                row_index += advance
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
                        {
                            "raw_value": raw_value,
                            "column_name": column_name,
                            "source_line": source_line,
                            "source_page": table.get("page_number"),
                            "source_engine": source_engine_for_table(table),
                            "source_table_id": source_table_id_for_table(table),
                            "source_bbox": source_bbox_for_row_block(row_block, table),
                            "fusion_status": fusion_status_for_table(table),
                            "source_engines_involved": source_engines_for_table(table),
                        },
                    )
                )
                row_index += advance
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
                        {
                            "row": effective_row,
                            "source_line": source_line,
                            "source_page": table.get("page_number"),
                            "source_engine": row_source_engine,
                            "source_table_id": row_source_table_id,
                            "source_bbox": source_bbox_for_row_block(row_block, table),
                            "fusion_status": row_fusion_status,
                            "source_engines_involved": row_source_engines,
                        },
                    )
                )
                row_index += advance
                continue
            unit_multiplier = table_unit_multiplier(table)
            fact_period = normalized_fact_period(period)
            fact_extraction_method = (
                FALLBACK_EXTRACTION_METHOD
                if table.get("extraction_method") == "text_table_fallback"
                else "dataframe_statement_parser"
            )
            fact_quality_flag = (
                "high_confidence_text_fallback" if table.get("extraction_method") == "text_table_fallback" else "dataframe_exact"
            )
            source_location = {
                "source_document_id": document_id,
                "source_table_type": statement_type,
                "source_table_index": table_index,
                "source_location": table.get("source_location"),
                "page_number": table.get("page_number"),
                "column_name": column_name,
                "raw_label": raw_label,
                "raw_value": raw_value,
                "source_line": source_line,
                "source_lines": [source_line] if source_line else [],
                "extraction_method": fact_extraction_method,
                "source_table_extraction_method": table.get("extraction_method"),
                "fact_source_kind": fact_extraction_method,
                "source_engine": row_source_engine,
                "source_page": table.get("page_number"),
                "source_table_id": row_source_table_id,
                "source_bbox": source_bbox_for_row_block(row_block, table),
                "fusion_status": row_fusion_status,
                "source_engines_involved": row_source_engines,
            }
            fact_warnings = sorted(
                {
                    *list(table.get("warnings") or []),
                    *list((row_block.get("diagnostics") or {}).get("warnings") or []),
                    *semantic_trust_warnings(table=table, row=effective_row, row_block=row_block),
                }
            )
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
                    quality_flag=fact_quality_flag,
                    extraction_method=fact_extraction_method,
                    warnings=fact_warnings,
                    eligible_for_metric_engine=True,
                )
            )
            row_index += advance
            continue
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
                    source_location={
                        **(row.source_location or {}),
                        "source_engine": (row.source_location or {}).get("source_engine") or table.get("extraction_method"),
                        "source_page": (row.source_location or {}).get("page_number") or table.get("page_number"),
                        "source_table_id": (row.source_location or {}).get("source_table_id") or source_table_id_for_table(table),
                        "source_bbox": (row.source_location or {}).get("source_bbox"),
                        "fusion_status": (row.source_location or {}).get("fusion_status") or fusion_status_for_table(table),
                        "source_engines_involved": (row.source_location or {}).get("source_engines_involved")
                        or source_engines_for_table(table),
                    },
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
                "source_engine": "derived_safe_fact",
                "source_page": None,
                "source_table_id": None,
                "source_bbox": None,
                "fusion_status": "single_engine",
                "source_engines_involved": sorted(
                    set((retail.source_location or {}).get("source_engines_involved") or [])
                    | set((corporate.source_location or {}).get("source_engines_involved") or [])
                )
                or ["derived_safe_fact"],
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
        if not issuer_class_is_banking(self._issuer_class(company_ticker)):
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
            if item.reason in BANKING_REJECTION_REASONS or (item.metric_code in BANKING_FACT_CODES if item.metric_code else False)
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
        elif "ocr_only_lower_trust" in list(fact.warnings or []):
            trust_warning = "ocr_only_lower_trust"
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
            "source_engine": location.get("source_engine") or location.get("source_table_extraction_method"),
            "source_page": location.get("source_page") or location.get("page_number") or location.get("page"),
            "source_table_id": location.get("source_table_id"),
            "source_bbox": location.get("source_bbox"),
            "fusion_status": location.get("fusion_status") or "single_engine",
            "source_engines_involved": location.get("source_engines_involved") or [location.get("source_engine")],
            "warnings": list(fact.warnings or []),
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
            "source_engine": details.get("source_engine"),
            "source_page": details.get("source_page") or details.get("page_number") or details.get("page"),
            "source_table_id": details.get("source_table_id"),
            "source_bbox": details.get("source_bbox"),
            "fusion_status": details.get("fusion_status") or "single_engine",
            "source_engines_involved": details.get("source_engines_involved") or [details.get("source_engine")],
        }

    def _numeric_evidence_payload(
        self,
        table: dict[str, Any],
        document_id: int,
        row: dict[str, Any],
        row_block: dict[str, Any] | None,
        raw_label: str | None,
        evidence_category: str,
        evidence_type: str,
        reasons: list[str],
    ) -> dict[str, Any]:
        evidence_topics = evidence_topic_tags([raw_label or "", row_to_source_line(row)])
        warnings = sorted(
            {
                *list(table.get("warnings") or []),
                *list((row_block or {}).get("diagnostics", {}).get("warnings") or []),
                *({"unresolved_evidence_supporting_material_only"} if evidence_topics else set()),
            }
        )
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
            "supporting_material_only": True,
            "evidence_topics": evidence_topics,
            "llm_analysis_use_cases": evidence_analysis_use_cases(evidence_topics),
            "llm_relevance": evidence_llm_relevance(evidence_topics, has_numeric=True),
            "warnings": warnings,
            "reasons": reasons,
            "source_engine": source_engine_for_row_block(row_block, table),
            "source_page": table.get("page_number"),
            "source_table_id": source_table_id_for_row_block(row_block, table),
            "source_bbox": source_bbox_for_row_block(row_block, table),
            "fusion_status": fusion_status_for_row_block(row_block, table),
            "source_engines_involved": source_engines_for_row_block(row_block, table),
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
        evidence_topics = table_evidence_topic_tags(table)
        warnings = sorted(
            {
                *list(table.get("warnings") or []),
                *({"unresolved_evidence_supporting_material_only"} if evidence_topics else set()),
            }
        )
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
            "supporting_material_only": True,
            "evidence_topics": evidence_topics,
            "llm_analysis_use_cases": evidence_analysis_use_cases(evidence_topics),
            "llm_relevance": evidence_llm_relevance(
                evidence_topics,
                has_numeric=any(row_has_numeric_values(row) for row in rows),
            ),
            "warnings": warnings,
            "reasons": reasons,
            "source_engine": source_engine_for_table(table),
            "source_page": table.get("page_number"),
            "source_table_id": source_table_id_for_table(table),
            "source_bbox": (table.get("source_location") or {}).get("source_bbox"),
            "fusion_status": fusion_status_for_table(table),
            "source_engines_involved": source_engines_for_table(table),
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
            if issuer_class_is_banking(self._issuer_class(company_ticker))
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
        topic_summary = evidence_topic_summary(unmapped_numeric_evidence, unmapped_table_evidence)
        return {
            "normalized_facts": structured_facts,
            "derived_safe_facts": derived_safe_facts,
            "rejected_rows": rejected_rows,
            "unresolved_numeric_evidence": unmapped_numeric_evidence,
            "unresolved_table_evidence": unmapped_table_evidence,
            "evidence_topic_summary": topic_summary,
            "llm_useful_unresolved_evidence_count": sum(topic_summary.values()),
            "evidence_warnings": sorted(
                {
                    "unresolved_evidence_not_confirmed_fact" if (unmapped_numeric_evidence or unmapped_table_evidence) else None,
                    "derived_facts_require_caution" if derived_safe_facts else None,
                    "topic_tagged_unresolved_evidence_is_supporting_material_only" if topic_summary else None,
                }
                - {None}
            ),
            "instructions": [
                "Use structured_facts as normalized facts.",
                "Use derived_safe_facts only with caution.",
                "Unresolved evidence may be mentioned only as unverified supporting material.",
                "Use evidence_topics and llm_analysis_use_cases only as unverified context, not as fact labels.",
                "Do not calculate ratios from unresolved evidence.",
                "Do not treat unresolved evidence as confirmed financial facts.",
                "Do not invent missing values.",
            ],
        }

    def _sector_policy(self, company_ticker: str) -> str:
        issuer_class = self._issuer_class(company_ticker)
        if issuer_class_is_banking(issuer_class):
            return "banking"
        if issuer_class_is_financial_non_bank(issuer_class):
            return "financial_non_bank"
        return "industrial"

    def _issuer_class(self, company_ticker: str) -> str:
        ticker = company_ticker.upper()
        if ticker in self._issuer_class_cache:
            return self._issuer_class_cache[ticker]
        company = None
        if self.db:
            company = self.db.scalar(select(Company).where(Company.ticker == ticker))
        issuer_class = resolve_issuer_class(
            ticker=ticker,
            sector=getattr(company, "sector", None),
            subsector=getattr(company, "subsector", None),
            short_name=getattr(company, "short_name", None),
            full_name=getattr(company, "full_name", None),
            aliases=getattr(company, "aliases_json", None),
        )
        self._issuer_class_cache[ticker] = issuer_class
        return issuer_class

    def _sector_policy_rejection_reason(self, company_ticker: str, metric_code: str) -> str | None:
        issuer_class = self._issuer_class(company_ticker)
        if issuer_class_is_banking(issuer_class) and metric_code in INDUSTRIAL_ONLY_FACT_CODES:
            return "industrial_concept_not_banking_metric"
        if not issuer_class_is_banking(issuer_class) and metric_code in BANKING_ONLY_FACT_CODES:
            return "banking_concept_not_industrial_metric"
        return None

    def _safe_fact_promotion_gate(
        self,
        *,
        table: dict[str, Any],
        row: dict[str, Any],
        row_block: dict[str, Any] | None,
        statement_type: str,
        raw_label: str | None,
    ) -> str | None:
        row_block = row_block or {}
        trace_document_id = int(
            table.get("document_id")
            or table.get("source_document_id")
            or table_traceability(table).get("source_document_id")
            or 0
        )
        if statement_type not in ALLOWED_BY_STATEMENT:
            return "statement_family_ambiguous"
        if not raw_label:
            return "label_ownership_unresolved"
        if not row_has_numeric_values(row):
            return "label_ownership_unresolved"
        if not traceability_ok(table, trace_document_id):
            return "policy_blocked"
        if not (
            table.get("page_number") is not None
            or (table_traceability(table).get("page") is not None)
            or row_block.get("source_page") is not None
        ):
            return "policy_blocked"

        label_confidence = semantic_label_confidence(raw_label, row, row_block)
        ownership_confidence = semantic_ownership_confidence(row, row_block)
        fact_period_confidence = semantic_fact_period_confidence(table, row_block)

        if label_confidence < 0.55:
            return "label_ownership_unresolved"
        if ownership_confidence < 0.7:
            return "label_ownership_unresolved"
        if fact_period_confidence < 0.6:
            return "period_header_ambiguous"
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
        diagnostics = row_block.get("diagnostics") or {}
        degraded = bool(
            row.get("stitched_from_rows")
            or row_block.get("stitched_from_rows")
            or diagnostics.get("inline_value_recovered")
            or row.get("inline_value_recovered")
            or row.get("label_recovered_from_word_layout")
            or row.get("recovery_mode")
            or row_block.get("recovery_mode")
            or row_block.get("fragment_role") == "recovered_statement_row"
        )
        if statement_type == "balance_sheet":
            return interpret_balance_sheet_row(raw_label, row_kind)
        if statement_type == "income_statement":
            return interpret_income_statement_row(
                raw_label,
                row_kind,
                self._issuer_class(company_ticker),
                degraded=degraded,
            )
        if statement_type == "cash_flow":
            return interpret_cash_flow_row(raw_label, row_kind, degraded=degraded)
        if statement_type == "changes_in_equity":
            return interpret_changes_in_equity_row(raw_label, row_kind, degraded=degraded)
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
    "profit_before_tax",
    "operating_expenses",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "cash_and_equivalents",
    "operating_cash_flow",
    "operating_cash_flow_before_working_capital",
    "net_cash_from_operations",
}

BANKING_ONLY_FACT_CODES = BANKING_FACT_CODES - SHARED_SECTOR_FACT_CODES

INDUSTRIAL_ONLY_FACT_CODES = {
    "revenue",
    "gross_profit",
    "cost_of_sales",
    "other_income",
    "distribution_costs",
    "administrative_expenses",
    "other_expenses",
    "operating_profit",
    "total_comprehensive_income",
    "total_comprehensive_income_attributable_to_parent",
    "non_controlling_interests",
    "finance_costs",
    "finance_income",
    "net_foreign_exchange_result",
    "income_tax_expense",
    "current_assets",
    "current_liabilities",
    "non_current_assets",
    "non_current_liabilities",
    "property_plant_and_equipment",
    "right_of_use_assets",
    "investment_properties",
    "goodwill",
    "intangible_assets",
    "other_non_current_assets",
    "indemnification_asset",
    "interest_accrued",
    "inventories",
    "trade_and_other_receivables",
    "current_income_tax_receivable",
    "vat_and_other_taxes_receivable",
    "short_term_financial_investments",
    "other_financial_assets_current",
    "other_financial_assets_non_current",
    "assets_held_for_sale",
    "deferred_tax_assets",
    "deferred_tax_liabilities",
    "trade_accounts_payable",
    "borrowings_current",
    "borrowings_non_current",
    "lease_liabilities_current",
    "lease_liabilities_non_current",
    "contract_liabilities_current",
    "income_tax_payable",
    "provisions_current",
    "provisions_non_current",
    "provisions_and_other_liabilities",
    "other_non_current_liabilities",
    "share_capital",
    "share_premium",
    "retained_earnings",
    "other_reserves",
    "capex",
    "depreciation_amortization_and_impairment",
    "gain_on_disposal_of_assets",
    "impairment_of_financial_assets",
    "impairment_of_prepayments",
    "share_based_compensation_expense",
    "other_non_cash_items",
    "change_in_receivables",
    "change_in_inventories",
    "change_in_trade_payables",
    "change_in_other_payables_and_contract_liabilities",
    "operating_cash_flow_before_working_capital",
    "net_cash_from_operations",
    "interest_paid",
    "interest_received",
    "income_tax_paid",
    "effect_of_exchange_rate_on_cash",
    "proceeds_from_borrowings",
    "repayment_of_borrowings",
    "lease_principal_payments",
    "acquisition_of_businesses",
    "proceeds_from_disposal_of_ppe",
    "payments_for_financial_investments",
    "proceeds_from_short_term_financial_investments",
    "investing_cash_flow",
    "financing_cash_flow",
    "cash_and_equivalents_beginning_of_period",
    "net_increase_in_cash",
    "total_debt",
    "dividends_paid",
    "declared_dividends",
}

ALLOWED_BY_STATEMENT = {
    "income_statement": {
        "revenue",
        "gross_profit",
        "cost_of_sales",
        "other_income",
        "distribution_costs",
        "administrative_expenses",
        "other_expenses",
        "operating_profit",
        "net_income",
        "net_income_attributable_to_parent",
        "total_comprehensive_income",
        "total_comprehensive_income_attributable_to_parent",
        "non_controlling_interests",
        "finance_costs",
        "finance_income",
        "net_foreign_exchange_result",
        "impairment_of_financial_assets",
        "income_tax_expense",
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
        "total_equity_and_liabilities",
        "current_assets",
        "current_liabilities",
        "non_current_assets",
        "non_current_liabilities",
        "property_plant_and_equipment",
        "right_of_use_assets",
        "investment_properties",
        "goodwill",
        "intangible_assets",
        "other_non_current_assets",
        "indemnification_asset",
        "interest_accrued",
        "inventories",
        "trade_and_other_receivables",
        "current_income_tax_receivable",
        "vat_and_other_taxes_receivable",
        "short_term_financial_investments",
        "other_financial_assets_current",
        "other_financial_assets_non_current",
        "assets_held_for_sale",
        "deferred_tax_assets",
        "deferred_tax_liabilities",
        "trade_accounts_payable",
        "borrowings_current",
        "borrowings_non_current",
        "lease_liabilities_current",
        "lease_liabilities_non_current",
        "contract_liabilities_current",
        "income_tax_payable",
        "provisions_current",
        "provisions_non_current",
        "provisions_and_other_liabilities",
        "other_non_current_liabilities",
        "share_capital",
        "share_premium",
        "retained_earnings",
        "other_reserves",
        "cash_and_equivalents",
        "total_debt",
        "loans_to_customers",
        "retail_customer_accounts",
        "corporate_customer_accounts",
        "customer_accounts",
    },
    "changes_in_equity": {
        "share_capital",
        "share_premium",
        "retained_earnings",
        "other_reserves",
        "non_controlling_interests",
        "total_equity",
    },
    "cash_flow": {
        "operating_cash_flow_before_working_capital",
        "net_cash_from_operations",
        "operating_cash_flow",
        "capex",
        "interest_paid",
        "interest_received",
        "income_tax_paid",
        "proceeds_from_borrowings",
        "repayment_of_borrowings",
        "lease_principal_payments",
        "profit_before_tax",
        "finance_costs",
        "cash_and_equivalents",
        "depreciation_amortization_and_impairment",
        "gain_on_disposal_of_assets",
        "impairment_of_financial_assets",
        "impairment_of_prepayments",
        "share_based_compensation_expense",
        "net_foreign_exchange_result",
        "other_non_cash_items",
        "change_in_receivables",
        "change_in_inventories",
        "change_in_trade_payables",
        "change_in_other_payables_and_contract_liabilities",
        "acquisition_of_businesses",
        "proceeds_from_disposal_of_ppe",
        "payments_for_financial_investments",
        "proceeds_from_short_term_financial_investments",
        "investing_cash_flow",
        "financing_cash_flow",
        "effect_of_exchange_rate_on_cash",
        "cash_and_equivalents_beginning_of_period",
        "net_increase_in_cash",
        "dividends_paid",
    },
}

LABELS = {
    "income_statement": {
        "revenue": ["revenue", "revenues", "sales", "sales and other operating revenues", "???????"],
        "gross_profit": ["gross profit", "gross income", "??????? ???????"],
        "cost_of_sales": ["cost of sales", "cost of goods sold", "????????????? ??????"],
        "other_income": ["other income", "?????? ??????"],
        "distribution_costs": ["distribution costs", "selling expenses", "commercial expenses", "???????????? ???????"],
        "administrative_expenses": [
            "administrative expenses",
            "general and administrative expenses",
            "administrative and general expenses",
            "????????????????? ? ???????????????? ???????",
        ],
        "other_expenses": ["other expenses", "?????? ???????"],
        "operating_profit": [
            "operating profit",
            "profit from operating activities",
            "??????? ?? ???????????? ????????????",
        ],
        "net_income": [
            "profit for the period",
            "profit for the year",
            "net income",
            "?????? ???????",
            "??????? ?? ???",
            "??????? ?? ??????",
        ],
        "net_income_attributable_to_parent": [
            "profit for the year attributable to equity holders of the parent",
            "profit for the year attributable to: equity holders of the parent",
            "profit attributable to equity holders of the parent",
            "profit attributable to owners of the parent",
        ],
        "total_comprehensive_income": [
            "total comprehensive income for the year",
            "total comprehensive income for the period",
            "total comprehensive income, net of tax",
        ],
        "total_comprehensive_income_attributable_to_parent": [
            "total comprehensive income for the year attributable to equity holders of the parent",
            "total comprehensive income for the year attributable to: equity holders of the parent",
            "total comprehensive income attributable to equity holders of the parent",
        ],
        "non_controlling_interests": [
            "non-controlling interests",
            "non-controlling interest",
            "non controlling interests",
            "non controlling interest",
        ],
        "finance_costs": ["finance costs", "financial costs", "?????????? ???????"],
        "finance_income": ["finance income", "financial income", "?????????? ??????"],
        "impairment_of_financial_assets": [
            "net impairment losses on financial assets",
            "impairment losses on financial assets",
        ],
        "net_foreign_exchange_result": [
            "net foreign exchange loss",
            "net foreign exchange gain",
            "net foreign exchange (loss)/gain",
            "net foreign exchange gain/loss",
        ],
        "income_tax_expense": ["income tax expense", "income tax", "?????? ?? ?????? ?? ???????"],
        "interest_income": ["interest income", "?????????? ??????"],
        "interest_expense": ["interest expense", "interest expenses", "?????????? ???????"],
        "fee_and_commission_income": ["fee and commission income", "fee commission income", "???????????? ??????"],
        "fee_and_commission_expense": [
            "fee and commission expense",
            "fee and commission expenses",
            "???????????? ???????",
        ],
        "net_interest_income": [
            "net interest income",
            "?????? ?????????? ?????",
            "?????? ?????????? ??????",
        ],
        "net_fee_commission_income": [
            "net fee and commission income",
            "net fee commission income",
            "?????? ???????????? ?????",
            "?????? ???????????? ??????",
        ],
        "net_trading_income": [
            "net trading income",
            "net gains from trading",
            "?????? ?????? ?? ???????? ? ??????????? ?????????????",
            "?????? ?????? ?? ???????? ????????",
        ],
        "operating_income": [
            "operating income",
            "total operating income",
            "???????????? ??????",
            "????? ???????????? ??????",
        ],
        "operating_expenses": [
            "operating expenses",
            "administrative and other operating expenses",
            "staff costs and administrative expenses",
            "personnel and administrative expenses",
            "???????????? ???????",
            "???????????????? ? ?????? ???????????? ???????",
            "??????? ?? ?????????? ????????? ? ???????????????? ???????",
        ],
        "impairment_charge": ["impairment losses", "credit loss allowance", "??????? ?? ????????? ???????"],
        "profit_before_tax": ["profit before tax", "profit before income tax", "??????? ?? ???????????????"],
    },
    "balance_sheet": {
        "total_assets": ["total assets", "????? ??????", "????? ???????", "??????"],
        "total_liabilities": ["total liabilities", "????? ????????????"],
        "total_equity": [
            "total equity",
            "total shareholders equity",
            "equity attributable to shareholders",
            "????? ??????????? ???????",
            "????? ????????",
            "????? ???????",
        ],
        "total_equity_and_liabilities": [
            "total equity and liabilities",
            "equity and liabilities",
            "total liabilities and equity",
        ],
        "current_assets": ["current assets", "total current assets", "????????? ??????", "????? ????????? ??????"],
        "current_liabilities": [
            "current liabilities",
            "total current liabilities",
            "current liabilities and provisions",
            "????????????? ?????????????",
            "????? ????????????? ?????????????",
        ],
        "non_current_assets": [
            "non current assets",
            "non-current assets",
            "total non current assets",
            "total non-current assets",
            "???????????? ??????",
            "??????????? ??????",
            "????? ???????????? ??????",
        ],
        "non_current_liabilities": [
            "non current liabilities",
            "non-current liabilities",
            "total non current liabilities",
            "total non-current liabilities",
            "???????????? ?????????????",
            "????? ???????????? ?????????????",
        ],
        "property_plant_and_equipment": ["property, plant and equipment", "property plant and equipment", "???????? ????????"],
        "right_of_use_assets": ["right-of-use assets", "right of use assets", "?????? ? ????? ????? ???????????"],
        "investment_properties": ["investment properties", "investment property", "?????????????? ????????????"],
        "goodwill": ["goodwill", "??????"],
        "intangible_assets": ["intangible assets", "other intangible assets", "?????????????? ??????"],
        "other_non_current_assets": ["other non-current assets", "other non current assets", "?????? ???????????? ??????"],
        "indemnification_asset": ["indemnification asset"],
        "interest_accrued": ["interest accrued", "accrued interest"],
        "inventories": ["inventories", "inventory", "??????"],
        "trade_and_other_receivables": [
            "trade and other accounts receivable and prepayments",
            "trade other accounts receivable and prepayments",
            "trade and other receivables",
            "trade receivables",
            "???????? ? ?????? ??????????? ?????????????",
        ],
        "current_income_tax_receivable": [
            "current income tax receivable",
            "income tax receivable",
            "?????? ?? ?????? ?? ???????",
        ],
        "vat_and_other_taxes_receivable": [
            "vat and other taxes receivable",
            "taxes receivable",
            "?????? ? ?????????? ????? ?????? ?? ???????",
        ],
        "short_term_financial_investments": [
            "short-term financial investments",
            "short term financial investments",
            "short-term investments",
            "????????????? ?????????? ????????",
            "?????????? ???????? ? ????? ????????",
        ],
        "other_financial_assets_current": [
            "other current financial assets",
            "other short-term financial assets",
            "?????? ????????????? ?????????? ??????",
        ],
        "other_financial_assets_non_current": [
            "other non-current financial assets",
            "other non current financial assets",
            "?????? ???????????? ?????????? ??????",
        ],
        "assets_held_for_sale": [
            "assets held for sale",
            "assets held for sale and discontinued operations",
            "?????? ??????????????? ??? ???????",
        ],
        "deferred_tax_assets": ["deferred tax assets", "?????????? ????????? ??????", "?????????? ????????? ?????"],
        "deferred_tax_liabilities": ["deferred tax liabilities", "deferred tax", "?????????? ????????? ?????????????"],
        "trade_accounts_payable": [
            "trade accounts payable",
            "trade payable",
            "trade and other payables",
            "???????? ???????????? ?????????????",
            "???????? ? ?????? ???????????? ?????????????",
        ],
        "borrowings_current": [
            "short-term borrowings",
            "short term borrowings",
            "current borrowings",
            "current portion of long-term borrowings",
            "????????????? ?????",
        ],
        "borrowings_non_current": [
            "long-term borrowings",
            "long term borrowings",
            "non-current borrowings",
            "???????????? ?????",
        ],
        "lease_liabilities_current": [
            "short-term lease liabilities",
            "short term lease liabilities",
            "current lease liabilities",
            "????????????? ????????????? ?? ??????",
        ],
        "lease_liabilities_non_current": [
            "long-term lease liabilities",
            "long term lease liabilities",
            "non-current lease liabilities",
            "???????????? ????????????? ?? ??????",
        ],
        "contract_liabilities_current": [
            "short-term contract liabilities",
            "short term contract liabilities",
            "current contract liabilities",
            "????????????? ????????????? ?? ?????????",
        ],
        "income_tax_payable": ["current income tax payable", "income tax payable", "??????? ????? ?? ??????? ? ??????"],
        "provisions_current": ["short-term provisions", "current provisions", "????????????? ???????"],
        "provisions_non_current": ["long-term provisions", "non-current provisions", "???????????? ???????"],
        "provisions_and_other_liabilities": [
            "provisions and other liabilities",
            "provision and other liabilities",
        ],
        "other_non_current_liabilities": [
            "other non-current liabilities",
            "other non current liabilities",
            "?????? ???????????? ?????????????",
        ],
        "share_capital": ["share capital", "issued capital", "??????????? ???????", "???????? ???????"],
        "share_premium": ["share premium", "??????????? ?????"],
        "retained_earnings": ["retained earnings", "???????????????? ???????"],
        "other_reserves": ["other capital reserves", "other reserves", "?????? ???????"],
        "cash_and_equivalents": [
            "cash and cash equivalents",
            "???????? ???????? ? ???????? ???????????",
            "???????? ???????? ? ?? ???????????",
            "???????? ???????? ? ????????????? ????????",
        ],
        "total_debt": ["total debt", "total borrowings", "loans and borrowings", "borrowings"],
        "loans_to_customers": ["loans and advances to customers", "loans to customers", "??????? ? ?????? ????????"],
        "retail_customer_accounts": ["amounts due to individuals", "retail customer accounts", "???????? ?????????? ???"],
        "corporate_customer_accounts": [
            "amounts due to corporate customers",
            "corporate customer accounts",
            "???????? ????????????? ????????",
        ],
        "customer_accounts": ["amounts due to customers", "customer accounts", "due to customers", "???????? ????????"],
    },
    "changes_in_equity": {
        "share_capital": [
            "share capital",
            "issued capital",
            "shareholders capital",
            "capital stock",
            "ordinary shares",
            "??????????? ???????",
            "???????? ???????",
        ],
        "share_premium": ["share premium", "additional paid-in capital", "additional capital", "??????????? ?????"],
        "retained_earnings": [
            "retained earnings",
            "retained profit",
            "accumulated profits",
            "accumulated earnings",
            "???????????????? ???????",
            "???????????????? ??????",
        ],
        "other_reserves": [
            "other capital reserves",
            "other reserves",
            "capital reserves",
            "revaluation reserve",
            "foreign currency translation reserve",
            "?????? ???????",
        ],
        "non_controlling_interests": [
            "non-controlling interests",
            "non-controlling interest",
            "non controlling interests",
            "non controlling interest",
        ],
        "total_equity": [
            "total equity",
            "equity attributable to equity holders of the parent and non-controlling interests",
            "total shareholders equity",
            "total shareholders' equity",
            "closing balance",
            "balance at 31 december",
            "РёС‚РѕРіРѕ РєР°РїРёС‚Р°Р»",
            "РєР°РїРёС‚Р°Р» Рё СЂРµР·РµСЂРІС‹",
        ],
    },
    "cash_flow": {
        "operating_cash_flow_before_working_capital": [
            "net cash from operating activities before changes in working capital",
            "net cash flows from operating activities before changes in working capital",
        ],
        "net_cash_from_operations": [
            "net cash flows from operations",
            "net cash from operations",
        ],
        "operating_cash_flow": [
            "net cash provided by operating activities",
            "net cash generated from operating activities",
            "net cash from operating activities",
            "net cash flows from operating activities",
            "cash flows from operating activities",
            "???????? ?????? ?? ???????????? ????????????",
            "?????? ???????? ???????? ?????????? ?? ???????????? ????????????",
        ],
        "capex": [
            "capital expenditures",
            "purchase of property plant and equipment",
            "purchases of property plant and equipment",
            "acquisition of property plant and equipment",
            "purchase of other intangible assets",
            "???????????? ???????? ???????",
            "??????????? ????????",
        ],
        "interest_paid": ["interest paid", "???????? ??????????"],
        "interest_received": ["interest received", "???????? ??????????"],
        "income_tax_paid": ["income tax paid", "taxes paid", "????? ?? ??????? ??????????"],
        "effect_of_exchange_rate_on_cash": [
            "effect of exchange rate changes on cash and cash equivalents",
            "effect of exchange rate changes on cash",
        ],
        "proceeds_from_borrowings": ["proceeds from loans", "proceeds from borrowings", "????????? ??????"],
        "repayment_of_borrowings": ["repayment of loans", "repayment of borrowings", "????????? ??????"],
        "profit_before_tax": ["profit before tax", "profit before income tax"],
        "finance_costs": ["finance costs, net", "finance costs net", "finance costs"],
        "cash_and_equivalents": [
            "cash and cash equivalents at the end of the year",
            "cash and cash equivalents at the end of the period",
            "cash and cash equivalents at year end",
            "cash and cash equivalents",
        ],
        "depreciation_amortization_and_impairment": [
            (
                "depreciation, amortisation and impairment of property, plant and equipment, "
                "right-of-use assets, investment properties, other intangible assets and goodwill"
            ),
            (
                "depreciation, amortization and impairment of property, plant and equipment, "
                "right-of-use assets, investment properties, other intangible assets and goodwill"
            ),
            "depreciation amortisation and impairment",
            "depreciation amortization and impairment",
        ],
        "gain_on_disposal_of_assets": [
            (
                "gain on disposal of property plant and equipment, investment properties and "
                "intangible assets and gain on derecognition of right-of-use assets"
            ),
            (
                "gain on disposal of property, plant and equipment, investment properties and "
                "intangible assets and gain on derecognition of right-of-use assets"
            ),
            "gain on disposal of assets",
        ],
        "impairment_of_financial_assets": [
            "net impairment losses on financial assets",
            "impairment losses on financial assets",
        ],
        "impairment_of_prepayments": ["impairment of prepayments"],
        "share_based_compensation_expense": ["share-based compensation expense", "share based compensation expense"],
        "net_foreign_exchange_result": [
            "net foreign exchange loss",
            "net foreign exchange gain",
            "net foreign exchange (loss)/gain",
        ],
        "other_non_cash_items": ["other non-cash items", "other non cash items"],
        "change_in_receivables": [
            "increase in trade, other accounts receivable and prepayments and vat and other taxes receivable",
            "increase in trade, other accounts receivable and prepayments and VAT and other taxes receivable",
            "increase in receivables",
        ],
        "change_in_inventories": ["increase in inventories", "decrease increase in inventories"],
        "change_in_trade_payables": ["increase in trade payable", "increase in trade payables"],
        "change_in_other_payables_and_contract_liabilities": [
            "increase in other accounts payable and contract liabilities",
            "increase in other payables and contract liabilities",
        ],
        "lease_principal_payments": [
            "payments of principal portion of lease liabilities",
            "lease liability principal payments",
            "????????? ???????????? ?? ??????",
        ],
        "acquisition_of_businesses": [
            "acquisition of businesses, net of cash acquired",
            "business acquisitions net of cash acquired",
        ],
        "proceeds_from_disposal_of_ppe": [
            "proceeds from disposal of property, plant and equipment",
            "proceeds from disposal of property plant and equipment",
        ],
        "payments_for_financial_investments": ["payments for financial investments", "purchase of financial investments"],
        "proceeds_from_short_term_financial_investments": [
            "proceeds from short-term financial investments",
            "proceeds from short term financial investments",
        ],
        "investing_cash_flow": [
            "net cash flows used in investing activities",
            "net cash used in investing activities",
            "net cash flows from investing activities",
            "net cash from investing activities",
        ],
        "financing_cash_flow": [
            "net cash flows used in financing activities",
            "net cash used in financing activities",
            "net cash flows from financing activities",
            "net cash from financing activities",
        ],
        "cash_and_equivalents_beginning_of_period": [
            "cash and cash equivalents at the beginning of the year",
            "cash and cash equivalents at the beginning of the period",
            "cash and cash equivalents at the beginning of year",
            "cash and cash equivalents at the beginning of period",
        ],
        "net_increase_in_cash": [
            "net increase in cash and cash equivalents",
            "net decrease increase in cash and cash equivalents",
        ],
        "dividends_paid": ["dividends paid", "????????? ??????????"],
    },
}

CONCEPT_POLICIES = {
    "income_statement": {
        metric_code: {
            "strict_exact_aliases": labels,
            "strict_contains_aliases": labels,
            "degraded_contains_aliases": [],
            "forbidden_context_markers": [],
        }
        for metric_code, labels in LABELS["income_statement"].items()
    },
    "cash_flow": {
        metric_code: {
            "strict_exact_aliases": labels,
            "strict_contains_aliases": labels,
            "degraded_contains_aliases": [],
            "forbidden_context_markers": [],
        }
        for metric_code, labels in LABELS["cash_flow"].items()
    },
    "balance_sheet": {
        metric_code: {
            "strict_exact_aliases": labels,
            "strict_contains_aliases": labels,
            "degraded_contains_aliases": [],
            "forbidden_context_markers": [],
        }
        for metric_code, labels in LABELS["balance_sheet"].items()
    },
    "changes_in_equity": {
        metric_code: {
            "strict_exact_aliases": labels,
            "strict_contains_aliases": labels,
            "degraded_contains_aliases": [],
            "forbidden_context_markers": [],
        }
        for metric_code, labels in LABELS["changes_in_equity"].items()
    },
}

CONCEPT_POLICIES["income_statement"]["revenue"] = {
    "strict_exact_aliases": LABELS["income_statement"]["revenue"],
    "strict_contains_aliases": ["sales and other operating revenues", "revenue", "sales"],
    "degraded_contains_aliases": ["sales revenue", "retail revenue", "sales"],
    "forbidden_context_markers": ["segment", "???????", "note", "??????", "other income"],
}
CONCEPT_POLICIES["income_statement"]["gross_profit"]["forbidden_context_markers"] = [
    "segment",
    "???????",
    "note",
    "??????",
    "other comprehensive income",
]
CONCEPT_POLICIES["income_statement"]["operating_profit"] = {
    "strict_exact_aliases": LABELS["income_statement"]["operating_profit"],
    "strict_contains_aliases": ["profit from operating activities", "operating profit"],
    "degraded_contains_aliases": ["operating result", "profit from operations", "result from operations"],
    "forbidden_context_markers": ["segment", "???????", "ebitda", "adjusted"],
}
CONCEPT_POLICIES["income_statement"]["net_income"] = {
    "strict_exact_aliases": LABELS["income_statement"]["net_income"],
    "strict_contains_aliases": ["profit for the year", "profit for the period", "net income"],
    "degraded_contains_aliases": ["net profit", "profit attributable"],
    "forbidden_context_markers": ["segment", "???????", "ebitda", "other comprehensive income"],
}
CONCEPT_POLICIES["income_statement"]["profit_before_tax"] = {
    "strict_exact_aliases": LABELS["income_statement"]["profit_before_tax"],
    "strict_contains_aliases": ["profit before tax", "profit before income tax"],
    "degraded_contains_aliases": ["earnings before tax"],
    "forbidden_context_markers": ["segment", "???????", "ebitda"],
}
for metric_code in [
    "cost_of_sales",
    "other_income",
    "distribution_costs",
    "administrative_expenses",
    "other_expenses",
    "finance_costs",
    "finance_income",
    "income_tax_expense",
]:
    CONCEPT_POLICIES["income_statement"][metric_code]["forbidden_context_markers"] = ["segment", "???????", "note", "??????"]
CONCEPT_POLICIES["income_statement"]["income_tax_expense"]["forbidden_context_markers"] = [
    "segment",
    "???????",
    "note",
    "??????",
    "before tax",
]

CONCEPT_POLICIES["cash_flow"]["operating_cash_flow"] = {
    "strict_exact_aliases": LABELS["cash_flow"]["operating_cash_flow"],
    "strict_contains_aliases": [
        "net cash provided by operating activities",
        "net cash generated from operating activities",
        "net cash flows from operating activities",
        "net cash flows from operations",
    ],
    "degraded_contains_aliases": [
        "cash generated from operating activities",
        "operating cash flow",
        "net cash from operating activities",
    ],
    "forbidden_context_markers": ["segment", "???????", "note", "??????"],
}
CONCEPT_POLICIES["cash_flow"]["operating_cash_flow_before_working_capital"] = {
    "strict_exact_aliases": LABELS["cash_flow"]["operating_cash_flow_before_working_capital"],
    "strict_contains_aliases": LABELS["cash_flow"]["operating_cash_flow_before_working_capital"],
    "degraded_contains_aliases": ["cash from operating activities before changes in working capital"],
    "forbidden_context_markers": ["segment", "???????", "note", "??????"],
}
CONCEPT_POLICIES["cash_flow"]["net_cash_from_operations"] = {
    "strict_exact_aliases": LABELS["cash_flow"]["net_cash_from_operations"],
    "strict_contains_aliases": LABELS["cash_flow"]["net_cash_from_operations"],
    "degraded_contains_aliases": [],
    "forbidden_context_markers": ["segment", "???????", "note", "??????"],
}
CONCEPT_POLICIES["cash_flow"]["capex"] = {
    "strict_exact_aliases": LABELS["cash_flow"]["capex"],
    "strict_contains_aliases": [
        "purchase of property plant and equipment",
        "purchases of property plant and equipment",
        "purchase of other intangible assets",
    ],
    "degraded_contains_aliases": [
        "acquisition of property plant and equipment and intangible assets",
        "purchase of ppe",
        "acquisition of ppe",
    ],
    "forbidden_context_markers": ["segment", "???????", "note", "??????"],
}
for metric_code in [
    "interest_paid",
    "interest_received",
    "income_tax_paid",
    "proceeds_from_borrowings",
    "repayment_of_borrowings",
    "lease_principal_payments",
    "acquisition_of_businesses",
    "proceeds_from_disposal_of_ppe",
    "payments_for_financial_investments",
    "net_increase_in_cash",
    "dividends_paid",
]:
    CONCEPT_POLICIES["cash_flow"][metric_code]["forbidden_context_markers"] = ["segment", "???????", "note", "??????"]
CONCEPT_POLICIES["cash_flow"]["dividends_paid"]["forbidden_context_markers"] = [
    "segment",
    "???????",
    "note",
    "??????",
    "non controlling",
    "non-controlling",
]
CONCEPT_POLICIES["changes_in_equity"]["total_equity"] = {
    "strict_exact_aliases": LABELS["changes_in_equity"]["total_equity"],
    "strict_contains_aliases": [
        "total equity",
        "total shareholders equity",
        "total shareholders' equity",
        "equity attributable to equity holders of the parent and non-controlling interests",
    ],
    "degraded_contains_aliases": ["closing balance", "capital and reserves"],
    "forbidden_context_markers": ["segment", "???????", "note", "??????"],
}
for metric_code in [
    "share_capital",
    "share_premium",
    "retained_earnings",
    "other_reserves",
    "non_controlling_interests",
]:
    CONCEPT_POLICIES["changes_in_equity"][metric_code]["forbidden_context_markers"] = [
        "segment",
        "???????",
        "note",
        "??????",
    ]

CONCEPT_POLICIES["balance_sheet"]["current_assets"]["forbidden_context_markers"] = [
    "other",
    "прочие",
    "inventories",
    "аванс",
]
CONCEPT_POLICIES["balance_sheet"]["current_liabilities"]["forbidden_context_markers"] = [
    "other",
    "прочие",
]


def _extend_unique_aliases(target: list[str], values: list[str]) -> None:
    existing = {str(item) for item in target}
    for value in values:
        if value not in existing:
            target.append(value)
            existing.add(value)


_extend_unique_aliases(
    LABELS["income_statement"]["revenue"],
    ["Выручка", "Выручка от реализации"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["operating_profit"],
    ["Операционная прибыль", "Прибыль от операционной деятельности", "Операционная прибыль (убыток)"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["net_income"],
    ["Чистая прибыль", "Прибыль за год", "Прибыль за период", "Прибыль, относящаяся к акционерам"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["operating_expenses"],
    ["Операционные расходы", "Коммерческие, общехозяйственные и административные расходы"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["profit_before_tax"],
    ["Прибыль до налогообложения", "Прибыль до налога на прибыль"],
)
_bank_income_aliases = {
    "interest_income": [
        "Процентные доходы",
        "Процентные доходы, рассчитанные по методу эффективной процентной ставки",
        "Прочие процентные доходы",
    ],
    "interest_expense": ["Процентные расходы"],
    "net_interest_income": [
        "Чистые процентные доходы",
        "Чистые процентные доходы после создания резерва под кредитные убытки",
    ],
    "fee_and_commission_income": ["Комиссионные доходы"],
    "fee_and_commission_expense": ["Комиссионные расходы"],
    "net_fee_commission_income": ["Чистые комиссионные доходы"],
    "net_trading_income": [
        "Доходы за вычетом расходов по операциям c финансовыми инструментами",
        "Доходы за вычетом расходов по операциям с финансовыми инструментами",
        "Доходы за вычетом расходов по операциям с иностранной валютой и драгоценными металлами",
    ],
    "operating_income": [
        "Операционные доходы",
        "Прочие непроцентные доходы от финансовой деятельности",
    ],
    "operating_expenses": [
        "Непроцентные расходы",
        "Прочие операционные расходы",
        "Расходы на содержание персонала и административные расходы",
    ],
    "impairment_charge": [
        "Создание резерва под кредитные убытки",
        "Восстановление резерва под кредитные убытки",
        "Восстановление/(создание) резерва под кредитные убытки",
        "Создание резерва под кредитные убытки по долговым финансовым активам",
    ],
    "profit_before_tax": ["Прибыль до налогообложения"],
    "income_tax_expense": [
        "Расход по налогу на прибыль",
        "Экономия/(расход) по налогу на прибыль",
    ],
    "net_income": [
        "Чистая прибыль",
        "Чистая прибыль после налогообложения",
    ],
    "net_income_attributable_to_parent": [
        "Чистая прибыль, приходящаяся на Акционеров материнского банка",
        "Чистая прибыль, приходящаяся на акционеров материнского банка",
    ],
    "non_controlling_interests": ["Неконтрольные доли участия"],
}
for _metric_code, _aliases in _bank_income_aliases.items():
    _extend_unique_aliases(LABELS["income_statement"][_metric_code], _aliases)
    _extend_unique_aliases(CONCEPT_POLICIES["income_statement"][_metric_code]["strict_contains_aliases"], _aliases)
    _extend_unique_aliases(CONCEPT_POLICIES["income_statement"][_metric_code]["degraded_contains_aliases"], _aliases)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["net_interest_income"]["forbidden_context_markers"],
    [
        "после создания резерва",
        "after credit loss allowance",
        "after allowance for credit losses",
    ],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["net_income"]["forbidden_context_markers"],
    [
        "от изменения справедливой стоимости",
        "от выбытия",
        "от дочерних компаний, приобретенных исключительно для перепродажи",
        "per share",
        "на одну акцию",
    ],
)
_extend_unique_aliases(
    LABELS["cash_flow"]["operating_cash_flow"],
    [
        "Денежные потоки от операционной деятельности",
        "Чистые денежные средства, полученные от операционной деятельности",
        "Чистый денежный поток от операционной деятельности",
    ],
)
_extend_unique_aliases(
    LABELS["cash_flow"]["capex"],
    [
        "Приобретение основных средств",
        "Приобретение основных средств и нематериальных активов",
        "Капитальные вложения",
    ],
)
_extend_unique_aliases(
    LABELS["income_statement"]["gross_profit"],
    ["\u0412\u0430\u043b\u043e\u0432\u0430\u044f \u043f\u0440\u0438\u0431\u044b\u043b\u044c"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["cost_of_sales"],
    ["\u0421\u0435\u0431\u0435\u0441\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c \u043f\u0440\u043e\u0434\u0430\u0436"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["other_income"],
    ["\u041f\u0440\u043e\u0447\u0438\u0435 \u0434\u043e\u0445\u043e\u0434\u044b"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["other_expenses"],
    ["\u041f\u0440\u043e\u0447\u0438\u0435 \u0440\u0430\u0441\u0445\u043e\u0434\u044b"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["finance_costs"],
    ["\u0424\u0438\u043d\u0430\u043d\u0441\u043e\u0432\u044b\u0435 \u0440\u0430\u0441\u0445\u043e\u0434\u044b"],
)
_extend_unique_aliases(
    LABELS["income_statement"]["finance_income"],
    ["\u0424\u0438\u043d\u0430\u043d\u0441\u043e\u0432\u044b\u0435 \u0434\u043e\u0445\u043e\u0434\u044b"],
)
_ru_label_aliases = {
    ("income_statement", "income_tax_expense"): [
        "Расход по налогу на прибыль",
    ],
    ("balance_sheet", "current_assets"): [
        "Оборотные активы",
        "Итого оборотные активы",
    ],
    ("balance_sheet", "current_liabilities"): [
        "Краткосрочные обязательства",
        "Итого краткосрочные обязательства",
    ],
    ("balance_sheet", "non_current_assets"): [
        "Внеоборотные активы",
        "Необоротные активы",
        "Итого внеоборотные активы",
    ],
    ("balance_sheet", "non_current_liabilities"): [
        "Долгосрочные обязательства",
        "Итого долгосрочные обязательства",
    ],
    ("balance_sheet", "property_plant_and_equipment"): [
        "Основные средства",
    ],
    ("balance_sheet", "right_of_use_assets"): [
        "Активы в форме права пользования",
    ],
    ("balance_sheet", "goodwill"): ["Гудвил"],
    ("balance_sheet", "intangible_assets"): [
        "Нематериальные активы",
        "Прочие нематериальные активы",
    ],
    ("balance_sheet", "inventories"): ["Запасы"],
    ("balance_sheet", "trade_and_other_receivables"): [
        "Торговая и прочая дебиторская задолженность",
        "Торговая дебиторская задолженность",
    ],
    ("balance_sheet", "current_income_tax_receivable"): [
        "Авансы по налогу на прибыль",
    ],
    ("balance_sheet", "vat_and_other_taxes_receivable"): [
        "Налоги к возмещению кроме налога на прибыль",
    ],
    ("balance_sheet", "short_term_financial_investments"): [
        "Банковские депозиты и займы выданные",
    ],
    ("balance_sheet", "other_financial_assets_current"): [
        "Прочие краткосрочные финансовые активы",
    ],
    ("balance_sheet", "other_financial_assets_non_current"): [
        "Прочие долгосрочные финансовые активы",
    ],
    ("balance_sheet", "assets_held_for_sale"): [
        "Активы предназначенные для продажи",
    ],
    ("balance_sheet", "deferred_tax_assets"): [
        "Отложенный налоговый актив",
        "Отложенные налоговые активы",
    ],
    ("balance_sheet", "deferred_tax_liabilities"): [
        "Отложенные налоговые обязательства",
    ],
    ("balance_sheet", "trade_accounts_payable"): [
        "Торговая и прочая кредиторская задолженность",
        "Торговая кредиторская задолженность",
    ],
    ("balance_sheet", "borrowings_current"): [
        "Краткосрочные займы",
    ],
    ("balance_sheet", "borrowings_non_current"): [
        "Долгосрочные займы",
    ],
    ("balance_sheet", "lease_liabilities_current"): [
        "Краткосрочные обязательства по аренде",
    ],
    ("balance_sheet", "lease_liabilities_non_current"): [
        "Долгосрочные обязательства по аренде",
    ],
    ("balance_sheet", "contract_liabilities_current"): [
        "Краткосрочные обязательства по договорам",
    ],
    ("balance_sheet", "income_tax_payable"): [
        "Текущий налог на прибыль к уплате",
    ],
    ("balance_sheet", "cash_and_equivalents"): [
        "Денежные средства и их эквиваленты",
    ],
    ("cash_flow", "interest_paid"): [
        "Проценты уплаченные",
    ],
    ("cash_flow", "interest_received"): [
        "Проценты полученные",
    ],
    ("cash_flow", "income_tax_paid"): [
        "Налог на прибыль уплаченный",
    ],
}

for (statement_type, metric_code), aliases in _ru_label_aliases.items():
    _extend_unique_aliases(LABELS[statement_type][metric_code], aliases)

_bank_balance_sheet_aliases = {
    "total_assets": ["Итого активов", "Всего активов", "Активы"],
    "total_liabilities": ["Итого обязательств", "Всего обязательств", "Обязательства"],
    "total_equity": [
        "Итого собственных средств",
        "Всего собственных средств",
        "Итого капитала",
        "Собственные средства",
        "Собственный капитал",
    ],
    "cash_and_equivalents": [
        "Денежные средства и их эквиваленты",
        "Денежные средства",
        "Средства в Банке России",
        "Средства в Центральном банке",
    ],
    "loans_to_customers": [
        "Кредиты и авансы клиентам",
        "Кредиты клиентам",
        "Кредиты и задолженность клиентов",
        "Ссуды клиентам",
    ],
    "customer_accounts": [
        "Средства клиентов",
        "Средства клиентов и банков",
        "Средства корпоративных клиентов и физических лиц",
        "Средства клиентов, не являющихся кредитными организациями",
    ],
    "retail_customer_accounts": [
        "Средства физических лиц",
        "Средства розничных клиентов",
        "Вклады физических лиц",
    ],
    "corporate_customer_accounts": [
        "Средства корпоративных клиентов",
        "Средства юридических лиц",
    ],
}

for _metric_code, _aliases in _bank_balance_sheet_aliases.items():
    _extend_unique_aliases(LABELS["balance_sheet"][_metric_code], _aliases)
    _extend_unique_aliases(CONCEPT_POLICIES["balance_sheet"][_metric_code]["strict_contains_aliases"], _aliases)
    _extend_unique_aliases(CONCEPT_POLICIES["balance_sheet"][_metric_code]["degraded_contains_aliases"], _aliases)

_canonical_bank_balance_sheet_aliases = {
    "total_assets": ["Итого активы", "Итого активов", "Всего активов", "Активы"],
    "total_liabilities": [
        "Итого обязательства",
        "Итого обязательств",
        "Всего обязательств",
        "Обязательства",
    ],
    "total_equity": [
        "Итого собственные средства",
        "Всего собственные средства",
        "Собственные средства",
        "Итого капитала",
        "Итого капитал",
        "Собственный капитал",
    ],
    "cash_and_equivalents": [
        "Денежные средства и краткосрочные активы",
        "Денежные средства и их эквиваленты",
        "Денежные средства",
        "Средства в центральных банках",
        "Средства в Банке России",
    ],
    "loans_to_customers": [
        "Кредиты и авансы клиентам",
        "Кредиты клиентам",
        "Кредиты и задолженность клиентов",
        "Ссуды клиентам",
    ],
}

for _metric_code, _aliases in _canonical_bank_balance_sheet_aliases.items():
    _extend_unique_aliases(LABELS["balance_sheet"][_metric_code], _aliases)
    _extend_unique_aliases(CONCEPT_POLICIES["balance_sheet"][_metric_code]["strict_contains_aliases"], _aliases)
    _extend_unique_aliases(CONCEPT_POLICIES["balance_sheet"][_metric_code]["degraded_contains_aliases"], _aliases)

_extend_unique_aliases(
    CONCEPT_POLICIES["balance_sheet"]["loans_to_customers"]["forbidden_context_markers"],
    ["залож", "заложен", "залог", "залон", "залоне", "репо", "repo", "pledged"],
)

_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["revenue"]["strict_contains_aliases"],
    ["Выручка", "Выручка от реализации"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["revenue"]["degraded_contains_aliases"],
    ["выручка от реализации"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["revenue"]["forbidden_context_markers"],
    ["сегмент", "примеч"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["operating_profit"]["strict_contains_aliases"],
    ["Операционная прибыль", "Прибыль от операционной деятельности"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["operating_profit"]["degraded_contains_aliases"],
    ["операционная прибыль"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["operating_profit"]["forbidden_context_markers"],
    ["сегмент"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["net_income"]["strict_contains_aliases"],
    ["Чистая прибыль", "Прибыль за год", "Прибыль за период"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["net_income"]["degraded_contains_aliases"],
    ["чистая прибыль"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["net_income"]["forbidden_context_markers"],
    ["сегмент", "прочий совокупный доход"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["profit_before_tax"]["strict_contains_aliases"],
    ["Прибыль до налогообложения", "Прибыль до налога на прибыль"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["profit_before_tax"]["degraded_contains_aliases"],
    ["прибыль до налогообложения"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["income_statement"]["profit_before_tax"]["forbidden_context_markers"],
    ["сегмент"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["cash_flow"]["operating_cash_flow"]["strict_contains_aliases"],
    ["Денежные потоки от операционной деятельности", "Чистые денежные средства, полученные от операционной деятельности"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["cash_flow"]["operating_cash_flow"]["degraded_contains_aliases"],
    ["чистый денежный поток от операционной деятельности"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["cash_flow"]["operating_cash_flow"]["forbidden_context_markers"],
    ["сегмент", "примеч"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["cash_flow"]["capex"]["strict_contains_aliases"],
    ["Приобретение основных средств"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["cash_flow"]["capex"]["degraded_contains_aliases"],
    ["приобретение основных средств и нематериальных активов", "капитальные вложения"],
)
_extend_unique_aliases(
    CONCEPT_POLICIES["cash_flow"]["capex"]["forbidden_context_markers"],
    ["сегмент", "примеч"],
)


def normalize_label(value: str) -> str:
    normalized = normalize_matching_text(value)
    return re.sub(r"^(?:[-−]?\(?\d[\d\s.,]*\)?%?\s+){1,4}(?=[A-Za-zА-Яа-я])", "", normalized).strip()


def concept_policy(statement_type: str, metric_code: str) -> dict[str, Any]:
    return CONCEPT_POLICIES.get(statement_type, {}).get(metric_code, {})


def normalized_aliases(values: list[str]) -> list[str]:
    aliases: list[str] = []
    for value in values:
        normalized = normalize_label(value)
        if normalized:
            aliases.append(normalized)
    return aliases


def has_forbidden_context(normalized: str, markers: list[str]) -> bool:
    return any(contains_normalized_marker(normalized, marker) for marker in markers)


def match_metric(raw_label: str, statement_type: str, *, degraded: bool = False) -> str | None:
    normalized = normalize_label(raw_label)
    if statement_type == "balance_sheet" and "капитал" in normalized and "обязател" in normalized and "итого" in normalized:
        return None
    for metric_code, labels in LABELS.get(statement_type, {}).items():
        policy = concept_policy(statement_type, metric_code)
        if has_forbidden_context(normalized, policy.get("forbidden_context_markers", [])):
            continue
        for label in normalized_aliases(policy.get("strict_exact_aliases", labels)):
            if normalized == label:
                return metric_code
    best: tuple[str | None, int] = (None, 0)
    for metric_code, labels in LABELS.get(statement_type, {}).items():
        policy = concept_policy(statement_type, metric_code)
        if has_forbidden_context(normalized, policy.get("forbidden_context_markers", [])):
            continue
        if metric_code == "current_assets" and any(
            contains_normalized_marker(normalized, marker) for marker in ("other", "other current", "прочие", "аванс")
        ):
            continue
        if metric_code == "current_liabilities" and any(
            contains_normalized_marker(normalized, marker) for marker in ("other", "прочие")
        ):
            continue
        if metric_code == "current_assets" and contains_non_current_assets_marker(normalized):
            continue
        if metric_code == "current_liabilities" and contains_non_current_liabilities_marker(normalized):
            continue
        contains_aliases = list(policy.get("strict_contains_aliases", labels))
        if degraded:
            contains_aliases.extend(policy.get("degraded_contains_aliases", []))
        for candidate in normalized_aliases(contains_aliases):
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
    return any(contains_normalized_marker(normalized_label, marker) for marker in markers)


def contains_non_current_liabilities_marker(normalized_label: str) -> bool:
    markers = (
        "non current liabilities",
        "non-current liabilities",
        "долгосрочные обязательства",
    )
    return any(contains_normalized_marker(normalized_label, marker) for marker in markers)


def normalized_row_block_fallback(row: dict[str, Any]) -> dict[str, Any]:
    raw_label = first_text_cell(row)
    normalized = normalize_label(raw_label or "")
    row_kind = "unknown"
    if raw_label and row_has_numeric_values(row):
        if contains_normalized_marker(normalized, "итого", "total"):
            row_kind = "grand_total"
        elif contains_normalized_marker(
            normalized,
            "current assets",
            "current liabilities",
            "оборотные активы",
            "краткосрочные обязательства",
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


@dataclass
class StatementRowInterpretationContext:
    raw_label: str
    row_kind: str
    issuer_class: str | None = None
    degraded: bool = False


class StatementRowInterpreter:
    statement_type: str = "unknown"
    allowed_row_kinds: set[str] = set()

    def metric_code(self, context: StatementRowInterpretationContext) -> str | None:
        return match_metric(context.raw_label, self.statement_type, degraded=context.degraded)

    def interpret(self, context: StatementRowInterpretationContext) -> tuple[str | None, str | None]:
        if context.row_kind not in self.allowed_row_kinds:
            return None, "row_kind_not_statement_line_item"
        return self.metric_code(context), None


class BalanceSheetRowInterpreter(StatementRowInterpreter):
    statement_type = "balance_sheet"
    allowed_row_kinds = {"statement_line_item", "subtotal", "grand_total"}

    def interpret(self, context: StatementRowInterpretationContext) -> tuple[str | None, str | None]:
        normalized = normalize_label(context.raw_label)
        metric_code = self.metric_code(context)
        ownership_blocker = balance_sheet_metric_ownership_blocker(metric_code, context.raw_label)
        if ownership_blocker:
            return None, ownership_blocker
        if metric_code in {"current_assets", "current_liabilities", "non_current_assets", "non_current_liabilities"}:
            if contains_normalized_marker(
                normalized,
                "total current",
                "total non current",
                "total non-current",
                "итого",
                "total",
                "current assets",
                "current liabilities",
                "non current assets",
                "non-current assets",
                "non current liabilities",
                "non-current liabilities",
                "оборотные активы",
                "краткосрочные обязательства",
                "внеоборотные активы",
                "необоротные активы",
                "долгосрочные обязательства",
            ):
                return metric_code, None
            return None, "component_row_not_total_metric"
        if metric_code == "total_equity_and_liabilities":
            if context.row_kind not in {"subtotal", "grand_total"}:
                return None, "subtotal_without_statement_context"
            if not contains_normalized_marker(normalized, "equity", "liabilit", "капитал", "обяз"):
                return None, "label_ownership_unresolved"
            return metric_code, None
        if metric_code in {"total_assets", "total_equity", "total_liabilities"} and context.row_kind not in {
            "subtotal",
            "grand_total",
        }:
            return None, "subtotal_without_statement_context"
        if context.row_kind == "grand_total" and not contains_normalized_marker(
            normalized,
            "актив",
            "asset",
            "капитал",
            "equity",
            "own funds",
            "\u0441\u043e\u0431\u0441\u0442\u0432\u0435\u043d\u043d",
            "обяз",
            "liabilit",
        ):
            return None, "component_row_not_total_metric"
        return metric_code, None


class IncomeStatementRowInterpreter(StatementRowInterpreter):
    statement_type = "income_statement"
    allowed_row_kinds = {"statement_line_item", "subtotal", "grand_total"}

    def interpret(self, context: StatementRowInterpretationContext) -> tuple[str | None, str | None]:
        metric_code = self.metric_code(context)
        normalized = normalize_label(context.raw_label)
        ownership_blocker = income_statement_metric_ownership_blocker(metric_code, context.raw_label)
        if ownership_blocker:
            return None, ownership_blocker
        if (context.issuer_class or "").startswith("industrial"):
            revenue_aliases = normalized_aliases(concept_policy("income_statement", "revenue").get("strict_exact_aliases", []))
            has_segment_like_context = any(
                contains_normalized_marker(normalized, marker) for marker in ("segment", "сегмент", "прочие", "other income")
            )
            if has_segment_like_context and any(alias in normalized for alias in revenue_aliases):
                return None, "component_row_not_total_metric"
        if context.row_kind not in self.allowed_row_kinds:
            return None, "row_kind_not_statement_line_item"
        return metric_code, None


class CashFlowRowInterpreter(StatementRowInterpreter):
    statement_type = "cash_flow"
    allowed_row_kinds = {"statement_line_item", "subtotal"}

    def interpret(self, context: StatementRowInterpretationContext) -> tuple[str | None, str | None]:
        metric_code = self.metric_code(context)
        normalized = normalize_label(context.raw_label)
        if context.row_kind not in self.allowed_row_kinds:
            return None, "row_kind_not_statement_line_item"
        if metric_code == "cash_and_equivalents":
            if contains_normalized_marker(
                normalized,
                "effect of exchange rate",
                "beginning of the year",
                "beginning of the period",
            ):
                return None, "component_row_not_total_metric"
            if not contains_normalized_marker(
                normalized,
                "at the end of the year",
                "at the end of the period",
                "at year end",
                "at period end",
                "end of year",
                "end of period",
            ):
                return None, "label_ownership_unresolved"
        return metric_code, None


class ChangesInEquityRowInterpreter(StatementRowInterpreter):
    statement_type = "changes_in_equity"
    allowed_row_kinds = {"statement_line_item", "subtotal", "grand_total"}

    def interpret(self, context: StatementRowInterpretationContext) -> tuple[str | None, str | None]:
        metric_code = self.metric_code(context)
        normalized = normalize_label(context.raw_label)
        if context.row_kind not in self.allowed_row_kinds:
            return None, "row_kind_not_statement_line_item"
        if metric_code == "total_equity":
            if not contains_normalized_marker(
                normalized,
                "total equity",
                "total shareholders equity",
                "total shareholders' equity",
                "equity",
                "capital",
                "РєР°РїРёС‚Р°Р»",
            ):
                return None, "label_ownership_unresolved"
            return metric_code, None
        return metric_code, None


BALANCE_SHEET_INTERPRETER = BalanceSheetRowInterpreter()
INCOME_STATEMENT_INTERPRETER = IncomeStatementRowInterpreter()
CASH_FLOW_INTERPRETER = CashFlowRowInterpreter()
CHANGES_IN_EQUITY_INTERPRETER = ChangesInEquityRowInterpreter()


def interpret_balance_sheet_row(raw_label: str, row_kind: str) -> tuple[str | None, str | None]:
    return BALANCE_SHEET_INTERPRETER.interpret(StatementRowInterpretationContext(raw_label=raw_label, row_kind=row_kind))
    normalized = normalize_label(raw_label)
    metric_code = match_metric(raw_label, "balance_sheet")
    if metric_code in {"current_assets", "current_liabilities"}:
        if any(marker in normalized for marker in ("total current", "итого", "total", "current assets", "current liabilities")):
            return metric_code, None
        return None, "component_row_not_total_metric"
    if metric_code in {"total_assets", "total_equity", "total_liabilities"} and row_kind not in {"subtotal", "grand_total"}:
        return None, "subtotal_without_statement_context"
    if row_kind == "grand_total" and not any(
        marker in normalized for marker in ("актив", "asset", "капитал", "equity", "обяз", "liabilit")
    ):
        return None, "component_row_not_total_metric"
    return metric_code, None


def interpret_income_statement_row(
    raw_label: str,
    row_kind: str,
    issuer_class: str,
    *,
    degraded: bool = False,
) -> tuple[str | None, str | None]:
    return INCOME_STATEMENT_INTERPRETER.interpret(
        StatementRowInterpretationContext(
            raw_label=raw_label,
            row_kind=row_kind,
            issuer_class=issuer_class,
            degraded=degraded,
        )
    )
    metric_code = match_metric(raw_label, "income_statement", degraded=degraded)
    normalized = normalize_label(raw_label)
    if issuer_class.startswith("industrial") and metric_code == "revenue":
        if any(marker in normalized for marker in ("segment", "сегмент", "прочие", "other")):
            return None, "component_row_not_total_metric"
    if row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
        return None, "row_kind_not_statement_line_item"
    return metric_code, None


def interpret_cash_flow_row(raw_label: str, row_kind: str, *, degraded: bool = False) -> tuple[str | None, str | None]:
    return CASH_FLOW_INTERPRETER.interpret(
        StatementRowInterpretationContext(raw_label=raw_label, row_kind=row_kind, degraded=degraded)
    )
    if row_kind not in {"statement_line_item", "subtotal"}:
        return None, "row_kind_not_statement_line_item"
    return match_metric(raw_label, "cash_flow", degraded=degraded), None


def interpret_changes_in_equity_row(
    raw_label: str,
    row_kind: str,
    *,
    degraded: bool = False,
) -> tuple[str | None, str | None]:
    return CHANGES_IN_EQUITY_INTERPRETER.interpret(
        StatementRowInterpretationContext(raw_label=raw_label, row_kind=row_kind, degraded=degraded)
    )


def first_text_cell(row: dict[str, Any]) -> str | None:
    preferred = normalize_label(row.get("line")) if isinstance(row, dict) and row.get("line") else ""
    if preferred:
        return row.get("line")
    for value in row.values():
        text = str(value or "").strip()
        if text and parse_number(text) is None:
            return text
    return None


def preferred_row_label(row: dict[str, Any], row_block: dict[str, Any] | None) -> str | None:
    row_block = row_block or {}
    line_value = row.get("line")
    if isinstance(line_value, str) and line_value.strip():
        cleaned_line = cleaned_statement_label(line_value, row)
        if cleaned_line:
            return cleaned_line
        return line_value
    if row_block.get("label_text"):
        cleaned_label = cleaned_statement_label(str(row_block.get("label_text")), row)
        if cleaned_label:
            return cleaned_label
        return str(row_block.get("label_text"))
    return first_text_cell(row)


def cleaned_statement_label(raw_label: str | None, row: dict[str, Any] | None) -> str | None:
    if not raw_label:
        return raw_label
    if not row_has_separate_numeric_cells(row):
        return raw_label
    normalized_raw_label = normalize_label(raw_label)
    if contains_normalized_marker(normalized_raw_label, "note", "notes", "прим", "примеч"):
        return raw_label
    numeric_cell_count = len(row_numeric_items(row))
    recovered = split_inline_label_and_values(str(raw_label))
    if recovered:
        label, numbers = recovered
        if label and len(numbers) >= 2 and len(numbers) == numeric_cell_count:
            raw_label = label
    section_suffix = strip_generic_statement_prefix(str(raw_label or ""))
    if section_suffix:
        raw_label = section_suffix
    embedded_total_suffix = embedded_statement_total_suffix(str(raw_label or ""))
    if embedded_total_suffix:
        raw_label = embedded_total_suffix
    semantic_suffix = semantic_label_suffix(str(raw_label or ""))
    if semantic_suffix:
        raw_label = semantic_suffix
    label_without_note_ref = re.sub(r"\s+\d{1,3}$", "", str(raw_label or "")).strip()
    if label_without_note_ref:
        return label_without_note_ref
    return raw_label


def balance_sheet_metric_ownership_blocker(metric_code: str | None, raw_label: str) -> str | None:
    if metric_code == "total_equity_and_liabilities":
        return None
    if metric_code not in {
        "current_assets",
        "current_liabilities",
        "non_current_assets",
        "non_current_liabilities",
        "cash_and_equivalents",
        "total_assets",
        "total_liabilities",
        "total_equity",
    }:
        return None
    prefix = semantic_label_leading_text("balance_sheet", metric_code, raw_label)
    if prefix and contains_meaningful_semantic_text(prefix) and not is_generic_balance_sheet_prefix(prefix):
        return "label_ownership_unresolved"
    suffix = semantic_label_trailing_text("balance_sheet", metric_code, raw_label)
    if suffix and contains_meaningful_semantic_text(suffix):
        return "label_ownership_unresolved"
    return None


def income_statement_metric_ownership_blocker(metric_code: str | None, raw_label: str) -> str | None:
    normalized = normalize_label(raw_label)
    if contains_normalized_marker(normalized, "earnings per share", "basic earnings per share", "diluted earnings per share"):
        return "per_share_row_not_statement_fact"
    if metric_code == "net_income_attributable_to_parent":
        return None
    if metric_code != "net_income":
        return None
    if contains_normalized_marker(
        normalized,
        "attributable to",
        "equity holders of the parent",
        "non controlling",
        "non-controlling",
        "owners of the parent",
        "total comprehensive income",
    ):
        return "label_ownership_unresolved"
    return None


def semantic_label_trailing_text(statement_type: str, metric_code: str, raw_label: str) -> str:
    normalized_label = normalize_label(raw_label)
    best_alias = matched_metric_alias(statement_type, metric_code, normalized_label)
    if not best_alias:
        return ""
    _, end = best_alias
    return normalized_label[end:].strip(" -:;,")


def semantic_label_leading_text(statement_type: str, metric_code: str, raw_label: str) -> str:
    normalized_label = normalize_label(raw_label)
    best_alias = matched_metric_alias(statement_type, metric_code, normalized_label)
    if not best_alias:
        return ""
    start, _ = best_alias
    return normalized_label[:start].strip(" -:;,")


def matched_metric_alias(statement_type: str, metric_code: str, normalized_label: str) -> tuple[int, int] | None:
    aliases = normalized_aliases(concept_policy(statement_type, metric_code).get("strict_exact_aliases", []))
    for alias in sorted(aliases, key=len, reverse=True):
        index = normalized_label.find(alias)
        if index >= 0:
            return index, index + len(alias)
    return None


def contains_meaningful_semantic_text(text: str) -> bool:
    cleaned = re.sub(r"\(?-?\d[\d\s,.\-]*\)?", " ", str(text or ""))
    cleaned = re.sub(r"\b(note|notes|прим|примеч)\b", " ", cleaned, flags=re.IGNORECASE)
    return bool(re.search(r"[A-Za-zА-Яа-я]{2,}", cleaned))


def is_generic_balance_sheet_prefix(text: str) -> bool:
    normalized = normalize_label(text)
    if normalized in {"assets", "liabilities", "equity", "активы", "обязательства", "капитал"}:
        return True
    generic_prefixes = {
        "current assets",
        "non current assets",
        "current liabilities",
        "non current liabilities",
        "equity and liabilities",
        "equity attributable to equity holders of the parent",
        "cash flows from operating activities",
        "cash flows from investing activities",
        "cash flows from financing activities",
        "movements in cash and cash equivalents",
        "assets non current assets",
        "assets current assets",
        "liabilities current liabilities",
        "liabilities non current liabilities",
        "оборотные активы",
        "внеоборотные активы",
        "краткосрочные обязательства",
        "долгосрочные обязательства",
        "денежные потоки от операционной деятельности",
        "денежные потоки от инвестиционной деятельности",
        "денежные потоки от финансовой деятельности",
    }
    if normalized in generic_prefixes:
        return True
    return any(normalized.startswith(prefix + " ") for prefix in generic_prefixes)


GENERIC_STATEMENT_PREFIXES = {
    "assets",
    "liabilities",
    "equity",
    "current assets",
    "non current assets",
    "current liabilities",
    "non current liabilities",
    "equity and liabilities",
    "equity attributable to equity holders of the parent",
    "cash flows from operating activities",
    "cash flows from investing activities",
    "cash flows from financing activities",
    "movements in cash and cash equivalents",
    "assets non current assets",
    "assets current assets",
    "liabilities current liabilities",
    "liabilities non current liabilities",
    "активы",
    "обязательства",
    "капитал",
    "оборотные активы",
    "внеоборотные активы",
    "краткосрочные обязательства",
    "долгосрочные обязательства",
    "денежные потоки от операционной деятельности",
    "денежные потоки от инвестиционной деятельности",
    "денежные потоки от финансовой деятельности",
}


def is_generic_statement_prefix(text: str) -> bool:
    normalized = normalize_label(text)
    if normalized in GENERIC_STATEMENT_PREFIXES:
        return True
    return any(normalized.startswith(prefix + " ") for prefix in GENERIC_STATEMENT_PREFIXES)


_GENERIC_STATEMENT_PREFIX_TOKEN_SEQUENCES: list[list[str]] | None = None


def generic_statement_prefix_token_sequences() -> list[list[str]]:
    global _GENERIC_STATEMENT_PREFIX_TOKEN_SEQUENCES
    if _GENERIC_STATEMENT_PREFIX_TOKEN_SEQUENCES is not None:
        return _GENERIC_STATEMENT_PREFIX_TOKEN_SEQUENCES
    sequences = [
        [token for token in normalize_label(prefix).split() if token]
        for prefix in GENERIC_STATEMENT_PREFIXES
    ]
    sequences = [tokens for tokens in sequences if tokens]
    sequences.sort(key=len, reverse=True)
    _GENERIC_STATEMENT_PREFIX_TOKEN_SEQUENCES = sequences
    return _GENERIC_STATEMENT_PREFIX_TOKEN_SEQUENCES


def strip_generic_statement_prefix(raw_label: str) -> str | None:
    raw_label = str(raw_label or "").strip()
    if not raw_label:
        return None
    normalized_label = normalize_label(raw_label)
    for prefix in sorted(GENERIC_STATEMENT_PREFIXES, key=len, reverse=True):
        normalized_prefix = normalize_label(prefix)
        if not normalized_label.startswith(normalized_prefix + " "):
            continue
        words = [word for word in normalized_prefix.split() if word]
        if not words:
            continue
        pattern = r"^\s*" + r"[\s\-]+".join(re.escape(word) for word in words) + r"[\s\-:;,]+"
        remainder = re.sub(pattern, "", raw_label, count=1, flags=re.IGNORECASE).strip(" -:;,")
        if remainder and contains_meaningful_semantic_text(remainder):
            return remainder
    return None


def embedded_statement_total_suffix(raw_label: str) -> str | None:
    normalized = normalize_label(raw_label)
    total_aliases = [
        "net cash flows used in investing activities",
        "net cash used in investing activities",
        "net cash flows from investing activities",
        "net cash from investing activities",
        "net cash flows used in financing activities",
        "net cash used in financing activities",
        "net cash flows from financing activities",
        "net cash from financing activities",
    ]
    best_index: int | None = None
    best_alias = ""
    for alias in total_aliases:
        index = normalized.find(alias)
        if index <= 0:
            continue
        if len(alias) > len(best_alias):
            best_index = index
            best_alias = alias
    if best_index is None:
        return None
    raw_lower = str(raw_label or "").lower()
    alias_index = raw_lower.find(best_alias)
    if alias_index < 0:
        alias_index = max(0, len(raw_label) - len(best_alias))
    prefix = str(raw_label or "")[:alias_index]
    if "-" not in prefix and "cash flows from" not in prefix.lower():
        return None
    suffix = str(raw_label or "")[alias_index:].strip(" -:;,")
    return suffix or None


def semantic_label_suffix(raw_label: str) -> str | None:
    tokens = str(raw_label or "").split()
    if len(tokens) < 2:
        return None
    normalized_tokens = [normalize_label(token) for token in tokens]
    best_start: int | None = None
    best_length = 0
    for alias_tokens in semantic_alias_token_sequences():
        width = len(alias_tokens)
        if width < 2 or width > len(tokens):
            continue
        for index in range(0, len(tokens) - width + 1):
            if normalized_tokens[index : index + width] == alias_tokens:
                if index == 0:
                    continue
                prefix = " ".join(tokens[:index]).strip()
                if prefix and contains_meaningful_semantic_text(prefix) and not is_generic_statement_prefix(prefix):
                    continue
                if width > best_length:
                    best_start = index
                    best_length = width
                break
    if best_start is None:
        return None
    return " ".join(tokens[best_start:]).strip()


_SEMANTIC_ALIAS_TOKEN_SEQUENCES: list[list[str]] | None = None


def semantic_alias_token_sequences() -> list[list[str]]:
    global _SEMANTIC_ALIAS_TOKEN_SEQUENCES
    if _SEMANTIC_ALIAS_TOKEN_SEQUENCES is not None:
        return _SEMANTIC_ALIAS_TOKEN_SEQUENCES
    sequences: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for statement_aliases in LABELS.values():
        for aliases in statement_aliases.values():
            for alias in aliases:
                normalized = normalize_label(alias)
                tokens = tuple(token for token in normalized.split() if token)
                if len(tokens) < 2 or tokens in seen:
                    continue
                seen.add(tokens)
                sequences.append(list(tokens))
    sequences.sort(key=len, reverse=True)
    _SEMANTIC_ALIAS_TOKEN_SEQUENCES = sequences
    return _SEMANTIC_ALIAS_TOKEN_SEQUENCES


def source_engine_for_table(table: dict[str, Any]) -> str | None:
    location = table_traceability(table)
    return (
        location.get("source_engine")
        or table.get("source_engine")
        or location.get("extraction_method")
        or table.get("extraction_method")
    )


def source_table_id_for_table(table: dict[str, Any]) -> str | None:
    location = table_traceability(table)
    return location.get("source_table_id") or table.get("source_table_id")


def fusion_status_for_table(table: dict[str, Any]) -> str:
    location = table_traceability(table)
    return str(location.get("fusion_status") or "single_engine")


def source_engines_for_table(table: dict[str, Any]) -> list[str]:
    location = table_traceability(table)
    engines = location.get("source_engines_involved")
    if isinstance(engines, list) and engines:
        return [str(engine) for engine in engines]
    engine = source_engine_for_table(table)
    return [engine] if engine else []


def row_block_traceability(row_block: dict[str, Any] | None, table: dict[str, Any]) -> dict[str, Any]:
    row_block = row_block or {}
    traceability = row_block.get("source_traceability")
    if isinstance(traceability, dict) and traceability:
        return traceability
    return table_traceability(table)


def source_engine_for_row_block(row_block: dict[str, Any] | None, table: dict[str, Any]) -> str | None:
    traceability = row_block_traceability(row_block, table)
    return traceability.get("source_engine") or (row_block or {}).get("source_engine") or source_engine_for_table(table)


def source_table_id_for_row_block(row_block: dict[str, Any] | None, table: dict[str, Any]) -> str | None:
    traceability = row_block_traceability(row_block, table)
    return traceability.get("source_table_id") or (row_block or {}).get("source_table_id") or source_table_id_for_table(table)


def fusion_status_for_row_block(row_block: dict[str, Any] | None, table: dict[str, Any]) -> str:
    traceability = row_block_traceability(row_block, table)
    return str(traceability.get("fusion_status") or (row_block or {}).get("fusion_status") or fusion_status_for_table(table))


def source_engines_for_row_block(row_block: dict[str, Any] | None, table: dict[str, Any]) -> list[str]:
    traceability = row_block_traceability(row_block, table)
    engines = traceability.get("source_engines_involved") or (row_block or {}).get("source_engines_involved")
    if isinstance(engines, list) and engines:
        return [str(engine) for engine in engines]
    engine = source_engine_for_row_block(row_block, table)
    return [engine] if engine else []


def source_bbox_for_row_block(row_block: dict[str, Any] | None, table: dict[str, Any]) -> Any:
    row_block = row_block or {}
    if row_block.get("source_bbox") is not None:
        return row_block.get("source_bbox")
    diagnostics = row_block.get("diagnostics") or {}
    if diagnostics.get("source_bbox") is not None:
        return diagnostics.get("source_bbox")
    location = table_traceability(table)
    return location.get("source_bbox")


def recover_inline_statement_row(
    table: dict[str, Any],
    row: dict[str, Any],
    row_block: dict[str, Any] | None,
    *,
    row_index: int | None = None,
    next_label: str | None = None,
) -> dict[str, Any]:
    if row_has_separate_numeric_cells(row):
        return row
    row_block = row_block or {}
    row_block_values = row_block.get("value_cells") or {}
    if row_block_values:
        recovered_from_block = {"line": row_block.get("label_text") or row.get("line") or ""}
        recovered_from_block.update(row_block_values)
        if row_has_numeric_values(recovered_from_block):
            return recovered_from_block
    label_text = str(row_block.get("label_text") or row.get("line") or "").strip()
    if not label_text:
        return row
    recovered = split_inline_label_and_values(label_text)
    if not recovered:
        recovered_from_page_layout = recover_statement_row_from_page_layout(
            table=table,
            row=row,
            row_block=row_block,
            row_index=row_index,
            next_label=next_label,
        )
        return recovered_from_page_layout or row
    label, numbers = recovered
    current_key, comparative_key = inline_period_columns(table)
    if not current_key:
        return row
    recovered_row: dict[str, Any] = {"line": label}
    if numbers:
        recovered_row[current_key] = numbers[0]
    if len(numbers) > 1 and comparative_key:
        recovered_row[comparative_key] = numbers[1]
    if row_has_numeric_values(recovered_row):
        return recovered_row
    recovered_from_page_layout = recover_statement_row_from_page_layout(
        table=table,
        row=row,
        row_block=row_block,
        row_index=row_index,
        next_label=next_label,
    )
    return recovered_from_page_layout or row


def recover_adjacent_statement_row(
    *,
    table: dict[str, Any],
    row_index: int,
    rows: list[dict[str, Any]],
    row_blocks: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    row = rows[row_index]
    row_block = row_blocks[row_index] if row_index < len(row_blocks) else normalized_row_block_fallback(row)
    next_row = rows[row_index + 1] if row_index + 1 < len(rows) else None
    next_block = (
        (
            row_blocks[row_index + 1]
            if next_row is not None and row_index + 1 < len(row_blocks)
            else normalized_row_block_fallback(next_row)
        )
        if next_row is not None
        else None
    )
    next_label = preferred_row_label(next_row, next_block) if next_row is not None else None
    effective_row = recover_inline_statement_row(table, row, row_block, row_index=row_index, next_label=next_label)
    if row_has_numeric_values(effective_row):
        return effective_row, with_recovered_row_metadata(row_block, effective_row, table), False
    if next_row is None:
        return effective_row, row_block, False

    next_effective_row = recover_inline_statement_row(table, next_row, next_block, row_index=row_index + 1)
    if not row_has_numeric_values(next_effective_row):
        return effective_row, row_block, False
    if not should_merge_adjacent_statement_rows(row_block, next_block, effective_row, next_effective_row):
        return effective_row, row_block, False

    merged_label = merged_adjacent_row_label(effective_row, row_block, next_effective_row, next_block)
    merged_row = build_adjacent_merged_row(merged_label, next_effective_row, row_index, next_row)
    merged_block = build_adjacent_merged_row_block(table, row_block, next_block, merged_label, merged_row, row_index)
    return merged_row, merged_block, True


def expand_merged_statement_rows(
    *,
    table: dict[str, Any],
    rows: list[dict[str, Any]],
    row_blocks: list[dict[str, Any]],
    statement_type: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expanded_rows: list[dict[str, Any]] = []
    expanded_blocks: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        row_block = row_blocks[row_index] if row_index < len(row_blocks) else normalized_row_block_fallback(row)
        leading_balance_total_rows = split_balance_sheet_row_with_leading_totals(
            table=table,
            row=row,
            row_block=row_block,
            row_index=row_index,
            statement_type=statement_type,
        )
        if leading_balance_total_rows:
            for split_row, split_block in leading_balance_total_rows:
                expanded_rows.append(split_row)
                expanded_blocks.append(split_block)
            continue
        embedded_split_rows = split_statement_row_with_embedded_values(
            table=table,
            row=row,
            row_block=row_block,
            row_index=row_index,
            statement_type=statement_type,
        )
        if embedded_split_rows:
            for split_row, split_block in embedded_split_rows:
                expanded_rows.append(split_row)
                expanded_blocks.append(split_block)
            continue
        split_rows = split_merged_statement_row(
            table=table,
            row=row,
            row_block=row_block,
            row_index=row_index,
            statement_type=statement_type,
        )
        if split_rows:
            for split_row, split_block in split_rows:
                expanded_rows.append(split_row)
                expanded_blocks.append(split_block)
            continue
        expanded_rows.append(row)
        expanded_blocks.append(row_block)
    return expanded_rows, expanded_blocks


def split_balance_sheet_row_with_leading_totals(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_block: dict[str, Any] | None,
    row_index: int,
    statement_type: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]] | None:
    row_block = row_block or {}
    if statement_type != "balance_sheet" or not row_has_separate_numeric_cells(row):
        return None
    source_line = str(
        row.get("inline_value_recovered_from")
        or (row_block.get("diagnostics") or {}).get("inline_value_recovered_from")
        or row.get("source_line")
        or (row_block.get("diagnostics") or {}).get("source_line")
        or preferred_row_label(row, row_block)
        or row.get("line")
        or ""
    ).strip()
    label_text = str(row.get("line") or preferred_row_label(row, row_block) or source_line).strip()
    tokens = [token for token in label_text.split() if token]
    if len(tokens) < 4 or parse_number(tokens[0]) is None or parse_number(tokens[1]) is None:
        return None
    current_key, comparative_key = inline_period_columns(table)
    if not current_key or not comparative_key:
        return None

    transition = leading_balance_transition_plan(" ".join(tokens[2:]).strip())
    if not transition:
        return None
    subtotal_metric = str(transition["subtotal_metric"])
    subtotal_label = str(transition["subtotal_label"])
    component_label = str(transition["component_label"])
    if match_metric(subtotal_label, "balance_sheet", degraded=True) != subtotal_metric:
        return None
    if match_metric(component_label, "balance_sheet", degraded=True) is None:
        return None

    subtotal_row = {
        "line": subtotal_label,
        current_key: tokens[0],
        comparative_key: tokens[1],
        "source_line": source_line,
        "source_bbox": row.get("source_bbox") or row_block.get("source_bbox"),
        "recovery_mode": "leading_balance_total_split",
        "inline_value_recovered": True,
        "inline_value_recovered_from": source_line,
        "merged_line_split": True,
    }
    split_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    total_segments = 2 if not transition.get("follow_up_label") else 3
    subtotal_block = build_split_statement_row_block(
        table=table,
        row_block=row_block,
        row_index=row_index,
        split_row=subtotal_row,
        segment_index=0,
        total_segments=total_segments,
    )
    split_rows.append((subtotal_row, subtotal_block))

    component_row = {
        **row,
        "line": component_label,
        "source_line": source_line,
        "source_bbox": row.get("source_bbox") or row_block.get("source_bbox"),
        "recovery_mode": "leading_balance_total_split_follow_up",
        "merged_line_split": True,
    }
    if transition.get("component_values"):
        component_values = transition["component_values"]
        component_row[current_key] = component_values[0]
        component_row[comparative_key] = component_values[1]
    component_builder = (
        build_split_statement_row_block
        if transition.get("follow_up_label")
        else build_follow_up_split_statement_row_block
    )
    if component_builder is build_split_statement_row_block:
        component_block = component_builder(
            table=table,
            row_block=row_block,
            row_index=row_index,
            split_row=component_row,
            segment_index=1,
            total_segments=total_segments,
        )
        split_rows.append((component_row, component_block))
        follow_up_row = {
            **row,
            "line": str(transition["follow_up_label"]),
            "source_line": source_line,
            "source_bbox": row.get("source_bbox") or row_block.get("source_bbox"),
            "recovery_mode": "leading_balance_total_split_final_follow_up",
            "merged_line_split": True,
        }
        follow_up_block = build_follow_up_split_statement_row_block(
            table=table,
            row_block=row_block,
            row_index=row_index,
            second_row=follow_up_row,
            source_line=source_line,
            segment_index=2,
            total_segments=total_segments,
        )
        split_rows.append((follow_up_row, follow_up_block))
        return split_rows

    component_block = component_builder(
        table=table,
        row_block=row_block,
        row_index=row_index,
        second_row=component_row,
        source_line=source_line,
        segment_index=1,
        total_segments=total_segments,
    )
    split_rows.append((component_row, component_block))
    return split_rows


def leading_balance_transition_plan(label_text: str) -> dict[str, Any] | None:
    transition_map = {
        "current_assets": "non_current_assets",
        "current_liabilities": "non_current_liabilities",
        "total_assets": "current_assets",
        "total_liabilities": "current_liabilities",
    }
    for header_metric, subtotal_metric in transition_map.items():
        stripped_label = strip_metric_prefix(label_text, "balance_sheet", header_metric)
        if stripped_label is None:
            continue
        subtotal_label = canonical_metric_label(subtotal_metric)
        if header_metric in {"total_assets", "total_liabilities"}:
            trailing_tokens = [token for token in str(stripped_label or "").split() if token]
            has_embedded_values = (
                len(trailing_tokens) >= 3
                and parse_number(trailing_tokens[0]) is not None
                and parse_number(trailing_tokens[1]) is not None
            )
            if has_embedded_values:
                follow_up_label = trim_short_note_reference(" ".join(trailing_tokens[2:]).strip())
                component_values = [trailing_tokens[0], trailing_tokens[1]]
                if follow_up_label and match_metric(follow_up_label, "balance_sheet", degraded=True) is not None:
                    return {
                        "header_metric": header_metric,
                        "subtotal_metric": subtotal_metric,
                        "subtotal_label": subtotal_label,
                        "component_label": canonical_metric_label(header_metric),
                        "component_values": component_values,
                        "follow_up_label": follow_up_label,
                    }
            if stripped_label:
                continue
            return {
                "header_metric": header_metric,
                "subtotal_metric": subtotal_metric,
                "subtotal_label": subtotal_label,
                "component_label": canonical_metric_label(header_metric),
            }
        component_label = trim_short_note_reference(stripped_label)
        if not component_label:
            continue
        component_metric = match_metric(component_label, "balance_sheet", degraded=True)
        if component_metric is None or component_metric == subtotal_metric:
            continue
        return {
            "header_metric": header_metric,
            "subtotal_metric": subtotal_metric,
            "subtotal_label": subtotal_label,
            "component_label": component_label,
        }
    return None


def strip_metric_prefix(raw_label: str, statement_type: str, metric_code: str) -> str | None:
    raw_tokens = [token for token in str(raw_label or "").split() if token]
    normalized_raw = normalize_label(raw_label)
    aliases = normalized_aliases(concept_policy(statement_type, metric_code).get("strict_exact_aliases", []))
    for alias in sorted(aliases, key=len, reverse=True):
        if normalized_raw == alias:
            return ""
        alias_tokens = [token for token in alias.split() if token]
        if not alias_tokens or len(raw_tokens) < len(alias_tokens):
            continue
        candidate_prefix = normalize_label(" ".join(raw_tokens[: len(alias_tokens)]))
        if candidate_prefix == alias:
            return " ".join(raw_tokens[len(alias_tokens) :]).strip()
    return None


def canonical_metric_label(metric_code: str) -> str:
    labels = {
        "current_assets": "Current assets",
        "non_current_assets": "Non-current assets",
        "current_liabilities": "Current liabilities",
        "non_current_liabilities": "Non-current liabilities",
        "total_assets": "Total assets",
        "total_liabilities": "Total liabilities",
    }
    return labels.get(metric_code, metric_code.replace("_", " ").title())


def split_statement_row_with_embedded_values(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_block: dict[str, Any] | None,
    row_index: int,
    statement_type: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]] | None:
    row_block = row_block or {}
    if statement_type not in {"income_statement", "cash_flow"}:
        return None
    if not row_has_separate_numeric_cells(row):
        return None
    label_text = str(preferred_row_label(row, row_block) or row.get("line") or "").strip()
    if not label_text:
        return None
    current_key, comparative_key = inline_period_columns(table)
    if not current_key:
        return None
    tokens = [token for token in label_text.split() if token]
    if len(tokens) < 6:
        return None
    embedded_segments = extract_embedded_statement_segments(tokens, statement_type)
    if not embedded_segments:
        return None
    consumed_tokens = embedded_segments[-1]["next_cursor"]
    tail_label = " ".join(tokens[consumed_tokens:]).strip()
    if not tail_label or match_metric(tail_label, statement_type, degraded=True) is None:
        return None
    split_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    total_segments = len(embedded_segments) + 1
    for segment_index, segment in enumerate(embedded_segments):
        split_row: dict[str, Any] = {
            "line": segment["label"],
            "source_line": label_text,
            "source_bbox": row.get("source_bbox") or row_block.get("source_bbox"),
            "recovery_mode": "embedded_statement_value_split",
            "inline_value_recovered": True,
            "inline_value_recovered_from": label_text,
            "merged_line_split": True,
        }
        split_row[current_key] = segment["values"][0]
        if comparative_key:
            split_row[comparative_key] = segment["values"][1]
        split_block = build_split_statement_row_block(
            table=table,
            row_block=row_block,
            row_index=row_index,
            split_row=split_row,
            segment_index=segment_index,
            total_segments=total_segments,
        )
        split_rows.append((split_row, split_block))

    final_row: dict[str, Any] = {
        **row,
        "line": tail_label,
        "source_line": label_text,
        "source_bbox": row.get("source_bbox") or row_block.get("source_bbox"),
        "recovery_mode": "embedded_statement_value_split_follow_up",
        "merged_line_split": True,
    }
    final_block = build_follow_up_split_statement_row_block(
        table=table,
        row_block=row_block,
        row_index=row_index,
        second_row=final_row,
        source_line=label_text,
        segment_index=total_segments - 1,
        total_segments=total_segments,
    )
    split_rows.append((final_row, final_block))
    return split_rows


def extract_embedded_statement_segments(tokens: list[str], statement_type: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(tokens):
        segment = next_embedded_statement_segment(tokens[cursor:], statement_type)
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


def next_embedded_statement_segment(tokens: list[str], statement_type: str) -> dict[str, Any] | None:
    if len(tokens) < 6:
        return None
    candidates: list[dict[str, Any]] = []
    for first_numeric_index, token in enumerate(tokens):
        if first_numeric_index <= 0 or parse_number(token) is None:
            continue
        label = " ".join(tokens[:first_numeric_index]).strip()
        if not label or match_metric(label, statement_type, degraded=True) is None:
            continue
        remaining_tokens = tokens[first_numeric_index:]
        note_offset = 1 if remaining_tokens and is_short_note_reference(remaining_tokens[0]) else 0
        if len(remaining_tokens) < note_offset + 3:
            continue
        embedded_value_tokens = remaining_tokens[note_offset : note_offset + 2]
        if len(embedded_value_tokens) != 2 or any(parse_number(item) is None for item in embedded_value_tokens):
            continue
        tail_tokens = remaining_tokens[note_offset + 2 :]
        if not tail_tokens:
            continue
        tail_label = " ".join(tail_tokens).strip()
        tail_is_metric = match_metric(tail_label, statement_type, degraded=True) is not None
        tail_has_next_segment = has_embedded_statement_segment(tail_tokens, statement_type)
        if not tail_is_metric and not tail_has_next_segment:
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


def has_embedded_statement_segment(tokens: list[str], statement_type: str) -> bool:
    return next_embedded_statement_segment(tokens, statement_type) is not None


def split_merged_statement_row(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_block: dict[str, Any] | None,
    row_index: int,
    statement_type: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]] | None:
    row_block = row_block or {}
    if statement_type not in {"income_statement", "cash_flow"}:
        return None
    if row_has_separate_numeric_cells(row):
        return None
    text = str(
        row.get("source_line")
        or (row_block.get("diagnostics") or {}).get("source_line")
        or row.get("line")
        or row_block.get("label_text")
        or ""
    ).strip()
    if not text:
        return None
    segments = split_multi_statement_line_segments(text, statement_type)
    if len(segments) < 2:
        segments = split_multi_statement_line_segments_from_tokens(table, row_index, statement_type)
    if len(segments) < 2:
        return None
    current_key, comparative_key = inline_period_columns(table)
    if not current_key:
        return None
    split_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    total_segments = len(segments)
    for segment_index, (label, numbers) in enumerate(segments):
        split_row: dict[str, Any] = {
            "line": label,
            "source_line": text,
            "source_bbox": row.get("source_bbox") or row_block.get("source_bbox"),
            "recovery_mode": "merged_statement_line_split",
            "inline_value_recovered": True,
            "inline_value_recovered_from": text,
            "merged_line_split": True,
        }
        split_row[current_key] = numbers[0]
        if len(numbers) > 1 and comparative_key:
            split_row[comparative_key] = numbers[1]
        if not row_has_numeric_values(split_row):
            return None
        split_block = build_split_statement_row_block(
            table=table,
            row_block=row_block,
            row_index=row_index,
            split_row=split_row,
            segment_index=segment_index,
            total_segments=total_segments,
        )
        split_rows.append((split_row, split_block))
    return split_rows


def recover_statement_row_from_page_layout(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_block: dict[str, Any] | None,
    row_index: int | None,
    next_label: str | None = None,
) -> dict[str, Any] | None:
    label = preferred_row_label(row, row_block)
    if not label:
        return None
    current_key, comparative_key = inline_period_columns(table)
    if not current_key:
        return None
    for layout_line in candidate_page_layout_lines(table, row_index):
        line_text = normalize_financial_text(layout_line.get("text"))
        if not line_text:
            continue
        numbers = extract_line_values_for_label(line_text, label, next_label=next_label)
        if not numbers:
            continue
        recovered_row: dict[str, Any] = {"line": label}
        recovered_row[current_key] = numbers[0]
        if len(numbers) > 1 and comparative_key:
            recovered_row[comparative_key] = numbers[1]
        recovered_row["source_bbox"] = layout_line.get("bbox")
        recovered_row["source_line"] = line_text
        recovered_row["inline_value_recovered"] = True
        recovered_row["inline_value_recovered_from"] = line_text
        recovered_row["recovery_mode"] = "page_layout_line_value_recovery"
        if row_has_numeric_values(recovered_row):
            return recovered_row
    return None


def build_split_statement_row_block(
    *,
    table: dict[str, Any],
    row_block: dict[str, Any],
    row_index: int,
    split_row: dict[str, Any],
    segment_index: int,
    total_segments: int,
) -> dict[str, Any]:
    split_block = dict(row_block or {})
    split_block["label_text"] = split_row.get("line")
    split_block["label_tokens"] = [token for token in str(split_row.get("line") or "").split() if token]
    split_block["value_cells"] = {
        key: value
        for key, value in split_row.items()
        if key
        not in {
            "line",
            "source_line",
            "source_bbox",
            "recovery_mode",
            "inline_value_recovered",
            "inline_value_recovered_from",
            "merged_line_split",
        }
    }
    split_block["row_kind"] = infer_recovered_row_kind(
        label_text=str(split_row.get("line") or ""),
        current_kind=str(row_block.get("row_kind") or "unknown"),
        statement_type=str(table.get("statement_type") or ""),
    ) or str(row_block.get("row_kind") or "statement_line_item")
    split_block["row_confidence"] = max(float(split_block.get("row_confidence") or 0.0), 0.84)
    split_block["label_confidence"] = max(float(split_block.get("label_confidence") or 0.0), 0.82)
    split_block["ownership_confidence"] = max(float(split_block.get("ownership_confidence") or 0.0), 0.84)
    split_block["value_confidence"] = max(float(split_block.get("value_confidence") or 0.0), 0.82)
    split_block["fact_period_confidence"] = max(
        float(split_block.get("fact_period_confidence") or 0.0),
        semantic_fact_period_confidence(table, row_block),
    )
    if split_row.get("source_bbox") is not None:
        split_block["source_bbox"] = split_row.get("source_bbox")
    diagnostics = dict(split_block.get("diagnostics") or {})
    diagnostics["source_line"] = split_row.get("source_line")
    diagnostics["inline_value_recovered"] = True
    diagnostics["inline_value_recovered_from"] = split_row.get("source_line")
    diagnostics["merged_statement_line_split"] = True
    diagnostics["merged_statement_segment_index"] = segment_index
    diagnostics["merged_statement_segment_count"] = total_segments
    diagnostics["recovery_mode"] = "merged_statement_line_split"
    split_block["diagnostics"] = diagnostics
    split_block["fragment_role"] = "recovered_statement_row"
    split_block["stitched_from_rows"] = [row_index]
    return split_block


def build_follow_up_split_statement_row_block(
    *,
    table: dict[str, Any],
    row_block: dict[str, Any],
    row_index: int,
    second_row: dict[str, Any],
    source_line: str,
    segment_index: int,
    total_segments: int,
) -> dict[str, Any]:
    split_block = dict(row_block or {})
    split_block["label_text"] = second_row.get("line")
    split_block["label_tokens"] = [token for token in str(second_row.get("line") or "").split() if token]
    split_block["value_cells"] = {
        key: value
        for key, value in second_row.items()
        if key
        not in {
            "line",
            "source_line",
            "source_bbox",
            "recovery_mode",
            "merged_line_split",
        }
    }
    split_block["row_kind"] = infer_recovered_row_kind(
        label_text=str(second_row.get("line") or ""),
        current_kind=str(row_block.get("row_kind") or "unknown"),
        statement_type=str(table.get("statement_type") or ""),
    ) or str(row_block.get("row_kind") or "statement_line_item")
    split_block["row_confidence"] = max(float(split_block.get("row_confidence") or 0.0), 0.84)
    split_block["label_confidence"] = max(float(split_block.get("label_confidence") or 0.0), 0.82)
    split_block["ownership_confidence"] = max(float(split_block.get("ownership_confidence") or 0.0), 0.84)
    split_block["value_confidence"] = max(float(split_block.get("value_confidence") or 0.0), 0.82)
    split_block["fact_period_confidence"] = max(
        float(split_block.get("fact_period_confidence") or 0.0),
        semantic_fact_period_confidence(table, row_block),
    )
    if second_row.get("source_bbox") is not None:
        split_block["source_bbox"] = second_row.get("source_bbox")
    diagnostics = dict(split_block.get("diagnostics") or {})
    diagnostics["source_line"] = source_line
    diagnostics["merged_statement_line_split"] = True
    diagnostics["merged_statement_segment_index"] = segment_index
    diagnostics["merged_statement_segment_count"] = total_segments
    diagnostics["recovery_mode"] = "embedded_statement_value_split_follow_up"
    diagnostics["embedded_statement_split_follow_up"] = True
    split_block["diagnostics"] = diagnostics
    split_block["fragment_role"] = "recovered_statement_row"
    split_block["stitched_from_rows"] = [row_index]
    return split_block


def with_recovered_row_metadata(
    row_block: dict[str, Any] | None,
    recovered_row: dict[str, Any],
    table: dict[str, Any],
) -> dict[str, Any]:
    row_block = dict(row_block or {})
    if recovered_row.get("line") and not row_block.get("label_text"):
        row_block["label_text"] = recovered_row.get("line")
        row_block["label_tokens"] = [token for token in str(recovered_row.get("line") or "").split() if token]
        row_block["label_confidence"] = max(float(row_block.get("label_confidence") or 0.0), 0.78)
        recovered_kind = infer_recovered_row_kind(
            label_text=str(recovered_row.get("line") or ""),
            current_kind=str(row_block.get("row_kind") or "unknown"),
            statement_type=str(table.get("statement_type") or ""),
        )
        if recovered_kind:
            row_block["row_kind"] = recovered_kind
            row_block["row_confidence"] = max(float(row_block.get("row_confidence") or 0.0), 0.84)
    if not (recovered_row.get("inline_value_recovered") or recovered_row.get("label_recovered_from_page_layout")):
        return row_block
    diagnostics = dict(row_block.get("diagnostics") or {})
    if recovered_row.get("inline_value_recovered"):
        diagnostics["inline_value_recovered"] = True
        diagnostics["inline_value_recovered_from"] = recovered_row.get("inline_value_recovered_from")
    if recovered_row.get("label_recovered_from_page_layout"):
        diagnostics["label_recovered_from_page_layout"] = True
        diagnostics["label_recovered_from"] = recovered_row.get("source_line")
    diagnostics["recovery_mode"] = recovered_row.get("recovery_mode") or "page_layout_line_value_recovery"
    row_block["diagnostics"] = diagnostics
    row_block["value_cells"] = {
        key: value
        for key, value in recovered_row.items()
        if key
        not in {
            "line",
            "source_bbox",
            "source_line",
            "inline_value_recovered",
            "inline_value_recovered_from",
            "recovery_mode",
        }
    }
    row_block["value_confidence"] = max(float(row_block.get("value_confidence") or 0.0), 0.78)
    row_block["ownership_confidence"] = max(float(row_block.get("ownership_confidence") or 0.0), 0.74)
    row_block["source_bbox"] = recovered_row.get("source_bbox") or source_bbox_for_row_block(row_block, table)
    return row_block


def recover_statement_numeric_fragment_label(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_block: dict[str, Any],
    row_index: int,
    statement_type: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if statement_type not in {"balance_sheet", "income_statement", "cash_flow"}:
        return row, row_block
    if preferred_row_label(row, row_block):
        return row, row_block
    if not row_has_numeric_values(row):
        return row, row_block
    diagnostics = dict((row_block or {}).get("diagnostics") or {})
    recovery_mode = str(row.get("recovery_mode") or row_block.get("recovery_mode") or diagnostics.get("recovery_mode") or "")
    anchor_hints = list(row.get("anchor_hints") or row_block.get("anchor_hints") or diagnostics.get("anchor_hints") or [])
    if "numeric_signature" not in " ".join(anchor_hints) and "numeric_signature" not in recovery_mode:
        return row, row_block
    recovered = recover_statement_label_from_page_layout(table=table, row=row, row_index=row_index)
    if not recovered:
        return row, row_block
    return recovered, with_recovered_row_metadata(row_block, recovered, table)


def recover_statement_label_from_page_layout(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_index: int,
) -> dict[str, Any] | None:
    numeric_values = [str(value).strip() for _, value in row_numeric_items(row) if str(value or "").strip()]
    if not numeric_values:
        return None
    label_candidates: list[tuple[str, dict[str, Any]]] = []
    for line in candidate_page_layout_lines(table, row_index):
        line_text = normalize_financial_text(line.get("text"))
        if not line_text:
            continue
        if not line_contains_numeric_suffix(line_text, numeric_values):
            continue
        label = trim_numeric_suffix_from_line(line_text, numeric_values)
        if not label:
            continue
        if parse_number(label) is not None:
            continue
        label_candidates.append((label, line))
    unique_labels = {normalize_label(label): (label, line) for label, line in label_candidates if normalize_label(label)}
    if len(unique_labels) != 1:
        return None
    label, line = next(iter(unique_labels.values()))
    recovered = dict(row)
    recovered["line"] = label
    recovered["source_line"] = normalize_financial_text(line.get("text"))
    recovered["source_bbox"] = line.get("bbox")
    recovered["label_recovered_from_page_layout"] = True
    recovered["recovery_mode"] = "page_layout_numeric_fragment_label_recovery"
    recovered["label_confidence"] = 0.82
    recovered["ownership_confidence"] = 0.8
    return recovered


def line_contains_numeric_suffix(line_text: str, numeric_values: list[str]) -> bool:
    line_numbers = trailing_numeric_tokens(
        str(line_text).split(),
        limit=max(2, len(numeric_values)),
    )
    if len(line_numbers) < len(numeric_values):
        return False
    line_suffix = line_numbers[-len(numeric_values) :]
    return numeric_sequences_match(line_suffix, numeric_values)


def trim_numeric_suffix_from_line(line_text: str, numeric_values: list[str]) -> str:
    words = str(line_text or "").split()
    if len(words) <= len(numeric_values):
        return ""
    tail = [str(word).strip() for word in words[-len(numeric_values) :]]
    if not numeric_sequences_match(tail, numeric_values):
        return ""
    return " ".join(words[: len(words) - len(numeric_values)]).strip()


def infer_recovered_row_kind(label_text: str, current_kind: str, statement_type: str) -> str | None:
    normalized = normalize_label(label_text)
    if not normalized:
        return None
    if current_kind not in {"numeric_fragment", "unknown"}:
        return None
    if statement_type == "balance_sheet":
        if contains_normalized_marker(normalized, "total", "итого"):
            if contains_normalized_marker(normalized, "asset", "актив", "equity", "капитал", "liabilit", "обяз"):
                return "grand_total"
            return "subtotal"
        if contains_normalized_marker(
            normalized,
            "current assets",
            "current liabilities",
            "оборотные активы",
            "краткосрочные обязательства",
        ):
            return "subtotal"
    return "statement_line_item"


def promotable_numeric_fragment_row_kind(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_block: dict[str, Any] | None,
    raw_label: str | None,
    statement_type: str,
) -> str | None:
    row_block = row_block or {}
    if not raw_label or not row_has_numeric_values(row):
        return None
    recovered_row_kind = infer_recovered_row_kind(raw_label, "numeric_fragment", statement_type)
    if not recovered_row_kind:
        return None
    label_confidence = semantic_label_confidence(raw_label, row, row_block)
    ownership_confidence = semantic_ownership_confidence(row, row_block)
    fact_period_confidence = semantic_fact_period_confidence(table, row_block)
    if label_confidence < 0.72:
        return None
    if ownership_confidence < 0.78:
        return None
    if fact_period_confidence < 0.7:
        return None
    return recovered_row_kind


def split_multi_statement_line_segments(text: str, statement_type: str) -> list[tuple[str, list[str]]]:
    tokens = [token for token in str(text or "").strip().split() if token]
    if len(tokens) < 6:
        return []
    segments: list[tuple[str, list[str]]] = []
    cursor = 0
    while cursor < len(tokens):
        first_numeric_index = next(
            (index for index in range(cursor, len(tokens)) if parse_number(tokens[index]) is not None),
            None,
        )
        if first_numeric_index is None or first_numeric_index <= cursor:
            return []
        label = " ".join(tokens[cursor:first_numeric_index]).strip()
        if not label:
            return []
        if match_metric(label, statement_type, degraded=True) is None:
            return []
        numbers = leading_numeric_tokens(tokens[first_numeric_index:], limit=2)
        if not numbers:
            return []
        segments.append((label, numbers))
        cursor = first_numeric_index + len(numbers)
    if len(segments) < 2:
        return []
    return segments


def split_multi_statement_line_segments_from_tokens(
    table: dict[str, Any],
    row_index: int,
    statement_type: str,
) -> list[tuple[str, list[str]]]:
    tokens = candidate_page_layout_tokens(table, row_index)
    if len(tokens) < 6:
        return []
    segments: list[tuple[str, list[str]]] = []
    current_tokens: list[str] = []
    previous_x0: float | None = None
    seen_numeric = False
    numeric_run = 0
    line_left = min((bbox_x0(token.get("bbox")) for token in tokens if bbox_x0(token.get("bbox")) is not None), default=None)
    for token in tokens:
        text = str(token.get("text") or "").strip()
        if not text:
            continue
        token_x0 = bbox_x0(token.get("bbox"))
        is_numeric = parse_number(text) is not None
        x_reset = (
            seen_numeric
            and not is_numeric
            and previous_x0 is not None
            and token_x0 is not None
            and (token_x0 + 25 < previous_x0 or (line_left is not None and token_x0 <= line_left + 30))
        )
        if x_reset and current_tokens:
            parsed = parse_statement_segment_tokens(current_tokens, statement_type)
            if not parsed:
                return []
            segments.append(parsed)
            current_tokens = [text]
            seen_numeric = False
            numeric_run = 0
        else:
            current_tokens.append(text)
        if is_numeric:
            seen_numeric = True
            numeric_run += 1
        elif numeric_run and seen_numeric:
            numeric_run = 0
        if token_x0 is not None:
            previous_x0 = token_x0
    if current_tokens:
        parsed = parse_statement_segment_tokens(current_tokens, statement_type)
        if not parsed:
            return []
        segments.append(parsed)
    if len(segments) < 2:
        return []
    return segments


def parse_statement_segment_tokens(tokens: list[str], statement_type: str) -> tuple[str, list[str]] | None:
    if len(tokens) < 2:
        return None
    first_numeric_index = next((index for index, token in enumerate(tokens) if parse_number(token) is not None), None)
    if first_numeric_index is None or first_numeric_index == 0:
        return None
    label = " ".join(tokens[:first_numeric_index]).strip()
    numbers = leading_numeric_tokens(tokens[first_numeric_index:], limit=2)
    if not label or not numbers:
        return None
    if match_metric(label, statement_type, degraded=True) is None:
        return None
    return label, numbers


def candidate_page_layout_lines(table: dict[str, Any], row_index: int | None) -> list[dict[str, Any]]:
    page_layout = dict(table.get("page_layout") or {})
    lines = list(page_layout.get("lines") or [])
    if row_index is None or not lines:
        return lines
    exact = [line for line in lines if line.get("row_index") == row_index]
    if exact:
        return exact + [line for line in lines if line.get("row_index") != row_index]
    return lines


def candidate_page_layout_tokens(table: dict[str, Any], row_index: int | None) -> list[dict[str, Any]]:
    page_layout = dict(table.get("page_layout") or {})
    tokens = list(page_layout.get("tokens") or [])
    if row_index is None or not tokens:
        return sort_layout_tokens(tokens)
    exact = [token for token in tokens if token.get("row_index") == row_index]
    if exact:
        return sort_layout_tokens(exact)
    return sort_layout_tokens(tokens)


def sort_layout_tokens(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        tokens,
        key=lambda token: (
            bbox_y0(token.get("bbox")) if bbox_y0(token.get("bbox")) is not None else 0.0,
            bbox_x0(token.get("bbox")) if bbox_x0(token.get("bbox")) is not None else 0.0,
        ),
    )


def bbox_x0(bbox: Any) -> float | None:
    if isinstance(bbox, dict):
        value = bbox.get("x0")
        return float(value) if value is not None else None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 1 and bbox[0] is not None:
        return float(bbox[0])
    return None


def bbox_y0(bbox: Any) -> float | None:
    if isinstance(bbox, dict):
        value = bbox.get("top")
        if value is None:
            value = bbox.get("y0")
        return float(value) if value is not None else None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 2 and bbox[1] is not None:
        return float(bbox[1])
    return None


def extract_line_values_for_label(
    line_text: str,
    label: str,
    *,
    next_label: str | None = None,
) -> list[str]:
    normalized_line = normalize_label(line_text)
    normalized_label = normalize_label(label)
    if not normalized_line or not normalized_label or normalized_label not in normalized_line:
        return []
    line_words = str(line_text or "").split()
    label_words = str(label or "").split()
    if not line_words or not label_words:
        return []
    start_index = first_token_sequence_index(line_words, label_words)
    if start_index is None:
        return []
    cursor = start_index + len(label_words)
    next_label_words = str(next_label or "").split()
    end_index = len(line_words)
    if next_label_words:
        next_index = first_token_sequence_index(line_words[cursor:], next_label_words)
        if next_index is not None:
            end_index = cursor + next_index
    return leading_numeric_tokens(line_words[cursor:end_index], limit=2)


def first_token_sequence_index(words: list[str], needle_words: list[str]) -> int | None:
    normalized_words = [normalize_label(word) for word in words]
    normalized_needle = [normalize_label(word) for word in needle_words if normalize_label(word)]
    if not normalized_words or not normalized_needle:
        return None
    width = len(normalized_needle)
    for index in range(0, len(normalized_words) - width + 1):
        if normalized_words[index : index + width] == normalized_needle:
            return index
    return None


def leading_numeric_tokens(words: list[str], limit: int = 2) -> list[str]:
    values: list[str] = []
    for word in words:
        cleaned = str(word or "").strip()
        if not cleaned:
            if values:
                break
            continue
        if parse_number(cleaned) is None:
            if values:
                break
            continue
        values.append(cleaned)
        if len(values) >= limit:
            break
    return values


def trailing_numeric_tokens(words: list[str], limit: int = 2) -> list[str]:
    values: list[str] = []
    for word in reversed(words):
        cleaned = str(word or "").strip()
        if not cleaned:
            if values:
                break
            continue
        if parse_number(cleaned) is None:
            if values:
                break
            continue
        values.append(cleaned)
        if len(values) >= limit:
            break
    return list(reversed(values))


def is_short_note_reference(raw: str) -> bool:
    cleaned = str(raw or "").strip("()").replace(" ", "").replace(",", "")
    return cleaned.isdigit() and len(cleaned) <= 2


def trim_short_note_reference(label: str) -> str:
    cleaned = str(label or "").split("|", 1)[0].strip()
    tokens = [token for token in cleaned.split() if token]
    if tokens and is_short_note_reference(tokens[-1]):
        tokens = tokens[:-1]
    return " ".join(tokens).strip()


def numeric_sequences_match(left: list[str], right: list[str]) -> bool:
    if len(left) != len(right):
        return False
    for left_value, right_value in zip(left, right, strict=False):
        left_number = parse_number(left_value)
        right_number = parse_number(right_value)
        if left_number is None or right_number is None:
            if str(left_value).strip() != str(right_value).strip():
                return False
            continue
        if abs(left_number - right_number) > 1e-9:
            return False
    return True


def should_merge_adjacent_statement_rows(
    current_block: dict[str, Any] | None,
    next_block: dict[str, Any] | None,
    current_row: dict[str, Any],
    next_row: dict[str, Any],
) -> bool:
    current_block = current_block or {}
    next_block = next_block or {}
    current_kind = str(current_block.get("row_kind") or "unknown")
    next_kind = str(next_block.get("row_kind") or "unknown")
    if current_kind in {"header", "footnote", "note_reference_only", "grand_total", "subtotal"}:
        return False

    current_label = preferred_row_label(current_row, current_block)
    next_label = preferred_row_label(next_row, next_block)
    note_like_value_tail = is_note_reference_like_label(next_label) and row_has_numeric_values(next_row)
    if next_kind in {"header", "footnote", "note_reference_only", "grand_total", "subtotal"} and not note_like_value_tail:
        return False
    if str(next_block.get("fusion_status") or "") == "conflict_retained_as_evidence":
        return False
    if not current_label:
        return False
    if not next_label:
        return True
    if note_like_value_tail:
        return True
    if next_kind == "numeric_fragment":
        return True
    if is_label_continuation_fragment(next_label) and (
        is_incomplete_statement_label(current_label) or len(normalize_label(current_label).split()) >= 2
    ):
        return True
    return False


def merged_adjacent_row_label(
    current_row: dict[str, Any],
    current_block: dict[str, Any] | None,
    next_row: dict[str, Any],
    next_block: dict[str, Any] | None,
) -> str:
    current_label = str(preferred_row_label(current_row, current_block) or "").strip()
    next_label = str(preferred_row_label(next_row, next_block) or "").strip()
    if not next_label or next_label == current_label:
        return current_label
    if is_note_reference_like_label(next_label):
        return current_label
    if is_label_continuation_fragment(next_label):
        return " ".join(part for part in [current_label, next_label] if part).strip()
    return current_label


def build_adjacent_merged_row(
    label: str,
    numeric_row: dict[str, Any],
    row_index: int,
    next_row: dict[str, Any],
) -> dict[str, Any]:
    merged_row: dict[str, Any] = {"line": label}
    for key, value in row_numeric_items(numeric_row):
        merged_row[str(key)] = value
    if numeric_row.get("source_bbox") is not None:
        merged_row["source_bbox"] = numeric_row.get("source_bbox")
    merged_row["stitched_from_rows"] = [row_index, row_index + 1]
    merged_row["source_line"] = " | ".join(
        part for part in [row_to_source_line({"line": label}), row_to_source_line(next_row)] if part
    )
    return merged_row


def build_adjacent_merged_row_block(
    table: dict[str, Any],
    current_block: dict[str, Any] | None,
    next_block: dict[str, Any] | None,
    merged_label: str,
    merged_row: dict[str, Any],
    row_index: int,
) -> dict[str, Any]:
    current_block = dict(current_block or {})
    next_block = dict(next_block or {})
    next_traceability = row_block_traceability(next_block, table)
    current_traceability = row_block_traceability(current_block, table)
    source_engines = sorted(
        set(source_engines_for_row_block(current_block, table)) | set(source_engines_for_row_block(next_block, table))
    )
    fusion_status = "merged_engines" if len(source_engines) > 1 else "single_engine"
    diagnostics = {
        **dict(current_block.get("diagnostics") or {}),
        **dict(next_block.get("diagnostics") or {}),
        "source_line": merged_row.get("source_line"),
        "stitch_warning": "parser_adjacent_label_value_rows_stitched",
        "stitch_mode": "label_plus_value_continuation_stitch",
        "warnings": sorted(
            {
                *list((current_block.get("diagnostics") or {}).get("warnings") or []),
                *list((next_block.get("diagnostics") or {}).get("warnings") or []),
            }
        ),
    }
    return {
        **current_block,
        "label_text": merged_label,
        "value_cells": {
            key: value
            for key, value in merged_row.items()
            if key not in {"line", "source_bbox", "stitched_from_rows", "source_line"}
        },
        "row_kind": "statement_line_item",
        "stitched_from_rows": [row_index, row_index + 1],
        "row_confidence": min(
            0.84,
            max(
                0.68,
                min(float(current_block.get("row_confidence") or 0.72), float(next_block.get("row_confidence") or 0.8)),
            ),
        ),
        "source_engine": next_traceability.get("source_engine") or current_traceability.get("source_engine"),
        "source_page": next_traceability.get("source_page") or current_traceability.get("source_page"),
        "source_table_id": next_traceability.get("source_table_id") or current_traceability.get("source_table_id"),
        "source_bbox": merged_row.get("source_bbox"),
        "fusion_status": fusion_status,
        "source_engines_involved": source_engines,
        "source_traceability": {
            "source_engine": next_traceability.get("source_engine") or current_traceability.get("source_engine"),
            "source_page": next_traceability.get("source_page") or current_traceability.get("source_page"),
            "source_table_id": next_traceability.get("source_table_id") or current_traceability.get("source_table_id"),
            "source_bbox": merged_row.get("source_bbox"),
            "fusion_status": fusion_status,
            "source_engines_involved": source_engines,
        },
        "diagnostics": diagnostics,
    }


def is_incomplete_statement_label(label: str) -> bool:
    normalized = normalize_label(label)
    if not normalized:
        return False
    trailing_tokens = {"and", "or", "from", "of", "to", "for", "on", "и", "или", "от", "по", "за", "для", "с"}
    last_token = normalized.split()[-1]
    if last_token in trailing_tokens:
        return True
    return str(label).rstrip().endswith(("...", "…", "-", "(", "/"))


def is_note_reference_like_label(label: str | None) -> bool:
    cleaned = str(label or "").strip()
    if not cleaned:
        return False
    normalized = normalize_label(cleaned)
    if not normalized:
        return False
    compact = re.sub(r"[\s().,]+", "", normalized)
    if compact.isdigit() and len(compact) <= 3:
        return True
    has_note_marker = contains_normalized_marker(normalized, "note", "notes", "прим", "примеч", "поясн")
    return has_note_marker and any(char.isdigit() for char in normalized)


def is_label_continuation_fragment(label: str) -> bool:
    cleaned = str(label or "").strip()
    if not cleaned:
        return False
    first = cleaned[0]
    if first.islower():
        return True
    normalized = normalize_label(cleaned)
    if not normalized:
        return False
    first_token = normalized.split()[0]
    continuation_heads = {
        "and",
        "or",
        "from",
        "of",
        "to",
        "for",
        "и",
        "или",
        "от",
        "по",
        "за",
        "для",
        "с",
        "revenue",
        "revenues",
        "expense",
        "expenses",
        "cost",
        "costs",
        "tax",
        "activities",
        "assets",
        "liabilities",
    }
    return first_token in continuation_heads


def split_inline_label_and_values(text: str) -> tuple[str, list[str]] | None:
    tokens = str(text or "").strip().split()
    if len(tokens) >= 3:
        trailing_values: list[str] = []
        for token in reversed(tokens):
            if parse_number(token) is None:
                break
            trailing_values.append(token)
            if len(trailing_values) == 2:
                break
        if trailing_values:
            trailing_values.reverse()
            label_tokens = tokens[: len(tokens) - len(trailing_values)]
            label = " ".join(label_tokens).strip()
            if label and len(trailing_values) in {1, 2}:
                return label, trailing_values

    pattern = re.compile(
        r"^(?P<label>.+?)\s+(?P<current>\(?-?\d[\d\s,\.]*\)?)"
        r"(?:\s+(?P<comparative>\(?-?\d[\d\s,\.]*\)?))?$"
    )
    match = pattern.match(str(text or "").strip())
    if not match:
        return None
    label = match.group("label").strip()
    current = match.group("current")
    comparative = match.group("comparative")
    if not label or parse_number(current) is None:
        return None
    numbers = [current]
    if comparative and parse_number(comparative) is not None:
        numbers.append(comparative)
    return label, numbers


def inline_period_columns(table: dict[str, Any]) -> tuple[str | None, str | None]:
    effective_period = normalized_fact_period(table.get("effective_period") or table.get("period"))
    comparative_period = normalized_fact_period(table.get("comparative_period"))
    current_year = period_year(effective_period)
    comparative_year = period_year(comparative_period) if comparative_period else (current_year - 1 if current_year else None)
    current_key = str(current_year) if current_year else "current"
    comparative_key = str(comparative_year) if comparative_year else None
    return current_key, comparative_key


def row_to_source_line(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    explicit = str(row.get("source_line") or "").strip()
    if explicit:
        return explicit
    parts: list[str] = []
    label = str(row.get("line") or "").strip()
    if label:
        parts.append(label)
    for _, value in row_numeric_items(row):
        text = str(value).strip()
        if text:
            parts.append(text)
    return " | ".join(parts) if parts else None


def row_to_raw_value(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    numeric = [str(value).strip() for _, value in row_numeric_items(row) if parse_number(value) is not None]
    return " | ".join(numeric) if numeric else None


def row_has_numeric_values(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    return any(parse_number(value) is not None for _, value in row_numeric_items(row))


def row_has_separate_numeric_cells(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    for _, value in row_numeric_items(row):
        if parse_number(value) is not None:
            return True
    return False


def extract_numeric_values(row: dict[str, Any] | None) -> list[float]:
    if not isinstance(row, dict):
        return []
    values: list[float] = []
    for _, value in row_numeric_items(row):
        parsed = parse_number(value)
        if parsed is not None:
            values.append(parsed)
    return values


def row_numeric_items(row: dict[str, Any] | None) -> list[tuple[str, Any]]:
    if not isinstance(row, dict):
        return []
    ignored_keys = {
        "line",
        "source_line",
        "source_bbox",
        "stitched_from_rows",
        "note_ref",
        "label_recovered_from_word_layout",
        "recovery_mode",
        "label_recovery_reason",
        "anchor_hints",
        "alignment_confidence",
        "ownership_confidence",
        "label_confidence",
        "value_confidence",
        "row_confidence",
        "stitch_confidence",
        "merged_statement_segment_index",
        "merged_statement_segment_count",
        "inline_value_recovered",
        "word_layout_column_recovered",
        "word_layout_note_ref_recovered",
        "word_layout_percent_change_columns_recovered",
        "is_total_like",
        "is_subtotal_like",
        "source_page",
        "source_table_id",
    }
    return [(str(key), value) for key, value in row.items() if str(key) not in ignored_keys and parse_number(value) is not None]


EVIDENCE_TOPIC_MARKERS: dict[str, tuple[str, ...]] = {
    "segment_disclosure": (
        "segment",
        "reportable segment",
        "operating segment",
        "сегмент",
        "выручка по сегмент",
    ),
    "debt_disclosure": (
        "borrowings",
        "borrowing",
        "debt",
        "loans",
        "loan",
        "credit facility",
        "bonds",
        "notes payable",
        "заем",
        "займ",
        "кредит",
        "облигац",
    ),
    "lease_disclosure": (
        "lease",
        "lease liabilities",
        "right-of-use",
        "right of use",
        "rou asset",
        "аренд",
    ),
    "dividend_disclosure": (
        "dividend",
        "dividends",
        "distribution to owners",
        "дивиденд",
    ),
    "capex_disclosure": (
        "capital expenditure",
        "capex",
        "additions to property",
        "additions to ppe",
        "purchase of property",
        "acquisition of property",
        "property, plant and equipment",
        "основн",
        "капитальн",
    ),
    "tax_disclosure": (
        "income tax",
        "deferred tax",
        "current tax",
        "tax expense",
        "налог",
    ),
    "cash_disclosure": (
        "cash and cash equivalents",
        "restricted cash",
        "денежн",
    ),
    "risk_disclosure": (
        "liquidity risk",
        "credit risk",
        "market risk",
        "currency risk",
        "interest rate risk",
        "финансовый риск",
        "кредитный риск",
        "рыночный риск",
        "валютный риск",
    ),
}


EVIDENCE_TOPIC_USE_CASES: dict[str, str] = {
    "segment_disclosure": "segment_context_for_revenue_mix_and_business_drivers",
    "debt_disclosure": "debt_and_financing_context",
    "lease_disclosure": "lease_obligation_context",
    "dividend_disclosure": "shareholder_distribution_context",
    "capex_disclosure": "investment_and_capex_context",
    "tax_disclosure": "tax_context",
    "cash_disclosure": "liquidity_context",
    "risk_disclosure": "risk_context",
}


def table_evidence_topic_tags(table: dict[str, Any]) -> list[str]:
    rows = table.get("rows") or []
    texts: list[str] = [
        str(table.get("statement_type") or ""),
        str(table.get("table_title") or ""),
        str(table.get("extraction_method") or ""),
    ]
    for row in rows:
        if isinstance(row, dict):
            texts.extend(str(value) for value in row.values() if value is not None)
        elif row is not None:
            texts.append(str(row))
    return evidence_topic_tags(texts)


def evidence_topic_tags(texts: list[str]) -> list[str]:
    haystack = normalize_matching_text(" ".join(text for text in texts if text))
    tags = [
        topic
        for topic, markers in EVIDENCE_TOPIC_MARKERS.items()
        if any(normalize_matching_text(marker) in haystack for marker in markers)
    ]
    return sorted(set(tags))


def evidence_analysis_use_cases(topics: list[str]) -> list[str]:
    return sorted({EVIDENCE_TOPIC_USE_CASES[topic] for topic in topics if topic in EVIDENCE_TOPIC_USE_CASES})


def evidence_llm_relevance(topics: list[str], *, has_numeric: bool) -> str:
    if topics and has_numeric:
        return "high"
    if topics or has_numeric:
        return "medium"
    return "low"


def evidence_topic_summary(
    unmapped_numeric_evidence: list[dict[str, Any]],
    unmapped_table_evidence: list[dict[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in [*unmapped_numeric_evidence, *unmapped_table_evidence]:
        for topic in item.get("evidence_topics") or []:
            counts[str(topic)] = counts.get(str(topic), 0) + 1
    return dict(sorted(counts.items()))


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


def semantic_label_confidence(raw_label: str | None, row: dict[str, Any], row_block: dict[str, Any] | None) -> float:
    row_block = row_block or {}
    if row_block.get("label_confidence") is not None:
        return float(row_block.get("label_confidence") or 0.0)
    if row.get("label_confidence") is not None:
        return float(row.get("label_confidence") or 0.0)
    if raw_label:
        return max(0.7, float(row_block.get("row_confidence") or 0.82))
    return 0.0


def semantic_ownership_confidence(row: dict[str, Any], row_block: dict[str, Any] | None) -> float:
    row_block = row_block or {}
    if row_block.get("ownership_confidence") is not None:
        return float(row_block.get("ownership_confidence") or 0.0)
    if row.get("ownership_confidence") is not None:
        return float(row.get("ownership_confidence") or 0.0)
    if row_has_numeric_values(row):
        return max(0.75, float(row_block.get("row_confidence") or 0.84))
    return 0.0


def semantic_fact_period_confidence(table: dict[str, Any], row_block: dict[str, Any] | None) -> float:
    row_block = row_block or {}
    if row_block.get("fact_period_confidence") is not None:
        return float(row_block.get("fact_period_confidence") or 0.0)
    if row_block.get("period_confidence") is not None:
        return float(row_block.get("period_confidence") or 0.0)
    if table.get("period_confidence") is not None:
        return float(table.get("period_confidence") or 0.0)
    if normalized_fact_period(table.get("effective_period") or table.get("period")):
        return 0.85
    return 0.0


def semantic_trust_warnings(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_block: dict[str, Any] | None,
) -> set[str]:
    row_block = row_block or {}
    warnings: set[str] = set()
    diagnostics = row_block.get("diagnostics") or {}
    source_engine = (source_engine_for_row_block(row_block, table) or "").lower()
    source_engines = [str(engine).lower() for engine in source_engines_for_row_block(row_block, table)]
    recovery_mode = str(row.get("recovery_mode") or row_block.get("recovery_mode") or "")

    if "ocr_only_lower_trust" in list(diagnostics.get("warnings") or []):
        warnings.add("ocr_only_lower_trust")
    elif "ocr" in source_engine and not any("native" in engine for engine in source_engines):
        warnings.add("ocr_only_lower_trust")

    if (
        row.get("label_recovered_from_word_layout")
        or row.get("label_recovered_from_page_layout")
        or "word_layout" in recovery_mode
        or "page_layout" in recovery_mode
        or str(row_block.get("fragment_role") or "") == "recovered_statement_row"
    ):
        warnings.add("lower_trust_recovered_statement_row")

    if table.get("extraction_method") in {"text_table_fallback", FALLBACK_EXTRACTION_METHOD}:
        warnings.add("lower_trust_text_fallback_statement_row")
        warnings.add("manual_upload_candidate_based")
    return warnings


def normalize_rejection_reason(reason: str) -> str:
    mapping = {
        "text_table_fallback_not_eligible_for_fact_normalization": "unsupported_metric_for_text_fallback",
        "ambiguous_period_column": "ambiguous_period_mapping",
        "missing_traceability": "policy_blocked",
        "numeric_value_not_detected": "multi_line_parse_uncertain",
        "debt_component_not_derived_in_dataframe_parser": "debt_component_not_total_debt",
    }
    return mapping.get(reason, reason)


def statement_tables_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = payload.get("normalized_statement_tables")
    if isinstance(normalized, list) and normalized:
        return normalized
    legacy = payload.get("statement_tables")
    return legacy if isinstance(legacy, list) else []


def normalized_table_rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    row_blocks = table.get("row_blocks") or []
    if row_blocks:
        rows = table.get("rows")
        normalized_rows: list[dict[str, Any]] = []
        for index, block in enumerate(row_blocks):
            block_row = normalized_row_from_block(block)
            if isinstance(rows, list) and index < len(rows) and isinstance(rows[index], dict):
                normalized_rows.append({**rows[index], **block_row})
            else:
                normalized_rows.append(block_row)
        return normalized_rows
    rows = table.get("rows")
    if isinstance(rows, list) and rows:
        return [row if isinstance(row, dict) else {"line": str(row or "")} for row in rows]
    return []


def normalized_text_fallback_table_for_parser(table: dict[str, Any]) -> dict[str, Any]:
    if table.get("extraction_method") != "text_table_fallback":
        return table
    rows = [materialize_text_fallback_row(table, row) for row in normalized_table_rows(table)]
    existing_blocks = list(table.get("row_blocks") or [])
    row_blocks: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        existing_block = existing_blocks[index] if index < len(existing_blocks) else None
        row_blocks.append(
            normalized_text_fallback_row_block(
                table=table,
                row=row,
                row_index=index,
                existing_block=existing_block,
            )
        )
    return {
        **table,
        "rows": rows,
        "row_blocks": row_blocks,
        "normalized_semantic_surface": "text_fallback_unified",
    }


def materialize_text_fallback_row(table: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row or {})
    if row_has_numeric_values(row):
        corrected = period_values_from_change_percent_line(row)
        if corrected:
            numeric_keys = [key for key, _ in row_numeric_items(row)]
            if len(numeric_keys) >= 2:
                row[numeric_keys[0]] = corrected[0]
                row[numeric_keys[1]] = corrected[1]
                row["change_percent_column_recovered"] = True
        return row
    line = str(row.get("line") or "").strip()
    if not line:
        return row
    recovered = split_text_fallback_numeric_groups(line)
    if not recovered:
        recovered = split_inline_label_and_values(line)
    if not recovered:
        return row
    label, values = recovered
    current_key, comparative_key = inline_period_columns(table)
    materialized = {
        **row,
        "line": label,
        "source_line": line,
        "inline_value_recovered": True,
        "inline_value_recovered_from": line,
    }
    if current_key and values:
        materialized[current_key] = values[0]
    if comparative_key and len(values) > 1:
        materialized[comparative_key] = values[1]
    return materialized


def normalized_text_fallback_row_block(
    *,
    table: dict[str, Any],
    row: dict[str, Any],
    row_index: int,
    existing_block: dict[str, Any] | None,
) -> dict[str, Any]:
    block = dict(existing_block or {})
    base = normalized_row_block_fallback(row)
    label_text = str(block.get("label_text") or base.get("label_text") or row.get("line") or "").strip()
    row_kind = str(block.get("row_kind") or text_fallback_row_kind(label_text, row, table) or base.get("row_kind") or "unknown")
    numeric_values = row_numeric_items(row)
    has_numeric = bool(numeric_values)
    traceability = {
        "source_engine": block.get("source_engine") or source_engine_for_table(table) or "text_table_fallback",
        "source_page": block.get("source_page") or table.get("page_number"),
        "source_table_id": block.get("source_table_id") or source_table_id_for_table(table),
        "source_bbox": block.get("source_bbox"),
        "fusion_status": block.get("fusion_status") or "single_engine",
        "source_engines_involved": (
            block.get("source_engines_involved") or source_engines_for_table(table) or ["text_table_fallback"]
        ),
    }
    diagnostics = {
        **dict(block.get("diagnostics") or {}),
        "source_line": row_to_source_line(row),
        "inline_value_recovered": bool(row.get("inline_value_recovered")),
        "inline_value_recovered_from": row.get("inline_value_recovered_from"),
        "fragment_role": str(block.get("fragment_role") or ("statement_row" if has_numeric else "text_only")),
    }
    warnings = set(diagnostics.get("warnings") or [])
    warnings.add("lower_trust_text_fallback_statement_row")
    diagnostics["warnings"] = sorted(warnings)
    ownership_confidence = float(block.get("ownership_confidence") or 0.0)
    if has_numeric and ownership_confidence < 0.86 and row_kind in {"statement_line_item", "subtotal", "grand_total"}:
        ownership_confidence = 0.86
    label_confidence = float(block.get("label_confidence") or 0.0)
    if label_text and label_confidence < 0.9:
        label_confidence = 0.9
    value_confidence = float(block.get("value_confidence") or 0.0)
    if has_numeric and value_confidence < 0.88:
        value_confidence = 0.88
    fact_period_confidence = block.get("fact_period_confidence")
    if fact_period_confidence is None and has_numeric and row_kind in {"statement_line_item", "subtotal", "grand_total"}:
        fact_period_confidence = max(float(table.get("period_confidence") or 0.0), 0.9)
    value_cells = {key: value for key, value in numeric_values}
    if not row.get("change_percent_column_recovered") and block.get("value_cells"):
        value_cells = block.get("value_cells")
    return {
        **block,
        "label_text": label_text,
        "value_cells": value_cells,
        "row_kind": row_kind,
        "row_confidence": max(float(block.get("row_confidence") or 0.0), 0.84 if has_numeric else 0.4),
        "label_confidence": label_confidence,
        "value_confidence": value_confidence,
        "ownership_confidence": ownership_confidence,
        "fact_period_confidence": fact_period_confidence,
        "source_engine": traceability["source_engine"],
        "source_page": traceability["source_page"],
        "source_table_id": traceability["source_table_id"],
        "source_bbox": traceability["source_bbox"],
        "fusion_status": traceability["fusion_status"],
        "source_engines_involved": traceability["source_engines_involved"],
        "source_traceability": traceability,
        "fragment_role": str(block.get("fragment_role") or ("statement_row" if has_numeric else "text_only")),
        "diagnostics": diagnostics,
        "row_index": row_index,
    }


def text_fallback_row_kind(label_text: str, row: dict[str, Any], table: dict[str, Any]) -> str:
    normalized = normalize_label(label_text)
    if not normalized:
        if row_has_numeric_values(row):
            return str(normalized_row_block_fallback(row).get("row_kind") or "statement_line_item")
        return "unknown"
    if is_statement_title_like_text(normalized):
        return "header"
    if is_statement_signature_like_text(normalized) or is_statement_footer_like_text(normalized):
        return "footnote"
    if not row_has_numeric_values(row):
        if contains_normalized_marker(
            normalized,
            "statement of",
            "consolidated statement",
            "for the year ended",
            "for the period ended",
            "expressed in",
            "unless otherwise stated",
            "по состоянию на",
            "за год",
            "за период",
            "в миллионах",
            "в млрд",
        ):
            return "header"
        return "unknown"
    if is_note_reference_like_label(label_text) or normalized == "note":
        return "footnote"
    if contains_normalized_marker(
        normalized,
        "for the year ended",
        "for the period ended",
        "31 december",
        "30 june",
        "30 september",
        "31 march",
        "2025 2024",
        "2023 2022",
    ):
        return "header"
    return str(normalized_row_block_fallback(row).get("row_kind") or "statement_line_item")


def is_statement_title_like_text(normalized: str) -> bool:
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


def is_statement_signature_like_text(normalized: str) -> bool:
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


def is_statement_footer_like_text(normalized: str) -> bool:
    return contains_normalized_marker(
        normalized,
        "the accompanying notes are the integral part",
        "accompanying notes are an integral part",
        "integral part of these consolidated financial statements",
        "integral part of these financial statements",
        "прилагаемые примечания являются неотъемлемой частью",
    )


def is_statement_context_only_label(raw_label: str) -> bool:
    normalized = normalize_label(raw_label)
    return (
        is_statement_title_like_text(normalized)
        or is_statement_signature_like_text(normalized)
        or is_statement_footer_like_text(normalized)
    )


def split_text_fallback_numeric_groups(text: str) -> tuple[str, list[str]] | None:
    tokens = str(text or "").strip().split()
    if len(tokens) < 3:
        return None
    groups: list[str] = []
    group_starts: list[int] = []
    index = len(tokens) - 1
    while index >= 0 and len(groups) < 2:
        token = tokens[index]
        if parse_number(token) is None:
            if groups:
                break
            index -= 1
            continue
        group = [token]
        start_index = index
        index -= 1
        while index >= 0 and len(group) < 2 and parse_number(tokens[index]) is not None:
            previous = tokens[index]
            compact_current_group = re.sub(r"\D", "", group[0])
            if len(group) == 1 and previous.isdigit() and len(previous) <= 3 and len(compact_current_group) >= 6:
                break
            if ("," in previous or "." in previous) and any("," in part or "." in part for part in group):
                break
            candidate = " ".join([previous, *group])
            if parse_number(candidate) is None:
                break
            group.insert(0, previous)
            start_index = index
            index -= 1
        groups.insert(0, " ".join(group))
        group_starts.insert(0, start_index)
    if len(groups) < 2:
        return None
    label = " ".join(tokens[: group_starts[0]]).strip()
    if not label:
        return None
    return label, groups


def text_fallback_notes_like(table: dict[str, Any]) -> bool:
    title = normalize_label(table.get("table_title") or "")
    return contains_normalized_marker(title, "note", "notes", "примеч", "поясн")


def normalized_row_from_block(row_block: dict[str, Any]) -> dict[str, Any]:
    row = {"line": row_block.get("label_text") or ""}
    value_cells = row_block.get("value_cells") or {}
    if isinstance(value_cells, dict):
        row.update(value_cells)
    if row_block.get("source_bbox") is not None:
        row["source_bbox"] = row_block.get("source_bbox")
    if row_block.get("stitched_from_rows"):
        row["stitched_from_rows"] = row_block.get("stitched_from_rows")
    diagnostics = row_block.get("diagnostics") or {}
    if diagnostics.get("source_line"):
        row["source_line"] = diagnostics.get("source_line")
    return row


def table_traceability(table: dict[str, Any]) -> dict[str, Any]:
    traceability = table.get("source_traceability")
    if isinstance(traceability, dict) and traceability:
        return traceability
    location = table.get("source_location")
    return location if isinstance(location, dict) else {}


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


def select_current_value(
    row: dict[str, Any],
    period: str,
    statement_type: str,
    *,
    table: dict[str, Any] | None = None,
    row_block: dict[str, Any] | None = None,
) -> tuple[Any, str | None, str | None]:
    numeric_cells = row_numeric_items(row)
    if not numeric_cells:
        return None, None, "numeric_value_not_detected"
    recovered_current = current_period_value_from_change_percent_line(row, row_block)
    if recovered_current is not None:
        return recovered_current, "source_line_current_period", None
    explicit_keys = explicit_current_period_column_keys(numeric_cells, period, table)
    if len(explicit_keys) == 1:
        key = explicit_keys[0]
        return row[key], str(key), None
    if len(explicit_keys) > 1:
        return None, None, "ambiguous_period_column"
    current_keys = [key for key, value in numeric_cells if is_current_period_column(str(key), period, statement_type)]
    if len(current_keys) == 1:
        key = current_keys[0]
        return row[key], str(key), None
    if len(current_keys) > 1:
        return None, None, "ambiguous_period_column"
    if len(numeric_cells) == 1:
        key, value = numeric_cells[0]
        return value, str(key), None
    if _eligible_positional_current_value_fallback(statement_type, table, row, row_block, numeric_cells):
        key, value = numeric_cells[0]
        return value, str(key), None
    return None, None, "ambiguous_period_column"


def current_period_value_from_change_percent_line(row: dict[str, Any], row_block: dict[str, Any] | None = None) -> str | None:
    if not isinstance(row, dict):
        return None
    diagnostics = (row_block or {}).get("diagnostics") or {}
    source_line = str(row.get("source_line") or diagnostics.get("source_line") or "").strip()
    if not source_line or "%" not in source_line:
        return None
    numeric_cells = row_numeric_items(row)
    if len(numeric_cells) < 2:
        return None
    label_value = current_decimal_value_from_change_percent_label(str(row.get("line") or ""))
    if label_value:
        return label_value
    values = period_values_from_change_percent_line({**row, "source_line": source_line})
    if not values:
        return None
    return values[0]


def period_values_from_change_percent_line(row: dict[str, Any]) -> tuple[str, str] | None:
    source_line = str(row.get("source_line") or "").strip()
    if not source_line or "%" not in source_line:
        return None
    values = financial_decimal_values_with_percent(source_line)
    if len(values) < 3 or not values[-1].endswith("%"):
        return None
    return values[-3].rstrip("%"), values[-2].rstrip("%")


def current_decimal_value_from_change_percent_label(label: str) -> str | None:
    matches = list(re.finditer(r"\(?-?\d{1,3}(?:\s\d{3})*[,.]\d+\)?%?", str(label or "")))
    plain_matches = [match for match in matches if not match.group(0).endswith("%")]
    if not plain_matches:
        return None
    match = plain_matches[-1]
    value = match.group(0).strip()
    suffix = str(label or "")[match.end() :]
    if re.search(r"\d", suffix):
        return value
    parts = value.strip("()").split()
    if len(parts) == 2 and len(parts[0]) <= 2:
        replacement = parts[1]
        if value.startswith("(") and value.endswith(")"):
            return f"({replacement})"
        return replacement
    return value


def financial_decimal_values_with_percent(text: str) -> list[str]:
    pattern = re.compile(r"\(?-?\d{1,3}(?:\s\d{3})*[,.]\d+\)?%?")
    return [match.group(0).strip() for match in pattern.finditer(str(text or ""))]


def explicit_current_period_column_keys(
    numeric_cells: list[tuple[str, Any]],
    period: str,
    table: dict[str, Any] | None,
) -> list[str]:
    normalized_periods = {
        value
        for value in {
            normalized_fact_period(period),
            normalized_fact_period((table or {}).get("effective_period")),
        }
        if value
    }
    if not normalized_periods:
        return []
    matched: list[str] = []
    for key, _ in numeric_cells:
        normalized_key = normalize_label(str(key))
        compact_key = re.sub(r"\s+", "", normalized_key)
        if any(
            normalized_key == normalized_period
            or compact_key == normalized_period.lower()
            or compact_key == normalized_period.lower().replace("q", " q").replace(" ", "")
            for normalized_period in normalized_periods
        ):
            matched.append(str(key))
    return matched


def _eligible_positional_current_value_fallback(
    statement_type: str,
    table: dict[str, Any] | None,
    row: dict[str, Any] | None,
    row_block: dict[str, Any] | None,
    numeric_cells: list[tuple[str, Any]],
) -> bool:
    if len(numeric_cells) != 2:
        return False
    if not isinstance(row_block, dict):
        return False
    ownership_confidence = float(row_block.get("ownership_confidence") or 0.0)
    if ownership_confidence < 0.9:
        return False
    if not isinstance(table, dict):
        return False
    effective_period = normalized_fact_period(table.get("effective_period") or table.get("period"))
    comparative_period = normalized_fact_period(table.get("comparative_period"))
    if not (effective_period and comparative_period):
        return False
    if statement_type == "balance_sheet":
        return True
    if statement_type not in {"income_statement", "cash_flow"}:
        return False
    fact_period_confidence = semantic_fact_period_confidence(table, row_block)
    if fact_period_confidence < 0.8:
        return False
    row_kind = str(row_block.get("row_kind") or "unknown")
    if statement_type == "cash_flow":
        if row_kind not in {"statement_line_item", "subtotal"}:
            return False
    elif row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
        return False
    label_confidence = semantic_label_confidence(str((row or {}).get("line") or ""), row or {}, row_block)
    value_confidence = float(row_block.get("value_confidence") or (row or {}).get("value_confidence") or 0.0)
    if label_confidence < 0.75 or value_confidence < 0.75:
        return False
    diagnostics = dict(row_block.get("diagnostics") or {})
    if str(row_block.get("fusion_status") or "") == "conflict_retained_as_evidence":
        return False
    if diagnostics.get("label_recovery_reason") == "label_ownership_unresolved_after_recovery":
        return False
    row = row or {}
    recovery_mode = str(row.get("recovery_mode") or row_block.get("recovery_mode") or "")
    if bool(
        row.get("label_recovered_from_word_layout")
        or row.get("inline_value_recovered")
        or row.get("stitched_from_rows")
        or row_block.get("stitched_from_rows")
        or recovery_mode
    ):
        return True
    return bool(row_block.get("source_engine") and row_block.get("source_table_id") and row_block.get("source_page") is not None)


def is_current_period_column(column: str, period: str, statement_type: str) -> bool:
    normalized = normalize_label(column)
    compact = re.sub(r"\s+", "", normalized)
    normalized_period = normalized_fact_period(period)
    if normalized_period and compact == normalized_period.lower():
        return True
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
    return bool(document_id and table_traceability(table) and table.get("table_index") is not None)


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
