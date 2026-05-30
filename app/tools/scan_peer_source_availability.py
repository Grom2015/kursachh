import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from app.core.config import get_settings
from app.services.periods import periods_between

DEFAULT_CANDIDATES = ["TATN", "SIBN", "ROSN", "GAZP", "NVTK"]
PROPER_SOURCE_ROLES = {"financial_statements", "financial_supplement", "annual_report"}


def scan(period_from: str, period_to: str, sector: str = "oil_and_gas", candidates: list[str] | None = None) -> dict[str, Any]:
    periods = periods_between(period_from, period_to)
    candidate_rows = [
        candidate_report(ticker.upper(), periods, sector)
        for ticker in candidates or DEFAULT_CANDIDATES
    ]
    recommended = choose_recommended_candidate(candidate_rows)
    report = {
        "sector": sector,
        "period_from": period_from,
        "period_to": period_to,
        "candidates": candidate_rows,
        "recommended_next_candidate": recommended,
        "warnings": scan_warnings(candidate_rows, recommended),
    }
    save_report(report)
    return report


def candidate_report(ticker: str, periods: list[str], sector: str = "oil_and_gas") -> dict[str, Any]:
    manifest = load_candidate_manifest(ticker)
    coverage = {}
    blockers = list(manifest.get("blockers") or [])
    trusted_sources = 0
    untrusted_sources = 0
    for period in periods:
        item = (manifest.get("coverage") or {}).get(period) or missing_source()
        role = item.get("source_role", "missing")
        coverage[period] = role
        if role != "missing":
            if item.get("trusted_domain"):
                trusted_sources += 1
            else:
                untrusted_sources += 1
    score = source_package_score(manifest, periods)
    fy_source_verified = coverage.get(periods[-1]) in {"financial_statements", "financial_supplement", "annual_report"}
    readiness = readiness_for(coverage, fy_source_verified)
    if not fy_source_verified and "FY/12M source not verified" not in blockers:
        blockers.append("FY/12M source not verified")
    return {
        "ticker": ticker,
        "company": manifest.get("company_name") or ticker,
        "sector": manifest.get("sector") or sector,
        "readiness": readiness,
        "source_package_score": score,
        "coverage": coverage,
        "fy_source_verified": fy_source_verified,
        "trusted_sources_count": trusted_sources,
        "untrusted_sources_count": untrusted_sources,
        "official_source_pages": manifest.get("official_source_pages") or [],
        "source_candidates_path": str(candidate_manifest_path(ticker)),
        "blockers": sorted(set(blockers)),
        "recommendation": recommendation_for(readiness),
    }


def source_package_score(manifest: dict[str, Any], periods: list[str]) -> int:
    score = 0
    coverage = manifest.get("coverage") or {}
    for period in periods[:3]:
        item = coverage.get(period) or {}
        role = item.get("source_role")
        if role in {"financial_statements", "financial_supplement"}:
            score += 1
    fy = coverage.get(periods[-1]) or {}
    if fy.get("source_role") in {"financial_statements", "financial_supplement", "annual_report"}:
        score += 2
    roles = [item.get("source_role") for item in coverage.values()]
    non_missing = [role for role in roles if role and role != "missing"]
    if non_missing and all(role == "press_release" for role in non_missing):
        score -= 2
    if any((item.get("source_role") != "missing" and not item.get("trusted_domain")) for item in coverage.values()):
        score -= 3
    if any(
        item.get("source_role") in PROPER_SOURCE_ROLES and item.get("expected_file_type") == "unknown"
        for item in coverage.values()
    ):
        score -= 2
    return score


def readiness_for(coverage: dict[str, str], fy_source_verified: bool) -> str:
    roles = set(coverage.values())
    has_interim = any(coverage.get(period) in {"financial_statements", "financial_supplement"} for period in list(coverage)[:3])
    has_proper = bool(roles & PROPER_SOURCE_ROLES)
    only_press = roles <= {"press_release", "missing"} and "press_release" in roles
    if fy_source_verified and has_interim:
        return "READY_CANDIDATE"
    if has_proper:
        return "PARTIAL_CANDIDATE"
    if only_press or not has_proper:
        return "NOT_SUITABLE"
    return "NOT_SUITABLE"


def recommendation_for(readiness: str) -> str:
    if readiness == "READY_CANDIDATE":
        return "best_next_real_peer_candidate"
    if readiness == "PARTIAL_CANDIDATE":
        return "do_not_use_for_full_year_peer_yet"
    return "do_not_use_for_real_peer_pipeline"


def choose_recommended_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    ready = [item for item in candidates if item["readiness"] == "READY_CANDIDATE"]
    if not ready:
        return None
    best = sorted(ready, key=lambda item: (item["source_package_score"], item["trusted_sources_count"]), reverse=True)[0]
    return {"ticker": best["ticker"], "reason": "best verified FY/interim coverage"}


def scan_warnings(candidates: list[dict[str, Any]], recommended: dict[str, Any] | None) -> list[str]:
    warnings = []
    if not recommended:
        warnings.append("No suitable next candidate with verified FY/interim source package was found.")
    if any(item["ticker"] == "TATN" and item["readiness"] == "PARTIAL_CANDIDATE" for item in candidates):
        warnings.append("TATN remains partial-only until FY/12M source is verified.")
    return warnings


def load_candidate_manifest(ticker: str) -> dict[str, Any]:
    path = candidate_manifest_path(ticker)
    if not path.exists():
        return {"company": ticker, "coverage": {}, "blockers": ["candidate source manifest missing"]}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def candidate_manifest_path(ticker: str) -> Path:
    return get_settings().root_dir / "data" / "manifests" / "candidates" / f"{ticker.casefold()}_source_candidates.yml"


def missing_source() -> dict[str, Any]:
    return {"source_role": "missing", "trusted_domain": False, "expected_file_type": "unknown"}


def save_report(report: dict[str, Any]) -> Path:
    path = get_settings().root_dir / "data" / "validation" / "PEERS" / "2021_oil_gas_source_availability_scan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan peer source availability without parsing/downloading raw reports.")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--sector", default="oil_and_gas")
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES))
    args = parser.parse_args(argv)
    candidates = [item.strip().upper() for item in args.candidates.split(",") if item.strip()]
    report = scan(args.period_from, args.period_to, args.sector, candidates)
    print(
        "\n".join(
            [
                f"sector: {report['sector']}",
                f"candidates_scanned: {len(report['candidates'])}",
                "recommended_next_candidate: "
                + ((report["recommended_next_candidate"] or {}).get("ticker") or "none"),
                f"report_path: {save_report(report)}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
