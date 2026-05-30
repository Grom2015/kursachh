from app.core.config import get_settings
from app.db.models import Company, MetricValue, ReportDocument, StatementFact
from app.services.quality.auto_support_status import evaluate_auto_support_status


def metric_to_dict(metric: MetricValue) -> dict:
    return {
        "metric_code": metric.metric_code,
        "period": metric.period,
        "value": metric.value,
        "display_value": metric.display_value,
        "formula": metric.formula,
        "inputs": metric.inputs_json,
        "source_type": metric.inputs_json.get("source_type"),
        "quality_flag": metric.quality_flag,
        "warnings": metric.warnings_json,
    }


def fact_to_dict(fact: StatementFact) -> dict:
    return {
        "period": fact.period,
        "metric_code": fact.metric_code,
        "value": fact.value,
        "currency": fact.currency,
        "unit_multiplier": fact.unit_multiplier,
        "statement_type": fact.statement_type,
        "source_type": (fact.source_location or {}).get("source_type")
        or ("fixture" if fact.quality_flag == "fixture" else None),
        "source_location": fact.source_location,
        "quality_flag": fact.quality_flag,
    }


def document_to_dict(document: ReportDocument) -> dict:
    return {
        "id": document.id,
        "period": document.report_period,
        "reporting_standard": document.reporting_standard,
        "document_type": document.document_type,
        "source_role": document.source_role,
        "source_type": document.source_type,
        "source_url": document.source_url,
        "file_name": document.file_name,
        "file_hash": document.file_hash,
        "status": document.status,
    }


class ResultBuilder:
    def build(
        self,
        company: Company,
        period_from: str,
        period_to: str,
        reporting_standard: str,
        facts: list[StatementFact],
        metrics: list[MetricValue],
        documents: list[ReportDocument],
        market_analysis: dict,
        peer_analysis: dict,
        warnings: list[str],
        data_mode: str = "fixture",
    ) -> dict:
        metric_items = [metric_to_dict(metric) for metric in metrics]
        flags = sorted(
            {
                item.get("quality_flag")
                for item in metric_items
                if item.get("quality_flag") and item.get("quality_flag") != "exact"
            }
            | {fact.quality_flag for fact in facts if fact.quality_flag != "exact"}
            | {
                document.source_type
                for document in documents
                if document.source_type in {"fixture", "unavailable"}
            }
        )
        market_source = market_analysis.get("market_data_source") or market_analysis.get("candles_summary", {}).get(
            "source_type"
        )
        market_mode = market_analysis.get("market_data_mode")
        if market_source in {"fixture", "fixture_candles", "fixture_candles_unaligned"}:
            flags.extend(["fixture", "market_data_fixture_only"])
        if market_mode == "replay_cache" or market_source in {"market_cache", "market_cache_unaligned"}:
            flags.append("market_data_replay_cache")
        if market_analysis.get("market_period_aligned") is False:
            flags.append("market_period_unaligned")
        if market_mode != "live" and market_source != "MOEX ISS":
            flags.append("market_live_not_validated")
        if market_analysis.get("candles_summary", {}).get("rows", 0):
            flags.append("market_data_available")
        liquidity = market_analysis.get("liquidity_metrics", {}) or {}
        bid_ask = liquidity.get("bid_ask_spread") if isinstance(liquidity, dict) else None
        if bid_ask and bid_ask.get("status") == "missing":
            flags.append("spread_unavailable")
        valuation_inputs = market_analysis.get("valuation_inputs", {}) or {}
        if valuation_inputs and any(item.get("status") == "missing" for item in valuation_inputs.values()):
            flags.append("valuation_inputs_missing")
        if liquidity and bid_ask and bid_ask.get("status") == "missing":
            flags.append("liquidity_limited_to_volume_turnover")
        if any("fixture" in row.get("data_quality_flags", []) for row in peer_analysis.get("peer_table", [])):
            flags.append("fixture")
        flags = sorted(set(flags))
        real_data_used = any(document.source_type != "fixture" for document in documents)
        fixture_data_used = any(document.source_type == "fixture" for document in documents)
        source_role_summary = {
            role: sum(1 for document in documents if document.source_role == role)
            for role in ["press_release", "financial_statements", "financial_supplement", "annual_report", "other"]
        }
        low_confidence_fact_count = sum(1 for fact in facts if fact.quality_flag == "low_confidence_parse")
        conflicting_fact_count = sum(1 for fact in facts if fact.quality_flag == "conflicting_sources")
        missing_metric_count = sum(1 for metric in metrics if metric.quality_flag == "missing")
        if fixture_data_used and not real_data_used:
            quality = "fixture_only"
        elif not facts and not documents:
            quality = "unavailable"
        elif conflicting_fact_count:
            quality = "low"
        elif real_data_used and (low_confidence_fact_count or missing_metric_count):
            quality = "partial"
        elif real_data_used:
            quality = "high"
        else:
            quality = "partial"
        all_warnings = sorted(set(warnings + [w for metric in metrics for w in (metric.warnings_json or [])]))
        automated = None
        if data_mode == "real":
            source_package_status = "READY" if documents else "NOT_READY"
            automated = evaluate_auto_support_status(
                company=company.ticker,
                period_from=period_from,
                period_to=period_to,
                reporting_standard=reporting_standard,
                source_package_status=source_package_status,
                documents=documents,
                facts=facts,
                metrics=metrics,
                manual_verification=None,
                data_mode=data_mode,
            ).to_dict()
        return {
            "company": {"ticker": company.ticker, "name": company.full_name, "sector": company.sector},
            "period": {
                "from": period_from,
                "to": period_to,
                "reporting_standard": reporting_standard,
            },
            "financial_analysis": {
                "facts_summary": [fact_to_dict(fact) for fact in facts],
                "metrics": metric_items,
                "warnings": [w for metric in metrics for w in (metric.warnings_json or [])],
            },
            "market_analysis": market_analysis,
            "peer_analysis": peer_analysis,
            "data_quality": {
                "data_mode": data_mode,
                "overall_quality": quality,
                "real_data_used": real_data_used,
                "fixture_data_used": fixture_data_used,
                "low_confidence_fact_count": low_confidence_fact_count,
                "missing_metric_count": missing_metric_count,
                "conflicting_fact_count": conflicting_fact_count,
                "source_document_count": len(documents),
                "source_role_summary": source_role_summary,
                "flags": flags,
                "automated_support_status": (automated or {}).get("status"),
                "manual_action_required": False if data_mode == "real" else None,
                "automated_quality_score": (automated or {}).get("automated_quality_score"),
                "blockers": (automated or {}).get("blockers", []),
                "supported_output_scopes": (automated or {}).get("supported_output_scopes", []),
                "unsupported_output_scopes": (automated or {}).get("unsupported_output_scopes", []),
                "scope_statuses": (automated or {}).get("scope_statuses", {}),
            },
            "source_documents": [document_to_dict(document) for document in documents],
            "warnings": all_warnings,
            "disclaimer": get_settings().disclaimer,
        }
