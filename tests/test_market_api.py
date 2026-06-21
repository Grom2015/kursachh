def test_market_technical_endpoint_returns_live_report(monkeypatch, client):
    monkeypatch.setattr(
        "app.api.routes.market.run_market_technical_report",
        lambda **kwargs: {
            "ticker": kwargs["ticker"],
            "board": kwargs["board"],
            "period_from": kwargs["period_from"],
            "period_to": kwargs["period_to"],
            "status": "PASS",
            "summary": {"candles_count": 123},
            "technical_indicators": [{"indicator_code": "ma_20", "value": 100}],
            "liquidity_metrics": [],
        },
    )

    response = client.get("/market/technical", params={"ticker": "vtbr"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticker"] == "VTBR"
    assert payload["status"] == "PASS"
    assert payload["summary"]["candles_count"] == 123


def test_market_candles_endpoint_returns_serialized_rows(monkeypatch, client):
    import pandas as pd

    monkeypatch.setattr(
        "app.api.routes.market.normalize_candles",
        lambda df: df,
    )

    monkeypatch.setattr(
        "app.api.routes.market.MoexClient.get_security_candles",
        lambda self, ticker, board, from_date, to_date: pd.DataFrame(
            [
                {
                    "date": pd.Timestamp("2025-01-10").date(),
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 1000,
                }
            ]
        ),
    )

    response = client.get("/market/candles", params={"ticker": "SBER", "period_from": "2025Q1", "period_to": "2025Q1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticker"] == "SBER"
    assert payload["count"] == 1
    assert payload["candles"][0]["date"] == "2025-01-10"
    assert payload["candles"][0]["close"] == 1.5
