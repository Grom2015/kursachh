from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.sectors.banking_policy import bank_catalog_path

EDISCLOSURE_RUSSIAN_SEARCH_NAMES = {
    "SBER": ['ПАО "Сбербанк"', "Сбербанк"],
    "GAZP": ['ПАО "Газпром"', "Газпром"],
    "LKOH": ['ПАО "ЛУКОЙЛ"', "ЛУКОЙЛ"],
    "ROSN": ['ПАО "НК Роснефть"', "Роснефть"],
    "NVTK": ['ПАО "НОВАТЭК"', "НОВАТЭК"],
    "GMKN": ['ПАО "ГМК Норильский никель"', "Норильский никель"],
    "TATN": ['ПАО "Татнефть"', "Татнефть"],
    "SIBN": ['ПАО "Газпром нефть"', "Газпром нефть"],
    "SNGS": ['ПАО "Сургутнефтегаз"', "Сургутнефтегаз"],
    "PLZL": ['ПАО "Полюс"', "Полюс"],
    "YDEX": ["Яндекс"],
    "MGNT": ['ПАО "Магнит"', "Магнит"],
    "FIVE": ["X5", "Икс 5"],
    "MOEX": ['ПАО "Московская Биржа"', "Московская Биржа"],
    "VTBR": ['Банк ВТБ (ПАО)', "ВТБ"],
    "ALRS": ['АК "АЛРОСА" (ПАО)', "АЛРОСА"],
    "NLMK": ['ПАО "НЛМК"', "НЛМК"],
    "CHMF": ['ПАО "Северсталь"', "Северсталь"],
    "MAGN": ['ПАО "ММК"', "ММК"],
    "RUAL": ['МКПАО "РУСАЛ"', "РУСАЛ"],
    "ENPG": ["ЭН+ ГРУП", "En+ Group"],
    "PHOR": ['ПАО "ФосАгро"', "ФосАгро"],
    "ACRN": ['ПАО "Акрон"', "Акрон"],
    "AFLT": ['ПАО "Аэрофлот"', "Аэрофлот"],
    "MTSS": ['ПАО "МТС"', "МТС"],
    "RTKM": ['ПАО "Ростелеком"', "Ростелеком"],
    "FEES": ['ПАО "Россети"', "Россети"],
    "HYDR": ['ПАО "РусГидро"', "РусГидро"],
    "IRAO": ['ПАО "Интер РАО"', "Интер РАО"],
    "TRNFP": ['ПАО "Транснефть"', "Транснефть"],
    "MSNG": ['ПАО "Мосэнерго"', "Мосэнерго"],
    "OGKB": ['ПАО "ОГК-2"', "ОГК-2"],
    "UPRO": ['ПАО "Юнипро"', "Юнипро"],
    "PIKK": ['ПАО "ПИК"', "ПИК"],
    "SMLT": ['ПАО "ГК Самолет"', "Самолет"],
    "LSRG": ['ПАО "Группа ЛСР"', "Группа ЛСР"],
    "AFKS": ['ПАО АФК "Система"', "Система"],
    "MVID": ['ПАО "М.видео"', "М.Видео"],
    "OZON": ["Ozon", "Озон"],
    "VKCO": ["ВК", "VK"],
    "POSI": ['ПАО "Группа Позитив"', "Positive Technologies"],
    "ASTR": ['ПАО "Группа Астра"', "Группа Астра"],
    "HEAD": ["HeadHunter", "Хэдхантер"],
    "CBOM": ['ПАО "МОСКОВСКИЙ КРЕДИТНЫЙ БАНК"', "МКБ"],
    "BSPB": ['ПАО "Банк Санкт-Петербург"', "Банк Санкт-Петербург"],
    "RASP": ['ПАО "Распадская"', "Распадская"],
    "MTLR": ['ПАО "Мечел"', "Мечел"],
    "KMAZ": ['ПАО "КАМАЗ"', "КАМАЗ"],
    "FLOT": ['ПАО "Совкомфлот"', "Совкомфлот"],
    "NMTP": ['ПАО "НМТП"', "НМТП"],
}

EDISCLOSURE_COMPANY_IDS = {
    "SBER": "3043",
    "GAZP": "934",
    "LKOH": "17",
    "ROSN": "6505",
    "NVTK": "225",
    "GMKN": "564",
    "TATN": "118",
    "SIBN": "347",
    "SNGS": "312",
    "PLZL": "12430",
    "MOEX": "43",
    "BSPB": "3935",
}


