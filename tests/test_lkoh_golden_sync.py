import json
import uuid
from pathlib import Path

import yaml

from app.db.models import ReportDocument, StatementFact
from app.services.validation import lkoh_manual_verification as manual


def _test_dir():
    root = Path("data") / "validation" / "LKOH" / "golden" / "test_tmp" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def _review_item(metric="revenue", location="page 5, table statement_text, line 9", raw_label="Revenue"):
    return {
        "period": "2021Q1",
        "metric_code": metric,
        "value": 100.0,
        "currency": "RUB",
        "unit_multiplier": 1_000_000,
        "period_type": "ytd",
        "quality_flag": "exact",
        "source_role": "financial_statements",
        "source_url": "https://www.lukoil.com/report.pdf",
        "document_id": 1,
        "source_location": location,
        "raw_label": raw_label,
        "table_index": "statement_text",
        "confidence_score": 0.9,
        "manual_status": "pending",
    }


def _pack(path, items):
    payload = {
        "company": "LKOH",
        "period_from": "2021Q1",
        "period_to": "2021Q4",
        "facts_selected": len(items),
        "periods_covered": ["2021Q1"],
        "metric_codes_covered": sorted({item["metric_code"] for item in items}),
        "items": items,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _golden(path, checks):
    payload = {
        "company": "LKOH",
        "period_from": "2021Q1",
        "period_to": "2021Q4",
        "reporting_standard": "IFRS",
        "checks": checks,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return payload


def _fact(db_session, metric="revenue", value=100, location=None):
    doc = ReportDocument(
        company_id=1,
        report_period="2021Q1",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="issuer_ir_manifest",
        source_url="https://www.lukoil.com/report.pdf",
        status="parsed",
    )
    db_session.add(doc)
    db_session.flush()
    fact = StatementFact(
        company_id=1,
        report_document_id=doc.id,
        period="2021Q1",
        reporting_standard="IFRS",
        statement_type="income_statement",
        metric_code=metric,
        metric_name_original=metric,
        value=value,
        currency="RUB",
        unit_multiplier=1_000_000,
        period_type="ytd",
        source_location=location
        or {
            "source_role": "financial_statements",
            "source_url": "https://www.lukoil.com/report.pdf",
            "page": 5,
            "table": "statement_text",
        },
        quality_flag="exact",
        confidence_score=0.9,
    )
    db_session.add(fact)
    db_session.flush()
    return fact


def test_sync_adds_missing_review_pack_facts_to_golden_yaml():
    root = _test_dir()
    pack_path = root / "pack.json"
    golden_path = root / "golden.yml"
    checklist_path = root / "checklist.md"
    _pack(pack_path, [_review_item("revenue"), _review_item("net_income")])
    _golden(golden_path, [])
    report = manual.sync_golden_from_review_pack("2021Q1", "2021Q4", pack_path, golden_path, checklist_path)
    synced = yaml.safe_load(golden_path.read_text(encoding="utf-8"))
    assert report["added_checks"] == 2
    assert len(synced["checks"]) == 2
    assert synced["checks"][0]["expected_value"] is None
    assert synced["checks"][0]["actual_extracted_value"] == 100.0
    assert synced["checks"][0]["origin"] == "review_pack_sync"


def test_sync_does_not_duplicate_existing_checks():
    root = _test_dir()
    pack_path = root / "pack.json"
    golden_path = root / "golden.yml"
    checklist_path = root / "checklist.md"
    item = _review_item("revenue")
    _pack(pack_path, [item])
    _golden(golden_path, [manual.review_item_to_pending_check(item)])
    report = manual.sync_golden_from_review_pack("2021Q1", "2021Q4", pack_path, golden_path, checklist_path)
    assert report["added_checks"] == 0
    assert report["skipped_duplicates"] == 1
    assert report["total_golden_checks"] == 1


def test_sync_enriches_duplicate_with_actual_fields(tmp_path=None):
    root = _test_dir()
    pack_path = root / "pack.json"
    golden_path = root / "golden.yml"
    checklist_path = root / "checklist.md"
    item = _review_item("revenue")
    existing = manual.review_item_to_pending_check(item)
    existing.pop("actual_source_url")
    _pack(pack_path, [item])
    _golden(golden_path, [existing])
    report = manual.sync_golden_from_review_pack("2021Q1", "2021Q4", pack_path, golden_path, checklist_path)
    synced = yaml.safe_load(golden_path.read_text(encoding="utf-8"))
    assert report["added_checks"] == 0
    assert synced["checks"][0]["actual_source_url"] == item["source_url"]


def test_unique_key_includes_source_location_and_raw_label():
    root = _test_dir()
    pack_path = root / "pack.json"
    golden_path = root / "golden.yml"
    checklist_path = root / "checklist.md"
    item_a = _review_item("revenue", location="page 5, table A", raw_label="Revenue")
    item_b = _review_item("revenue", location="page 6, table B", raw_label="Sales")
    _pack(pack_path, [item_a, item_b])
    _golden(golden_path, [])
    report = manual.sync_golden_from_review_pack("2021Q1", "2021Q4", pack_path, golden_path, checklist_path)
    assert report["added_checks"] == 2


def test_golden_verification_pending_count_equals_total_pending_checks(monkeypatch):
    checks = [manual.review_item_to_pending_check(_review_item(f"metric_{index}")) for index in range(20)]
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: {"checks": checks})
    report = manual.verify_golden_checks("2021Q1", "2021Q4", facts=[], persist=False)
    assert report["total_checks_count"] == 20
    assert report["pending_count"] == 20


def test_verification_status_no_verified_checks_when_all_twenty_pending(monkeypatch):
    checks = [manual.review_item_to_pending_check(_review_item(f"metric_{index}")) for index in range(20)]
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: {"checks": checks})
    report = manual.verify_golden_checks("2021Q1", "2021Q4", facts=[], persist=False)
    assert report["status"] == "NO_VERIFIED_CHECKS"
    assert report["verified_checks_count"] == 0


