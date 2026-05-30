from app.services.parsing.ifrs.concept_registry import CONCEPTS


def test_registry_contains_required_concepts():
    required = {
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
        "short_term_borrowings",
        "long_term_borrowings",
        "total_debt",
        "ebitda",
    }

    assert required.issubset(CONCEPTS)


def test_ebitda_is_marked_non_ifrs_and_not_derived_by_registry():
    ebitda = CONCEPTS["ebitda"]

    assert ebitda.is_non_ifrs_metric is True
    assert "Do not derive" in ebitda.notes
