from pathlib import Path

import yaml

from app.core.config import get_settings
from app.services.periods import period_in_range


class ReportManifest:
    def __init__(self, root: Path | None = None):
        self.root = root or get_settings().root_dir

    def load_for_company(self, ticker: str, period_from: str, period_to: str) -> list[dict]:
        path = self.root / "data" / "manifests" / f"{ticker.casefold()}.yml"
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        reports = []
        for item in data.get("reports", []):
            if period_in_range(item["period"], period_from, period_to):
                reports.append(item)
        return reports

