from typing import Literal

StatementContext = Literal[
    "profit_or_loss",
    "financial_position",
    "cash_flow",
    "changes_in_equity",
    "debt_note",
    "segment_note",
    "cash_note",
    "cash_flow_note",
    "financial_risk_note",
    "unknown",
]


def classify_statement(title_text: str = "", nearby_text: str = "", table_headers: list[str] | None = None) -> StatementContext:
    text = " ".join([title_text, nearby_text, " ".join(table_headers or [])]).casefold()
    normalized = " ".join(text.split())
    if "statement of changes in equity" in normalized:
        return "changes_in_equity"
    if "statement of cash flows" in normalized:
        return "cash_flow"
    if "net cash from operating activities" in normalized:
        return "cash_flow_note"
    if "statement of comprehensive income" in normalized or "statement of profit or loss" in normalized:
        return "profit_or_loss"
    if "balance sheet" in normalized or "statement of financial position" in normalized:
        return "financial_position"
    if "debt" in normalized or "borrowings" in normalized:
        return "debt_note"
    if "segment information" in normalized:
        return "segment_note"
    if "cash and cash equivalents" in normalized:
        return "cash_note"
    if "financial risk factors" in normalized:
        return "financial_risk_note"
    return "unknown"
