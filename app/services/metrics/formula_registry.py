from dataclasses import dataclass


@dataclass(frozen=True)
class FormulaDefinition:
    metric_code: str
    display_name: str
    formula: str
    required_inputs: list[str]
    applicable_company_types: list[str]
    output_format: str


FORMULAS: dict[str, FormulaDefinition] = {
    "revenue_growth": FormulaDefinition(
        "revenue_growth",
        "Revenue growth",
        "(revenue - previous_revenue) / previous_revenue",
        ["revenue", "previous_revenue"],
        ["all"],
        "percent",
    ),
    "ebitda_margin": FormulaDefinition(
        "ebitda_margin", "EBITDA margin", "ebitda / revenue", ["ebitda", "revenue"], ["all"], "percent"
    ),
    "operating_margin": FormulaDefinition(
        "operating_margin",
        "Operating margin",
        "operating_profit / revenue",
        ["operating_profit", "revenue"],
        ["all"],
        "percent",
    ),
    "net_margin": FormulaDefinition(
        "net_margin", "Net margin", "net_income / revenue", ["net_income", "revenue"], ["all"], "percent"
    ),
    "roe": FormulaDefinition(
        "roe", "ROE", "net_income_ttm / average_equity", ["net_income", "total_equity"], ["all"], "percent"
    ),
    "roa": FormulaDefinition(
        "roa", "ROA", "net_income_ttm / average_assets", ["net_income", "total_assets"], ["all"], "percent"
    ),
    "debt_to_equity": FormulaDefinition(
        "debt_to_equity", "Debt to equity", "total_debt / total_equity", ["total_debt", "total_equity"], ["all"], "ratio"
    ),
    "net_debt_to_ebitda": FormulaDefinition(
        "net_debt_to_ebitda",
        "Net debt to EBITDA",
        "(total_debt - cash_and_equivalents) / ebitda_ttm",
        ["total_debt", "cash_and_equivalents", "ebitda"],
        ["all"],
        "ratio",
    ),
    "current_ratio": FormulaDefinition(
        "current_ratio",
        "Current ratio",
        "current_assets / current_liabilities",
        ["current_assets", "current_liabilities"],
        ["all"],
        "ratio",
    ),
    "fcf": FormulaDefinition(
        "fcf", "Free cash flow", "operating_cash_flow - capex", ["operating_cash_flow", "capex"], ["all"], "currency"
    ),
    "fcf_margin": FormulaDefinition(
        "fcf_margin", "FCF margin", "fcf / revenue", ["operating_cash_flow", "capex", "revenue"], ["all"], "percent"
    ),
    "pe_ratio": FormulaDefinition(
        "pe_ratio", "P/E", "market_cap / net_income_ttm", ["market_cap", "net_income"], ["all"], "ratio"
    ),
    "ev_to_ebitda": FormulaDefinition(
        "ev_to_ebitda", "EV/EBITDA", "enterprise_value / ebitda_ttm", ["enterprise_value", "ebitda"], ["all"], "ratio"
    ),
    "dividend_yield": FormulaDefinition(
        "dividend_yield", "Dividend yield", "dividend_per_share / price", ["dividend_per_share", "price"], ["all"], "percent"
    ),
    "net_interest_margin": FormulaDefinition(
        "net_interest_margin",
        "Net interest margin",
        "net_interest_income / average_interest_earning_assets",
        ["net_interest_income", "average_interest_earning_assets"],
        ["bank"],
        "percent",
    ),
    "cost_to_income": FormulaDefinition(
        "cost_to_income",
        "Cost to income",
        "operating_expenses / operating_income",
        ["operating_expenses", "operating_income"],
        ["bank"],
        "percent",
    ),
    "loan_to_deposit": FormulaDefinition(
        "loan_to_deposit",
        "Loan to deposit",
        "loans_to_customers / customer_accounts",
        ["loans_to_customers", "customer_accounts"],
        ["bank"],
        "ratio",
    ),
    "equity_to_assets": FormulaDefinition(
        "equity_to_assets",
        "Equity to assets",
        "total_equity / total_assets",
        ["total_equity", "total_assets"],
        ["bank"],
        "percent",
    ),
    "net_margin_like": FormulaDefinition(
        "net_margin_like",
        "Net income to operating income",
        "net_income / operating_income",
        ["net_income", "operating_income"],
        ["bank"],
        "percent",
    ),
}
