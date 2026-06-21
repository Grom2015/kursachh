from datetime import datetime
from types import SimpleNamespace


def test_pdf_analysis_batch_endpoint_chains_create_and_augment(client, monkeypatch):
    calls = []

    class DummyService:
        def __init__(self, db):
            self.db = db

        def analyze(self, **kwargs):
            calls.append(("analyze", kwargs))
            return SimpleNamespace(
                id="result-1",
                version=1,
                parent_result_id=None,
                created_at=datetime(2026, 6, 21, 12, 0, 0),
                data_snapshot_json={"source": "pdf_upload"},
                result_json={"documents": [{"file_name": kwargs["original_filename"]}]},
            )

        def augment(self, **kwargs):
            calls.append(("augment", kwargs))
            return SimpleNamespace(
                id="result-2",
                version=2,
                parent_result_id="result-1",
                created_at=datetime(2026, 6, 21, 12, 1, 0),
                data_snapshot_json={"source": "pdf_augment"},
                result_json={"documents": [{"file_name": "one.pdf"}, {"file_name": kwargs["original_filename"]}]},
            )

    monkeypatch.setattr("app.api.routes.pdf_analysis.PdfAnalysisService", DummyService)

    response = client.post(
        "/pdf-analysis/batch",
        files=[
            ("files", ("one.pdf", b"%PDF-1.4 one", "application/pdf")),
            ("files", ("two.pdf", b"%PDF-1.4 two", "application/pdf")),
        ],
        data={"company_ticker": "X5", "reporting_standard": "IFRS"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "result-2"
    assert payload["analytics"]["batch_upload_count"] == 2
    assert payload["analytics"]["batch_uploaded_filenames"] == ["one.pdf", "two.pdf"]
    assert [name for name, _ in calls] == ["analyze", "augment"]


def test_pdf_analysis_staged_batch_endpoint_chains_create_and_augment(client, monkeypatch):
    calls = []

    class DummyService:
        def __init__(self, db):
            self.db = db

        def analyze(self, **kwargs):
            calls.append(("analyze", kwargs))
            return SimpleNamespace(
                id="result-1",
                version=1,
                parent_result_id=None,
                created_at=datetime(2026, 6, 21, 12, 0, 0),
                data_snapshot_json={"source": "pdf_upload"},
                result_json={"documents": [{"file_name": kwargs["original_filename"]}]},
            )

        def augment(self, **kwargs):
            calls.append(("augment", kwargs))
            return SimpleNamespace(
                id="result-2",
                version=2,
                parent_result_id="result-1",
                created_at=datetime(2026, 6, 21, 12, 1, 0),
                data_snapshot_json={"source": "pdf_augment"},
                result_json={"documents": [{"file_name": "one.pdf"}, {"file_name": kwargs["original_filename"]}]},
            )

    monkeypatch.setattr("app.api.routes.pdf_analysis.PdfAnalysisService", DummyService)

    first = client.post("/pdf-analysis/staged-file", files={"file": ("one.pdf", b"%PDF-1.4 one", "application/pdf")})
    second = client.post("/pdf-analysis/staged-file", files={"file": ("two.pdf", b"%PDF-1.4 two", "application/pdf")})
    assert first.status_code == 200
    assert second.status_code == 200

    response = client.post(
        "/pdf-analysis/staged-batch",
        json={
            "stage_ids": [first.json()["stage_id"], second.json()["stage_id"]],
            "company_ticker": "X5",
            "reporting_standard": "IFRS",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "result-2"
    assert payload["analytics"]["batch_upload_count"] == 2
    assert payload["analytics"]["batch_uploaded_filenames"] == ["one.pdf", "two.pdf"]
    assert [name for name, _ in calls] == ["analyze", "augment"]
