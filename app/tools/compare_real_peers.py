import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select

from app.core.config import get_settings
from app.db.models import AnalysisResult, Company
from app.db.session import SessionLocal
from app.services.periods import periods_between

VALUATION_METRICS = {"pe_ratio", "ev_to_ebitda", "dividend_yield"}
EBITDA_METRICS = {"ebitda_margin", "net_debt_to_ebitda", "ev_to_ebitda"}
ACCEPTABLE_SOURCE_ROLES = {"financial_statements", "financial_supplement", "annual_report", "manual_verified"}


def latest_result(db, ticker: str, period_from: str, period_to: str) -> AnalysisResult | None:
    company = db.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if not company:
        return None
    return db.scalar(
        select(AnalysisResult)
        .where(
            AnalysisResult.company_id == company.id,
            AnalysisResult.period_from == period_from,
            AnalysisResult.period_to == period_to,
        )
        .order_by(desc(AnalysisResult.created_at))
    )


def metric_map(result: AnalysisResult | None) -> dict[tuple[str, str], dict[str, Any]]:
    if not result:
        return {}
    return {
        (metric["period"], metric["metric_code"]): metric
        for metric in result.result_json.get("financial_analysis", {}).get("metrics", [])
    }


def audit_metric_map(ticker: str, period_from: str, period_to: str) -> dict[tuple[str, str], dict[str, Any]]:
    report = load_json(validation_path(ticker) / f"{period_from}_{period_to}_real_metrics_audit.json")
    return {(item["period"], item["metric_code"]): item for item in report.get("metrics", [])}


def validation_path(ticker: str) -> Path:
    return get_settings().root_dir / "data" / "validation" / ticker.upper()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def compare(target: str, peer: str, period_from: str, period_to: str) -> dict[str, Any]:
    target = target.upper()
    peer = peer.upper()
    with SessionLocal() as db:
        target_result = latest_result(db, target, period_from, period_to)
        peer_result = latest_result(db, peer, period_from, period_to)
    target_metrics = audit_metric_map(target, period_from, period_to) or metric_map(target_result)
    peer_metrics = audit_metric_map(peer, period_from, period_to) or metric_map(peer_result)
    keys = sorted(set(target_metrics) | set(peer_metrics))
    comparable_metrics = []
    excluded_metrics = []
    missing_by_company = {target: [], peer: []}
    for key in keys:
        target_metric = target_metrics.get(key)
        peer_metric = peer_metrics.get(key)
        decision = compatibility_decision(target_metric, peer_metric, key[1])
        if decision["comparison_status"] == "comparable":
            comparable_metrics.append(comparable_row(target, peer, key, target_metric or {}, peer_metric or {}, decision))
            continue
        excluded_metrics.append(excluded_row(key, decision))
        if not metric_is_valid(target_metric):
            missing_by_company[target].append({"period": key[0], "metric_code": key[1], "reason": side_reason(target_metric)})
        if not metric_is_valid(peer_metric):
            missing_by_company[peer].append({"period": key[0], "metric_code": key[1], "reason": side_reason(peer_metric)})
    data_quality = {
        target: company_quality_summary(target, target_result, period_from, period_to),
        peer: company_quality_summary(peer, peer_result, period_from, period_to),
    }
    full_year_available = is_full_year_available(data_quality, peer)
    readiness = analytical_readiness(len(comparable_metrics), full_year_available)
    status = "PARTIAL" if comparable_metrics else "NOT_READY"
    report = {
        "target_ticker": target,
        "peer_ticker": peer,
        "status": status,
        "comparison_scope": {
            "target": target,
            "peer": peer,
            "period_from": period_from,
            "period_to": period_to,
            "is_full_year_comparison": full_year_available,
            "reason_not_full_year": None if full_year_available else f"{peer} Q4/FY source missing",
        },
        "data_quality_by_company": data_quality,
        "comparable_metrics_count": len(comparable_metrics),
        "excluded_metrics_count": len(excluded_metrics),
        "valuation_metrics_available": valuation_metrics_available(comparable_metrics),
        "ebitda_metrics_available": ebitda_metrics_available(comparable_metrics),
        "full_year_comparison_available": full_year_available,
        "analytical_readiness": readiness,
        "comparable_metrics": comparable_metrics,
        "excluded_metrics": excluded_metrics,
        "metrics_table": comparable_metrics,
        "missing_by_company": missing_by_company,
        "warnings": comparison_warnings(peer, data_quality, comparable_metrics, excluded_metrics),
    }
    filename = f"{target}_{peer}_2021_peer_comparison_report.json"
    path = get_settings().root_dir / "data" / "validation" / "PEERS" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def compatibility_decision(
    target_metric: dict[str, Any] | None, peer_metric: dict[str, Any] | None, metric_code: str
) -> dict[str, Any]:
    if metric_code in VALUATION_METRICS:
        return {"comparison_status": "excluded", "reason": "valuation_inputs_missing"}
    if metric_code in EBITDA_METRICS:
        return {"comparison_status": "excluded", "reason": "ebitda_missing"}
    for side, metric in [("target", target_metric), ("peer", peer_metric)]:
        if not metric:
            return {"comparison_status": "excluded", "reason": f"missing_for_{side}"}
        if metric.get("status") == "questionable":
            return {"comparison_status": "excluded", "reason": f"questionable_for_{side}"}
        if metric.get("status") != "valid" or metric.get("quality_flag") in {"missing", "fixture"}:
            return {"comparison_status": "excluded", "reason": f"missing_for_{side}"}
        if has_bad_inputs(metric):
            return {"comparison_status": "excluded", "reason": f"low_quality_inputs_for_{side}"}
        if not has_traceability(metric):
            return {"comparison_status": "excluded", "reason": f"traceability_missing_for_{side}"}
        if not source_roles_acceptable(metric):
            return {"comparison_status": "excluded", "reason": f"unacceptable_source_role_for_{side}"}
    target_signature = period_signature(target_metric or {})
    peer_signature = period_signature(peer_metric or {})
    if target_signature != peer_signature:
        return {
            "comparison_status": "excluded",
            "reason": "incompatible_period_coverage",
            "target_period_signature": target_signature,
            "peer_period_signature": peer_signature,
        }
    return {
        "comparison_status": "comparable",
        "reason": None,
        "period_type": target_signature.get("period_type"),
        "coverage": target_signature.get("coverage"),
    }


