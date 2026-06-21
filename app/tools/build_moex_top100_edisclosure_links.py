from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import requests

from app.core.config import get_settings
from app.services.reports.report_source_catalog import (
    EDISCLOSURE_COMPANY_IDS,
    EDISCLOSURE_RUSSIAN_SEARCH_NAMES,
)

MOEX_TQBR_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json"
)
EDISCLOSURE_SEARCH_URL = "https://www.e-disclosure.ru/poisk-po-kompaniyam"
EDISCLOSURE_PORTAL_BASE = "https://www.e-disclosure.ru/portal"


@dataclass
class TopCompanyLinkRow:
    rank: int
    ticker: str
    company_name: str
    issuecapitalization_rub: float
    listlevel: int | None
    e_disclosure_company_id: str | None
    e_disclosure_company_url: str | None
    e_disclosure_files_type_4_url: str | None
    e_disclosure_files_type_3_url: str | None
    e_disclosure_files_type_2_url: str | None
    e_disclosure_search_url: str
    e_disclosure_search_terms: list[str]
    link_status: str
    source_board: str = "TQBR"
    source_universe: str = "top100_by_issuecapitalization_common_shares"
    captured_on: str = date.today().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build MOEX top-100 e-disclosure links catalog."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="How many MOEX TQBR ordinary-share issuers to include.",
    )
    args = parser.parse_args()

    root = get_settings().root_dir.resolve()
    out_dir = root / "data" / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = build_top100_rows(limit=args.limit)

    csv_path = out_dir / "moex_top100_edisclosure_links.csv"
    json_path = out_dir / "moex_top100_edisclosure_links.json"

    write_csv(csv_path, rows)
    write_json(json_path, rows)

    print(
        json.dumps(
            {
                "companies_count": len(rows),
                "csv_path": str(csv_path),
                "json_path": str(json_path),
                "source": MOEX_TQBR_URL,
            },
            ensure_ascii=False,
        )
    )
    return 0


def build_top100_rows(limit: int = 100) -> list[TopCompanyLinkRow]:
    payload = requests.get(MOEX_TQBR_URL, timeout=30).json()
    sec_cols = payload["securities"]["columns"]
    md_cols = payload["marketdata"]["columns"]

    rows: list[dict[str, Any]] = []
    for sec_row, market_row in zip(
        payload["securities"]["data"], payload["marketdata"]["data"], strict=False
    ):
        sec = dict(zip(sec_cols, sec_row, strict=False))
        market = dict(zip(md_cols, market_row, strict=False))
        if sec.get("SECTYPE") != "1":
            continue
        cap = market.get("ISSUECAPITALIZATION")
        if cap in (None, 0):
            continue
        rows.append(
            {
                "ticker": str(sec.get("SECID") or "").upper(),
                "company_name": str(sec.get("SECNAME") or sec.get("SHORTNAME") or "").strip(),
                "issuecapitalization_rub": float(cap),
                "listlevel": sec.get("LISTLEVEL"),
            }
        )

    rows.sort(key=lambda item: item["issuecapitalization_rub"], reverse=True)
    rows = rows[:limit]

    return [
        TopCompanyLinkRow(
            rank=index,
            ticker=item["ticker"],
            company_name=item["company_name"],
            issuecapitalization_rub=item["issuecapitalization_rub"],
            listlevel=_as_int(item.get("listlevel")),
            e_disclosure_company_id=company_id,
            e_disclosure_company_url=(
                f"{EDISCLOSURE_PORTAL_BASE}/company.aspx?id={company_id}"
                if company_id
                else None
            ),
            e_disclosure_files_type_4_url=(
                f"{EDISCLOSURE_PORTAL_BASE}/files.aspx?id={company_id}&type=4"
                if company_id
                else None
            ),
            e_disclosure_files_type_3_url=(
                f"{EDISCLOSURE_PORTAL_BASE}/files.aspx?id={company_id}&type=3"
                if company_id
                else None
            ),
            e_disclosure_files_type_2_url=(
                f"{EDISCLOSURE_PORTAL_BASE}/files.aspx?id={company_id}&type=2"
                if company_id
                else None
            ),
            e_disclosure_search_url=EDISCLOSURE_SEARCH_URL,
            e_disclosure_search_terms=search_terms,
            link_status="known_company_id" if company_id else "search_only",
        )
        for index, item in enumerate(rows, start=1)
        for company_id, search_terms in [edisclosure_lookup_payload(item["ticker"], item["company_name"])]
    ]


def edisclosure_lookup_payload(
    ticker: str, company_name: str
) -> tuple[str | None, list[str]]:
    company_id = EDISCLOSURE_COMPANY_IDS.get(ticker.upper())
    terms: list[str] = []
    for value in EDISCLOSURE_RUSSIAN_SEARCH_NAMES.get(ticker.upper(), []):
        normalized = str(value).strip()
        if normalized and normalized not in terms:
            terms.append(normalized)
    for value in (ticker.upper(), company_name.strip()):
        if value and value not in terms:
            terms.append(value)
    return company_id, terms


def write_csv(path: Path, rows: list[TopCompanyLinkRow]) -> None:
    fieldnames = list(asdict(rows[0]).keys()) if rows else list(TopCompanyLinkRow.__annotations__.keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            item = asdict(row)
            item["e_disclosure_search_terms"] = " | ".join(item["e_disclosure_search_terms"])
            writer.writerow(item)


def write_json(path: Path, rows: list[TopCompanyLinkRow]) -> None:
    payload = {
        "catalog_name": "moex_top100_edisclosure_links",
        "captured_on": date.today().isoformat(),
        "source_board": "TQBR",
        "source_metric": "ISSUECAPITALIZATION",
        "source_url": MOEX_TQBR_URL,
        "edisclosure_search_url": EDISCLOSURE_SEARCH_URL,
        "note": (
            "Entries are top-100 TQBR ordinary-share issuers by current MOEX issue capitalization. "
            "Known e-disclosure company IDs are included when available; otherwise use the search URL "
            "with the provided search terms because the public site is bot-protected."
        ),
        "companies": [asdict(row) for row in rows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
