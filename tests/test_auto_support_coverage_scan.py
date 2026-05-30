from app.db.models import Company
from app.tools import scan_auto_support_coverage as scan


class FakeSessionLocal:
    def __init__(self, companies):
        self.companies = companies

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def scalars(self, _stmt):
        return self

    def all(self):
        return self.companies


def _company(ticker):
    return Company(ticker=ticker, board="TQBR", short_name=ticker, full_name=ticker, is_active=True)


def test_coverage_scan_counts_statuses_and_supported_outputs(monkeypatch):
    monkeypatch.setattr(scan, "init_db", lambda: None)
    monkeypatch.setattr(scan, "SessionLocal", FakeSessionLocal([_company("AAA"), _company("BBB")]))
    monkeypatch.setattr(scan, "save_report", lambda report, period_from: None)
    monkeypatch.setattr(
        scan,
        "load_validation_report",
        lambda ticker, *_args: {
            "automated_support_status": {
                "status": "AUTO_PARTIAL" if ticker == "AAA" else "PARSER_BLOCKED",
                "automated_quality_score": 0.5,
                "blockers": ["parser missing"] if ticker == "BBB" else [],
                "warnings": [],
                "supported_outputs": {"financial_facts": ticker == "AAA"},
            }
        },
    )

    report = scan.scan_auto_support_coverage("2021Q1", "2021Q4")

    assert report["companies_scanned"] == 2
    assert report["status_counts"]["AUTO_PARTIAL"] == 1
    assert report["status_counts"]["PARSER_BLOCKED"] == 1


def test_coverage_scan_continues_on_company_errors(monkeypatch):
    monkeypatch.setattr(scan, "init_db", lambda: None)
    monkeypatch.setattr(scan, "SessionLocal", FakeSessionLocal([_company("AAA")]))
    monkeypatch.setattr(scan, "save_report", lambda report, period_from: None)
    monkeypatch.setattr(scan, "company_row", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))

    report = scan.scan_auto_support_coverage("2021Q1", "2021Q4")

    assert report["status_counts"]["UNSUPPORTED"] == 1
    assert "Coverage scan failed for company: boom" in report["companies"][0]["blockers"]
