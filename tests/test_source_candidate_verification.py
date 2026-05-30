import httpx

from app.core.config import get_settings
from app.tools import verify_source_candidate as candidate


class FakeClient:
    def __init__(self, content: bytes, content_type: str = "application/pdf", status_code: int = 200):
        self.content = content
        self.content_type = content_type
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _url):
        return httpx.Response(
            self.status_code,
            content=self.content,
            headers={"content-type": self.content_type},
            request=httpx.Request("GET", "https://old.tatneft.ru/report.pdf"),
        )


def patch_client(monkeypatch, content: bytes, content_type: str = "application/pdf"):
    monkeypatch.setattr(candidate.httpx, "Client", lambda **_kwargs: FakeClient(content, content_type))


def test_candidate_rejected_if_non_allowlisted_domain():
    get_settings().report_source_allowed_domains = ["old.tatneft.ru"]
    report = candidate.verify_candidate("https://example.com/report.pdf", "TATN", min_size_bytes=10)

    assert report["status"] == "FAIL"
    assert report["failure_reason"] == "non_allowlisted_domain"


def test_candidate_rejected_if_not_pdf(monkeypatch):
    get_settings().report_source_allowed_domains = ["old.tatneft.ru"]
    patch_client(monkeypatch, b"<html>not pdf but long enough</html>" * 5, "text/html")
    report = candidate.verify_candidate("https://old.tatneft.ru/report.html", "TATN", min_size_bytes=10)

    assert report["status"] == "FAIL"
    assert report["failure_reason"] == "not_pdf"


def test_candidate_rejected_if_required_text_markers_absent(monkeypatch):
    get_settings().report_source_allowed_domains = ["old.tatneft.ru"]
    patch_client(monkeypatch, b"%PDF fake content" * 10)
    monkeypatch.setattr(candidate, "extract_first_pages_text", lambda _content: "Tatneft random document 2021")
    report = candidate.verify_candidate("https://old.tatneft.ru/report.pdf", "TATN", min_size_bytes=10)

    assert report["status"] == "FAIL"
    assert "required_text_markers_absent" in report["failure_reason"]


def test_candidate_accepted_if_pdf_markers_present(monkeypatch):
    get_settings().report_source_allowed_domains = ["old.tatneft.ru"]
    patch_client(monkeypatch, b"%PDF fake content" * 10)
    monkeypatch.setattr(
        candidate,
        "extract_first_pages_text",
        lambda _content: (
            "Tatneft Group IFRS consolidated financial statements 2021 "
            "year ended 31 December 2021 audited"
        ),
    )
    report = candidate.verify_candidate("https://old.tatneft.ru/report.pdf", "TATN", min_size_bytes=10)

    assert report["status"] == "PASS"
    assert report["sha256"]