def metric_is_valid(metric: dict[str, Any] | None) -> bool:
    return bool(metric and metric.get("status") == "valid" and metric.get("quality_flag") not in {"missing", "fixture"})


def side_reason(metric: dict[str, Any] | None) -> str:
    if not metric:
        return "missing"
    if metric.get("status") == "questionable":
        return "questionable"
    if metric.get("metric_code") in VALUATION_METRICS:
        return "valuation_inputs_missing"
    return metric.get("missing_reason") or metric.get("status") or "missing"


def has_bad_inputs(metric: dict[str, Any]) -> bool:
    flags = set((metric.get("input_quality_flags") or {}).values())
    return bool(flags & {"low_confidence_parse", "conflicting_sources", "fixture"})


def has_traceability(metric: dict[str, Any]) -> bool:
    locations = metric.get("input_source_locations") or metric.get("source_locations") or []
    return bool(locations and all(location.get("source_url") and location.get("raw_label") for location in locations))


def source_roles_acceptable(metric: dict[str, Any]) -> bool:
    roles = set((metric.get("input_source_roles") or {}).values())
    if not roles:
        roles = {location.get("source_role") for location in metric.get("input_source_locations") or []}
    return bool(roles and roles <= ACCEPTABLE_SOURCE_ROLES)


def period_signature(metric: dict[str, Any]) -> dict[str, Any]:
    period_types = set((metric.get("input_period_types") or {}).values())
    locations = metric.get("input_source_locations") or []
    ytd_months = {location.get("ytd_months") for location in locations if location.get("ytd_months")}
    if period_types == {"ytd"} and len(ytd_months) == 1:
        return {"period_type": "ytd", "coverage": f"{next(iter(ytd_months))}M"}
    if len(period_types) == 1:
        period_type = next(iter(period_types))
        return {"period_type": period_type, "coverage": "12M" if period_type == "annual" else period_type}
    return {"period_type": "mixed", "coverage": "mixed"}


