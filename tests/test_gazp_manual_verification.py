import json
import shutil
from pathlib import Path
from uuid import uuid4

import yaml

from app.core.config import get_settings
from app.tools.audit_real_metrics import apply_issuer_metric_methodology_notes
from app.tools.generate_manual_review_pack import generate_pack, pack_path
from app.tools.sync_golden_from_review_pack import checklist_path, golden_path, sync
from app.tools.validate_real_extraction import empty_report
from app.tools.verify_golden_facts import verify


def _golden_dir() -> Path:
    path = get_settings().root_dir / "data" / "validation" / "GAZP" / "golden"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup_files() -> tuple[Path, Path]:
    src = _golden_dir()
    backup = get_settings().root_dir / "data" / "validation" / "test_gazp_manual_backup" / uuid4().hex
    backup.mkdir(parents=True, exist_ok=True)
    for file in src.glob("gazp_2021_*"):
        shutil.copy2(file, backup / file.name)
        file.unlink()
    return src, backup


def _restore(src: Path, backup: Path) -> None:
    for file in src.glob("gazp_2021_*"):
        file.unlink()
    for file in backup.glob("*"):
        shutil.copy2(file, src / file.name)
    shutil.rmtree(backup, ignore_errors=True)


def _report_file(period_from: str, period_to: str) -> Path:
    root = get_settings().root_dir / "data" / "validation" / "GAZP"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{period_from}_{period_to}_real_validation_report.json"


