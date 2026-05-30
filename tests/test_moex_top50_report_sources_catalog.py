import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "reference" / "moex_top50_report_sources.csv"
JSON_PATH = ROOT / "data" / "reference" / "moex_top50_report_sources.json"

REQUIRED_FIELDS = {
    "rank",
    "ticker",
    "company_name",
    "sector",
    "issuer_website",
    "investor_relations_url",
    "reports_url",
    "edisclosure_url",
    "reporting_standards",
    "document_types",
    "suitable_for_analysis",
    "source_trust_level",
    "last_verified_at",
    "verification_status",
    "source_page_status",
    "preferred_acquisition_method",
    "notes",
}

TRUSTED_LEVELS = {
    "official issuer",
    "official disclosure",
    "regulated disclosure",
    "needs_review",
}

UNTRUSTED_URL_MARKERS = {
    "example.com",
    "fixture",
    "localhost",
    "127.0.0.1",
    "wikipedia.org",
}


def _csv_rows() -> list[dict[str, str]]:
    with CSV_PATH.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _json_payload() -> dict:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def test_catalog_files_exist_and_have_expected_shape() -> None:
    assert CSV_PATH.exists()
    assert JSON_PATH.exists()

    rows = _csv_rows()
    payload = _json_payload()

    assert len(rows) == 50
    assert payload["companies_count"] == 50
    assert len(payload["companies"]) == 50
    assert set(rows[0]) == REQUIRED_FIELDS


def test_catalog_has_50_unique_ranked_tickers_in_csv_and_json() -> None:
    rows = _csv_rows()
    payload = _json_payload()

    csv_tickers = [row["ticker"] for row in rows]
    json_tickers = [row["ticker"] for row in payload["companies"]]
    ranks = [int(row["rank"]) for row in rows]

    assert len(set(csv_tickers)) == 50
    assert csv_tickers == json_tickers
    assert ranks == list(range(1, 51))


def test_required_fields_are_populated_and_urls_are_trusted_https() -> None:
    for row in _csv_rows():
        for field in REQUIRED_FIELDS:
            assert row[field], f"{row['ticker']} missing {field}"

        for field in ("issuer_website", "investor_relations_url", "reports_url", "edisclosure_url"):
            value = row[field]
            assert value.startswith("https://"), f"{row['ticker']} {field} must be https"
            assert not any(marker in value.lower() for marker in UNTRUSTED_URL_MARKERS)

        assert row["source_trust_level"] in TRUSTED_LEVELS
        assert row["verification_status"] in {"verified_known_pipeline_source", "needs_manual_review"}


def test_suitable_rows_have_report_or_disclosure_source_and_supported_documents() -> None:
    for row in _csv_rows():
        if row["suitable_for_analysis"].lower() != "true":
            continue

        assert row["reports_url"].startswith("https://")
        assert row["edisclosure_url"].startswith("https://disclosure.skrin.ru/")
        assert "financial_statements" in row["document_types"]
        assert row["reporting_standards"] in {"IFRS", "RAS", "both", "unclear"}


def test_json_payload_records_catalog_trust_policy() -> None:
    payload = _json_payload()
    trust_policy = payload["trust_policy"]
    acquisition_policy = payload["acquisition_policy"]

    assert "official issuer IR/reporting pages" in trust_policy["trusted_primary_sources"]
    assert "fixture data" in trust_policy["excluded_as_trusted_sources"]
    assert trust_policy["moex_iss_role"] == (
        "company identity and market metadata only, not financial statement source"
    )
    assert acquisition_policy["document_must_pass_validation"] is True
    assert acquisition_policy["no_fact_persistence_in_catalog_check"] is True


def test_catalog_records_acquisition_status_and_next_method() -> None:
    statuses = {row["source_page_status"] for row in _csv_rows()}
    methods = {row["preferred_acquisition_method"] for row in _csv_rows()}

    assert "reachable_no_static_direct_links" in statuses
    assert "stale_or_needs_update" in statuses
    assert "issuer_specific_js_or_embedded_json_adapter" in methods
    assert "update_source_page_then_direct_download" in methods
