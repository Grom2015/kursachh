from pathlib import Path

import yaml

from app.core.config import get_settings
from app.services.periods import period_in_range
from app.services.reports.source_adapters import DiscoveredReport


class RealSourceManifest:
    def __init__(self, root: Path | None = None):
        self.root = root or get_settings().root_dir
        self.warnings: list[str] = []

    def load_for_company(
        self, ticker: str, period_from: str, period_to: str, reporting_standard: str
    ) -> list[DiscoveredReport]:
        path = self.root / "data" / "manifests" / f"{ticker.casefold()}_real_sources.yml"
        if not path.exists():
            self.warnings.append(f"Real source manifest not found for {ticker}")
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        reports = []
        for item in data.get("reports", []) or []:
            if not item.get("source_url"):
                continue
            if item.get("reporting_standard", "").upper() != reporting_standard.upper():
                continue
            if not period_in_range(item["period"], period_from, period_to):
                continue
            reports.append(
                DiscoveredReport(
                    company_ticker=ticker,
                    report_period=item["period"],
                    period_from=item.get("period_from"),
                    period_to=item.get("period_to"),
                    reporting_standard=item.get("reporting_standard", "UNKNOWN"),
                    document_type=item.get("document_type", "other"),
                    source_role=item.get("source_role") or item.get("document_type", "other"),
                    source_type=data.get("source_type", "issuer_ir_manifest"),
                    source_url=item["source_url"],
                    title=item.get("title", ""),
                    language=item.get("language"),
                    published_at=None,
                    expected_file_type=item.get("expected_file_type", "unknown"),
                    confidence_score=float(item.get("confidence_score", 0.9)),
                    notes=item.get("notes"),
                )
            )
        return reports