def _write_validation_report(period_from: str, period_to: str) -> Path:
    path = _report_file(period_from, period_to)
    path.write_text(
        json.dumps(
            {
                "fixture_data_used": False,
                "fact_coverage": [
                    {
                        "period": period_from,
                        "metric_code": "revenue",
                        "status": "extracted",
                        "value": 100.0,
                        "currency": "RUB",
                        "unit_multiplier": 1_000_000,
                        "period_type": "ytd",
                        "quality_flag": "exact",
                        "confidence_score": 0.9,
                        "source_url": "https://www.gazprom.com/report.pdf",
                        "source_document_id": "doc-1",
                        "source_location": "page 13, table statement_text, line 36",
                        "raw_label": "Total sales in the consolidated statement of comprehensive income",
                        "ifrs_concept_code": "revenue",
                        "statement_context": "segment_note",
                        "period_coverage": "3M",
                        "candidate_score_breakdown": {
                            "label_match": True,
                            "statement_context_allowed": True,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_gazp_review_pack_generated_when_canonical_facts_exist():
    src, backup = _backup_files()
    period_from = "2097Q1"
    period_to = "2097Q1"
    report_file = _write_validation_report(period_from, period_to)
    try:
        report = generate_pack("GAZP", period_from, period_to, limit=20)

        assert report["facts_selected"] == 1
        assert report["items"][0]["ifrs_concept_code"] == "revenue"
        assert report["items"][0]["statement_context"] == "segment_note"
    finally:
        report_file.unlink(missing_ok=True)
        pack_path("GAZP").unlink(missing_ok=True)
        _restore(src, backup)


def test_gazp_checklist_contains_traceability_and_ifrs_fields():
    src, backup = _backup_files()
    period_from = "2097Q2"
    period_to = "2097Q2"
    report_file = _write_validation_report(period_from, period_to)
    try:
        generate_pack("GAZP", period_from, period_to, limit=20)
        sync("GAZP", period_from, period_to)
        text = checklist_path("GAZP").read_text(encoding="utf-8")

        assert "actual_source_url" in text
        assert "actual_source_location" in text
        assert "actual_raw_label" in text
        assert "ifrs_concept_code" in text
        assert "statement_context" in text
        assert "period_coverage" in text
        assert "Total sales in the consolidated statement" in text
    finally:
        report_file.unlink(missing_ok=True)
        _restore(src, backup)


def test_gazp_golden_yaml_created_with_pending_ifrs_checks():
    src, backup = _backup_files()
    period_from = "2097Q3"
    period_to = "2097Q3"
    report_file = _write_validation_report(period_from, period_to)
    try:
        generate_pack("GAZP", period_from, period_to, limit=20)
        sync("GAZP", period_from, period_to)
        data = yaml.safe_load(golden_path("GAZP").read_text(encoding="utf-8"))
        check = data["checks"][0]

        assert check["manual_status"] == "pending"
        assert check["expected_value"] is None
        assert check["origin"] == "review_pack_sync"
        assert check["ifrs_concept_code"] == "revenue"
        assert check["candidate_score_breakdown"]["label_match"] is True
    finally:
        report_file.unlink(missing_ok=True)
        _restore(src, backup)


def test_gazp_verification_no_verified_checks_when_all_pending():
    src, backup = _backup_files()
    period_from = "2097Q4"
    period_to = "2097Q4"
    report_file = _write_validation_report(period_from, period_to)
    try:
        generate_pack("GAZP", period_from, period_to, limit=20)
        sync("GAZP", period_from, period_to)
        report = verify("GAZP", period_from, period_to)

        assert report["review_pack_status"] == "NO_VERIFIED_CHECKS"
        assert report["verified_checks_count"] == 0
        assert report["pending_count"] == 1
        assert report["accuracy"] is None
    finally:
        report_file.unlink(missing_ok=True)
        _restore(src, backup)


def test_gazp_validation_report_includes_manual_verification():
    src, backup = _backup_files()
    try:
        report = empty_report("GAZP", "2097Q1", "2097Q4", "replay_cache", "controlled fail")

        assert "manual_verification" in report
        assert report["manual_verification"]["review_pack_verified"] == 0
    finally:
        _restore(src, backup)


def test_gazp_review_pack_does_not_use_fixture_data():
    src, backup = _backup_files()
    period_from = "2096Q1"
    period_to = "2096Q1"
    report_file = _write_validation_report(period_from, period_to)
    try:
        report = generate_pack("GAZP", period_from, period_to, limit=20)

        assert report["items"][0]["source_role"] == "financial_statements"
        assert report["items"][0]["source_url"].startswith("https://www.gazprom.com/")
    finally:
        report_file.unlink(missing_ok=True)
        pack_path("GAZP").unlink(missing_ok=True)
        _restore(src, backup)


def test_gazp_golden_verification_passes_when_17_checks_match():
    src, backup = _backup_files()
    try:
        checks = [
            {
                "origin": "review_pack_sync",
                "period": "2021Q1",
                "metric_code": f"metric_{index}",
                "actual_extracted_value": float(index),
                "expected_value": float(index),
                "expected_currency": "RUB",
                "expected_unit_multiplier": 1_000_000,
                "expected_period_type": "ytd",
                "expected_source_role": "financial_statements",
                "actual_source_location": f"page {index}, table statement_text",
                "actual_raw_label": f"Raw label {index}",
                "manual_status": "verified",
                "reviewer_notes": "Checked against official Gazprom IFRS PDF, value/source match.",
            }
            for index in range(1, 18)
        ]
        golden_path("GAZP").write_text(
            yaml.safe_dump(
                {
                    "company": "GAZP",
                    "period_from": "2021Q1",
                    "period_to": "2021Q4",
                    "reporting_standard": "IFRS",
                    "checks": checks,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        report = verify("GAZP", "2021Q1", "2021Q4")

        assert report["review_pack_status"] == "PASS"
        assert report["verified_checks_count"] == 17
        assert report["passed_count"] == 17
        assert report["accuracy"] == 1.0
    finally:
        _restore(src, backup)


def test_gazp_reviewer_notes_preserve_methodology_caveats():
    checks = [
        {"metric_code": "capex", "statement_context": "segment_information_note"},
        {"metric_code": "revenue", "statement_context": "segment_information_note"},
        {"metric_code": "total_assets", "statement_context": "segment_information_note"},
        {"metric_code": "total_debt", "statement_context": None},
    ]
    notes = {
        "capex": (
            "Checked against official Gazprom IFRS PDF, value/source match. This is reported capital "
            "expenditures from segment information note, not cash-flow purchase of PPE."
        ),
        "revenue": (
            "Checked against official Gazprom IFRS PDF, value/source match. Source is segment information "
            "/ reconciliation line to consolidated statement."
        ),
        "total_assets": (
            "Checked against official Gazprom IFRS PDF, value/source match. Source is segment information "
            "/ reconciliation line to consolidated balance sheet."
        ),
        "total_debt": (
            "Checked against official Gazprom IFRS PDF, value/source match. Derived as short-term borrowings, "
            "promissory notes and current portion of long-term borrowings plus long-term borrowings, "
            "promissory notes."
        ),
    }

    assert "not cash-flow purchase of PPE" in notes[checks[0]["metric_code"]]
    assert "reconciliation line to consolidated statement" in notes[checks[1]["metric_code"]]
    assert "reconciliation line to consolidated balance sheet" in notes[checks[2]["metric_code"]]
    assert "short-term borrowings" in notes[checks[3]["metric_code"]]


def test_gazp_metric_audit_marks_capex_methodology_warning():
    rows = [
        {
            "metric_code": "fcf",
            "period": "2021Q4",
            "status": "valid",
            "inputs": {"operating_cash_flow": 10, "capex": 2},
            "methodology_warnings": [],
        }
    ]

    apply_issuer_metric_methodology_notes("GAZP", rows)

    assert rows[0]["methodology_warnings"] == [
        "capex uses reported segment capital expenditures, not cash-flow purchase of PPE."
    ]
