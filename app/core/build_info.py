from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings


@dataclass(frozen=True)
class BuildInfo:
    app_version: str
    build_id: str
    build_source: str
    started_at: str
    process_id: int

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


_STARTED_AT = datetime.now(UTC).isoformat()
_BUILD_INFO: BuildInfo | None = None


def current_build_info() -> BuildInfo:
    global _BUILD_INFO
    if _BUILD_INFO is None:
        _BUILD_INFO = BuildInfo(
            app_version="0.1.0",
            build_id=_compute_build_id(),
            build_source="source_fingerprint",
            started_at=_STARTED_AT,
            process_id=os.getpid(),
        )
    return _BUILD_INFO


def _compute_build_id() -> str:
    settings = get_settings()
    root = settings.root_dir.resolve()
    digest = hashlib.sha256()
    for path in _fingerprint_paths(root):
        stat = path.stat()
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(str(int(stat.st_mtime_ns)).encode("utf-8"))
        digest.update(str(stat.st_size).encode("utf-8"))
    return digest.hexdigest()[:12]


def _fingerprint_paths(root: Path) -> list[Path]:
    candidates = [
        root / "app" / "main.py",
        root / "app" / "api" / "routes" / "reports.py",
        root / "app" / "api" / "routes" / "ui.py",
        root / "app" / "services" / "reports" / "manual_report_ingestion.py",
        root / "app" / "web" / "index.html",
        root / "app" / "web" / "assets" / "control-center.js",
        root / "app" / "web" / "assets" / "control-center.css",
    ]
    return [path for path in candidates if path.exists()]