def test_verification_status_in_progress_when_some_verified_and_some_pending(db_session, monkeypatch):
    verified_check = manual.review_item_to_pending_check(_review_item("revenue"))
    verified_check["manual_status"] = "verified"
    verified_check["expected_value"] = 100.0
    pending_check = manual.review_item_to_pending_check(_review_item("net_income"))
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: {"checks": [verified_check, pending_check]})
    fact = _fact(db_session, "revenue", 100.0)
    report = manual.verify_golden_checks("2021Q1", "2021Q4", facts=[fact], persist=False)
    assert report["status"] == "IN_PROGRESS"
    assert report["full_golden_dataset_status"] == "IN_PROGRESS"
    assert report["review_pack_status"] == "IN_PROGRESS"
    assert report["verified_checks_count"] == 1
    assert report["pending_count"] == 1


def test_review_pack_status_pass_while_full_dataset_in_progress(db_session, monkeypatch):
    verified_check = manual.review_item_to_pending_check(_review_item("revenue"))
    verified_check["manual_status"] = "verified"
    verified_check["expected_value"] = 100.0
    seed_check = {
        "origin": "seed_manual_check",
        "period": "2021Q1",
        "metric_code": "net_income",
        "expected_value": None,
        "expected_currency": "RUB",
        "expected_unit_multiplier": 1_000_000,
        "expected_period_type": "ytd",
        "expected_source_role": "financial_statements",
        "expected_source_location_contains": ["page", "table"],
        "manual_status": "pending",
    }
    monkeypatch.setattr(manual, "load_golden_checks", lambda path=None: {"checks": [verified_check, seed_check]})
    fact = _fact(db_session, "revenue", 100.0)
    report = manual.verify_golden_checks("2021Q1", "2021Q4", facts=[fact], persist=False)
    assert report["review_pack_status"] == "PASS"
    assert report["review_pack_verified"] == 1
    assert report["review_pack_accuracy"] == 1.0
    assert report["full_golden_dataset_status"] == "IN_PROGRESS"
    assert report["pending_count"] == 1


def test_checklist_markdown_is_generated():
    checklist_path = _test_dir() / "checklist.md"
    golden = {"checks": [manual.review_item_to_pending_check(_review_item("revenue"))]}
    path = manual.write_manual_checklist(golden, checklist_path)
    text = path.read_text(encoding="utf-8")
    assert "# LKOH 2021 Manual Fact Review Checklist" in text
    assert "actual_source_url" in text
    assert "| 1 | https://www.lukoil.com/report.pdf | 1 | 2021Q1 | revenue |" in text


def test_checklist_puts_seed_checks_in_separate_section():
    checklist_path = _test_dir() / "checklist.md"
    seed = {
        "period": "2021Q1",
        "metric_code": "revenue",
        "expected_unit_multiplier": 1_000_000,
        "expected_period_type": "ytd",
        "manual_status": "pending",
    }
    path = manual.write_manual_checklist({"checks": [seed]}, checklist_path)
    text = path.read_text(encoding="utf-8")
    assert "## Seed checks without extracted fact" in text
    assert "| 1 | 2021Q1 | revenue | None |" in text


def test_checklist_includes_source_references_for_derived_fact():
    checklist_path = _test_dir() / "checklist.md"
    item = _review_item("total_debt")
    item["source_references"] = [
        {
            "metric_code": "short_term_borrowings",
            "value": 10,
            "source_url": "https://www.lukoil.com/report.pdf",
            "document_id": 1,
            "period_type": "balance_sheet_snapshot",
            "source_location": "page 4, table statement_text, line 20",
            "raw_label": "Short-term borrowings",
        }
    ]
    golden = {"checks": [manual.review_item_to_pending_check(item)]}
    path = manual.write_manual_checklist(golden, checklist_path)
    text = path.read_text(encoding="utf-8")
    assert "source_reference" in text
    assert "short_term_borrowings" in text
