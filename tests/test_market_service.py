import httpx
import pandas as pd
import pytest

from app.services.market.moex_client import MoexClient, MoexClientError


def test_moex_client_parses_mocked_json(monkeypatch):
    payload = {
        "candles": {
            "columns": ["begin", "open", "high", "low", "close", "volume", "value"],
            "data": [["2025-01-01 00:00:00", 1, 2, 1, 2, 100, 200]],
        }
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    df = MoexClient(retries=0).get_security_candles("LKOH", "TQBR", "2025-01-01", "2025-01-02")
    assert len(df) == 1
    assert df.iloc[0]["close"] == 2
    assert df.attrs["provider_columns"] == ["begin", "open", "high", "low", "close", "volume", "value"]
    assert "securities/LKOH/candles.json" in df.attrs["provider_endpoint"]


def test_moex_network_error_is_controlled(monkeypatch):
    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with pytest.raises(MoexClientError):
        MoexClient(retries=0).get_security_history("LKOH", "TQBR", "2025-01-01", "2025-01-02")


def test_moex_empty_candles(monkeypatch):
    payload = {
        "candles": {
            "columns": ["begin", "open", "high", "low", "close", "volume", "value"],
            "data": [],
        }
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    df = MoexClient(retries=0).get_security_candles("LKOH", "TQBR", "2025-01-01", "2025-01-02")
    assert df.empty
    assert {"date", "open", "high", "low", "close", "volume", "value"} <= set(df.columns)


def test_moex_malformed_json_is_controlled(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("bad json")

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with pytest.raises(MoexClientError):
        MoexClient(retries=0).get_security_candles("LKOH", "TQBR", "2025-01-01", "2025-01-02")


def test_moex_http_error_is_controlled(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("500", request=httpx.Request("GET", "https://x"), response=httpx.Response(500))

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with pytest.raises(MoexClientError):
        MoexClient(retries=0).get_security_candles("LKOH", "TQBR", "2025-01-01", "2025-01-02")


def test_moex_missing_columns_is_controlled(monkeypatch):
    payload = {"candles": {"columns": [], "data": []}}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with pytest.raises(MoexClientError):
        MoexClient(retries=0).get_security_candles("LKOH", "TQBR", "2025-01-01", "2025-01-02")


def test_moex_client_pages_candles_until_range_end(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    calls: list[dict] = []

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params):
            calls.append(dict(params))
            start = int(params.get("start", 0))
            if start == 0:
                rows = [
                    [f"{day.date()} 00:00:00", 1, 2, 1, 2, 100 + index, 200 + index]
                    for index, day in enumerate(pd.bdate_range("2025-01-01", periods=500))
                ]
            elif start == 500:
                rows = [
                    [f"{day.date()} 00:00:00", 1, 2, 1, 2, 600 + index, 700 + index]
                    for index, day in enumerate(pd.bdate_range("2026-12-02", periods=5))
                ]
            else:
                rows = []
            return FakeResponse(
                {
                    "candles": {
                        "columns": ["begin", "open", "high", "low", "close", "volume", "value"],
                        "data": rows,
                    }
                }
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    df = MoexClient(retries=0).get_security_candles("LKOH", "TQBR", "2025-01-01", "2025-06-05")

    assert len(df) == 505
    assert calls[0]["start"] == 0
    assert calls[1]["start"] == 500
