from dataclasses import dataclass, field


@dataclass(frozen=True)
class IssuerOverride:
    ticker: str
    additional_label_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    blocked_label_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    context_aliases: dict[str, str] = field(default_factory=dict)
    custom_debt_derivation: str | None = None


OVERRIDES: dict[str, IssuerOverride] = {
    "LKOH": IssuerOverride(
        "LKOH",
        additional_label_patterns={
            "revenue": (r"^sales \(including excise and export tariffs\)",),
            "net_income": (r"^profit for the year$",),
        },
        context_aliases={"statement_text": "profit_or_loss"},
    ),
    "TATN": IssuerOverride(
        "TATN",
        additional_label_patterns={
            "revenue": (
                r"^sales and other operating revenues on non-banking activities, net$",
                r"^sales and other operating revenues$",
            ),
            "operating_profit": (r"^operating profit on non-banking activities$",),
            "total_debt": (r"^derived total debt$",),
        },
        custom_debt_derivation="tatn_current_portion_safe",
    ),
    "GAZP": IssuerOverride(
        "GAZP",
        additional_label_patterns={
            "revenue": (r"^total sales in the consolidated(?: interim condensed)? statement of comprehensive income$",),
            "total_assets": (r"^total assets in the consolidated(?: interim condensed)? balance sheet$",),
            "short_term_borrowings": (
                r"^short-term borrowings, promissory notes and current portion of long-term borrowings$",
            ),
            "long_term_borrowings": (r"^long-term borrowings, promissory notes$",),
            "operating_cash_flow": (r"^net cash from operating activities$",),
        },
        blocked_label_patterns={
            "net_income": (r"attributable to",),
            "capex": (r"capital expenditures2",),
        },
        context_aliases={
            "segment_information_note": "segment_note",
            "cash_and_cash_equivalents_note": "cash_note",
            "net_cash_from_operating_activities_note": "cash_flow_note",
            "financial_risk_factors_note": "financial_risk_note",
        },
        custom_debt_derivation="gazp_borrowings_plus_promissory_notes",
    ),
}


def get_issuer_override(ticker: str | None) -> IssuerOverride:
    return OVERRIDES.get((ticker or "").upper(), IssuerOverride((ticker or "UNKNOWN").upper()))
