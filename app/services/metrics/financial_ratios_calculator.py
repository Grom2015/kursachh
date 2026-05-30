import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, StatementFact
from app.services.metrics.financial_ratios import display_value
from app.services.metrics.formula_registry import FORMULAS
from app.services.metrics.metric_methodology_registry import METHODOLOGIES
from app.services.periods import periods_between, previous_period
from app.services.sectors.banking_policy import (
    BANKING_METRIC_CODES,
    BANKING_UNSUPPORTED_INDUSTRIAL_METRICS,
    sector_is_banking,
)

METRIC_CODES = [
    "roe",
    "roa",
    "debt_to_equity",
    "current_ratio",
    "fcf",
    "fcf_margin",
    "ebitda_margin",
    "operating_margin",
    "net_margin",
    "revenue_growth",
]
TEXT_FALLBACK_METHOD = "text_table_fallback_semantic_gate"
SAFE_QUALITY_FLAGS = {"exact", "derived", "high_confidence", "manual_verified", "reviewed"}
BALANCE_SHEET_FACTS = {
    "total_assets",
    "total_equity",
    "current_assets",
    "current_liabilities",
    "cash_and_equivalents",
    "total_debt",
}
FORMULA_TEXT_OVERRIDES = {
    "roe": "net_income / average_total_equity",
    "roa": "net_income / average_total_assets",
}


@dataclass
class FinancialRatiosRequest:
    company_ticker: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    source: str = "persisted_facts"
    allow_text_fallback_candidates: bool = False
    output_format: str = "json"


@dataclass
class RatioFact:
    metric_code: str
    period: str
    value: float
    reporting_standard: str
    period_type: str
    source_fact_id: str | int | None
    source_location: dict[str, Any] | None
    quality_flag: str
    unit_multiplier: float = 1.0
    extraction_method: str | None = None
    raw_label: str | None = None

    @property
    def economic_value(self) -> float:
        return self.value * (self.unit_multiplier or 1.0)

    @property
    def uses_text_fallback(self) -> bool:
        return self.extraction_method == TEXT_FALLBACK_METHOD or (
            isinstance(self.source_location, dict)
            and (
                self.source_location.get("extraction_method") == TEXT_FALLBACK_METHOD
                or self.source_location.get("fact_source_kind") == TEXT_FALLBACK_METHOD
            )
        )


