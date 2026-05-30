import asyncio

import httpx

from app.core.config import get_settings
from app.db.models import Company
from app.services.reports.adapters.lkoh_ir_adapter import LKOHIRAdapter


class _FakeResponse:
    def __init__(self, text="", error=None):
        self.text = text
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error


def test_lkoh_adapter_parses_mocked_html(monkeypatch):
    settings = get_settings()
    settings.lkoh_ir_reports_url = "https://www.lukoil.ru/reports"
    settings.report_source_allowed_domains = ["www.lukoil.ru"]

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url):
            return _FakeResponse(
                '<a href="/files/ifrs-financial-results-2021q1.pdf">IFRS financial results 2021Q1</a>'
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    company = Company(ticker="LKOH", board="TQBR", short_name="LKOH", full_name="LKOH")
    reports = asyncio.run(LKOHIRAdapter().discover_reports(company, "2021Q1", "2021Q1", "IFRS"))
    assert len(reports) == 1
    assert reports[0].source_url == "https://www.lukoil.ru/files/ifrs-financial-results-2021q1.pdf"


def test_lkoh_adapter_ignores_non_allowlisted_domains(monkeypatch):
    settings = get_settings()
    settings.lkoh_ir_reports_url = "https://www.lukoil.ru/reports"
    settings.report_source_allowed_domains = ["www.lukoil.ru"]

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url):
            return _FakeResponse(
                '<a href="https://evil.example/ifrs-financial-results-2021q1.pdf">IFRS financial results 2021Q1</a>'
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    company = Company(ticker="LKOH", board="TQBR", short_name="LKOH", full_name="LKOH")
    reports = asyncio.run(LKOHIRAdapter().discover_reports(company, "2020Q1", "2020Q1", "IFRS"))
    assert reports == []


def test_lkoh_adapter_returns_empty_on_http_failure(monkeypatch):
    settings = get_settings()
    settings.lkoh_ir_reports_url = "https://www.lukoil.ru/reports"
    settings.report_source_allowed_domains = ["www.lukoil.ru"]

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    adapter = LKOHIRAdapter()
    company = Company(ticker="LKOH", board="TQBR", short_name="LKOH", full_name="LKOH")
    reports = asyncio.run(adapter.discover_reports(company, "2020Q1", "2020Q1", "IFRS"))
    assert reports == []
    assert adapter.warnings
