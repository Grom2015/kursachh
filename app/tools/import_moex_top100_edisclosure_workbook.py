from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class TickerLinkRow:
    rank: int
    ticker: str
    company_name: str
    share_type: str | None
    edisclosure_company_id: str
    edisclosure_url: str
    moex_source_url: str | None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def import_workbook(workbook_path: str | Path, root: Path | None = None) -> dict[str, Any]:
    base_root = (root or Path.cwd()).resolve()
    source = Path(workbook_path).resolve()
    frame = pd.read_excel(source, sheet_name="100 бумаг", header=4)
    frame = frame.rename(
        columns={
            "№": "rank",
            "Тикер": "ticker",
            "Эмитент / наименование на Мосбирже": "company_name",
            "Тип акций": "share_type",
            "ID e-disclosure": "edisclosure_company_id",
            "Прямая ссылка e-disclosure": "edisclosure_url",
            "Источник состава MOEX": "moex_source_url",
        }
    )
    frame = frame[frame["ticker"].notna()].copy()

    items: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        ticker = _clean(row.get("ticker"))
        company_id = _clean(row.get("edisclosure_company_id"))
        company_url = _clean(row.get("edisclosure_url"))
        if not ticker or not company_id or not company_url:
            continue
        item = TickerLinkRow(
            rank=int(row.get("rank") or 0),
            ticker=ticker.upper(),
            company_name=_clean(row.get("company_name")) or ticker.upper(),
            share_type=_clean(row.get("share_type")),
            edisclosure_company_id=company_id,
            edisclosure_url=company_url,
            moex_source_url=_clean(row.get("moex_source_url")),
        )
        items.append(asdict(item))

    out_dir = base_root / "data" / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "moex_top100_edisclosure_registry.json"
    csv_path = out_dir / "moex_top100_edisclosure_registry.csv"

    payload = {
        "registry_type": "moex_top100_edisclosure_registry",
        "as_of_date": "2026-06-19",
        "source_workbook": str(source),
        "sheets_used": ["100 бумаг"],
        "items_count": len(items),
        "items": items,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(items).to_csv(csv_path, index=False, encoding="utf-8")
    return {
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "items_count": len(items),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Import MOEX top-100 e-disclosure workbook into project reference files.")
    parser.add_argument("workbook_path", help="Path to moex_top100_edisclosure_*.xlsx")
    args = parser.parse_args()
    result = import_workbook(args.workbook_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
