import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, ReportDocument
from app.services.reports.tls import TLSConfigError, classify_httpx_failure, tls_verify_config


class ReportDownloadError(RuntimeError):
    pass


class ReportDownloader:
    def __init__(self, db: Session):
        self.db = db
        self.root = get_settings().root_dir

    def register(self, company: Company, manifest_item: dict) -> ReportDocument:
        fixture_path = manifest_item.get("fixture_path")
        source_url = manifest_item.get("source_url")
        storage_path: str | None = None
        file_bytes: bytes = b""
        if manifest_item.get("source_type") == "fixture" and fixture_path:
            data_root = (self.root / "data").resolve()
            path = (self.root / fixture_path).resolve()
            if not path.is_relative_to(data_root):
                raise ValueError(f"Fixture path escapes data directory: {fixture_path}")
            storage_path = str(path)
            file_bytes = path.read_bytes()
        elif source_url:
            return self.download(company, manifest_item)
        file_hash = hashlib.sha256(file_bytes).hexdigest() if file_bytes else None
        doc = ReportDocument(
            company_id=company.id,
            report_period=manifest_item["period"],
            reporting_standard=manifest_item.get("reporting_standard", "UNKNOWN"),
            document_type=manifest_item.get("document_type", "other"),
            source_role=manifest_item.get("source_role", manifest_item.get("document_type", "other")),
            source_type=manifest_item.get("source_type", "other"),
            source_url=source_url,
            storage_path=storage_path,
            file_name=Path(storage_path).name if storage_path else None,
            file_hash=file_hash,
            language=manifest_item.get("language"),
            status="downloaded" if storage_path else "discovered",
        )
        self.db.add(doc)
        self.db.flush()
        return doc

    def download(self, company: Company, item: dict) -> ReportDocument:
        settings = get_settings()
        source_url = item.get("source_url")
        if not source_url:
            raise ReportDownloadError("source_url is required for real report download")
        self._ensure_allowed_url(source_url)
        existing = self.db.scalar(
            select(ReportDocument).where(
                ReportDocument.company_id == company.id,
                ReportDocument.source_url == source_url,
                ReportDocument.status.in_(["downloaded", "parsed"]),
            )
        )
        if existing:
            return existing

        doc = ReportDocument(
            company_id=company.id,
            report_period=item["period"],
            reporting_standard=item.get("reporting_standard", "UNKNOWN"),
            document_type=item.get("document_type", "other"),
            source_role=item.get("source_role", item.get("document_type", "other")),
            source_type=item.get("source_type", "other"),
            source_url=source_url,
            language=item.get("language"),
            published_at=item.get("published_at"),
            status="discovered",
        )
        self.db.add(doc)
        self.db.flush()
        try:
            tls_config = tls_verify_config()
            response = httpx.get(source_url, timeout=30, verify=tls_config.verify)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            self._validate_content_type(content_type, item.get("expected_file_type", "unknown"))
            content_length = int(response.headers.get("content-length", "0") or 0)
            max_bytes = settings.max_report_download_mb * 1024 * 1024
            if content_length and content_length > max_bytes:
                raise ReportDownloadError("Report download exceeds MAX_REPORT_DOWNLOAD_MB")
            file_bytes = response.content
            if len(file_bytes) > max_bytes:
                raise ReportDownloadError("Report download exceeds MAX_REPORT_DOWNLOAD_MB")
            file_hash = hashlib.sha256(file_bytes).hexdigest()
            filename = self._safe_filename(source_url, item.get("expected_file_type", "unknown"))
            storage_dir = (
                self.root
                / settings.report_download_dir
                / company.ticker.upper()
                / item.get("reporting_standard", "UNKNOWN")
                / item["period"]
            ).resolve()
            download_root = (self.root / settings.report_download_dir).resolve()
            if not storage_dir.is_relative_to(download_root):
                raise ReportDownloadError("Storage path escapes report download directory")
            storage_dir.mkdir(parents=True, exist_ok=True)
            path = storage_dir / f"{file_hash}_{filename}"
            if not path.exists():
                path.write_bytes(file_bytes)
            doc.storage_path = str(path)
            doc.file_name = path.name
            doc.file_hash = file_hash
            doc.status = "downloaded"
        except TLSConfigError as exc:
            doc.status = "failed"
            self.db.flush()
            raise ReportDownloadError(f"TLS configuration error for report download: {exc}") from exc
        except Exception as exc:
            doc.status = "failed"
            self.db.flush()
            failure_reason = classify_httpx_failure(exc)
            raise ReportDownloadError(
                f"Report download failed for {source_url}: {failure_reason}: {exc}"
            ) from exc
        self.db.flush()
        return doc

    def _ensure_allowed_url(self, url: str) -> None:
        parsed = urlparse(url)
        allowed = set(get_settings().report_source_allowed_domains)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ReportDownloadError("Only http(s) report URLs are allowed")
        if parsed.hostname.lower() not in allowed:
            raise ReportDownloadError(f"Report URL domain is not allowlisted: {parsed.hostname}")

    def _validate_content_type(self, content_type: str, expected_file_type: str) -> None:
        if not content_type or expected_file_type == "unknown":
            return
        allowed = {
            "pdf": ["pdf", "octet-stream"],
            "xlsx": ["spreadsheet", "excel", "octet-stream"],
            "zip": ["zip", "octet-stream"],
        }
        if expected_file_type in allowed and not any(token in content_type for token in allowed[expected_file_type]):
            raise ReportDownloadError(f"Unexpected content-type for {expected_file_type}: {content_type}")

    def _safe_filename(self, url: str, expected_file_type: str) -> str:
        name = unquote(Path(urlparse(url).path).name)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
        if not name:
            name = f"report.{expected_file_type if expected_file_type != 'unknown' else 'bin'}"
        if expected_file_type in {"pdf", "xlsx", "zip"} and not name.lower().endswith(f".{expected_file_type}"):
            name = f"{name}.{expected_file_type}"
        if "/" in name or "\\" in name or ".." in name:
            raise ReportDownloadError("Unsafe report filename")
        return name
