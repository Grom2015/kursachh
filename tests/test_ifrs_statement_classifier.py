from app.services.parsing.ifrs.statement_classifier import classify_statement


def test_classifies_profit_or_loss():
    assert classify_statement("Consolidated Statement of Comprehensive Income") == "profit_or_loss"


def test_classifies_financial_position():
    assert classify_statement("Consolidated Statement of Financial Position") == "financial_position"


def test_classifies_cash_flow_and_debt_note():
    assert classify_statement("Consolidated Statement of Cash Flows") == "cash_flow"
    assert classify_statement("Note 12 Debt") == "debt_note"