@dataclass
class FinancialRatiosReport:
    company_ticker: str
    period_from: str
    period_to: str
    reporting_standard: str
    facts_source: str
    generated_at: str
    metrics: list[dict[str, Any]]
    missing_metrics: list[dict[str, Any]]
    unsupported_metrics: list[dict[str, Any]]
    warnings: list[str]
    blockers: list[str]
    methodology_notes: list[str]
    summary: dict[str, int] = field(default_factory=dict)
    sector_policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FinancialRatiosCalculator:
    def __init__(self, db: Session | None = None, root: Path | None = None):
        self.db = db
        self.root = (root or get_settings().root_dir).resolve()

    def calculate(self, request: FinancialRatiosRequest) -> FinancialRatiosReport:
        request = FinancialRatiosRequest(
            company_ticker=request.company_ticker.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            source=request.source,
            allow_text_fallback_candidates=request.allow_text_fallback_candidates,
            output_format=request.output_format,
        )
        is_banking = self._is_banking_request(request)
        facts, warnings = self._load_facts(request)
        by_period = self._index_facts(facts)
        metrics: list[dict[str, Any]] = []
        metric_codes = list(dict.fromkeys([*METRIC_CODES, *BANKING_METRIC_CODES])) if is_banking else METRIC_CODES
        for period in periods_between(request.period_from, request.period_to):
            for code in metric_codes:
                metrics.append(self._calculate_metric(code, period, by_period, request, is_banking=is_banking))
        missing = [item for item in metrics if item["status"] == "missing"]
        unsupported = [item for item in metrics if item["status"] in {"unsupported_by_policy", "unsupported_by_sector_policy"}]
        blockers = sorted({item.get("reason") for item in metrics if item["status"].startswith("blocked") and item.get("reason")})
        methodology_notes = sorted({note for item in metrics for note in item.get("methodology_notes", [])})
        summary = {
            "calculated_count": sum(1 for item in metrics if item["status"] == "calculated"),
            "missing_count": len(missing),
            "unsupported_count": len(unsupported),
            "blocked_count": sum(1 for item in metrics if item["status"].startswith("blocked")),
        }
        return FinancialRatiosReport(
            company_ticker=request.company_ticker,
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard,
            facts_source=request.source,
            generated_at=datetime.now(UTC).isoformat(),
            metrics=metrics,
            missing_metrics=missing,
            unsupported_metrics=unsupported,
            warnings=warnings,
            blockers=blockers,
            methodology_notes=methodology_notes,
            summary=summary,
            sector_policy={
                "sector_profile": "banking" if is_banking else "standard_corporate",
                "banking_industrial_metrics_unsupported": sorted(BANKING_UNSUPPORTED_INDUSTRIAL_METRICS) if is_banking else [],
            },
        )

    def save_report(self, report: FinancialRatiosReport) -> Path:
        root = self.root / "data" / "validation" / report.company_ticker.upper()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{report.period_from}_{report.period_to}_financial_ratios.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _load_facts(self, request: FinancialRatiosRequest) -> tuple[list[RatioFact], list[str]]:
        if request.source == "dataframe_parse_report":
            return self._facts_from_parse_report(request)
        if request.source != "persisted_facts":
            return [], [f"Unsupported facts source: {request.source}"]
        return self._persisted_facts(request)

    def _persisted_facts(self, request: FinancialRatiosRequest) -> tuple[list[RatioFact], list[str]]:
        if self.db is None:
            return [], ["Database session is required for persisted_facts source."]
        company = self.db.scalar(select(Company).where(Company.ticker == request.company_ticker))
        if not company:
            return [], [f"Company not found: {request.company_ticker}"]
        candidate_periods = set(periods_between(request.period_from, request.period_to))
        previous = previous_period(request.period_from)
        if previous:
            candidate_periods.add(previous)
        previous_annual = _previous_year_q4(request.period_to)
        if previous_annual:
            candidate_periods.add(previous_annual)
        rows = self.db.scalars(
            select(StatementFact).where(
                StatementFact.company_id == company.id,
                StatementFact.period.in_(candidate_periods),
                StatementFact.reporting_standard == request.reporting_standard,
            )
        ).all()
        facts: list[RatioFact] = []
        warnings: list[str] = []
        for row in rows:
            if row.quality_flag == "fixture":
                continue
            if row.quality_flag in {"low_confidence_parse", "conflicting_sources"}:
                continue
            location = row.source_location or {}
            extraction_method = location.get("extraction_method") or location.get("fact_source_kind")
            if extraction_method == TEXT_FALLBACK_METHOD and not request.allow_text_fallback_candidates:
                continue
            if row.value is None:
                continue
            facts.append(
                RatioFact(
                    metric_code=row.metric_code,
                    period=row.period,
                    value=row.value,
                    reporting_standard=row.reporting_standard,
                    period_type=row.period_type,
                    source_fact_id=row.id,
                    source_location=location,
                    quality_flag=row.quality_flag,
                    unit_multiplier=row.unit_multiplier or 1.0,
                    extraction_method=extraction_method,
                    raw_label=location.get("raw_label"),
                )
            )
        if not facts:
            warnings.append("No eligible persisted statement facts found.")
        return facts, warnings

    def _facts_from_parse_report(self, request: FinancialRatiosRequest) -> tuple[list[RatioFact], list[str]]:
        path = self.root / "data" / "validation" / request.company_ticker / (
            f"{request.period_from}_{request.period_to}_dataframe_statement_fact_parse.json"
        )
        if not path.exists():
            return [], [f"DataFrame parse report not found: {path}"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        facts: list[RatioFact] = []
        candidate_periods = set(periods_between(request.period_from, request.period_to))
        previous = previous_period(request.period_from)
        if previous:
            candidate_periods.add(previous)
        previous_annual = _previous_year_q4(request.period_to)
        if previous_annual:
            candidate_periods.add(previous_annual)
        for item in payload.get("facts", []):
            if (item.get("reporting_standard") or "").upper() != request.reporting_standard:
                continue
            period = item.get("period")
            if not period or period not in candidate_periods:
                continue
            extraction_method = item.get("extraction_method") or (item.get("source_location") or {}).get("fact_source_kind")
            if extraction_method == "dataframe_statement_parser_derived":
                continue
            if extraction_method == TEXT_FALLBACK_METHOD and not request.allow_text_fallback_candidates:
                continue
            quality_flag = item.get("quality_flag") or "unknown"
            if quality_flag in {"fixture", "low_confidence_parse", "conflicting_sources"}:
                continue
            if item.get("value") is None:
                continue
            facts.append(
                RatioFact(
                    metric_code=item["metric_code"],
                    period=period,
                    value=float(item["value"]),
                    reporting_standard=request.reporting_standard,
                    period_type=item.get("period_type") or "unknown",
                    source_fact_id=item.get("source_fact_id") or item.get("source_document_id"),
                    source_location=item.get("source_location"),
                    quality_flag=quality_flag,
                    unit_multiplier=1.0,
                    extraction_method=extraction_method,
                    raw_label=item.get("raw_label"),
                )
            )
        warnings = [] if facts else ["No eligible DataFrame fact candidates found."]
        return facts, warnings

    def _index_facts(self, facts: list[RatioFact]) -> dict[str, dict[str, RatioFact]]:
        indexed: dict[str, dict[str, RatioFact]] = {}
        for fact in facts:
            indexed.setdefault(fact.period, {})
            current = indexed[fact.period].get(fact.metric_code)
            if current is None or _fact_rank(fact) > _fact_rank(current):
                indexed[fact.period][fact.metric_code] = fact
        return indexed

    def _calculate_metric(
        self,
        code: str,
        period: str,
        by_period: dict[str, dict[str, RatioFact]],
        request: FinancialRatiosRequest,
        is_banking: bool = False,
    ) -> dict[str, Any]:
        if is_banking and code in BANKING_UNSUPPORTED_INDUSTRIAL_METRICS:
            return self._missing_metric(
                code,
                period,
                "unsupported_by_sector_policy",
                "industrial_metric_not_applicable_to_bank",
            )
        if is_banking and code in BANKING_METRIC_CODES and code not in {"roe", "roa"}:
            return self._banking_metric(code, period, by_period)
        facts = by_period.get(period, {})
        if code == "ebitda_margin" and "ebitda" not in facts:
            return self._missing_metric(
                code,
                period,
                "unsupported_by_policy",
                "explicit_ebitda_missing_no_proxy_allowed",
                inputs_missing=["ebitda"],
            )
        if code == "roe":
            return self._average_base_metric(code, period, by_period, "total_equity", "average_total_equity")
        if code == "roa":
            return self._average_base_metric(code, period, by_period, "total_assets", "average_total_assets")
        if code == "revenue_growth":
            return self._revenue_growth(period, by_period)
        if code in {"fcf", "fcf_margin"}:
            return self._fcf_metric(code, period, by_period)
        inputs = {
            "operating_margin": ["operating_profit", "revenue"],
            "net_margin": ["net_income", "revenue"],
            "current_ratio": ["current_assets", "current_liabilities"],
            "debt_to_equity": ["total_debt", "total_equity"],
            "ebitda_margin": ["ebitda", "revenue"],
        }[code]
        missing = [name for name in inputs if name not in facts]
        if missing:
            return self._missing_metric(code, period, "missing", "missing_inputs", inputs_missing=missing)
        if code == "debt_to_equity" and _is_derived_total_debt(facts["total_debt"]):
            return self._missing_metric(
                code,
                period,
                "missing",
                "explicit_total_debt_missing_no_component_derivation",
                inputs_missing=["total_debt"],
            )
        period_blocker = self._period_blocker(code, [facts[name] for name in inputs])
        if period_blocker:
            return self._missing_metric(code, period, "blocked_by_period_semantics", period_blocker)
        denominator = facts[inputs[1]].economic_value
        if denominator == 0:
            return self._missing_metric(code, period, "blocked_by_zero_denominator", "division_by_zero")
        value = facts[inputs[0]].economic_value / denominator
        return self._calculated_metric(code, period, value, [facts[name] for name in inputs])

    def _banking_metric(self, code: str, period: str, by_period: dict[str, dict[str, RatioFact]]) -> dict[str, Any]:
        facts = by_period.get(period, {})
        inputs_by_code = {
            "net_interest_margin": ["net_interest_income", "average_interest_earning_assets"],
            "cost_to_income": ["operating_expenses", "operating_income"],
            "loan_to_deposit": ["loans_to_customers", "customer_accounts"],
            "equity_to_assets": ["total_equity", "total_assets"],
            "net_margin_like": ["net_income", "operating_income"],
        }
        inputs = inputs_by_code[code]
        missing = [name for name in inputs if name not in facts]
        if missing:
            reason = "missing_average_interest_earning_assets" if code == "net_interest_margin" else "missing_inputs"
            return self._missing_metric(code, period, "missing", reason, inputs_missing=missing)
        period_blocker = self._period_blocker(code, [facts[name] for name in inputs])
        if period_blocker:
            return self._missing_metric(code, period, "blocked_by_period_semantics", period_blocker)
        denominator = facts[inputs[1]].economic_value
        if denominator == 0:
            return self._missing_metric(code, period, "blocked_by_zero_denominator", "division_by_zero")
        numerator = facts[inputs[0]].economic_value
        if code == "cost_to_income":
            numerator = abs(numerator)
        return self._calculated_metric(code, period, numerator / denominator, [facts[name] for name in inputs])

    def _fcf_metric(self, code: str, period: str, by_period: dict[str, dict[str, RatioFact]]) -> dict[str, Any]:
        facts = by_period.get(period, {})
        required = ["operating_cash_flow", "capex"]
        if code == "fcf_margin":
            required.append("revenue")
        missing = [name for name in required if name not in facts]
        if missing:
            return self._missing_metric(code, period, "missing", "missing_inputs", inputs_missing=missing)
        period_blocker = self._period_blocker(code, [facts[name] for name in required])
        if period_blocker:
            return self._missing_metric(code, period, "blocked_by_period_semantics", period_blocker)
        ocf = facts["operating_cash_flow"].economic_value
        capex = facts["capex"].economic_value
        fcf = ocf + capex if capex < 0 else ocf - capex
        if code == "fcf":
            return self._calculated_metric(code, period, fcf, [facts["operating_cash_flow"], facts["capex"]])
        revenue = facts["revenue"].economic_value
        if revenue == 0:
            return self._missing_metric(code, period, "blocked_by_zero_denominator", "division_by_zero")
        return self._calculated_metric(code, period, fcf / revenue, [facts[name] for name in required])

    def _average_base_metric(
        self,
        code: str,
        period: str,
        by_period: dict[str, dict[str, RatioFact]],
        base_code: str,
        average_name: str,
    ) -> dict[str, Any]:
        facts = by_period.get(period, {})
        preferred_prev_period = _previous_average_base_period(period, facts.get("net_income"))
        fallback_prev_period = previous_period(period)
        if preferred_prev_period and base_code in by_period.get(preferred_prev_period, {}):
            prev_period = preferred_prev_period
        else:
            prev_period = fallback_prev_period
        prev_facts = by_period.get(prev_period or "", {})
        if "net_income" not in facts:
            return self._missing_metric(code, period, "missing", "missing_inputs", inputs_missing=["net_income"])
        if base_code not in facts:
            return self._missing_metric(code, period, "missing", "missing_inputs", inputs_missing=[base_code])
        if base_code not in prev_facts:
            return self._missing_metric(code, period, "missing", "missing_average_base_snapshot", inputs_missing=[base_code])
        period_blocker = self._period_blocker(code, [facts["net_income"], facts[base_code], prev_facts[base_code]])
        if period_blocker:
            return self._missing_metric(code, period, "blocked_by_period_semantics", period_blocker)
        average_base = (facts[base_code].economic_value + prev_facts[base_code].economic_value) / 2
        if average_base == 0:
            return self._missing_metric(code, period, "blocked_by_zero_denominator", "division_by_zero")
        metric = self._calculated_metric(
            code,
            period,
            facts["net_income"].economic_value / average_base,
            [facts["net_income"], facts[base_code]],
        )
        metric["inputs"][average_name] = {
            "value": average_base,
            "current_source_fact_id": facts[base_code].source_fact_id,
            "previous_source_fact_id": prev_facts[base_code].source_fact_id,
            "previous_period": prev_period,
        }
        return metric

    def _revenue_growth(self, period: str, by_period: dict[str, dict[str, RatioFact]]) -> dict[str, Any]:
        facts = by_period.get(period, {})
        prev_period = previous_period(period)
        prev_facts = by_period.get(prev_period or "", {})
        if "revenue" not in facts:
            return self._missing_metric("revenue_growth", period, "missing", "missing_inputs", inputs_missing=["revenue"])
        if "revenue" not in prev_facts:
            return self._missing_metric(
                "revenue_growth",
                period,
                "missing",
                "previous_comparable_revenue_missing",
                inputs_missing=["previous_revenue"],
            )
        current = facts["revenue"]
        previous = prev_facts["revenue"]
        if current.period_type != previous.period_type:
            return self._missing_metric(
                "revenue_growth",
                period,
                "blocked_by_period_semantics",
                "incomparable_revenue_period_type",
            )
        if previous.economic_value == 0:
            return self._missing_metric("revenue_growth", period, "blocked_by_zero_denominator", "division_by_zero")
        metric = self._calculated_metric(
            "revenue_growth",
            period,
            current.economic_value / previous.economic_value - 1,
            [current],
        )
        metric["inputs"]["previous_revenue"] = self._input_payload(previous)
        if previous.uses_text_fallback and "trust_warning" not in metric:
            metric["trust_warning"] = "metric_uses_text_fallback_fact_candidates"
            metric["warnings"].append("metric_uses_text_fallback_fact_candidates")
        return metric

    def _period_blocker(self, code: str, facts: list[RatioFact]) -> str | None:
        if code in {"current_ratio", "debt_to_equity", "loan_to_deposit", "equity_to_assets"}:
            if any(fact.period_type != "balance_sheet_snapshot" for fact in facts):
                return "balance_sheet_snapshot_required"
            return None
        if code == "net_interest_margin":
            if facts[0].period_type not in {"annual", "ytd"}:
                return "income_period_type_not_supported"
            if facts[1].period_type != "balance_sheet_snapshot":
                return "balance_sheet_snapshot_required"
            return None
        if code in {"roe", "roa"}:
            if facts[0].period_type not in {"annual", "ytd"}:
                return "income_period_type_not_supported"
            if any(fact.period_type != "balance_sheet_snapshot" for fact in facts[1:]):
                return "balance_sheet_snapshot_required"
            return None
        period_types = {fact.period_type for fact in facts}
        if len(period_types) > 1:
            return "mixed_period_types"
        if period_types & {"standalone_quarter"} and period_types & {"ytd", "annual"}:
            return "mixed_ytd_and_standalone_quarter"
        return None

    def _calculated_metric(self, code: str, period: str, value: float, facts: list[RatioFact]) -> dict[str, Any]:
        formula = FORMULAS[code]
        methodology = METHODOLOGIES[code]
        payload = {
            "metric_code": code,
            "metric_name": formula.display_name,
            "period": period,
            "value": value,
            "display_value": display_value(value, formula.output_format),
            "unit": "ratio" if formula.output_format == "percent" else formula.output_format,
            "status": "calculated",
            "formula": FORMULA_TEXT_OVERRIDES.get(code, formula.formula),
            "inputs": {fact.metric_code: self._input_payload(fact) for fact in facts},
            "warnings": [],
            "methodology_notes": list(methodology.methodology_notes),
        }
        if any(fact.uses_text_fallback for fact in facts):
            payload["trust_warning"] = "metric_uses_text_fallback_fact_candidates"
            payload["warnings"].append("metric_uses_text_fallback_fact_candidates")
        return payload

    def _missing_metric(
        self,
        code: str,
        period: str,
        status: str,
        reason: str,
        inputs_missing: list[str] | None = None,
    ) -> dict[str, Any]:
        formula = FORMULAS[code]
        methodology = METHODOLOGIES[code]
        return {
            "metric_code": code,
            "metric_name": formula.display_name,
            "period": period,
            "value": None,
            "display_value": None,
            "unit": "ratio" if formula.output_format == "percent" else formula.output_format,
            "status": status,
            "reason": reason,
            "formula": FORMULA_TEXT_OVERRIDES.get(code, formula.formula),
            "inputs_missing": inputs_missing or [],
            "warnings": [],
            "methodology_notes": list(methodology.methodology_notes),
        }

    def _input_payload(self, fact: RatioFact) -> dict[str, Any]:
        return {
            "value": fact.economic_value,
            "source_fact_id": fact.source_fact_id,
            "source_location": fact.source_location,
            "quality_flag": fact.quality_flag,
            "period_type": fact.period_type,
            "raw_label": fact.raw_label,
        }

    def _is_banking_request(self, request: FinancialRatiosRequest) -> bool:
        if self.db is not None:
            company = self.db.scalar(select(Company).where(Company.ticker == request.company_ticker))
            if company is not None:
                return sector_is_banking(company.sector, company.subsector, company.ticker)
        return sector_is_banking(None, None, request.company_ticker)


def _fact_rank(fact: RatioFact) -> int:
    if fact.uses_text_fallback:
        return 1
    if fact.quality_flag in SAFE_QUALITY_FLAGS:
        return 3
    return 2


def _previous_average_base_period(period: str, income_fact: RatioFact | None) -> str | None:
    if income_fact and income_fact.period_type == "annual" and str(period).endswith("Q4"):
        year = int(str(period)[:4])
        return f"{year - 1}Q4"
    return previous_period(period)


def _previous_year_q4(period: str) -> str | None:
    raw = str(period or "").upper()
    if raw.endswith("Q4") and len(raw) >= 4 and raw[:4].isdigit():
        return f"{int(raw[:4]) - 1}Q4"
    return None


def _is_derived_total_debt(fact: RatioFact) -> bool:
    if fact.metric_code != "total_debt":
        return False
    if fact.quality_flag == "derived":
        return True
    location = fact.source_location or {}
    return bool(location.get("formula") or location.get("inputs") or location.get("derived_from"))
