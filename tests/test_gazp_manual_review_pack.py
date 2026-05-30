import json

from app.core.config import get_settings
from app.tools.generate_manual_review_pack import generate_pack, pack_path


def report_file(period_from: str, period_to: str):
    root = get_settings().root_dir / "data" / "validation" / "GAZP"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{period_from}_{period_to}_real_validation_report.json"


def test_gazp_manual_review_pack_generated_only_when_canonical_facts_exist():
    period_from = "2098Q1"
    period_to = "2098Q1"
    path = report_file(period_from, period_to)
    path.write_text(
        json.dumps(
            {
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
                        "source_url": "https://www.gazprom.com/report.pdf",
                        "source_document_id": "doc-1",
                        "source_location": "page 14, table statement_text",
                        "raw_label": "Total sales in the consolidated interim condensed statement of comprehensive income",
                        "confidence_score": 0.9,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        report = generate_pack("GAZP", period_from, period_to, limit=20)
    finally:
        path.unlink(missing_ok=True)
        pack_path("GAZP").unlink(missing_ok=True)

    assert report["facts_selected"] == 1
    assert report["items"][0]["manual_status"] == "pending"
    assert report["items"][0]["raw_label"].startswith("Total sales")


def test_gazp_manual_review_pack_selects_no_missing_facts():
    period_from = "2098Q2"
    period_to = "2098Q2"
    path = report_file(period_from, period_to)
    path.write_text(json.dumps({"fact_coverage": [{"period": period_from, "status": "missing"}]}), encoding="utf-8")
    try:
        report = generate_pack("GAZP", period_from, period_to, limit=20)
    finally:
        path.unlink(missing_ok=True)
        pack_path("GAZP").unlink(missing_ok=True)

    assert report["facts_selected"] == 0
