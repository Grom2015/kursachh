import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from app.tools.generate_manual_review_pack import golden_path, pack_path


def checklist_path(ticker: str) -> Path:
    return golden_path(ticker).with_name(f"{ticker.casefold()}_2021_manual_review_checklist.md")


def sync(ticker: str, period_from: str, period_to: str) -> dict[str, Any]:
    ticker = ticker.upper()
    pack_file = pack_path(ticker)
    golden_file = golden_path(ticker)
    if not pack_file.exists():
        raise FileNotFoundError(f"Manual review pack not found: {pack_file}")
    pack = json.loads(pack_file.read_text(encoding="utf-8"))
    golden = load_golden(golden_file, ticker, period_from, period_to)
    golden["company"] = ticker
    golden["period_from"] = period_from
    golden["period_to"] = period_to
    golden["reporting_standard"] = "IFRS"
    checks = golden.setdefault("checks", [])
    existing_count = len(checks)
    existing_keys = {unique_key(check) for check in checks}
    added = 0
    skipped = 0
    for item in pack.get("items", []):
        check = review_item_to_check(item)
        key = unique_key(check)
        if key in existing_keys:
            merge_actual_fields(next(row for row in checks if unique_key(row) == key), check)
            skipped += 1
            continue
        legacy = find_legacy_metric_code_raw_label_check(checks, check)
        if legacy:
            old_key = unique_key(legacy)
            merge_actual_fields(legacy, check)
            existing_keys.discard(old_key)
            existing_keys.add(unique_key(legacy))
            skipped += 1
            continue
        checks.append(check)
        existing_keys.add(key)
        added += 1
    golden_file.parent.mkdir(parents=True, exist_ok=True)
    golden_file.write_text(yaml.safe_dump(golden, allow_unicode=True, sort_keys=False), encoding="utf-8")
    write_checklist(ticker, golden)
    return {
        "review_pack_count": len(pack.get("items", [])),
        "existing_golden_checks": existing_count,
        "added_checks": added,
        "skipped_duplicates": skipped,
        "total_golden_checks": len(checks),
        "golden_path": str(golden_file),
        "checklist_path": str(checklist_path(ticker)),
    }


def load_golden(path: Path, ticker: str, period_from: str, period_to: str) -> dict[str, Any]:
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.setdefault("company", ticker)
        data.setdefault("period_from", period_from)
        data.setdefault("period_to", period_to)
        data.setdefault("reporting_standard", "IFRS")
        data.setdefault("checks", [])
        return data
    return {
        "company": ticker,
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": "IFRS",
        "checks": [],
    }


def review_item_to_check(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "origin": "review_pack_sync",
        "period": item.get("period"),
        "metric_code": item.get("metric_code"),
        "actual_extracted_value": item.get("value"),
        "expected_value": None,
        "expected_currency": item.get("currency"),
        "expected_unit_multiplier": item.get("unit_multiplier"),
        "expected_period_type": item.get("period_type"),
        "expected_source_role": item.get("source_role") or "financial_statements",
        "actual_source_url": item.get("source_url"),
        "actual_document_id": item.get("document_id"),
        "actual_source_location": item.get("source_location"),
        "actual_raw_label": item.get("raw_label"),
        "actual_source_references": item.get("source_references") or [],
        "ifrs_concept_code": item.get("ifrs_concept_code"),
        "statement_context": item.get("statement_context"),
        "period_coverage": item.get("period_coverage"),
        "candidate_score_breakdown": item.get("candidate_score_breakdown"),
        "applied_issuer_override": item.get("applied_issuer_override"),
        "quality_flag": item.get("quality_flag"),
        "confidence_score": item.get("confidence_score"),
        "manual_status": "pending",
        "reviewer_notes": "",
    }


def unique_key(check: dict[str, Any]) -> tuple[Any, ...]:
    return (
        check.get("period"),
        check.get("metric_code"),
        check.get("expected_period_type"),
        check.get("expected_source_role"),
        check.get("actual_source_location"),
        check.get("actual_raw_label"),
    )


def find_legacy_metric_code_raw_label_check(
    checks: list[dict[str, Any]], incoming: dict[str, Any]
) -> dict[str, Any] | None:
    """Find stale checks where raw_label was incorrectly stored as metric_code."""
    for check in checks:
        if is_stale_derived_check(check, incoming):
            return check
        if check.get("actual_raw_label") != check.get("metric_code"):
            continue
        if (
            check.get("period") == incoming.get("period")
            and check.get("metric_code") == incoming.get("metric_code")
            and check.get("expected_period_type") == incoming.get("expected_period_type")
            and check.get("expected_source_role") == incoming.get("expected_source_role")
            and check.get("actual_source_location") == incoming.get("actual_source_location")
        ):
            return check
    return None


