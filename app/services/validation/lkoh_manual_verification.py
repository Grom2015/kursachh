import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AnalysisResult, Company, StatementFact
from app.services.periods import periods_between

GOLDEN_DIR = Path("data") / "validation" / "LKOH" / "golden"
GOLDEN_CHECKS_FILE = "lkoh_2021_manual_fact_checks.yml"
MANUAL_REVIEW_PACK_FILE = "lkoh_2021_manual_review_pack.json"
GOLDEN_REPORT_FILE = "lkoh_2021_golden_verification_report.json"
MANUAL_CHECKLIST_FILE = "lkoh_2021_manual_review_checklist.md"
MANUAL_STATUSES = {"pending", "verified", "mismatch", "not_found", "needs_review"}


def golden_dir() -> Path:
    root = get_settings().root_dir / GOLDEN_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def golden_checks_path() -> Path:
    return golden_dir() / GOLDEN_CHECKS_FILE


def manual_review_pack_path() -> Path:
    return golden_dir() / MANUAL_REVIEW_PACK_FILE


def golden_report_path() -> Path:
    return golden_dir() / GOLDEN_REPORT_FILE


def manual_checklist_path() -> Path:
    return golden_dir() / MANUAL_CHECKLIST_FILE


def load_golden_checks(path: Path | None = None) -> dict[str, Any] | None:
    target = path or golden_checks_path()
    if not target.exists():
        return None
    return yaml.safe_load(target.read_text(encoding="utf-8")) or {}


def manual_verification_summary(period_from: str, period_to: str, facts: list[StatementFact] | None = None) -> dict[str, Any]:
    golden = load_golden_checks()
    if not golden:
        return {
            "golden_checks_total": 0,
            "verified_checks_count": 0,
            "passed_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "accuracy": None,
            "status": "not_started",
        }
    report = verify_golden_checks(period_from, period_to, facts=facts, persist=False)
    if report["status"] in {"NO_VERIFIED_CHECKS", "IN_PROGRESS"}:
        status = "in_progress" if report["pending_count"] else "not_started"
    elif report["status"] == "PASS":
        status = "pass"
    else:
        status = "fail"
    return {
        "golden_checks_total": report["total_checks_count"],
        "verified_checks_count": report["verified_checks_count"],
        "passed_count": report["passed_count"],
        "failed_count": report["failed_count"],
        "pending_count": report["pending_count"],
        "accuracy": report["accuracy"],
        "status": status,
    }