def comparable_row(
    target: str,
    peer: str,
    key: tuple[str, str],
    target_metric: dict[str, Any],
    peer_metric: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    return {
        "metric_code": key[1],
        "period": key[0],
        "target_value": target_metric.get("value"),
        "peer_value": peer_metric.get("value"),
        "period_type": decision.get("period_type"),
        "coverage": decision.get("coverage"),
        "comparison_status": "comparable",
        "target_metric": target_metric,
        "peer_metric": peer_metric,
        "warnings": [f"{target} vs {peer} comparison is limited to compatible real-data metrics."],
    }


def excluded_row(key: tuple[str, str], decision: dict[str, Any]) -> dict[str, Any]:
    row = {"metric_code": key[1], "period": key[0], "reason": decision["reason"]}
    for optional in ["target_period_signature", "peer_period_signature"]:
        if optional in decision:
            row[optional] = decision[optional]
    return row


def company_quality_summary(
    ticker: str, result: AnalysisResult | None, period_from: str, period_to: str
) -> dict[str, Any]:
    source_package = load_json(validation_path(ticker) / f"{period_from}_{period_to}_source_package_report.json")
    validation = load_json(validation_path(ticker) / f"{period_from}_{period_to}_real_validation_report.json")
    golden = load_json(validation_path(ticker) / "golden" / f"{ticker.casefold()}_2021_golden_verification_report.json")
    metrics = load_json(validation_path(ticker) / f"{period_from}_{period_to}_real_metrics_audit.json")
    document_periods = {document.get("period") for document in validation.get("documents", [])}
    missing_periods = [period for period in periods_between(period_from, period_to) if period not in document_periods]
    snapshot = result.data_snapshot_json if result else {}
    return {
        "source_package_status": source_package.get("status", "UNKNOWN"),
        "validation_status": validation.get("status", "UNKNOWN"),
        "golden_status": golden.get("status", "UNKNOWN"),
        "metrics_valid": metrics.get("summary", {}).get("metrics_valid_count", 0),
        "metrics_questionable": metrics.get("summary", {}).get("metrics_questionable_count", 0),
        "metrics_missing": metrics.get("summary", {}).get("metrics_missing_count", 0),
        "missing_periods": missing_periods,
        "data_mode": (snapshot or {}).get("data_mode"),
        "validation_mode": (snapshot or {}).get("validation_mode"),
        "fixture_data_used": bool((snapshot or {}).get("fixture_data_used")),
        "result_found": result is not None,
    }


def is_full_year_available(data_quality: dict[str, dict[str, Any]], peer: str) -> bool:
    peer_quality = data_quality.get(peer.upper(), {})
    return "2021Q4" not in set(peer_quality.get("missing_periods") or [])


def valuation_metrics_available(comparable_metrics: list[dict[str, Any]]) -> bool:
    return any(row["metric_code"] in VALUATION_METRICS for row in comparable_metrics)


def ebitda_metrics_available(comparable_metrics: list[dict[str, Any]]) -> bool:
    return any(row["metric_code"] in EBITDA_METRICS for row in comparable_metrics)


def analytical_readiness(comparable_count: int, full_year_available: bool) -> str:
    if comparable_count < 5:
        return "not_ready"
    if not full_year_available:
        return "limited"
    return "usable_with_warnings"


def comparison_warnings(
    peer: str,
    data_quality: dict[str, dict[str, Any]],
    comparable_metrics: list[dict[str, Any]],
    excluded_metrics: list[dict[str, Any]],
) -> list[str]:
    warnings = {
        "Peer comparison is partial and not a full-year analytical comparison.",
        "Valuation metrics are unavailable.",
    }
    peer_quality = data_quality.get(peer.upper(), {})
    if not peer_quality.get("result_found"):
        warnings.add(f"{peer.upper()} real validation result not found.")
    if "2021Q4" in set(peer_quality.get("missing_periods") or []):
        warnings.add(f"{peer.upper()} Q4/FY source is missing.")
    if peer_quality.get("metrics_valid", 0) < 10:
        warnings.add(f"{peer.upper()} metrics coverage is narrow.")
    if any(row["reason"] == "ebitda_missing" for row in excluded_metrics):
        warnings.add("EBITDA-based metrics are unavailable.")
    if not comparable_metrics:
        warnings.add("No compatible real-data metrics survived peer comparison guardrails.")
    return sorted(warnings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare real metrics for two peers.")
    parser.add_argument("target")
    parser.add_argument("peer")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    args = parser.parse_args(argv)
    report = compare(args.target, args.peer, args.period_from, args.period_to)
    print(
        "\n".join(
            [
                f"status: {report['status']}",
                f"target_ticker: {report['target_ticker']}",
                f"peer_ticker: {report['peer_ticker']}",
                f"comparable_metrics_count: {report['comparable_metrics_count']}",
                f"excluded_metrics_count: {report['excluded_metrics_count']}",
                f"analytical_readiness: {report['analytical_readiness']}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
