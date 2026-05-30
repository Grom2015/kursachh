import json
import shutil
from pathlib import Path

from sqlalchemy import select

from app.db.models import Company, ReportDocument, StatementFact
from app.services.review.fact_candidate_review import (
    FactCandidatePromotionRequest,
    FactCandidateReviewRequest,
    FactCandidateReviewService,
)


def _root() -> Path:
    root = Path("pytest-cache-files-fact-review").resolve()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manual_doc(db_session, ticker="SBER"):
    company = db_session.scalar(select(Company).where(Company.ticker == ticker))
    if not company:
        company = Company(
            ticker=ticker,
            board="TQBR",
            short_name=ticker,
            full_name="Sberbank",
            aliases_json=[],
            sector="financials",
            subsector="bank",
        )
        db_session.add(company)
        db_session.flush()
    doc = ReportDocument(
        company_id=company.id,
        company=company,
        report_period="2025Q4",
        reporting_standard="IFRS",
        document_type="financial_statements",
        source_role="financial_statements",
        source_type="manual_upload",
        status="validated",
        trust_level="user_provided_unverified",
        source_trust_bucket="manual_upload_validated",
        official_source_verified=False,
        source_package_ready_contribution=False,
    )
    db_session.add(doc)
    db_session.flush()
    return company, doc


def _write_parse_report(root: Path, doc: ReportDocument):
    path = root / "data" / "validation" / "SBER" / "2025Q1_2025Q4_dataframe_statement_fact_parse.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "company_ticker": "SBER",
                "period_from": "2025Q1",
                "period_to": "2025Q4",
                "reporting_standard": "IFRS",
                "facts": [
                    {
                        "company_ticker": "SBER",
                        "period": "2025Q4",
                        "reporting_standard": "IFRS",
                        "metric_code": "total_assets",
                        "value": 68_814_500_000_000,
                        "currency": "RUB",
                        "period_type": "balance_sheet_snapshot",
                        "source_document_id": doc.id,
                        "source_table_type": "balance_sheet",
                        "source_table_index": 0,
                        "raw_label": "ИТОГО АКТИВОВ",
                        "raw_value": "68814,5",
                        "quality_flag": "high_confidence_text_fallback",
                        "extraction_method": "text_table_fallback_semantic_gate",
                        "source_location": {
                            "page_number": 5,
                            "source_line": "ИТОГО АКТИВОВ 68814,5 60855,1",
                            "fact_source_kind": "text_table_fallback_semantic_gate",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_fact_candidate_review_report_is_audit_only(db_session):
    root = _root()
    _company, doc = _manual_doc(db_session)
    _write_parse_report(root, doc)

    report, path = FactCandidateReviewService(db_session, root=root).build_review_report(
        FactCandidateReviewRequest("SBER", "2025Q1", "2025Q4")
    )

    assert path.exists()
    assert report.candidates_count == 1
    assert report.eligible_count == 1
    assert report.safety["facts_persisted"] is False
    assert db_session.scalars(select(StatementFact)).all() == []
    shutil.rmtree(root, ignore_errors=True)


def test_fact_candidate_promotion_requires_confirmation_and_preserves_manual_trust(db_session):
    root = _root()
    _company, doc = _manual_doc(db_session)
    _write_parse_report(root, doc)
    service = FactCandidateReviewService(db_session, root=root)

    try:
        service.promote_reviewed_candidates(FactCandidatePromotionRequest("SBER", "2025Q1", "2025Q4", confirm=False))
    except ValueError as exc:
        assert "confirm=true" in str(exc)
    else:
        raise AssertionError("Promotion without confirmation must fail")

    result, path = service.promote_reviewed_candidates(
        FactCandidatePromotionRequest("SBER", "2025Q1", "2025Q4", confirm=True)
    )
    fact = db_session.scalar(select(StatementFact).where(StatementFact.metric_code == "total_assets"))

    assert path.exists()
    assert result["promoted_count"] == 1
    assert result["safety"]["quality_flag"] == "reviewed_manual_upload"
    assert fact is not None
    assert fact.quality_flag == "reviewed_manual_upload"
    assert fact.source_location["official_source_verified"] is False
    assert fact.source_location["source_package_ready_contribution"] is False
    shutil.rmtree(root, ignore_errors=True)
