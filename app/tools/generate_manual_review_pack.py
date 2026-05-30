import argparse
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings


def report_path(ticker: str, period_from: str, period_to: str) -> Path:
    return (
        get_settings().root_dir
        / "data"
        / "validation"
        / ticker.upper()
        / f"{period_from}_{period_to}_real_validation_report.json"
    )


def pack_path(ticker: str) -> Path:
    return (
        get_settings().root_dir
        / "data"
        / "validation"
        / ticker.upper()
        / "golden"
        / f"{ticker.casefold()}_2021_manual_review_pack.json"
    )


def golden_path(ticker: str) -> Path:
    return (
        get_settings().root_dir
        / "data"
        / "validation"
        / ticker.upper()
        / "golden"
        / f"{ticker.casefold()}_2021_manual_fact_checks.yml"
    )


def generate_pack(ticker: str, period_from: str, period_to: str, limit: int) -> dict[str, Any]:
    path = report_path(ticker, period_from, period_to)
    if not path.exists():
        raise FileNotFoundError(f"Validation report not found: {path}")
    report = json.loads(path.read_text(encoding="utf-8"))
    items = []
    period_summary: dict[str, dict[str, Any]] = {}
    for row in report.get("fact_coverage", []):
        period = row.get("period")
        if period:
            summary = period_summary.setdefault(
                period,
                {
                    "extracted_fact_count": 0,
                    "high_confidence_fact_count": 0,
                    "selected_fact_count": 0,
                    "warnings": [],
                },
            )
            if row.get("status") != "missing":
                summary["extracted_fact_count"] += 1
            if row.get("status") != "missing" and (row.get("confidence_score") is None or row.get("confidence_score") >= 0.7):
                summary["high_confidence_fact_count"] += 1
        if row.get("status") == "missing":
            continue
        if not row.get("source_url") or not row.get("source_location"):
            continue
        raw_label = row.get("raw_label") or row.get("metric_name_original")
        if not raw_label:
            raw_label = "Derived fact" if row.get("quality_flag") == "derived" else row.get("metric_code")
        items.append(
            {
                "period": period,
                "metric_code": row.get("metric_code"),
                "value": row.get("value"),
                "currency": row.get("currency"),
                "unit_multiplier": row.get("unit_multiplier"),
                "period_type": row.get("period_type") or row.get("quality_flag"),
                "quality_flag": row.get("quality_flag"),
                "conflict_warning": "conflicting fact; manual review required before use"
                if row.get("quality_flag") == "conflicting_sources"
                else None,
                "source_role": "financial_statements",
                "source_url": row.get("source_url"),
                "document_id": row.get("source_document_id"),
                "source_location": row.get("source_location"),
                "raw_label": raw_label,
                "ifrs_concept_code": row.get("ifrs_concept_code"),
                "statement_context": row.get("statement_context"),
                "period_coverage": row.get("period_coverage"),
                "candidate_score_breakdown": row.get("candidate_score_breakdown"),
                "applied_issuer_override": row.get("applied_issuer_override"),
                "source_references": row.get("source_references") or [],
                "confidence_score": row.get("confidence_score"),
                "manual_status": "pending",
            }
        )
        if period:
            period_summary[period]["selected_fact_count"] += 1
        if len(items) >= limit:
            break
    for period, summary in period_summary.items():
        if summary["high_confidence_fact_count"] == 0:
            summary["warnings"].append(f"no high-confidence {period} facts")
        elif summary["selected_fact_count"] == 0:
            summary["warnings"].append(f"{period} facts excluded by selection logic")
    payload = {
        "company": ticker.upper(),
        "period_from": period_from,
        "period_to": period_to,
        "facts_selected": len(items),
        "periods_covered": sorted({item["period"] for item in items}),
        "metric_codes_covered": sorted({item["metric_code"] for item in items}),
        "period_selection_summary": period_summary,
        "warnings": [
            warning
            for period in sorted(period_summary)
            for warning in period_summary[period].get("warnings", [])
        ],
        "items": items,
    }
    out = pack_path(ticker)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ensure_golden_seed(ticker, period_from, period_to)
    return payload


def ensure_golden_seed(ticker: str, period_from: str, period_to: str) -> None:
    path = golden_path(ticker)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"company: {ticker.upper()}",
                f"period_from: {period_from}",
                f"period_to: {period_to}",
                "reporting_standard: IFRS",
                "checks: []",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate manual fact review pack for a ticker.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    report = generate_pack(args.ticker, args.period_from, args.period_to, args.limit)
    print(
        "\n".join(
            [
                f"facts_selected: {report['facts_selected']}",
                f"periods_covered: {', '.join(report['periods_covered'])}",
                f"metric_codes_covered: {', '.join(report['metric_codes_covered'])}",
                f"warnings: {'; '.join(report.get('warnings') or []) or 'none'}",
                f"output_path: {pack_path(args.ticker)}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
