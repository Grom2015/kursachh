from app.db.models import Company, ReportDocument
from app.services.parsing.tatn_ifrs_pdf_parser import TATNIFRSPDFParser
from app.tools.validate_lkoh_real_extraction import build_fact_coverage


def _document() -> ReportDocument:
    return ReportDocument(
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


def _debt_text(include_net: bool = True) -> str:
    rows = [
        "TATNEFT",
        "Notes to the Consolidated Interim Condensed Financial Statements (unaudited)",
        "Note 12: Debt",
        "Total short-term debt 6,958",
        "Current portion of long-term debt 2,756",
        "Total short-term debt, including current portion of long-term debt 9,714",
        "Total long-term debt 25,682",
        "Less: current portion (2,756)",
    ]
    if include_net:
        rows.append("Total long-term debt, net of current portion 22,926")
    return "\n".join(rows)


def _q3_debt_text() -> str:
    return "\n".join(
        [
            "TATNEFT",
            "Notes to the Consolidated Interim Condensed Financial Statements (unaudited)",
            "Note 12: Debt",
            "Total short-term debt 6,705 7,988",
            "Current portion of long-term debt 2,814 2,973",
            "Total short-term debt, including current portion of",
            "long-term debt 9,519 10,961",
            "Total long-term debt 27,155 26,625",
            "Less: current portion (2,814) (2,973)",
            "Total long-term debt, net of current portion 24,341 23,652",
        ]
    )


def _parse_debt(include_net: bool = True):
    parser = TATNIFRSPDFParser()
    doc = _document()
    facts = parser._extract_from_statement_text(doc, _debt_text(include_net), page_number=20)
    facts.extend(parser._derive_total_debt(doc, facts))
    return facts


def _parse_q3_debt():
    parser = TATNIFRSPDFParser()
    doc = _document()
    doc.report_period = "2021Q3"
    facts = parser._extract_from_statement_text(doc, _q3_debt_text(), page_number=20)
    facts.extend(parser._derive_total_debt(doc, facts))
    return facts


def test_tatn_debt_prefers_including_current_portion_plus_net_long_term_debt():
    facts = _parse_debt()
    debt = next(fact for fact in facts if fact.metric_code == "total_debt")

    assert debt.value == 32640
    assert debt.source_location["formula"] == (
        "short_term_debt_including_current_portion + long_term_debt_net_of_current_portion"
    )
    assert debt.quality_flag == "derived"


def test_tatn_debt_does_not_double_count_gross_long_term_debt():
    facts = _parse_debt()
    debt = next(fact for fact in facts if fact.metric_code == "total_debt")

    assert debt.value != 35396
    assert "total_long_term_debt_gross" not in debt.source_location["inputs"]


def test_tatn_debt_fallback_uses_plain_short_term_plus_gross_long_term():
    facts = _parse_debt(include_net=False)
    debt = next(fact for fact in facts if fact.metric_code == "total_debt")

    assert debt.value == 32640
    assert debt.source_location["formula"] == "total_short_term_debt + total_long_term_debt_gross"


def test_tatn_debt_inputs_use_selected_component_source_references():
    facts = _parse_debt()
    debt = next(fact for fact in facts if fact.metric_code == "total_debt")
    inputs = debt.source_location["inputs"]

    assert set(inputs) == {
        "short_term_debt_including_current_portion",
        "long_term_debt_net_of_current_portion",
    }
    assert inputs["short_term_debt_including_current_portion"]["source_location"]["raw_label"] == (
        "Total short-term debt, including current portion of long-term debt"
    )
    assert inputs["long_term_debt_net_of_current_portion"]["source_location"]["raw_label"] == (
        "Total long-term debt, net of current portion"
    )


def test_tatn_debt_fact_coverage_exposes_selected_source_references():
    facts = _parse_debt()
    row = next(item for item in build_fact_coverage(["2021Q2"], facts) if item["metric_code"] == "total_debt")

    reference_codes = {reference["metric_code"] for reference in row["source_references"]}
    assert reference_codes == {
        "short_term_debt_including_current_portion",
        "long_term_debt_net_of_current_portion",
    }
    assert all(reference["metric_code"] != "total_long_term_debt_gross" for reference in row["source_references"])


def test_tatn_q3_like_debt_note_uses_net_long_term_debt():
    facts = _parse_q3_debt()
    debt = next(fact for fact in facts if fact.metric_code == "total_debt")
    inputs = debt.source_location["inputs"]

    assert debt.value == 33860
    assert debt.source_location["formula"] == (
        "short_term_debt_including_current_portion + long_term_debt_net_of_current_portion"
    )
    assert set(inputs) == {
        "short_term_debt_including_current_portion",
        "long_term_debt_net_of_current_portion",
    }
    assert inputs["short_term_debt_including_current_portion"]["value"] == 9519
    assert inputs["long_term_debt_net_of_current_portion"]["value"] == 24341
    assert "total_long_term_debt_gross" not in inputs
