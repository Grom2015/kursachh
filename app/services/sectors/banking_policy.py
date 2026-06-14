from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings

BANKING_TICKERS = {"SBER", "VTBR", "BSPB", "CBOM"}
FINANCIAL_NON_BANK_TICKERS = {"MOEX"}

BANKING_UNSUPPORTED_INDUSTRIAL_METRICS = {
    "debt_to_equity",
    "current_ratio",
    "fcf",
    "fcf_margin",
    "ebitda_margin",
    "operating_margin",
    "net_margin",
    "revenue_growth",
}

BANKING_METRIC_CODES = [
    "roe",
    "roa",
    "net_interest_margin",
    "cost_to_income",
    "loan_to_deposit",
    "equity_to_assets",
    "net_margin_like",
]

BANKING_FACT_CODES = {
    "interest_income",
    "interest_expense",
    "fee_and_commission_income",
    "fee_and_commission_expense",
    "net_interest_income",
    "net_fee_commission_income",
    "net_trading_income",
    "operating_income",
    "operating_expenses",
    "impairment_charge",
    "profit_before_tax",
    "net_income",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "cash_and_equivalents",
    "loans_to_customers",
    "retail_customer_accounts",
    "corporate_customer_accounts",
    "customer_accounts",
    "operating_cash_flow",
}

BANKING_KEY_FACT_CODES = {
    "total_assets",
    "total_liabilities",
    "total_equity",
    "cash_and_equivalents",
    "loans_to_customers",
    "customer_accounts",
    "net_interest_income",
    "operating_income",
    "operating_expenses",
    "profit_before_tax",
    "net_income",
    "operating_cash_flow",
}


@dataclass(frozen=True)
class BankingCatalogItem:
    ticker: str
    issuer_name_ru: str
    edisclosure_id: str | None
    is_bank: bool


def is_banking_ticker(ticker: str | None) -> bool:
    return str(ticker or "").upper() in BANKING_TICKERS


def is_financial_non_bank_ticker(ticker: str | None) -> bool:
    return str(ticker or "").upper() in FINANCIAL_NON_BANK_TICKERS


def _identity_text(*parts: object) -> str:
    return " ".join(str(part or "") for part in parts).casefold()


def _legacy_sector_is_banking(sector: str | None, subsector: str | None = None, ticker: str | None = None) -> bool:
    if is_banking_ticker(ticker):
        return True
    normalized = " ".join([str(sector or ""), str(subsector or "")]).casefold()
    return "bank" in normalized or "банк" in normalized


def sector_is_banking(sector: str | None, subsector: str | None = None, ticker: str | None = None) -> bool:
    if is_banking_ticker(ticker):
        return True
    normalized = _identity_text(sector, subsector)
    return any(marker in normalized for marker in ("bank", "банк", "банков", "credit institution", "кредитн", "Р±Р°РЅРє"))


def sector_is_financial_non_bank(
    sector: str | None,
    subsector: str | None = None,
    ticker: str | None = None,
    short_name: str | None = None,
    full_name: str | None = None,
) -> bool:
    if is_financial_non_bank_ticker(ticker):
        return True
    if sector_is_banking(sector, subsector, ticker):
        return False
    normalized = _identity_text(sector, subsector, short_name, full_name)
    return any(
        marker in normalized
        for marker in ("exchange", "бирж", "insurance", "страх", "leasing", "лизинг", "financial", "финанс")
    )


def resolve_issuer_class(
    *,
    ticker: str | None = None,
    sector: str | None = None,
    subsector: str | None = None,
    short_name: str | None = None,
    full_name: str | None = None,
    aliases: list[str] | None = None,
) -> str:
    identity = _identity_text(sector, subsector, short_name, full_name, " ".join(aliases or []))
    if sector_is_banking(sector, subsector, ticker) or any(
        marker in identity for marker in ("bank", "банк", "банков", "credit institution", "кредитн", "Р±Р°РЅРє")
    ):
        return "bank_ifrs"
    if sector_is_financial_non_bank(sector, subsector, ticker, short_name, full_name):
        return "financial_non_bank_ifrs"
    ticker_normalized = str(ticker or "").upper()
    if ticker_normalized in {"MGNT", "X5", "FIVE"}:
        return "industrial_ifrs_retail"
    if ticker_normalized in {"LKOH", "GAZP", "ROSN", "SIBN", "TATN", "NVTK"}:
        return "industrial_ifrs_oil_gas"
    if ticker_normalized in {"GMKN", "PLZL", "CHMF", "MAGN", "PHOR"}:
        return "industrial_ifrs_metals_mining"
    return "industrial_ifrs_general"


def issuer_class_is_banking(issuer_class: str | None) -> bool:
    return str(issuer_class or "").startswith("bank")


def issuer_class_is_financial_non_bank(issuer_class: str | None) -> bool:
    return str(issuer_class or "").startswith("financial_non_bank")


def bank_catalog_path(root: Path | None = None) -> Path:
    base = (root or get_settings().root_dir).resolve()
    return base / "data" / "reference" / "moex_bank_report_sources.json"
