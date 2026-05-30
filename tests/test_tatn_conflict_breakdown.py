from app.db.models import Company, ReportDocument, StatementFact
from app.services.parsing.reconciliation import SourceReconciler
from app.services.parsing.tatn_ifrs_pdf_parser import TATNIFRSPDFParser
from app.tools.validate_real_extraction import conflict_breakdown


def _fact(metric_code: str, value: float, period_type: str = "balance_sheet_snapshot") -> StatementFact:
    return StatementFact(
        company_id=1,
        report_document_id=1,
        period="2021Q2",
        reporting_standard="IFRS",
        statement_type="balance_sheet",
        metric_code=metric_code,
        value=value,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type=period_type,
        source_location={
            "source_role": "financial_statements",
            "source_type": "issuer_ir_manifest",
            "source_url": "https://old.tatneft.ru/report.pdf",
            "raw_label": metric_code,
            "table_title": "note",
        },
        quality_flag="exact",
        confidence_score=0.85,
    )


def test_conflict_breakdown_generated_for_conflicting_fact():
    reconciled = SourceReconciler().reconcile([_fact("current_assets", 1), _fact("current_assets", 2)])
    rows = conflict_breakdown(reconciled)

    assert rows
    assert rows[0]["metric_code"] == "current_assets"
    assert rows[0]["candidate_values"] == [None, 2]
    assert rows[0]["recommended_action"]


def test_duplicate_same_value_conflict_merges():
    reconciled = SourceReconciler().reconcile([_fact("current_assets", 1), _fact("current_assets", 1)])

    assert len(reconciled) == 1
    assert reconciled[0].value == 1
    assert reconciled[0].quality_flag == "exact"


def test_different_period_type_does_not_conflict():
    reconciled = SourceReconciler().reconcile(
        [_fact("revenue", 100, period_type="ytd"), _fact("revenue", 50, period_type="quarter")]
    )

    assert len(reconciled) == 2
    assert all(fact.quality_flag == "exact" for fact in reconciled)


def test_tatn_parser_excludes_wrong_note_matches():
    parser = TATNIFRSPDFParser()
    text = "\n".join(
        [
            "TATNEFT",
            "Notes to the Consolidated Interim Condensed Financial Statements (unaudited)",
            "Note 18: Related party transactions",
            "Total current assets 437 795",
            "Cash and cash equivalents 12,628 14,007",
        ]
    )

    document = ReportDocument(
        id=1,
        company_id=1,
        company=Company(id=1, ticker="TATN", board="TQBR", short_name="TATN", full_name="TATN"),
        report_period="2021Q2",
        reporting_standard="IFRS",
        source_type="issuer_ir_manifest",
        source_role="financial_statements",
        source_url="https://old.tatneft.ru/report.pdf",
        storage_path="report.pdf",
    )
    facts = parser._extract_from_statement_text(document, text, page_number=29)

    assert facts == []
