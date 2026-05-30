from app.services.parsing.ifrs.candidate_scorer import score_candidate
from app.services.parsing.ifrs.issuer_overrides import get_issuer_override


def test_tatn_non_banking_revenue_override_maps_to_revenue():
    score = score_candidate(
        "Sales and other operating revenues on non-banking activities, net",
        "profit_or_loss",
        "ytd",
        True,
        True,
        True,
        ticker="TATN",
    )

    assert score.is_strong is True
    assert score.concept_code == "revenue"
    assert score.applied_issuer_override == "TATN"


def test_gazp_blocks_attributable_profit_as_net_income():
    score = score_candidate(
        "Profit for the year attributable to the owners of PJSC Gazprom",
        "profit_or_loss",
        "annual",
        True,
        True,
        True,
        ticker="GAZP",
    )

    assert score.is_strong is False
    assert score.rejection_reason == "excluded_label_pattern"


def test_tatn_debt_override_is_current_portion_safe():
    override = get_issuer_override("TATN")

    assert override.custom_debt_derivation == "tatn_current_portion_safe"
