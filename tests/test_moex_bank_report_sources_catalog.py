import csv
import json
from pathlib import Path

from app.services.reports.report_source_catalog import ReportSourceCatalog

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "reference" / "moex_bank_report_sources.csv"
JSON_PATH = ROOT / "data" / "reference" / "moex_bank_report_sources.json"


def test_bank_catalog_files_exist_and_have_unique_tickers():
    assert CSV_PATH.exists()
    assert JSON_PATH.exists()

    rows = list(csv.DictReader(CSV_PATH.open("r", encoding="utf-8", newline="")))
    payload = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    tickers = [item["ticker"] for item in payload["companies"]]

    assert len(rows) == len(payload["companies"]) == 5
    assert len(set(tickers)) == 5
    assert {"SBER", "VTBR", "BSPB", "CBOM", "MOEX"} == set(tickers)


def test_sber_bank_catalog_has_type_2_3_4_edisclosure_urls():
    payload = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    sber = next(item for item in payload["companies"] if item["ticker"] == "SBER")

    assert sber["edisclosure_id"] == "3043"
    assert sber["files_type_4_url"] == "https://e-disclosure.ru/portal/files.aspx?id=3043&type=4"
    assert sber["files_type_3_url"] == "https://e-disclosure.ru/portal/files.aspx?id=3043&type=3"
    assert sber["files_type_2_url"] == "https://e-disclosure.ru/portal/files.aspx?id=3043&type=2"
    assert sber["manual_upload_hint"]


def test_source_catalog_surfaces_banking_policy_and_links():
    result = ReportSourceCatalog(root=ROOT).source_for_ticker("SBER")

    assert result["found"] is True
    assert result["banking_policy"]["is_bank"] is True
    assert result["banking_policy"]["do_not_use_industrial_metrics"] is True
    links = result["manual_upload_guidance"]["recommended_links"]
    assert any(link["url"].endswith("id=3043&type=4") for link in links)


def test_unknown_bank_identifier_resolution_is_explicit():
    payload = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    cbom = next(item for item in payload["companies"] if item["ticker"] == "CBOM")

    assert cbom["source_status"] == "needs_edisclosure_id_resolution"
    assert cbom["files_type_4_url"] is None
