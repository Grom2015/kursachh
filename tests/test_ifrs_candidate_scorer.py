from app.services.parsing.ifrs.candidate_scorer import score_candidate


def test_revenue_label_maps_only_in_allowed_context():
    strong = score_candidate("Revenue", "profit_or_loss", "ytd", True, True, True)
    weak = score_candidate("Revenue", "changes_in_equity", "ytd", True, True, True)

    assert strong.is_strong is True
    assert strong.concept_code == "revenue"
    assert weak.is_strong is False
    assert weak.rejection_reason == "statement_context_allowed"


def test_profit_for_period_maps_to_net_income_not_equity_statement():
    strong = score_candidate("Profit for the period", "profit_or_loss", "ytd", True, True, True)
    weak = score_candidate("Profit for the period", "changes_in_equity", "ytd", True, True, True)

    assert strong.concept_code == "net_income"
    assert strong.is_strong is True
    assert weak.is_strong is False


def test_operating_cash_flow_maps_only_in_cash_flow():
    strong = score_candidate("Net cash provided by operating activities", "cash_flow", "ytd", True, True, True)
    weak = score_candidate("Net cash provided by operating activities", "profit_or_loss", "ytd", True, True, True)

    assert strong.concept_code == "operating_cash_flow"
    assert strong.is_strong is True
    assert weak.rejection_reason == "statement_context_allowed"


def test_weak_ambiguous_label_not_canonical():
    score = score_candidate("Profit", "profit_or_loss", "ytd", True, True, True)

    assert score.is_strong is False
    assert score.concept_code is None
