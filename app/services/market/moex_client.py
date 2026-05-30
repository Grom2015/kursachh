from typing import Any

import httpx
import pandas as pd

from app.core.config import get_settings


class MoexClientError(RuntimeError):
    pass


class MoexClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0, retries: int = 2):
        self.base_url = (base_url or get_settings().moex_base_url).rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def get_security_candles(
        self, ticker: str, board: str, from_date: str, to_date: str, interval: int = 24
    ) -> pd.DataFrame:
        url = (
            f"{self.base_url}/engines/stock/markets/shares/boards/{board}/"
            f"securities/{ticker}/candles.json"
        )
        return self._request_table(
            url,
            {"from": from_date, "till": to_date, "interval": interval},
            table="candles",
        )

    def get_security_history(self, ticker: str, board: str, from_date: str, to_date: str) -> pd.DataFrame:
        url = (
            f"{self.base_url}/history/engines/stock/markets/shares/boards/{board}/"
            f"securities/{ticker}.json"
        )
        return self._request_table(url, {"from": from_date, "till": to_date}, table="history")

    def _request_table(self, url: str, params: dict[str, Any], table: str) -> pd.DataFrame:
        last_exc: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(url, params={**params, "iss.meta": "off"})
                response.raise_for_status()
                df = self._parse_table(response.json(), table)
                df.attrs["provider_endpoint"] = url
                df.attrs["provider_params"] = {**params, "iss.meta": "off"}
                return df
            except Exception as exc:
                last_exc = exc
        raise MoexClientError(f"MOEX ISS request failed: {last_exc}") from last_exc

    def _parse_table(self, payload: dict[str, Any], table: str) -> pd.DataFrame:
        block = payload.get(table) or {}
        columns = block.get("columns") or []
        data = block.get("data") or []
        if not columns:
            raise MoexClientError(f"MOEX ISS response does not contain table {table}")
        df = pd.DataFrame(data, columns=[str(c).lower() for c in columns])
        df.attrs["provider_columns"] = [str(c) for c in columns]
        rename = {"begin": "date", "tradedate": "date", "value": "value"}
        df = df.rename(columns=rename)
        for col in ["open", "high", "low", "close", "volume", "value"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df