@dataclass
class ReportSourceCatalog:
    root: Path | None = None

    def __post_init__(self) -> None:
        self.root = (self.root or get_settings().root_dir).resolve()
        self.catalog_path = self.root / "data" / "reference" / "moex_top50_report_sources.json"
        self.bank_catalog_path = bank_catalog_path(self.root)

    def all_sources(self) -> dict[str, Any]:
        payload = self._load()
        bank_payload = self._load_bank_catalog()
        return {
            "catalog_path": self._relative(self.catalog_path),
            "bank_catalog_path": self._relative(self.bank_catalog_path) if self.bank_catalog_path.exists() else None,
            "companies_count": payload.get("companies_count", 0),
            "universe": payload.get("universe"),
            "trust_policy": payload.get("trust_policy", {}),
            "acquisition_policy": payload.get("acquisition_policy", {}),
            "items": [self._public_item(item) for item in payload.get("companies", [])],
            "banking_items": bank_payload.get("companies", []),
        }

    def source_for_ticker(self, ticker: str) -> dict[str, Any]:
        ticker = ticker.upper()
        payload = self._load()
        for item in payload.get("companies", []):
            if str(item.get("ticker", "")).upper() == ticker:
                bank_item = self._bank_item(ticker)
                return {
                    "found": True,
                    "catalog_path": self._relative(self.catalog_path),
                    "bank_catalog_path": self._relative(self.bank_catalog_path) if bank_item else None,
                    "item": self._public_item(item),
                    "banking_policy": self._banking_policy_payload(bank_item),
                    "manual_upload_guidance": self._manual_upload_guidance(item, bank_item),
                }
        return {
            "found": False,
            "ticker": ticker,
            "catalog_path": self._relative(self.catalog_path),
            "manual_upload_guidance": {
                **self._edisclosure_manual_upload_guidance(ticker),
                "status": "ticker_not_in_top50_catalog",
                "recommended_action": "use_edisclosure_search_then_manual_upload",
            },
        }

    def _load(self) -> dict[str, Any]:
        return json.loads(self.catalog_path.read_text(encoding="utf-8"))

    def _load_bank_catalog(self) -> dict[str, Any]:
        if not self.bank_catalog_path.exists():
            return {"companies": []}
        return json.loads(self.bank_catalog_path.read_text(encoding="utf-8"))

    def _bank_item(self, ticker: str) -> dict[str, Any] | None:
        for item in self._load_bank_catalog().get("companies", []):
            if str(item.get("ticker", "")).upper() == ticker.upper():
                return item
        return None

    def _public_item(self, item: dict[str, Any]) -> dict[str, Any]:
        ticker = str(item.get("ticker") or "")
        return {
            "rank": item.get("rank"),
            "ticker": item.get("ticker"),
            "company_name": item.get("company_name"),
            "sector": item.get("sector"),
            "issuer_website": item.get("issuer_website"),
            "investor_relations_url": item.get("investor_relations_url"),
            "reports_url": item.get("reports_url"),
            "edisclosure_url": item.get("edisclosure_url"),
            "edisclosure_search_url": self._edisclosure_search_url(ticker),
            "edisclosure_company_id": EDISCLOSURE_COMPANY_IDS.get(ticker.upper()),
            "edisclosure_direct_links": self._edisclosure_direct_links(ticker),
            "edisclosure_search_terms": self._edisclosure_search_terms(ticker, str(item.get("company_name") or "")),
            "reporting_standards": item.get("reporting_standards"),
            "document_types": item.get("document_types"),
            "suitable_for_analysis": item.get("suitable_for_analysis"),
            "source_trust_level": item.get("source_trust_level"),
            "verification_status": item.get("verification_status"),
            "source_page_status": item.get("source_page_status"),
            "preferred_acquisition_method": item.get("preferred_acquisition_method"),
            "notes": item.get("notes"),
        }

    def _banking_policy_payload(self, bank_item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not bank_item:
            return None
        return {
            "is_bank": bool(bank_item.get("is_bank")),
            "source_status": bank_item.get("source_status"),
            "preferred_document_type": bank_item.get("preferred_document_type"),
            "manual_upload_hint": bank_item.get("manual_upload_hint"),
            "do_not_use_industrial_metrics": bool(bank_item.get("is_bank")),
        }

    def _manual_upload_guidance(self, item: dict[str, Any], bank_item: dict[str, Any] | None = None) -> dict[str, Any]:
        ticker = str(item.get("ticker") or "")
        company_name = str(item.get("company_name") or "")
        guidance = self._edisclosure_manual_upload_guidance(ticker, company_name)
        if bank_item:
            guidance["banking_manual_upload_hint"] = bank_item.get("manual_upload_hint")
            guidance["preferred_bank_document_type"] = bank_item.get("preferred_document_type")
            bank_links = [
                {
                    "label": "E-Disclosure consolidated financial statements",
                    "url": bank_item.get("files_type_4_url"),
                    "trust_level": "regulated disclosure portal",
                },
                {
                    "label": "E-Disclosure accounting financial statements",
                    "url": bank_item.get("files_type_3_url"),
                    "trust_level": "regulated disclosure portal",
                },
                {
                    "label": "E-Disclosure annual reports",
                    "url": bank_item.get("files_type_2_url"),
                    "trust_level": "regulated disclosure portal",
                },
                {
                    "label": "Official investor relations",
                    "url": bank_item.get("investor_relations_url"),
                    "trust_level": "official issuer",
                },
            ]
            existing_urls = {link.get("url") for link in guidance["recommended_links"]}
            guidance["recommended_links"] = [
                *[link for link in bank_links if link.get("url") and link.get("url") not in existing_urls],
                *guidance["recommended_links"],
            ]
        return guidance

    def _edisclosure_manual_upload_guidance(self, ticker: str, company_name: str = "") -> dict[str, Any]:
        return {
            "status": "manual_upload_supported",
            "document_to_download": (
                "Open E-Disclosure search, find the issuer by ticker/name, download official IFRS/RAS "
                "financial statements or an annual report that contains audited financial statements, then upload it here."
            ),
            "search_provider": "E-Disclosure / Interfax",
            "search_policy": (
                "The application does not mark downloaded files as verified official source packages. "
                "Every manually uploaded document must still pass DocumentValidator."
            ),
            "accepted_file_types": [".pdf", ".xlsx", ".html", ".htm", ".zip"],
            "not_supported": [".xls"],
            "trust_boundary": {
                "source_trust_bucket_after_validation": "manual_upload_validated",
                "official_source_verified": False,
                "source_package_ready_contribution": False,
            },
            "recommended_links": [
                *self._edisclosure_direct_links(ticker),
                {
                    "label": "E-Disclosure company search",
                    "url": self._edisclosure_search_url(ticker),
                    "trust_level": "regulated disclosure portal",
                },
                {
                    "label": "E-Disclosure portal",
                    "url": "https://www.e-disclosure.ru/",
                    "trust_level": "regulated disclosure portal",
                },
            ],
            "search_terms": self._edisclosure_search_terms(ticker, company_name),
        }

    def _edisclosure_search_url(self, query: str) -> str:
        return "https://e-disclosure.ru/poisk-po-kompaniyam"

    def _edisclosure_direct_links(self, ticker: str) -> list[dict[str, str]]:
        company_id = EDISCLOSURE_COMPANY_IDS.get(ticker.upper())
        if not company_id:
            return []
        base = "https://e-disclosure.ru/portal"
        return [
            {
                "label": "E-Disclosure company card",
                "url": f"{base}/company.aspx?id={company_id}",
                "trust_level": "regulated disclosure portal",
            },
            {
                "label": "Consolidated financial statements",
                "url": f"{base}/files.aspx?id={company_id}&type=4",
                "trust_level": "regulated disclosure portal",
            },
            {
                "label": "Annual reports",
                "url": f"{base}/files.aspx?id={company_id}&type=2",
                "trust_level": "regulated disclosure portal",
            },
            {
                "label": "Accounting financial statements",
                "url": f"{base}/files.aspx?id={company_id}&type=3",
                "trust_level": "regulated disclosure portal",
            },
        ]

    def _edisclosure_search_terms(self, ticker: str, company_name: str = "") -> list[str]:
        terms = [*EDISCLOSURE_RUSSIAN_SEARCH_NAMES.get(ticker.upper(), []), ticker, company_name]
        result = []
        for term in terms:
            clean = term.strip()
            if clean and clean not in result:
                result.append(clean)
        return result

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path)