def generate_manual_review_pack(
    period_from: str,
    period_to: str,
    limit: int = 20,
    db: Session | None = None,
    facts: list[StatementFact] | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    facts = facts if facts is not None else load_current_facts(period_from, period_to, db=db)
    selected = select_review_facts(facts, limit)
    items = [fact_to_review_item(fact) for fact in selected]
    payload = {
        "company": "LKOH",
        "period_from": period_from,
        "period_to": period_to,
        "facts_selected": len(items),
        "periods_covered": sorted({item["period"] for item in items}),
        "metric_codes_covered": sorted({item["metric_code"] for item in items}),
        "items": items,
    }
    target = output_path or manual_review_pack_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload


def sync_golden_from_review_pack(
    period_from: str,
    period_to: str,
    review_pack_path: Path | None = None,
    golden_path: Path | None = None,
    checklist_path: Path | None = None,
) -> dict[str, Any]:
    pack_path = review_pack_path or manual_review_pack_path()
    target_golden_path = golden_path or golden_checks_path()
    target_checklist_path = checklist_path or manual_checklist_path()
    if not pack_path.exists():
        raise FileNotFoundError(f"Manual review pack not found: {pack_path}")
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    golden = load_golden_checks(target_golden_path) or {
        "company": "LKOH",
        "period_from": period_from,
        "period_to": period_to,
        "reporting_standard": "IFRS",
        "checks": [],
    }
    checks = golden.setdefault("checks", [])
    existing_count = len(checks)
    existing_keys = {check_unique_key(check) for check in checks}
    added = 0
    skipped = 0
    for item in pack.get("items", []):
        check = review_item_to_pending_check(item)
        key = check_unique_key(check)
        if key in existing_keys:
            merge_actual_fields(next(existing for existing in checks if check_unique_key(existing) == key), check)
            skipped += 1
            continue
        checks.append(check)
        existing_keys.add(key)
        added += 1
    for check in checks:
        check.setdefault("origin", infer_origin(check))
    target_golden_path.parent.mkdir(parents=True, exist_ok=True)
    target_golden_path.write_text(yaml.safe_dump(golden, allow_unicode=True, sort_keys=False), encoding="utf-8")
    write_manual_checklist(golden, target_checklist_path)
    return {
        "review_pack_count": len(pack.get("items", [])),
        "existing_golden_checks": existing_count,
        "added_checks": added,
        "skipped_duplicates": skipped,
        "total_golden_checks": len(checks),
        "golden_path": str(target_golden_path),
        "checklist_path": str(target_checklist_path),
    }


def review_item_to_pending_check(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "origin": "review_pack_sync",
        "period": item.get("period"),
        "metric_code": item.get("metric_code"),
        "expected_value": None,
        "expected_currency": item.get("currency"),
        "expected_unit_multiplier": item.get("unit_multiplier"),
        "expected_period_type": item.get("period_type"),
        "expected_source_role": item.get("source_role") or "financial_statements",
        "expected_source_location_contains": ["page", "table"],
        "actual_extracted_value": item.get("value"),
        "actual_source_url": item.get("source_url"),
        "actual_document_id": item.get("document_id"),
        "actual_source_location": item.get("source_location"),
        "actual_raw_label": item.get("raw_label"),
        "actual_source_references": item.get("source_references") or [],
        "manual_status": "pending",
        "reviewer_notes": "",
    }


def check_unique_key(check: dict[str, Any]) -> tuple[Any, ...]:
    return (
        check.get("period"),
        check.get("metric_code"),
        check.get("expected_period_type"),
        check.get("expected_source_role"),
        check.get("actual_source_location") or "|".join(check.get("expected_source_location_contains") or []),
        check.get("actual_raw_label"),
    )


def merge_actual_fields(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    existing.setdefault("origin", infer_origin(existing))
    for key in [
        "origin",
        "actual_extracted_value",
        "actual_source_url",
        "actual_document_id",
        "actual_source_location",
        "actual_raw_label",
        "actual_source_references",
    ]:
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]


def verify_golden_checks(
    period_from: str,
    period_to: str,
    db: Session | None = None,
    facts: list[StatementFact] | None = None,
    tolerance: float = 0.01,
    persist: bool = True,
) -> dict[str, Any]:
    golden = load_golden_checks()
    warnings: list[str] = []
    if not golden:
        warnings.append("Manual golden verification has not been performed.")
        report = empty_golden_report(period_from, period_to, warnings)
        if persist:
            golden_report_path().write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    checks = [
        check
        for check in golden.get("checks", [])
        if check.get("period") in periods_between(period_from, period_to)
    ]
    for check in checks:
        check.setdefault("origin", infer_origin(check))
    facts = facts if facts is not None else load_current_facts(period_from, period_to, db=db)
    fact_by_key = {
        (fact.period, fact.metric_code, fact.period_type, (fact.source_location or {}).get("source_role")): fact
        for fact in facts
    }
    total_checks_count = len(checks)
    verified = [check for check in checks if check.get("manual_status") == "verified"]
    pending_count = sum(1 for check in checks if check.get("manual_status") == "pending")
    mismatch_count = sum(1 for check in checks if check.get("manual_status") == "mismatch")
    not_found_count = sum(1 for check in checks if check.get("manual_status") == "not_found")
    needs_review_count = sum(1 for check in checks if check.get("manual_status") == "needs_review")
    failures: list[dict[str, Any]] = []
    passed_count = 0
    for check in verified:
        status, reason = compare_check(check, fact_by_key, tolerance)
        if status == "passed":
            passed_count += 1
        else:
            failures.append({"period": check.get("period"), "metric_code": check.get("metric_code"), "reason": reason})
    verified_count = len(verified)
    failed_count = len(failures)
    accuracy = (passed_count / verified_count) if verified_count else None
    if verified_count == 0:
        status = "NO_VERIFIED_CHECKS"
    elif accuracy is not None and accuracy < 0.9:
        status = "FAIL"
    elif pending_count:
        status = "IN_PROGRESS"
    elif accuracy is not None and accuracy >= 0.9 and verified_count >= 20:
        status = "PASS"
    else:
        status = "FAIL"
        if verified_count < 20:
            warnings.append("At least 20 verified checks are required for PASS.")
    review_pack_report = review_pack_status(checks, fact_by_key, tolerance)
    report = {
        "company": "LKOH",
        "period_from": period_from,
        "period_to": period_to,
        "review_pack_sample_status": review_pack_report["review_pack_status"],
        "full_golden_dataset_status": status,
        **review_pack_report,
        "total_checks_count": total_checks_count,
        "verified_checks_count": verified_count,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "pending_count": pending_count,
        "mismatch_count": mismatch_count,
        "not_found_count": not_found_count,
        "needs_review_count": needs_review_count,
        "accuracy": accuracy,
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "next_actions": next_actions(status, pending_count, failures),
    }
    if persist:
        golden_report_path().write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def load_current_facts(period_from: str, period_to: str, db: Session | None = None) -> list[StatementFact]:
    if db is not None:
        company = db.scalar(select(Company).where(Company.ticker == "LKOH"))
        return _facts_from_db(db, company, period_from, period_to) if company else []
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        company = session.scalar(select(Company).where(Company.ticker == "LKOH"))
        return _facts_from_db(session, company, period_from, period_to) if company else []


def _facts_from_db(db: Session, company: Company, period_from: str, period_to: str) -> list[StatementFact]:
    result = db.scalar(
        select(AnalysisResult)
        .where(
            AnalysisResult.company_id == company.id,
            AnalysisResult.period_from == period_from,
            AnalysisResult.period_to == period_to,
        )
        .order_by(desc(AnalysisResult.created_at))
    )
    document_ids: set[int] = set()
    if result:
        for document in result.result_json.get("source_documents", []):
            if document.get("source_type") != "fixture":
                document_ids.add(document["id"])
    periods = periods_between(period_from, period_to)
    facts = db.scalars(
        select(StatementFact).where(StatementFact.company_id == company.id, StatementFact.period.in_(periods))
    ).all()
    return [
        fact
        for fact in facts
        if (not document_ids or fact.report_document_id in document_ids)
        and (fact.source_location or {}).get("source_type") != "fixture"
    ]


def select_review_facts(facts: list[StatementFact], limit: int) -> list[StatementFact]:
    periods = sorted({fact.period for fact in facts})
    categories = [
        lambda fact: fact.period_type in {"ytd", "annual"} and fact.statement_type == "income_statement",
        lambda fact: fact.period_type == "balance_sheet_snapshot",
        lambda fact: fact.statement_type == "cash_flow",
        lambda fact: fact.quality_flag == "derived",
    ]
    selected: list[StatementFact] = []
    seen: set[int] = set()
    for category in categories:
        for period in periods:
            for fact in sorted(facts, key=lambda item: (item.period, item.metric_code)):
                if fact.period != period or fact.id in seen or not category(fact):
                    continue
                selected.append(fact)
                seen.add(fact.id)
                if len(selected) >= limit:
                    return selected
                break
    for fact in sorted(facts, key=lambda item: (item.period, item.metric_code)):
        if fact.id not in seen:
            selected.append(fact)
            seen.add(fact.id)
            if len(selected) >= limit:
                break
    return selected


def fact_to_review_item(fact: StatementFact) -> dict[str, Any]:
    location = fact.source_location or {}
    return {
        "period": fact.period,
        "metric_code": fact.metric_code,
        "value": fact.value,
        "currency": fact.currency,
        "unit_multiplier": fact.unit_multiplier,
        "period_type": fact.period_type,
        "quality_flag": fact.quality_flag,
        "source_role": location.get("source_role"),
        "source_url": location.get("source_url"),
        "document_id": location.get("document_id") or fact.report_document_id,
        "source_location": format_source_location(location),
        "raw_label": location.get("raw_label") or fact.metric_name_original,
        "table_index": location.get("table"),
        "source_references": source_references(location),
        "confidence_score": fact.confidence_score,
        "manual_status": "pending",
    }


def compare_check(
    check: dict[str, Any],
    fact_by_key: dict[tuple[str, str, str, str | None], StatementFact],
    tolerance: float,
) -> tuple[str, str]:
    key = (
        check.get("period"),
        check.get("metric_code"),
        check.get("expected_period_type"),
        check.get("expected_source_role"),
    )
    fact = fact_by_key.get(key)
    if not fact:
        return "failed", "Extracted fact not found for expected period/metric/period_type/source_role."
    location = fact.source_location or {}
    if not format_source_location(location):
        return "failed", "source_location is missing"
    source_text = format_source_location(location) or ""
    if not source_text and check.get("actual_source_location"):
        source_text = str(check["actual_source_location"])
    reference_text = " ".join(
        str(reference.get("source_location") or "") for reference in check.get("actual_source_references") or []
    )
    for expected_part in check.get("expected_source_location_contains", []) or []:
        if expected_part not in source_text and expected_part not in reference_text:
            return "failed", f"source_location does not contain {expected_part}"
    if fact.currency != check.get("expected_currency"):
        return "failed", f"currency mismatch: expected {check.get('expected_currency')}, got {fact.currency}"
    if fact.unit_multiplier != check.get("expected_unit_multiplier"):
        return (
            "failed",
            f"unit_multiplier mismatch: expected {check.get('expected_unit_multiplier')}, got {fact.unit_multiplier}",
        )
    expected = check.get("expected_value")
    if expected is None:
        return "failed", "verified check has no expected_value"
    actual = fact.value
    if actual is None:
        return "failed", "actual value is missing"
    diff = abs(float(actual) - float(expected))
    allowed = max(tolerance, abs(float(expected)) * tolerance)
    if diff > allowed:
        return "failed", f"value mismatch: expected {expected}, got {actual}"
    return "passed", ""


def empty_golden_report(period_from: str, period_to: str, warnings: list[str]) -> dict[str, Any]:
    return {
        "company": "LKOH",
        "period_from": period_from,
        "period_to": period_to,
        "review_pack_sample_status": "NO_REVIEW_PACK_CHECKS",
        "full_golden_dataset_status": "NO_VERIFIED_CHECKS",
        "review_pack_total": 0,
        "review_pack_verified": 0,
        "review_pack_passed": 0,
        "review_pack_failed": 0,
        "review_pack_accuracy": None,
        "review_pack_status": "NO_REVIEW_PACK_CHECKS",
        "total_checks_count": 0,
        "verified_checks_count": 0,
        "passed_count": 0,
        "failed_count": 0,
        "pending_count": 0,
        "mismatch_count": 0,
        "not_found_count": 0,
        "needs_review_count": 0,
        "accuracy": None,
        "status": "NO_VERIFIED_CHECKS",
        "failures": [],
        "warnings": warnings,
        "next_actions": ["Create golden checks and have an analyst mark reviewed rows as verified."],
    }


def next_actions(status: str, pending_count: int, failures: list[dict[str, Any]]) -> list[str]:
    actions = []
    if status == "NO_VERIFIED_CHECKS":
        actions.append("Analyst must fill expected_value and mark at least 20 checks as verified.")
    if pending_count:
        actions.append("Complete pending manual checks before trusting high-confidence parser output.")
    if failures:
        actions.append("Review failed checks against source PDF pages and parser source_location.")
    return actions


def write_manual_checklist(golden: dict[str, Any], path: Path | None = None) -> Path:
    target = path or manual_checklist_path()
    checks = [check for check in golden.get("checks", []) if check.get("manual_status") == "pending"]
    extracted_checks = [
        check
        for check in checks
        if check.get("actual_extracted_value") is not None and check.get("actual_source_location")
    ]
    seed_checks = [check for check in checks if check not in extracted_checks]
    lines = [
        "# LKOH 2021 Manual Fact Review Checklist",
        "",
        "## Extracted facts",
        "",
        (
            "| # | actual_source_url | document_id | period | metric_code | actual_extracted_value | "
            "unit_multiplier | period_type | actual_source_location | actual_raw_label | manual_status |"
        ),
        "|---|---|---|---|---|---:|---|---|---|---|---|",
    ]
    for index, check in enumerate(extracted_checks, start=1):
        lines.append(
            "| "
            f"{index} | "
            f"{_escape_md(check.get('actual_source_url'))} | "
            f"{check.get('actual_document_id') or ''} | "
            f"{check.get('period')} | "
            f"{check.get('metric_code')} | "
            f"{check.get('actual_extracted_value')} | "
            f"{check.get('expected_unit_multiplier')} | "
            f"{check.get('expected_period_type')} | "
            f"{_escape_md(check.get('actual_source_location'))} | "
            f"{_escape_md(check.get('actual_raw_label'))} | "
            f"{check.get('manual_status')} |"
        )
        for reference in check.get("actual_source_references") or []:
            lines.append(
                "| "
                f"  | {_escape_md(reference.get('source_url'))} | {reference.get('document_id') or ''} | "
                f"{check.get('period')} | {reference.get('metric_code')} | {reference.get('value')} | "
                f"{check.get('expected_unit_multiplier')} | {reference.get('period_type') or ''} | "
                f"{_escape_md(reference.get('source_location'))} | {_escape_md(reference.get('raw_label'))} | "
                "source_reference |"
            )
    if seed_checks:
        lines.extend(
            [
                "",
                "## Seed checks without extracted fact",
                "",
                (
                    "| # | period | metric_code | actual_extracted_value | unit_multiplier | period_type | "
                    "actual_source_location | actual_raw_label | manual_status |"
                ),
                "|---|---|---|---:|---|---|---|---|---|",
            ]
        )
        for index, check in enumerate(seed_checks, start=1):
            lines.append(
                "| "
                f"{index} | "
                f"{check.get('period')} | "
                f"{check.get('metric_code')} | "
                f"{check.get('actual_extracted_value')} | "
                f"{check.get('expected_unit_multiplier')} | "
                f"{check.get('expected_period_type')} | "
                f"{_escape_md(check.get('actual_source_location'))} | "
                f"{_escape_md(check.get('actual_raw_label'))} | "
                f"{check.get('manual_status')} |"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def infer_origin(check: dict[str, Any]) -> str:
    if check.get("actual_extracted_value") is not None or check.get("actual_source_location"):
        return "review_pack_sync"
    return "seed_manual_check"


def review_pack_status(
    checks: list[dict[str, Any]],
    fact_by_key: dict[tuple[str, str, str, str | None], StatementFact],
    tolerance: float,
) -> dict[str, Any]:
    review_checks = [check for check in checks if check.get("origin") == "review_pack_sync"]
    verified = [check for check in review_checks if check.get("manual_status") == "verified"]
    passed = 0
    failed = 0
    for check in verified:
        status, _reason = compare_check(check, fact_by_key, tolerance)
        if status == "passed":
            passed += 1
        else:
            failed += 1
    accuracy = (passed / len(verified)) if verified else None
    if not review_checks:
        status = "NO_REVIEW_PACK_CHECKS"
    elif not verified:
        status = "NO_VERIFIED_CHECKS"
    elif accuracy is not None and accuracy < 0.9:
        status = "FAIL"
    elif len(verified) < len(review_checks):
        status = "IN_PROGRESS"
    else:
        status = "PASS"
    return {
        "review_pack_total": len(review_checks),
        "review_pack_verified": len(verified),
        "review_pack_passed": passed,
        "review_pack_failed": failed,
        "review_pack_accuracy": accuracy,
        "review_pack_status": status,
    }


def _escape_md(value: Any) -> str:
    return str(value or "").replace("|", "\\|")


def format_source_location(location: dict[str, Any]) -> str | None:
    if not location:
        return None
    parts = []
    if location.get("page"):
        parts.append(f"page {location['page']}")
    if location.get("table"):
        parts.append(f"table {location['table']}")
    if location.get("line"):
        parts.append(f"line {location['line']}")
    if not parts and location.get("formula"):
        parts.append(f"derived: {location['formula']}")
    return ", ".join(parts) if parts else None


def source_references(location: dict[str, Any]) -> list[dict[str, Any]]:
    references = []
    for metric_code, payload in (location.get("inputs") or {}).items():
        source_location = payload.get("source_location") or {}
        references.append(
            {
                "metric_code": metric_code,
                "value": payload.get("value"),
                "source_url": source_location.get("source_url"),
                "document_id": source_location.get("document_id"),
                "period_type": source_location.get("period_type"),
                "source_location": format_source_location(source_location),
                "raw_label": source_location.get("raw_label"),
            }
        )
    return references