def is_stale_derived_check(check: dict[str, Any], incoming: dict[str, Any]) -> bool:
    if check.get("actual_raw_label") != incoming.get("actual_raw_label"):
        return False
    if check.get("actual_raw_label") != "Derived total debt":
        return False
    return (
        check.get("period") == incoming.get("period")
        and check.get("metric_code") == incoming.get("metric_code")
        and check.get("expected_period_type") == incoming.get("expected_period_type")
        and check.get("expected_source_role") == incoming.get("expected_source_role")
        and str(check.get("actual_source_location") or "").startswith("derived:")
        and str(incoming.get("actual_source_location") or "").startswith("derived:")
    )


def merge_actual_fields(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    existing.setdefault("origin", "review_pack_sync")
    for key in [
        "actual_extracted_value",
        "actual_source_url",
        "actual_document_id",
        "actual_source_location",
        "actual_raw_label",
        "actual_source_references",
        "ifrs_concept_code",
        "statement_context",
        "period_coverage",
        "candidate_score_breakdown",
        "applied_issuer_override",
        "quality_flag",
        "confidence_score",
    ]:
        if incoming.get(key):
            existing[key] = incoming[key]


def write_checklist(ticker: str, golden: dict[str, Any]) -> Path:
    checks = [check for check in golden.get("checks", []) if check.get("manual_status") == "pending"]
    lines = [
        f"# {ticker.upper()} 2021 Manual Fact Review Checklist",
        "",
        (
            "| # | actual_source_url | document_id | period | metric_code | actual_extracted_value | "
            "unit_multiplier | period_type | actual_source_location | actual_raw_label | ifrs_concept_code | "
            "statement_context | period_coverage | quality_flag | confidence_score | manual_status |"
        ),
        "|---|---|---|---|---|---:|---|---|---|---|---|---|---|---|---|",
    ]
    row_number = 1
    for check in checks:
        lines.append(checklist_row(row_number, check, check.get("manual_status") or "pending"))
        row_number += 1
        for reference in check.get("actual_source_references") or []:
            lines.append(reference_row(reference, check))
    path = checklist_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def checklist_row(index: int, check: dict[str, Any], status: str) -> str:
    return (
        f"| {index} | {_md(check.get('actual_source_url'))} | {check.get('actual_document_id') or ''} | "
        f"{check.get('period')} | {check.get('metric_code')} | {check.get('actual_extracted_value')} | "
        f"{check.get('expected_unit_multiplier')} | {check.get('expected_period_type')} | "
        f"{_md(check.get('actual_source_location'))} | {_md(check.get('actual_raw_label'))} | "
        f"{check.get('ifrs_concept_code') or ''} | {check.get('statement_context') or ''} | "
        f"{check.get('period_coverage') or ''} | {check.get('quality_flag') or ''} | "
        f"{check.get('confidence_score') or ''} | {status} |"
    )


def reference_row(reference: dict[str, Any], check: dict[str, Any]) -> str:
    return (
        f"|  | {_md(reference.get('source_url'))} | {reference.get('document_id') or ''} | "
        f"{check.get('period')} | {reference.get('metric_code')} | {reference.get('value')} | "
        f"{check.get('expected_unit_multiplier')} | {reference.get('period_type') or ''} | "
        f"{_md(reference.get('source_location'))} | {_md(reference.get('raw_label'))} | "
        f"{reference.get('ifrs_concept_code') or ''} | {reference.get('statement_context') or ''} | "
        f"{reference.get('period_coverage') or ''} | {reference.get('quality_flag') or ''} | "
        f"{reference.get('confidence_score') or ''} | source_reference |"
    )


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|")


def print_summary(report: dict[str, Any]) -> None:
    print(
        "\n".join(
            [
                f"review_pack_count: {report['review_pack_count']}",
                f"existing_golden_checks: {report['existing_golden_checks']}",
                f"added_checks: {report['added_checks']}",
                f"skipped_duplicates: {report['skipped_duplicates']}",
                f"total_golden_checks: {report['total_golden_checks']}",
                f"golden_path: {report['golden_path']}",
                f"checklist_path: {report['checklist_path']}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync ticker golden YAML from manual review pack.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    args = parser.parse_args(argv)
    report = sync(args.ticker, args.period_from, args.period_to)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
