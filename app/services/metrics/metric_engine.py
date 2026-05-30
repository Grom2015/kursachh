from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MetricValue, StatementFact
from app.services.metrics.financial_ratios import display_value, safe_divide
from app.services.metrics.formula_registry import FORMULAS
from app.services.metrics.metric_methodology_registry import METHODOLOGIES
from app.services.periods import period_key, previous_period


class MetricEngine:
    def __init__(self, db: Session):
        self.db = db

    def calculate(
        self,
        company_id: int,
        periods: list[str],
        report_document_ids: list[int] | None = None,
        exclude_fixture: bool = False,
    ) -> list[MetricValue]:
        stmt = select(StatementFact).where(
            StatementFact.company_id == company_id,
            StatementFact.period.in_(periods),
        )
        if report_document_ids is not None:
            stmt = stmt.where(StatementFact.report_document_id.in_(report_document_ids))
        facts = self.db.scalars(stmt).all()
        if exclude_fixture:
            facts = [fact for fact in facts if fact.quality_flag != "fixture"]
        by_period: dict[str, dict[str, float]] = defaultdict(dict)
        quality_by_period: dict[str, dict[str, str]] = defaultdict(dict)
        period_type_by_period: dict[str, dict[str, str]] = defaultdict(dict)
        fact_by_period: dict[str, dict[str, StatementFact]] = defaultdict(dict)
        for fact in facts:
            if fact.value is not None:
                by_period[fact.period][fact.metric_code] = fact.value * (fact.unit_multiplier or 1.0)
                quality_by_period[fact.period][fact.metric_code] = fact.quality_flag
                period_type_by_period[fact.period][fact.metric_code] = fact.period_type
                fact_by_period[fact.period][fact.metric_code] = fact
        values: list[MetricValue] = []
        for period in periods:
            values.extend(
                self.calculate_for_period(
                    company_id,
                    period,
                    by_period,
                    quality_by_period,
                    period_type_by_period,
                    fact_by_period,
                )
            )
        self.db.add_all(values)
        self.db.flush()
        return values

    def calculate_for_period(
        self,
        company_id: int,
        period: str,
        by_period: dict[str, dict[str, float]],
        quality_by_period: dict[str, dict[str, str]] | None = None,
        period_type_by_period: dict[str, dict[str, str]] | None = None,
        fact_by_period: dict[str, dict[str, StatementFact]] | None = None,
    ) -> list[MetricValue]:
        quality_by_period = quality_by_period or {}
        period_type_by_period = period_type_by_period or {}
        fact_by_period = fact_by_period or {}
        data = by_period.get(period, {})
        prev_data = by_period.get(previous_period(period) or "", {})
        results: list[MetricValue] = []
        for code, formula in FORMULAS.items():
            value: float | None = None
            quality = "exact"
            warnings: list[str] = []
            inputs: dict[str, object] = {name: data.get(name) for name in formula.required_inputs}
            source_type = self._source_type(period, formula.required_inputs, quality_by_period)
            if source_type:
                inputs["source_type"] = source_type

            if code == "revenue_growth":
                inputs["previous_revenue"] = prev_data.get("revenue")
                if prev_data.get("revenue") is not None and source_type == "fixture":
                    inputs["previous_revenue_source_type"] = self._source_type(
                        previous_period(period) or "", ["revenue"], quality_by_period
                    )
                value, warning = safe_divide(
                    None
                    if data.get("revenue") is None or prev_data.get("revenue") is None
                    else data["revenue"] - prev_data["revenue"],
                    prev_data.get("revenue"),
                )
            elif code in {"ebitda_margin", "operating_margin", "net_margin"}:
                num_key = {
                    "ebitda_margin": "ebitda",
                    "operating_margin": "operating_profit",
                    "net_margin": "net_income",
                }[code]
                value, warning = safe_divide(data.get(num_key), data.get("revenue"))
            elif code == "roe":
                net_income_ttm, ttm_warning = self._ttm(period, by_period, "net_income")
                inputs["net_income_ttm"] = net_income_ttm
                if ttm_warning:
                    warnings.append(ttm_warning)
                avg_equity = self._average_or_current(period, by_period, "total_equity")
                inputs["average_equity"] = avg_equity
                value, warning = safe_divide(net_income_ttm, avg_equity)
                if avg_equity == data.get("total_equity") and prev_data.get("total_equity") is None:
                    quality = "derived"
                    warnings.append("Average equity unavailable; current equity used")
            elif code == "roa":
                net_income_ttm, ttm_warning = self._ttm(period, by_period, "net_income")
                inputs["net_income_ttm"] = net_income_ttm
                if ttm_warning:
                    warnings.append(ttm_warning)
                avg_assets = self._average_or_current(period, by_period, "total_assets")
                inputs["average_assets"] = avg_assets
                value, warning = safe_divide(net_income_ttm, avg_assets)
                if avg_assets == data.get("total_assets") and prev_data.get("total_assets") is None:
                    quality = "derived"
                    warnings.append("Average assets unavailable; current assets used")
            elif code == "debt_to_equity":
                value, warning = safe_divide(data.get("total_debt"), data.get("total_equity"))
            elif code == "net_debt_to_ebitda":
                net_debt = None
                if data.get("total_debt") is not None and data.get("cash_and_equivalents") is not None:
                    net_debt = data["total_debt"] - data["cash_and_equivalents"]
                inputs["net_debt"] = net_debt
                ebitda_ttm, ttm_warning = self._ttm(period, by_period, "ebitda")
                inputs["ebitda_ttm"] = ebitda_ttm
                if ttm_warning:
                    warnings.append(ttm_warning)
                value, warning = safe_divide(net_debt, ebitda_ttm)
            elif code == "current_ratio":
                value, warning = safe_divide(data.get("current_assets"), data.get("current_liabilities"))
            elif code == "fcf":
                absent = [name for name in ["operating_cash_flow", "capex"] if data.get(name) is None]
                if absent:
                    warnings.append(f"Missing inputs for {code}: {', '.join(absent)}")
                    warning = "missing input"
                else:
                    value = data["operating_cash_flow"] - data["capex"]
                    warning = None
            elif code == "fcf_margin":
                fcf = None
                if data.get("operating_cash_flow") is not None and data.get("capex") is not None:
                    fcf = data["operating_cash_flow"] - data["capex"]
                inputs["fcf"] = fcf
                value, warning = safe_divide(fcf, data.get("revenue"))
            elif code in {"pe_ratio", "ev_to_ebitda", "dividend_yield"}:
                warning = "market valuation input unavailable"
            else:
                warning = "formula unavailable"
            if warning:
                if warning == "division by zero":
                    warning = "denominator_zero"
                warnings.append(warning)
                quality = "missing" if value is None else quality
            methodology_warning = self._methodology_warning(code, period, period_type_by_period)
            if methodology_warning:
                warnings.append(methodology_warning)
                if code in {"current_ratio", "debt_to_equity"}:
                    value = None
                    quality = "missing"
                elif value is not None and quality == "exact":
                    quality = "derived"
            if value is not None and source_type == "fixture":
                quality = "fixture"
                warnings.append("Metric calculated from fixture/demo data")
            if value is not None:
                denominator_warning = self._denominator_warning(code, inputs)
                if denominator_warning:
                    warnings.append(denominator_warning)
            results.append(
                MetricValue(
                    company_id=company_id,
                    period=period,
                    metric_code=code,
                    metric_name=formula.display_name,
                    value=value,
                    display_value=display_value(value, formula.output_format),
                    formula=formula.formula,
                    inputs_json=self._metric_inputs_json(
                        code,
                        period,
                        inputs,
                        formula.required_inputs,
                        fact_by_period,
                    ),
                    quality_flag=quality,
                    warnings_json=warnings,
                )
            )
        return results

    def _metric_inputs_json(
        self,
        code: str,
        period: str,
        inputs: dict[str, object],
        required_inputs: list[str],
        fact_by_period: dict[str, dict[str, StatementFact]],
    ) -> dict[str, object]:
        facts = {
            name: fact_by_period.get(period, {}).get(name)
            for name in required_inputs
            if fact_by_period.get(period, {}).get(name) is not None
        }
        return {
            **inputs,
            "required_facts": list(METHODOLOGIES[code].required_facts),
            "source_references": {
                name: self._fact_source_reference(fact)
                for name, fact in facts.items()
            },
            "input_quality_flags": {name: fact.quality_flag for name, fact in facts.items()},
            "input_period_types": {name: fact.period_type for name, fact in facts.items()},
            "input_manual_verification_status": {name: "not_checked_in_metric_engine" for name in facts},
            "metric_data_trust": "unverified",
        }

    def _fact_source_reference(self, fact: StatementFact) -> dict[str, object]:
        location = fact.source_location or {}
        return {
            "source_document_id": fact.report_document_id,
            "source_url": location.get("source_url"),
            "source_location": self._format_source_location(location),
            "raw_label": location.get("raw_label"),
            "statement_context": location.get("statement_context") or location.get("table_title"),
            "source_role": location.get("source_role"),
            "quality_flag": fact.quality_flag,
            "period_type": fact.period_type,
        }

    def _format_source_location(self, location: dict[str, object]) -> str | None:
        if isinstance(location.get("source_location"), str):
            return location.get("source_location")
        parts = []
        if location.get("page") is not None:
            parts.append(f"page {location['page']}")
        if location.get("table"):
            parts.append(f"table {location['table']}")
        if location.get("line") is not None:
            parts.append(f"line {location['line']}")
        return ", ".join(parts) if parts else None

    def _average_or_current(
        self, period: str, by_period: dict[str, dict[str, float]], metric_code: str
    ) -> float | None:
        current = by_period.get(period, {}).get(metric_code)
        prev = by_period.get(previous_period(period) or "", {}).get(metric_code)
        if current is None:
            return None
        if prev is None:
            return current
        return (current + prev) / 2

    def _ttm(
        self, period: str, by_period: dict[str, dict[str, float]], metric_code: str
    ) -> tuple[float | None, str | None]:
        ordered = sorted((item for item in by_period if period_key(item) <= period_key(period)), key=period_key)
        last_four = ordered[-4:]
        values = [by_period[item].get(metric_code) for item in last_four]
        if len(last_four) < 4 or any(value is None for value in values):
            return None, f"TTM unavailable for {metric_code}: fewer than 4 complete quarters"
        return sum(value for value in values if value is not None), None

    def _source_type(
        self, period: str, inputs: list[str], quality_by_period: dict[str, dict[str, str]]
    ) -> str | None:
        qualities = [quality_by_period.get(period, {}).get(item) for item in inputs]
        if any(item == "fixture" for item in qualities):
            return "fixture"
        return None

    def _denominator_warning(self, code: str, inputs: dict[str, object]) -> str | None:
        denominators = {
            "ebitda_margin": "revenue",
            "operating_margin": "revenue",
            "net_margin": "revenue",
            "roe": "average_equity",
            "roa": "average_assets",
            "debt_to_equity": "total_equity",
            "net_debt_to_ebitda": "ebitda_ttm",
            "current_ratio": "current_liabilities",
            "fcf_margin": "revenue",
        }
        key = denominators.get(code)
        denominator = inputs.get(key) if key else None
        if isinstance(denominator, int | float) and denominator < 0:
            return f"Negative denominator used for {code}: {key}"
        return None

    def _methodology_warning(
        self, code: str, period: str, period_type_by_period: dict[str, dict[str, str]]
    ) -> str | None:
        period_types = period_type_by_period.get(period, {})
        if code in {"ebitda_margin", "operating_margin", "net_margin"}:
            inputs = {
                "ebitda_margin": ["ebitda", "revenue"],
                "operating_margin": ["operating_profit", "revenue"],
                "net_margin": ["net_income", "revenue"],
            }[code]
            types = {period_types.get(item) for item in inputs if period_types.get(item)}
            if len(types) > 1:
                return f"Mixed period coverage for {code}: {sorted(types)}"
        if code == "fcf":
            types = {period_types.get(item) for item in ["operating_cash_flow", "capex"] if period_types.get(item)}
            if len(types) > 1:
                return f"Mixed period coverage for {code}: {sorted(types)}"
        if code == "fcf_margin":
            types = {
                period_types.get(item)
                for item in ["operating_cash_flow", "capex", "revenue"]
                if period_types.get(item)
            }
            if len(types) > 1:
                return f"Mixed period coverage for {code}: {sorted(types)}"
        if code == "current_ratio":
            types = {period_types.get(item) for item in ["current_assets", "current_liabilities"] if period_types.get(item)}
            if types and types not in [{"balance_sheet_snapshot"}, {"quarter"}]:
                return "Current ratio requires balance_sheet_snapshot inputs from the same report period"
        if code == "debt_to_equity":
            types = {period_types.get(item) for item in ["total_debt", "total_equity"] if period_types.get(item)}
            if types and types not in [{"balance_sheet_snapshot"}, {"quarter"}]:
                return "Debt to equity requires balance_sheet_snapshot inputs from the same report period"
        if code in {"roe", "roa"}:
            income_type = period_types.get("net_income")
            if income_type in {"ytd", "annual"}:
                return f"{code.upper()} uses {income_type} income with balance sheet snapshots; review annualization methodology"
        return None
