import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.services.reports.tls import TLSConfigError, classify_httpx_failure, tls_report_fields, tls_verify_config
from app.tools.diagnose_tls_source import diagnose_url
from app.tools.diagnose_tls_source import save_report as save_tls_diagnostics_report

REQUIRED_MARKER_GROUPS = [
    ["tatneft", "pjsc tatneft", "tatneft group"],
    ["ifrs", "international financial reporting standards"],
    ["consolidated financial statements"],
    ["2021"],
    ["year ended 31 december 2021", "31 december 2021", "audited"],
]
PDF_CONTENT_TYPES = {"application/pdf", "application/octet-stream", "binary/octet-stream"}
DEFAULT_MIN_SIZE_BYTES = 50_000


def verify_candidate(
    url: str,
    ticker: str = "TATN",
    min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
    tls_diagnostics: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not allowed_domain(url):
        return candidate_report(ticker, url, "FAIL", checks, warnings, "non_allowlisted_domain")
    response = fetch_candidate(url)
    if response.get("error"):
        diagnostics_path = None
        if tls_diagnostics and response.get("failure_reason") == "tls_certificate_verify_failed":
            diagnostics_path = str(save_tls_diagnostics_report(diagnose_url(url)))
        return candidate_report(
            ticker,
            url,
            "FAIL",
            checks,
            warnings,
            response.get("failure_reason", response["error"]),
            tls_diagnostics_path=diagnostics_path,
        )
    content = response["content"]
    content_type = response["content_type"]
    sha256 = hashlib.sha256(content).hexdigest()
    checks.append({"check": "sha256", "status": "PASS", "value": sha256})
    checks.append({"check": "file_size", "status": "PASS" if len(content) >= min_size_bytes else "FAIL", "value": len(content)})
    if len(content) < min_size_bytes:
        return candidate_report(ticker, url, "FAIL", checks, warnings, "file_too_small", sha256, len(content), content_type)
    is_pdf_type = content_type_matches_pdf(content_type)
    is_pdf_bytes = content.startswith(b"%PDF")
    checks.append({"check": "content_type_pdf", "status": "PASS" if is_pdf_type else "FAIL", "value": content_type})
    checks.append({"check": "pdf_like_bytes", "status": "PASS" if is_pdf_bytes else "FAIL", "value": is_pdf_bytes})
    if not (is_pdf_type or is_pdf_bytes):
        return candidate_report(ticker, url, "FAIL", checks, warnings, "not_pdf", sha256, len(content), content_type)
    text = extract_first_pages_text(content)
    marker_results = marker_checks(text)
    checks.extend(marker_results)
    missing = [item["marker_group"] for item in marker_results if item["status"] != "PASS"]
    if missing:
        return candidate_report(
            ticker,
            url,
            "FAIL",
            checks,
            warnings,
            f"required_text_markers_absent: {', '.join(missing)}",
            sha256,
            len(content),
            content_type,
        )
    return candidate_report(ticker, url, "PASS", checks, warnings, None, sha256, len(content), content_type)


def allowed_domain(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in set(get_settings().report_source_allowed_domains)


def fetch_candidate(url: str) -> dict[str, Any]:
    try:
        tls_config = tls_verify_config()
        with httpx.Client(timeout=30, follow_redirects=True, verify=tls_config.verify) as client:
            response = client.get(url)
            response.raise_for_status()
            return {
                "content": response.content,
                "content_type": response.headers.get("content-type", ""),
            }
    except TLSConfigError as exc:
        return {"error": f"tls_config_error: {exc}", "failure_reason": "tls_config_error"}
    except Exception as exc:
        failure_reason = classify_httpx_failure(exc)
        return {"error": f"live_get_failed: {failure_reason}: {exc}", "failure_reason": failure_reason}


def content_type_matches_pdf(content_type: str | None) -> bool:
    normalized = (content_type or "").split(";")[0].strip().casefold()
    return normalized in PDF_CONTENT_TYPES or "pdf" in normalized


def extract_first_pages_text(content: bytes, max_pages: int = 5) -> str:
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        with pdfplumber.open(tmp_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages[:max_pages])
    except Exception:
        return ""
    finally:
        tmp_path.unlink(missing_ok=True)


def marker_checks(text: str) -> list[dict[str, Any]]:
    normalized = " ".join(text.casefold().split())
    rows = []
    for group in REQUIRED_MARKER_GROUPS:
        matched = next((marker for marker in group if marker in normalized), None)
        rows.append(
            {
                "check": "required_text_marker",
                "marker_group": " | ".join(group),
                "status": "PASS" if matched else "FAIL",
                "matched_marker": matched,
            }
        )
    return rows


def candidate_report(
    ticker: str,
    url: str,
    status: str,
    checks: list[dict[str, Any]],
    warnings: list[str],
    failure_reason: str | None,
    sha256: str | None = None,
    file_size_bytes: int | None = None,
    content_type: str | None = None,
    tls_diagnostics_path: str | None = None,
) -> dict[str, Any]:
    return {
        "ticker": ticker.upper(),
        "url": url,
        "status": status,
        "trusted_domain": allowed_domain(url),
        "content_type": content_type,
        "sha256": sha256,
        "file_size_bytes": file_size_bytes,
        "checks": checks,
        "warnings": warnings,
        "failure_reason": failure_reason,
        "tls_diagnostics_path": tls_diagnostics_path,
        **tls_report_fields(),
    }


def save_report(report: dict[str, Any]) -> Path:
    root = get_settings().root_dir / "data" / "validation" / report["ticker"].upper()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "source_candidate_verification_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a candidate real-source PDF URL.")
    parser.add_argument("ticker")
    parser.add_argument("url")
    parser.add_argument("--tls-diagnostics", action="store_true", help="Write TLS diagnostics if TLS verification fails.")
    args = parser.parse_args(argv)
    report = verify_candidate(args.url, args.ticker, tls_diagnostics=args.tls_diagnostics)
    path = save_report(report)
    print(
        "\n".join(
            [
                f"ticker: {report['ticker']}",
                f"status: {report['status']}",
                f"trusted_domain: {report['trusted_domain']}",
                f"content_type: {report['content_type']}",
                f"sha256: {report['sha256']}",
                f"file_size_bytes: {report['file_size_bytes']}",
                f"failure_reason: {report['failure_reason']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
