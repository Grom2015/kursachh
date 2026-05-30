import ssl
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import certifi
import httpx

from app.core.config import get_settings


class TLSConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class TLSVerifyConfig:
    verify: bool | str
    tls_verify_mode: str
    ca_bundle_path_used: str | None
    ca_bundle_exists: bool
    ca_bundle_size_bytes: int | None
    certifi_path: str


def tls_verify_config() -> TLSVerifyConfig:
    settings = get_settings()
    certifi_path = certifi.where()
    bundle = (settings.report_tls_ca_bundle or "").strip()
    if not bundle:
        return TLSVerifyConfig(
            verify=True,
            tls_verify_mode="default",
            ca_bundle_path_used=None,
            ca_bundle_exists=False,
            ca_bundle_size_bytes=None,
            certifi_path=certifi_path,
        )
    path = Path(bundle).expanduser()
    if not path.exists() or not path.is_file():
        raise TLSConfigError(f"REPORT_TLS_CA_BUNDLE does not point to an existing file: {bundle}")
    size = path.stat().st_size
    if size <= 0:
        raise TLSConfigError(f"REPORT_TLS_CA_BUNDLE points to an empty file: {bundle}")
    return TLSVerifyConfig(
        verify=str(path),
        tls_verify_mode="custom_ca_bundle",
        ca_bundle_path_used=str(path),
        ca_bundle_exists=True,
        ca_bundle_size_bytes=size,
        certifi_path=certifi_path,
    )


def tls_report_fields() -> dict[str, Any]:
    try:
        config = tls_verify_config()
        return {
            "tls_verify_mode": config.tls_verify_mode,
            "ca_bundle_path_used": config.ca_bundle_path_used,
            "ca_bundle_exists": config.ca_bundle_exists,
            "ca_bundle_size_bytes": config.ca_bundle_size_bytes,
            "certifi_path": config.certifi_path,
            "tls_config_error": None,
        }
    except TLSConfigError as exc:
        return {
            "tls_verify_mode": "custom_ca_bundle",
            "ca_bundle_path_used": get_settings().report_tls_ca_bundle or None,
            "ca_bundle_exists": Path(get_settings().report_tls_ca_bundle).expanduser().exists()
            if get_settings().report_tls_ca_bundle
            else False,
            "ca_bundle_size_bytes": (
                Path(get_settings().report_tls_ca_bundle).expanduser().stat().st_size
                if get_settings().report_tls_ca_bundle
                and Path(get_settings().report_tls_ca_bundle).expanduser().exists()
                and Path(get_settings().report_tls_ca_bundle).expanduser().is_file()
                else None
            ),
            "certifi_path": certifi.where(),
            "tls_config_error": str(exc),
        }


def classify_httpx_failure(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".casefold()
    if isinstance(exc, httpx.ConnectError) and (
        "certificate_verify_failed" in text or "certificate verify failed" in text
    ):
        return "tls_certificate_verify_failed"
    if "certificate_verify_failed" in text or "certificate verify failed" in text:
        return "tls_certificate_verify_failed"
    return "live_request_failed"


def certificate_metadata(url: str) -> dict[str, Any] | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    port = parsed.port or 443
    try:
        pem = ssl.get_server_certificate((parsed.hostname, port))
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="utf-8") as tmp:
            tmp.write(pem)
            tmp_path = Path(tmp.name)
        try:
            decoded = ssl._ssl._test_decode_cert(str(tmp_path))  # noqa: SLF001
        finally:
            tmp_path.unlink(missing_ok=True)
        return {
            "subject": decoded.get("subject"),
            "issuer": decoded.get("issuer"),
            "not_before": decoded.get("notBefore"),
            "not_after": decoded.get("notAfter"),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def runtime_tls_metadata() -> dict[str, Any]:
    return {
        "python_version": sys.version,
        "openssl_version": ssl.OPENSSL_VERSION,
        "certifi_path": certifi.where(),
    }
