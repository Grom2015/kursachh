import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, ReportDocument, StatementFact


@dataclass
class FactCandidateReviewRequest:
    company_ticker: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"


@dataclass
class FactCandidatePromotionRequest(FactCandidateReviewRequest):
    confirm: bool = False
    metric_codes: list[str] | None = None


@dataclass
class FactCandidateReviewReport:
    company_ticker: str
    period_from: str
    period_to: str
    reporting_standard: str
    generated_at: str
    parse_report_path: str
    candidates_count: int
    eligible_count: int
    needs_review_count: int
    blocked_count: int
    review_items: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    safety: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FactCandidateReviewService:
    def __init__(self, db: Session, root: Path | None = None):
        self.db = db
        self.root = (root or get_settings().root_dir).resolve()

    def build_review_report(self, request: FactCandidateReviewRequest) -> tuple[FactCandidateReviewReport, Path]:
        request = FactCandidateReviewRequest(
            company_ticker=request.company_ticker.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
        )
        parse_path = self._parse_report_path(request)
        warnings: list[str] = []
        if not parse_path.exists():
            report = FactCandidateReviewReport(
                company_ticker=request.company_ticker,
                period_from=request.period_from,
                period_to=request.period_to,
                reporting_standard=request.reporting_standard,
                generated_at=datetime.now(UTC).isoformat(),
                parse_report_path=self._relative(parse_path),
                candidates_count=0,
                eligible_count=0,
                needs_review_count=0,
                blocked_count=0,
                review_items=[],
                warnings=["dataframe_parse_report_missing"],
                safety=self._safety(),
            )
            return report, self.save_review_report(report)
        payload = json.loads(parse_path.read_text(encoding="utf-8"))
        items = [self._review_item(item) for item in payload.get("facts", [])]
        warnings.extend(payload.get("warnings") or [])
        report = FactCandidateReviewReport(
            company_ticker=request.company_ticker,
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard,
            generated_at=datetime.now(UTC).isoformat(),
            parse_report_path=self._relative(parse_path),
            candidates_count=len(items),
            eligible_count=sum(1 for item in items if item["review_status"] == "eligible_for_review"),
            needs_review_count=sum(1 for item in items if item["review_status"] == "needs_manual_check"),
            blocked_count=sum(1 for item in items if item["review_status"] == "blocked"),
            review_items=items,
            warnings=warnings,
            safety=self._safety(),
        )
        return report, self.save_review_report(report)

    def promote_reviewed_candidates(self, request: FactCandidatePromotionRequest) -> tuple[dict[str, Any], Path]:
        if not request.confirm:
            raise ValueError("Promotion requires confirm=true.")
        review, path = self.build_review_report(request)
        company = self.db.scalar(select(Company).where(Company.ticker == request.company_ticker.upper()))
        if not company:
            raise ValueError(f"Company not found: {request.company_ticker}")
        allowed_metric_codes = set(request.metric_codes or [])
        promoted = 0
        skipped = 0
        blocked = 0
        promoted_items: list[dict[str, Any]] = []
        for item in review.review_items:
            if item["review_status"] != "eligible_for_review":
                blocked += 1
                continue
            if allowed_metric_codes and item["metric_code"] not in allowed_metric_codes:
                skipped += 1
                continue
            existing = self.db.scalar(
                select(StatementFact).where(
                    StatementFact.company_id == company.id,
                    StatementFact.report_document_id == item["source_document_id"],
                    StatementFact.period == item["period"],
                    StatementFact.metric_code == item["metric_code"],
                    StatementFact.reporting_standard == request.reporting_standard.upper(),
                )
            )
            payload = self._statement_fact_payload(company.id, item, request.reporting_standard.upper())
            if existing:
                if existing.quality_flag != "reviewed_manual_upload":
                    skipped += 1
                    continue
                for key, value in payload.items():
                    setattr(existing, key, value)
            else:
                self.db.add(StatementFact(**payload))
            promoted += 1
            promoted_items.append(
                {
                    "metric_code": item["metric_code"],
                    "period": item["period"],
                    "value": item["value"],
                    "source_document_id": item["source_document_id"],
                }
            )
        self.db.commit()
        result = {
            "company_ticker": request.company_ticker.upper(),
            "period_from": request.period_from,
            "period_to": request.period_to,
            "reporting_standard": request.reporting_standard.upper(),
            "review_report_path": self._relative(path),
            "promoted_count": promoted,
            "skipped_count": skipped,
            "blocked_count": blocked,
            "promoted_items": promoted_items,
            "safety": {
                "facts_persisted": promoted > 0,
                "quality_flag": "reviewed_manual_upload",
                "official_source_verified": False,
                "source_package_ready_contribution": False,
                "metric_engine_invoked": False,
                "valuation_invoked": False,
                "llm_invoked": False,
            },
        }
        output = self._promotion_report_path(request)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result, output

    def save_review_report(self, report: FactCandidateReviewReport) -> Path:
        path = self._review_report_path(report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _review_item(self, item: dict[str, Any]) -> dict[str, Any]:
        source_location = item.get("source_location") or {}
        document = self.db.get(ReportDocument, item.get("source_document_id")) if item.get("source_document_id") else None
        has_trace = bool(
            source_location.get("page_number") or source_location.get("source_line") or source_location.get("inputs")
        )
        lower_trust = item.get("extraction_method") in {
            "text_table_fallback_semantic_gate",
            "dataframe_statement_parser_derived",
        }
        if item.get("value") is None:
            review_status = "blocked"
            reason = "value_missing"
        elif not has_trace:
            review_status = "needs_manual_check"
            reason = "traceability_incomplete"
        else:
            review_status = "eligible_for_review"
            reason = "candidate_has_value_and_traceability"
        return {
            "metric_code": item.get("metric_code"),
            "period": item.get("period"),
            "value": item.get("value"),
            "currency": item.get("currency"),
            "period_type": item.get("period_type"),
            "source_document_id": item.get("source_document_id"),
            "source_table_type": item.get("source_table_type"),
            "source_table_index": item.get("source_table_index"),
            "page_number": source_location.get("page_number"),
            "raw_label": item.get("raw_label"),
            "raw_value": item.get("raw_value"),
            "source_line": source_location.get("source_line"),
            "quality_flag": item.get("quality_flag"),
            "extraction_method": item.get("extraction_method"),
            "review_status": review_status,
            "reason": reason,
            "source_trust_bucket": getattr(document, "source_trust_bucket", None) or "manual_upload_candidate_based",
            "official_source_verified": bool(getattr(document, "official_source_verified", False)),
            "source_package_ready_contribution": bool(getattr(document, "source_package_ready_contribution", False)),
            "trust_warning": "manual_upload_candidate_based" if lower_trust else None,
            "source_location": source_location,
        }

    def _statement_fact_payload(self, company_id: int, item: dict[str, Any], reporting_standard: str) -> dict[str, Any]:
        source_location = {
            **(item.get("source_location") or {}),
            "review_status": "reviewed",
            "source_trust_bucket": "manual_upload_validated",
            "official_source_verified": False,
            "source_package_ready_contribution": False,
            "promotion_stage": "manual_review",
        }
        return {
            "company_id": company_id,
            "report_document_id": item["source_document_id"],
            "period": item["period"],
            "reporting_standard": reporting_standard,
            "statement_type": item.get("source_table_type") or "other",
            "metric_code": item["metric_code"],
            "metric_name_original": item.get("raw_label"),
            "value": item.get("value"),
            "currency": item.get("currency"),
            "unit_multiplier": 1.0,
            "period_type": item.get("period_type") or "unknown",
            "source_location": source_location,
            "quality_flag": "reviewed_manual_upload",
            "confidence_score": 0.95,
        }

    def _parse_report_path(self, request: FactCandidateReviewRequest) -> Path:
        return self.root / "data" / "validation" / request.company_ticker / (
            f"{request.period_from}_{request.period_to}_dataframe_statement_fact_parse.json"
        )

    def _review_report_path(self, report: FactCandidateReviewReport) -> Path:
        return self.root / "data" / "validation" / report.company_ticker / (
            f"{report.period_from}_{report.period_to}_fact_candidate_review.json"
        )

    def _promotion_report_path(self, request: FactCandidateReviewRequest) -> Path:
        return self.root / "data" / "validation" / request.company_ticker.upper() / (
            f"{request.period_from}_{request.period_to}_fact_candidate_promotion.json"
        )

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _safety() -> dict[str, Any]:
        return {
            "review_stage_only": True,
            "facts_persisted": False,
            "metric_engine_invoked": False,
            "valuation_invoked": False,
            "llm_invoked": False,
            "manual_upload_remains_lower_trust": True,
            "official_source_verified": False,
            "source_package_ready_contribution": False,
        }
