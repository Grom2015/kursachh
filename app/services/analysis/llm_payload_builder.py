from app.core.config import get_settings
from app.db.models import Company


class LLMPayloadBuilder:
    def build(
        self,
        company: Company,
        period_from: str,
        period_to: str,
        reporting_standard: str,
        financial_metrics: list[dict],
        market_analysis: dict,
        peer_analysis: dict,
        source_documents: list[dict],
        warnings: list[str],
        language: str = "ru",
        data_mode: str = "fixture",
        data_quality: dict | None = None,
    ) -> dict:
        disclaimer = get_settings().disclaimer
        flags = sorted(
            {
                item.get("quality_flag")
                for item in financial_metrics
                if item.get("quality_flag") and item.get("quality_flag") != "exact"
            }
        )
        if any(document.get("source_type") == "fixture" for document in source_documents):
            flags = sorted(set(flags + ["fixture"]))
            warnings = sorted(set(warnings + ["Fixture/demo data is used; numbers are not real filings data"]))
        if market_analysis.get("market_period_aligned") is False:
            flags = sorted(set(flags + ["market_period_unaligned"]))
            warnings = sorted(
                set(
                    warnings
                    + [
                        "Market indicators are based on fixture data outside the requested analysis period and must "
                        "not be interpreted as 2021 LKOH market analysis."
                    ]
                )
            )
        automated_status = (data_quality or {}).get("automated_support_status")
        if automated_status and automated_status != "AUTO_READY":
            warnings = sorted(
                set(
                    warnings
                    + [
                        f"Automated support status is {automated_status}; state the data limitations explicitly."
                    ]
                )
            )
        scope_statuses = (data_quality or {}).get("scope_statuses") or {}
        limited_scopes = [
            f"{scope}: {payload.get('status')}"
            for scope, payload in scope_statuses.items()
            if payload.get("status") not in {"AUTO_READY"}
        ]
        if limited_scopes:
            warnings = sorted(set(warnings + [f"Output scope limitations: {', '.join(limited_scopes)}."]))
        return {
            "task": "prepare_financial_analytics_note",
            "language": language,
            "compliance": {
                "not_individual_investment_recommendation": True,
                "disclaimer": disclaimer,
            },
            "company": {
                "id": company.id,
                "ticker": company.ticker,
                "short_name": company.short_name,
                "full_name": company.full_name,
                "sector": company.sector,
                "board": company.board,
            },
            "period": {
                "from": period_from,
                "to": period_to,
                "reporting_standard": reporting_standard,
            },
            "data_mode": data_mode,
            "financial_metrics": financial_metrics,
            "technical_analysis": market_analysis,
            "peer_analysis": peer_analysis,
            "source_documents": source_documents,
            "data_quality_flags": flags,
            "data_quality": data_quality or {},
            "scope_statuses": scope_statuses,
            "warnings": warnings,
            "instructions_for_llm": [
                "Use only numbers from this JSON.",
                "Do not invent missing values.",
                "Use only calculated values from JSON; never infer unavailable metrics.",
                "If automated_support_status is not AUTO_READY, explicitly state limitations.",
                "Clearly distinguish facts, calculations, and analytical interpretation.",
                "Do not provide personalized investment advice.",
                "Use cautious wording: analytical conclusion, factors, risks, technical signals.",
            ],
        }
