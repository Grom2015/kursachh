import json
import shutil
from pathlib import Path
from uuid import uuid4

import yaml

from app.tools.generate_manual_review_pack import generate_pack
from app.tools.sync_golden_from_review_pack import sync
from app.tools.validate_real_extraction import empty_report
from app.tools.verify_golden_facts import verify


def _golden_dir() -> Path:
    path = Path("data") / "validation" / "TATN" / "golden"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup_files() -> tuple[Path, Path]:
    src = _golden_dir()
    backup = Path("data") / "validation" / "test_tatn_manual_backup" / uuid4().hex
    backup.mkdir(parents=True, exist_ok=True)
    for file in src.glob("tatn_2021_*"):
        shutil.copy2(file, backup / file.name)
        file.unlink()
    return src, backup


def _restore(src: Path, backup: Path) -> None:
    for file in src.glob("tatn_2021_*"):
        file.unlink()
    for file in backup.glob("*"):
        shutil.copy2(file, src / file.name)
    shutil.rmtree(backup, ignore_errors=True)


def _write_pack(items: list[dict]) -> None:
    path = _golden_dir() / "tatn_2021_manual_review_pack.json"
    payload = {
        "company": "TATN",
        "period_from": "2021Q1",
        "period_to": "2021Q4",
        "facts_selected": len(items),
        "periods_covered": sorted({item["period"] for item in items}),
        "metric_codes_covered": sorted({item["metric_code"] for item in items}),
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _item(metric_code: str = "revenue", value: float = 100.0) -> dict:
    return {
        "period": "2021Q2",
        "metric_code": metric_code,
        "value": value,
        "currency": "RUB",
        "unit_multiplier": 1_000_000,
        "period_type": "ytd",
        "source_role": "financial_statements",
        "source_url": "https://old.tatneft.ru/report.pdf",
        "document_id": 1,
        "source_location": "page 5, table statement_text, line 10",
        "raw_label": metric_code,
        "source_references": [
            {
                "metric_code": "short_term_borrowings",
                "value": 40.0,
                "source_url": "https://old.tatneft.ru/report.pdf",
                "document_id": 1,
                "period_type": "balance_sheet_snapshot",
                "source_location": "page 20, table statement_text, line 15",
                "raw_label": "Total short-term debt",
            }
        ]
        if metric_code == "total_debt"
        else [],
    }


def test_tatn_review_pack_sync_creates_golden_yaml():
    src, backup = _backup_files()
    try:
        _write_pack([_item()])
        report = sync("TATN", "2021Q1", "2021Q4")

        assert report["total_golden_checks"] == 1
        assert (src / "tatn_2021_manual_fact_checks.yml").exists()
    finally:
        _restore(src, backup)


def test_tatn_checklist_contains_source_url_and_location():
    src, backup = _backup_files()
    try:
        _write_pack([_item()])
        sync("TATN", "2021Q1", "2021Q4")
        text = (src / "tatn_2021_manual_review_checklist.md").read_text(encoding="utf-8")

        assert "actual_source_url" in text
        assert "page 5, table statement_text" in text
    finally:
        _restore(src, backup)


def test_tatn_golden_verification_no_verified_checks_when_pending():
    src, backup = _backup_files()
    try:
        _write_pack([_item()])
        sync("TATN", "2021Q1", "2021Q4")
        report = verify("TATN", "2021Q1", "2021Q4")

        assert report["status"] == "NO_VERIFIED_CHECKS"
        assert report["pending_count"] == 1
    finally:
        _restore(src, backup)


def test_tatn_verification_pass_if_mocked_verified_match():
    src, backup = _backup_files()
    try:
        _write_pack([_item(f"metric_{idx}", float(idx)) for idx in range(10)])
        sync("TATN", "2021Q1", "2021Q4")
        path = src / "tatn_2021_manual_fact_checks.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for check in data["checks"]:
            check["expected_value"] = check["actual_extracted_value"]
            check["manual_status"] = "verified"
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

        report = verify("TATN", "2021Q1", "2021Q4")

        assert report["review_pack_status"] == "PASS"
        assert report["accuracy"] == 1.0
    finally:
        _restore(src, backup)


def test_tatn_verification_fail_if_mismatch():
    src, backup = _backup_files()
    try:
        _write_pack([_item()])
        sync("TATN", "2021Q1", "2021Q4")
        path = src / "tatn_2021_manual_fact_checks.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["checks"][0]["expected_value"] = 999
        data["checks"][0]["manual_status"] = "verified"
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

        report = verify("TATN", "2021Q1", "2021Q4")

        assert report["status"] == "FAIL"
    finally:
        _restore(src, backup)


def test_derived_total_debt_source_references_appear_in_checklist():
    src, backup = _backup_files()
    try:
        _write_pack([_item("total_debt", 150.0)])
        sync("TATN", "2021Q1", "2021Q4")
        text = (src / "tatn_2021_manual_review_checklist.md").read_text(encoding="utf-8")

        assert "source_reference" in text
        assert "short_term_borrowings" in text
    finally:
        _restore(src, backup)


def test_validation_report_includes_manual_verification_section():
    report = empty_report("TATN", "2021Q1", "2021Q4", "replay_cache", "no docs")

    assert "manual_verification" in report
    assert "review_pack_total" in report["manual_verification"]


def test_generate_pack_preserves_pdf_raw_label_from_validation_report():
    src, backup = _backup_files()
    validation_path = Path("data") / "validation" / "TATN" / "2021Q1_2021Q4_real_validation_report.json"
    original = validation_path.read_text(encoding="utf-8") if validation_path.exists() else None
    try:
        validation_path.parent.mkdir(parents=True, exist_ok=True)
        validation_path.write_text(
            json.dumps(
                {
                    "fact_coverage": [
                        {
                            "period": "2021Q2",
                            "metric_code": "revenue",
                            "status": "extracted",
                            "value": 100.0,
                            "currency": "RUB",
                            "unit_multiplier": 1_000_000,
                            "period_type": "ytd",
                            "quality_flag": "exact",
                            "confidence_score": 0.85,
                            "source_url": "https://old.tatneft.ru/report.pdf",
                            "source_document_id": 1,
                            "source_location": "page 5, table statement_text, line 10",
                            "raw_label": "Sales and other operating revenues on non-banking activities, net",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        pack = generate_pack("TATN", "2021Q1", "2021Q4", 20)

        assert pack["items"][0]["metric_code"] == "revenue"
        assert pack["items"][0]["raw_label"] == "Sales and other operating revenues on non-banking activities, net"
    finally:
        if original is None:
            validation_path.unlink(missing_ok=True)
        else:
            validation_path.write_text(original, encoding="utf-8")
        _restore(src, backup)


def test_sync_updates_stale_metric_code_raw_label_without_duplicate():
    src, backup = _backup_files()
    try:
        stale = _item("revenue")
        stale["raw_label"] = "revenue"
        updated = _item("revenue")
        updated["raw_label"] = "Sales and other operating revenues on non-banking activities, net"
        _write_pack([stale])
        sync("TATN", "2021Q1", "2021Q4")
        _write_pack([updated])
        report = sync("TATN", "2021Q1", "2021Q4")
        data = yaml.safe_load((src / "tatn_2021_manual_fact_checks.yml").read_text(encoding="utf-8"))

        assert report["total_golden_checks"] == 1
        assert data["checks"][0]["actual_raw_label"] == updated["raw_label"]
    finally:
        _restore(src, backup)


def test_sync_updates_stale_derived_total_debt_formula_without_duplicate():
    src, backup = _backup_files()
    try:
        stale = _item("total_debt", 35396.0)
        stale["source_location"] = "derived: short_term_borrowings + long_term_borrowings"
        stale["raw_label"] = "Derived total debt"
        updated = _item("total_debt", 32640.0)
        updated["source_location"] = (
            "derived: short_term_debt_including_current_portion + long_term_debt_net_of_current_portion"
        )
        updated["raw_label"] = "Derived total debt"
        updated["source_references"] = [
            {
                "metric_code": "short_term_debt_including_current_portion",
                "value": 9714.0,
                "source_url": "https://old.tatneft.ru/report.pdf",
                "document_id": 1,
                "period_type": "balance_sheet_snapshot",
                "source_location": "page 20, table statement_text, line 15",
                "raw_label": "Total short-term debt, including current portion of long-term debt",
            },
            {
                "metric_code": "long_term_debt_net_of_current_portion",
                "value": 22926.0,
                "source_url": "https://old.tatneft.ru/report.pdf",
                "document_id": 1,
                "period_type": "balance_sheet_snapshot",
                "source_location": "page 20, table statement_text, line 28",
                "raw_label": "Total long-term debt, net of current portion",
            },
        ]
        _write_pack([stale])
        sync("TATN", "2021Q1", "2021Q4")
        _write_pack([updated])
        report = sync("TATN", "2021Q1", "2021Q4")
        data = yaml.safe_load((src / "tatn_2021_manual_fact_checks.yml").read_text(encoding="utf-8"))

        assert report["total_golden_checks"] == 1
        assert data["checks"][0]["actual_extracted_value"] == 32640.0
        assert data["checks"][0]["actual_source_location"] == updated["source_location"]
        assert {row["metric_code"] for row in data["checks"][0]["actual_source_references"]} == {
            "short_term_debt_including_current_portion",
            "long_term_debt_net_of_current_portion",
        }
    finally:
        _restore(src, backup)
