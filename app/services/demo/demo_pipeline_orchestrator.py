import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from app.core.config import get_settings


@dataclass
class DemoPipelineRequest:
    tickers: list[str]
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    replay_cache: bool = True
    include_manual_fallback: bool = True
    include_provider_feasibility: bool = True
    include_readiness: bool = True
    output_formats: list[str] = field(default_factory=lambda: ["json", "md"])


@dataclass
class DemoPipelineReport:
    generated_at: str
    period_from: str
    period_to: str
    reporting_standard: str
    tickers: list[str]
    executive_summary: dict[str, Any]
    company_results: list[dict[str, Any]]
    cross_company_summary: dict[str, Any]
    blockers_summary: list[dict[str, Any]]
    manual_fallback_summary: dict[str, Any]
    provider_strategy_summary: dict[str, Any]
    architecture_summary: dict[str, Any]
    limitations: list[str]
    next_steps: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DemoPipelineOrchestrator:
    def __init__(self, root: Path | None = None):
        self.root = (root or get_settings().root_dir).resolve()
        self.warnings: list[str] = []

    def build_report(self, request: DemoPipelineRequest) -> DemoPipelineReport:
        self.warnings = []
        coverage = self._load_json(self._coverage_path(request), required=True)
        provider = self._load_json(self.root / "data" / "validation" / "providers" / "provider_feasibility_scan.json")
        edisclosure = self._load_json(self.root / "data" / "validation" / "providers" / "edisclosure_proof_of_access.json")
        coverage_items = {item.get("ticker"): item for item in coverage.get("company_items", [])}
        company_results = [
            self._company_result(ticker.upper(), request, coverage_items.get(ticker.upper()))
            for ticker in request.tickers
        ]
        blockers = self._blockers_summary(company_results)
        provider_summary = self._provider_strategy_summary(provider, edisclosure, request.include_provider_feasibility)
        manual_summary = self._manual_fallback_summary(company_results, request.include_manual_fallback)
        cross_summary = self._cross_company_summary(company_results)
        return DemoPipelineReport(
            generated_at=datetime.now(UTC).isoformat(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            tickers=[ticker.upper() for ticker in request.tickers],
            executive_summary=self._executive_summary(company_results, provider_summary, manual_summary),
            company_results=company_results,
            cross_company_summary=cross_summary,
            blockers_summary=blockers,
            manual_fallback_summary=manual_summary,
            provider_strategy_summary=provider_summary,
            architecture_summary={
                "pipeline": [
                    "Company identity",
                    "Source discovery",
                    "Financial report discovery",
                    "Document validation",
                    "Statement table extraction",
                    "DataFrame artifacts",
                    "Fact candidates",
                    "Scoped readiness",
                ],
                "mode": "report_only_existing_artifacts",
                "does_not_run": ["downloads", "metric_engine", "valuation", "peer_comparison", "llm", "fact_persistence"],
            },
            limitations=[
                "This demo report is a reporting layer, not a new analysis engine.",
                "The system does not claim universal support across all MOEX issuers.",
                "Valuation metrics remain unavailable without a separate valuation input module.",
                "Manual upload is lower-trust fallback and does not make source package READY.",
                "No OCR/table-image extraction is implemented for image-only primary statements.",
                "No production external provider API access is verified yet.",
            ],
            next_steps=self._next_steps(blockers),
            warnings=self.warnings,
        )

    def save_report(self, report: DemoPipelineReport, output_formats: list[str] | None = None) -> dict[str, str]:
        output_formats = output_formats or ["json", "md", "html"]
        root = self.root / "data" / "validation" / "demo"
        root.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}
        if "json" in output_formats:
            path = root / "demo_pipeline_report.json"
            path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            paths["json"] = str(path)
        if "md" in output_formats:
            path = root / "demo_pipeline_report.md"
            path.write_text(render_markdown_report(report), encoding="utf-8")
            paths["md"] = str(path)
        if "html" in output_formats:
            path = root / "demo_pipeline_report.html"
            path.write_text(render_html_report(report), encoding="utf-8")
            paths["html"] = str(path)
        return paths

    def _company_result(self, ticker: str, request: DemoPipelineRequest, coverage_item: dict[str, Any] | None) -> dict[str, Any]:
        if not coverage_item:
            self.warnings.append(f"Coverage item missing for {ticker}.")
            coverage_item = {
                "ticker": ticker,
                "coverage_level": "UNSUPPORTED",
                "main_blocker": "coverage_item_missing",
                "blockers": [],
            }
        extraction = self._load_json(self._company_report_path(ticker, request, "statement_table_extraction"))
        fact_parse = self._load_json(self._company_report_path(ticker, request, "dataframe_statement_fact_parse"))
        comparison = self._load_json(self._company_report_path(ticker, request, "dataframe_vs_existing_fact_comparison"))
        ratios = self._load_json(self._company_report_path(ticker, request, "financial_ratios"))
        llm_payload_path = self._company_report_path(ticker, request, "llm_analysis_payload")
        market_technical = self._load_json(self._company_report_path(ticker, request, "market_technical_report"))
        peer_report = self._load_json(self._peer_report_path(ticker, request))
        manual = self._load_json(self.root / "data" / "validation" / ticker / f"{request.period_to}_manual_report_ingestion.json")
        source_package = self._load_json(self._company_report_path(ticker, request, "source_package_report"))
        validation = self._load_json(self._company_report_path(ticker, request, "real_validation_report"))
        coverage_level = coverage_item.get("coverage_level", "UNSUPPORTED")
        main_status = self._main_status(coverage_level)
        metric_scopes = coverage_item.get("metric_scope_statuses") or {}
        counts = coverage_item.get("counts") or {}
        fact_candidates_count = max(
            int(fact_parse.get("canonical_fact_candidates") or 0),
            int(fact_parse.get("canonical_facts_created") or 0),
            int(comparison.get("dataframe_candidates_count") or 0),
            int(counts.get("canonical_fact_candidates") or 0),
        )
        key_numbers = {
            "documents_count": counts.get("cached_documents", 0),
            "tables_count": extraction.get("statement_tables_count", counts.get("statement_tables", 0)),
            "balance_sheet_count": extraction.get("balance_sheet_tables_count", 0),
            "income_statement_count": extraction.get("income_statement_tables_count", 0),
            "cash_flow_count": extraction.get("cash_flow_tables_count", 0),
            "fact_candidates_count": fact_candidates_count,
            "matched_existing_facts_count": comparison.get("matched_count", 0),
            "conflicts_count": comparison.get("conflict_count", 0),
            "financial_ratios_calculated_count": (ratios.get("summary") or {}).get("calculated_count", 0),
            "financial_ratios_missing_count": (ratios.get("summary") or {}).get("missing_count", 0),
            "financial_ratios_unsupported_count": (ratios.get("summary") or {}).get("unsupported_count", 0),
            "financial_ratios_blocked_count": (ratios.get("summary") or {}).get("blocked_count", 0),
            "market_candles_count": (market_technical.get("summary") or {}).get("candles_count", 0),
            "market_technical_valid_count": (market_technical.get("summary") or {}).get("technical_indicators_valid_count", 0),
            "peer_count": (peer_report.get("summary") or {}).get("peer_count", 0),
            "peer_metrics_compared_count": (peer_report.get("summary") or {}).get("metrics_compared_count", 0),
        }
        return {
            "ticker": ticker,
            "company_name": coverage_item.get("company_name"),
            "coverage_level": coverage_level,
            "main_status": main_status,
            "main_blocker": None if main_status == "READY" else coverage_item.get("main_blocker"),
            "pipeline_stage_statuses": {
                "identity": coverage_item.get("identity_status", "UNKNOWN"),
                "source_discovery": coverage_item.get("source_discovery_status", "UNKNOWN"),
                "report_discovery": coverage_item.get("report_discovery_status", "UNKNOWN"),
                "document_validation": coverage_item.get("documents_status", "UNKNOWN"),
                "table_extraction": coverage_item.get("table_extraction_status", "UNKNOWN"),
                "fact_extraction": coverage_item.get("fact_extraction_status", "UNKNOWN"),
                "statement_readiness": metric_scopes.get("statement_based_financials", "UNKNOWN"),
                "valuation_readiness": metric_scopes.get("valuation_metrics", "UNAVAILABLE"),
            },
            "available_outputs": self._available_outputs(
                coverage_item, extraction, fact_parse, comparison, manual, market_technical, peer_report
            ),
            "unavailable_outputs": coverage_item.get("unsupported_outputs", []),
            "evidence_paths": self._evidence_paths(ticker, request),
            "key_numbers": key_numbers,
            "trust_notes": self._trust_notes(coverage_level, manual, comparison),
            "recommended_action": self._recommended_action(ticker, coverage_item, source_package, validation),
            "blockers": coverage_item.get("blockers", []),
            "financial_ratios_summary": self._financial_ratios_summary(ratios),
            "llm_payload_available": llm_payload_path.exists(),
            "llm_payload_path": str(llm_payload_path) if llm_payload_path.exists() else None,
            "market_technical_summary": self._market_technical_summary(market_technical),
            "peer_analysis_summary": self._peer_analysis_summary(peer_report),
        }

    def _available_outputs(
        self,
        coverage_item: dict[str, Any],
        extraction: dict[str, Any],
        fact_parse: dict[str, Any],
        comparison: dict[str, Any],
        manual: dict[str, Any],
        market_technical: dict[str, Any] | None = None,
        peer_report: dict[str, Any] | None = None,
    ) -> list[str]:
        outputs = list(coverage_item.get("supported_outputs", []))
        if int(extraction.get("statement_tables_count") or 0) > 0:
            outputs.extend(["statement_tables", "dataframe_artifacts"])
        if int(fact_parse.get("canonical_fact_candidates") or fact_parse.get("canonical_facts_created") or 0) > 0:
            outputs.append("fact_candidates")
        if int(comparison.get("matched_count") or 0) > 0:
            outputs.append("fact_candidate_comparison")
        if manual.get("source_trust_bucket"):
            outputs.append("manual_document_analysis")
        if market_technical and int((market_technical.get("summary") or {}).get("candles_count") or 0) > 0:
            outputs.append("market_technical_analysis")
        if peer_report and peer_report.get("peer_selection_status") == "ready":
            outputs.append("peer_selection")
        if peer_report and peer_report.get("comparison_readiness") in {"READY_WITH_LIMITATIONS", "PARTIAL"}:
            outputs.append("peer_comparison_report")
        return sorted(set(outputs))

    def _trust_notes(self, coverage_level: str, manual: dict[str, Any], comparison: dict[str, Any]) -> list[str]:
        notes = ["Existing issuer-specific real facts remain primary where available."]
        if coverage_level == "FULL_STATEMENT_READY":
            notes.append("Statement readiness is scoped, not full-company universal readiness.")
        if comparison.get("dataframe_trust_buckets"):
            notes.append("DataFrame fact candidates are audit-only unless a future gated persist stage promotes them.")
        if manual:
            notes.append(
                "Manual upload is lower-trust fallback: "
                f"source_trust_bucket={manual.get('source_trust_bucket')}, "
                f"official_source_verified={manual.get('official_source_verified')}, "
                f"source_package_ready_contribution={manual.get('source_package_ready_contribution')}."
            )
        return notes

    def _recommended_action(
        self,
        ticker: str,
        coverage_item: dict[str, Any],
        source_package: dict[str, Any],
        validation: dict[str, Any],
    ) -> str:
        blocker = coverage_item.get("main_blocker")
        if ticker == "LKOH" or coverage_item.get("coverage_level") == "FULL_STATEMENT_READY":
            return "Package current official pipeline, comparison evidence, and scoped readiness for demo/API presentation."
        if blocker == "missing_fy_report":
            return "Improve FY/12M source acquisition, provider access, or use manual upload fallback for an official FY report."
        if blocker == "image_only_primary_statement_pages":
            return "Design OCR/table-image extraction or obtain an alternative machine-readable official source."
        return (
            coverage_item.get("recommended_next_action")
            or source_package.get("recommended_next_action")
            or validation.get("recommended_next_action")
            or "Review blockers."
        )

    def _manual_fallback_summary(self, company_results: list[dict[str, Any]], include: bool) -> dict[str, Any]:
        if not include:
            return {"included": False}
        manual_results = [
            {
                "ticker": item["ticker"],
                "path": item["evidence_paths"]["manual_ingestion_report"],
                "trust_notes": [note for note in item["trust_notes"] if "Manual upload" in note],
            }
            for item in company_results
            if item["evidence_paths"].get("manual_ingestion_report")
        ]
        return {
            "included": True,
            "available": bool(manual_results),
            "documents": manual_results,
            "summary": "Manual upload works as lower-trust fallback and does not make source package READY.",
        }

    def _provider_strategy_summary(self, provider: dict[str, Any], edisclosure: dict[str, Any], include: bool) -> dict[str, Any]:
        if not include:
            return {"included": False}
        best = provider.get("best_candidates_by_data_type") or {}
        providers = {item.get("provider_name"): item for item in provider.get("provider_items", [])}
        return {
            "included": True,
            "providers_checked": provider.get("providers_checked", 0),
            "production_ready_providers": (provider.get("summary") or {}).get("production_ready_count", 0),
            "ifrs_report_metadata_candidate": best.get("IFRS report discovery") or "E-Disclosure / Interfax",
            "ras_financials_candidate": best.get("RAS financial statements") or "FNS GIR BO",
            "market_identity_candidate": best.get("company identity") or "MOEX ISS",
            "valuation_candidate": best.get("valuation inputs") or "Cbonds / RU Data",
            "edisclosure_access_status": edisclosure.get("access_status", "requires_auth"),
            "notes": [
                "No provider is production-ready until access, limits, archive depth, quality, and reuse rights are verified.",
                "MOEX ISS is identity/market data only, not a financial statements source.",
                "FNS GIR BO is RAS-focused, not IFRS consolidated coverage.",
                "Commercial valuation providers require license review.",
            ],
            "provider_roles": {
                name: item.get("role_in_architecture", [])
                for name, item in providers.items()
                if name in {"E-Disclosure / Interfax", "FNS GIR BO", "MOEX ISS", "Cbonds / RU Data"}
            },
        }

    def _executive_summary(
        self,
        company_results: list[dict[str, Any]],
        provider_summary: dict[str, Any],
        manual_summary: dict[str, Any],
    ) -> dict[str, Any]:
        status_counts = Counter(item["main_status"] for item in company_results)
        return {
            "what_the_system_does": (
                "Measures and demonstrates the real financial-report pipeline "
                "from company identity to scoped readiness."
            ),
            "what_worked": [
                f"{status_counts.get('READY', 0)} company is READY in the scoped statement pipeline.",
                "Manual upload fallback can validate and extract statement tables while remaining lower-trust.",
                "Provider feasibility is cataloged without claiming production access.",
            ],
            "what_is_blocked": [
                f"{status_counts.get('BLOCKED', 0)} companies are blocked by source or parser limitations.",
                "Valuation inputs are unavailable without a separate module.",
            ],
            "what_is_not_claimed": [
                "No universal issuer support is claimed.",
                "No metric improvement is claimed by this report.",
                "No provider integration is production-ready.",
            ],
            "manual_fallback_available": manual_summary.get("available", False),
            "production_ready_providers": provider_summary.get("production_ready_providers", 0),
        }

    def _cross_company_summary(self, company_results: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "companies_count": len(company_results),
            "main_status_counts": dict(Counter(item["main_status"] for item in company_results)),
            "coverage_level_counts": dict(Counter(item["coverage_level"] for item in company_results)),
        }

    def _blockers_summary(self, company_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_code: dict[str, list[str]] = {}
        for item in company_results:
            for blocker in item.get("blockers", []):
                by_code.setdefault(blocker, []).append(item["ticker"])
        return [
            {"blocker_code": blocker, "count": len(tickers), "tickers_sample": tickers[:5]}
            for blocker, tickers in sorted(by_code.items(), key=lambda pair: (-len(pair[1]), pair[0]))
        ]

    def _next_steps(self, blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        codes = [item["blocker_code"] for item in blockers]
        steps = []
        if "missing_fy_report" in codes:
            steps.append({"priority": 1, "action": "Improve FY/12M source acquisition and provider access."})
        if "image_only_primary_statement_pages" in codes:
            steps.append({"priority": 2, "action": "Design OCR/table-image extraction for image-only primary statements."})
        steps.extend(
            [
                {"priority": 3, "action": "Enrich issuer identifiers for provider POC."},
                {"priority": 4, "action": "Build valuation input module separately from parser work."},
            ]
        )
        return steps

    def _main_status(self, coverage_level: str) -> str:
        if coverage_level == "FULL_STATEMENT_READY":
            return "READY"
        if coverage_level == "PARTIAL_STATEMENT_READY":
            return "PARTIAL"
        return "BLOCKED"

    def _coverage_path(self, request: DemoPipelineRequest) -> Path:
        year = request.period_from[:4]
        filename = f"moex_{request.reporting_standard.lower()}_{year}_universal_coverage_scan.json"
        return self.root / "data" / "validation" / "coverage" / filename

    def _company_report_path(self, ticker: str, request: DemoPipelineRequest, suffix: str) -> Path:
        return self.root / "data" / "validation" / ticker / f"{request.period_from}_{request.period_to}_{suffix}.json"

    def _evidence_paths(self, ticker: str, request: DemoPipelineRequest) -> dict[str, str | None]:
        manual_path = self.root / "data" / "validation" / ticker / f"{request.period_to}_manual_report_ingestion.json"
        return {
            "coverage_report": str(self._coverage_path(request)),
            "source_package_report": self._existing_path(self._company_report_path(ticker, request, "source_package_report")),
            "extraction_report": self._existing_path(
                self._company_report_path(ticker, request, "statement_table_extraction")
            ),
            "fact_parse_report": self._existing_path(
                self._company_report_path(ticker, request, "dataframe_statement_fact_parse")
            ),
            "comparison_report": self._existing_path(
                self._company_report_path(ticker, request, "dataframe_vs_existing_fact_comparison")
            ),
            "financial_ratios_report": self._existing_path(self._company_report_path(ticker, request, "financial_ratios")),
            "llm_analysis_payload": self._existing_path(self._company_report_path(ticker, request, "llm_analysis_payload")),
            "market_technical_report": self._existing_path(self._company_report_path(ticker, request, "market_technical_report")),
            "peer_analysis_report": self._existing_path(self._peer_report_path(ticker, request)),
            "manual_ingestion_report": self._existing_path(manual_path),
            "real_validation_report": self._existing_path(self._company_report_path(ticker, request, "real_validation_report")),
        }

    def _financial_ratios_summary(self, ratios: dict[str, Any]) -> dict[str, Any]:
        if not ratios:
            return {"available": False}
        calculated = [item for item in ratios.get("metrics", []) if item.get("status") == "calculated"]
        return {
            "available": True,
            "summary": ratios.get("summary") or {},
            "sample_calculated_metrics": [
                {
                    "metric_code": item.get("metric_code"),
                    "period": item.get("period"),
                    "display_value": item.get("display_value"),
                }
                for item in calculated[:5]
            ],
        }

    def _market_technical_summary(self, market_technical: dict[str, Any]) -> dict[str, Any]:
        if not market_technical:
            return {"available": False}
        return {
            "available": True,
            "status": market_technical.get("status"),
            "market_data_mode": market_technical.get("market_data_mode"),
            "candles_count": (market_technical.get("summary") or {}).get("candles_count", 0),
            "latest_summary": market_technical.get("latest_summary") or {},
        }

    def _peer_analysis_summary(self, peer_report: dict[str, Any]) -> dict[str, Any]:
        if not peer_report:
            return {"available": False}
        return {
            "available": True,
            "peer_selection_status": peer_report.get("peer_selection_status"),
            "comparison_data_status": peer_report.get("comparison_data_status"),
            "comparison_readiness": peer_report.get("comparison_readiness"),
            "valuation_status": peer_report.get("valuation_status"),
            "peer_count": (peer_report.get("summary") or {}).get("peer_count", 0),
            "metrics_compared_count": (peer_report.get("summary") or {}).get("metrics_compared_count", 0),
        }

    def _peer_report_path(self, ticker: str, request: DemoPipelineRequest) -> Path:
        filename = f"{ticker.upper()}_{request.period_from}_{request.period_to}_peer_analysis_report.json"
        return self.root / "data" / "validation" / "PEERS" / filename

    def _existing_path(self, path: Path) -> str | None:
        return str(path) if path.exists() else None

    def _load_json(self, path: Path, required: bool = False) -> dict[str, Any]:
        if not path.exists():
            message = f"Report missing: {path}"
            if required:
                self.warnings.append(message)
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.warnings.append(f"Report invalid JSON: {path}: {exc}")
            return {}


def render_markdown_report(report: DemoPipelineReport) -> str:
    data = report.to_dict()
    lines = [
        "# Demo Pipeline Report",
        "",
        "## Executive Summary",
        data["executive_summary"]["what_the_system_does"],
        "",
        "**What worked**",
        *[f"- {item}" for item in data["executive_summary"]["what_worked"]],
        "",
        "**Blocked or unavailable**",
        *[f"- {item}" for item in data["executive_summary"]["what_is_blocked"]],
        "",
        "**What the system does not claim**",
        *[f"- {item}" for item in data["executive_summary"]["what_is_not_claimed"]],
        "",
        "## Architecture",
        (
            "Company → source discovery → report discovery → validation → table extraction "
            "→ DataFrame artifacts → fact candidates → scoped readiness"
        ),
        "",
        (
            "This report reads existing artifacts only. It does not download documents, persist facts, "
            "run valuation, run peer analysis, or call LLMs."
        ),
        "",
        "## Results by Company",
    ]
    for item in data["company_results"]:
        numbers = item["key_numbers"]
        lines.extend(
            [
                "",
                f"### {item['ticker']}",
                f"- Status: {item['main_status']} / {item['coverage_level']}",
                f"- Main blocker: {item.get('main_blocker') or 'none'}",
                f"- Statement readiness: {item['pipeline_stage_statuses'].get('statement_readiness')}",
                f"- Valuation readiness: {item['pipeline_stage_statuses'].get('valuation_readiness')}",
                f"- Documents: {numbers.get('documents_count')}; tables: {numbers.get('tables_count')}; "
                f"balance sheet: {numbers.get('balance_sheet_count')}; "
                f"income statement: {numbers.get('income_statement_count')}; "
                f"cash flow: {numbers.get('cash_flow_count')}",
                f"- Fact candidates: {numbers.get('fact_candidates_count')}; matched existing facts: "
                f"{numbers.get('matched_existing_facts_count')}; conflicts: {numbers.get('conflicts_count')}",
                f"- Financial ratios calculated: {numbers.get('financial_ratios_calculated_count')}; "
                f"missing: {numbers.get('financial_ratios_missing_count')}; "
                f"unsupported: {numbers.get('financial_ratios_unsupported_count')}; "
                f"blocked: {numbers.get('financial_ratios_blocked_count')}",
                f"- Recommended action: {item['recommended_action']}",
            ]
        )
    manual = data["manual_fallback_summary"]
    lines.extend(
        [
            "",
            "## Manual Upload Fallback",
            manual.get("summary", "Manual fallback summary unavailable."),
            "",
        ]
    )
    for item in manual.get("documents", []):
        lines.append(f"- {item['ticker']}: {item['path']} ({'; '.join(item.get('trust_notes') or [])})")
    provider = data["provider_strategy_summary"]
    lines.extend(
        [
            "",
            "## Provider Strategy",
            f"- Production-ready providers: {provider.get('production_ready_providers', 0)}",
            f"- IFRS/report metadata candidate: {provider.get('ifrs_report_metadata_candidate')}",
            f"- RAS financials candidate: {provider.get('ras_financials_candidate')}",
            f"- Market/identity candidate: {provider.get('market_identity_candidate')}",
            f"- Valuation candidate: {provider.get('valuation_candidate')}",
            "",
            "## Blockers and Next Engineering Actions",
        ]
    )
    for blocker in data["blockers_summary"][:10]:
        lines.append(f"- {blocker['blocker_code']}: {blocker['count']} ({', '.join(blocker['tickers_sample'])})")
    lines.extend(["", "Recommended next actions:"])
    for step in data["next_steps"]:
        lines.append(f"{step['priority']}. {step['action']}")
    lines.extend(["", "## Limitations"])
    lines.extend(f"- {item}" for item in data["limitations"])
    return "\n".join(lines) + "\n"


def render_html_report(report: DemoPipelineReport) -> str:
    data = report.to_dict()
    company_rows = "\n".join(
        f"""
        <section class="company">
          <h2>{escape(item['ticker'])}</h2>
          <p><strong>Status:</strong> {escape(item['main_status'])} / {escape(item['coverage_level'])}</p>
          <p><strong>Main blocker:</strong> {escape(str(item.get('main_blocker') or 'none'))}</p>
          <p><strong>Facts:</strong> {item['key_numbers'].get('fact_candidates_count', 0)}
          · <strong>Ratios calculated:</strong> {item['key_numbers'].get('financial_ratios_calculated_count', 0)}
          · <strong>Conflicts:</strong> {item['key_numbers'].get('conflicts_count', 0)}</p>
          <p><strong>Recommended action:</strong> {escape(item['recommended_action'])}</p>
        </section>
        """
        for item in data["company_results"]
    )
    blockers = "\n".join(
        f"<li>{escape(item['blocker_code'])}: {item['count']} ({escape(', '.join(item['tickers_sample']))})</li>"
        for item in data["blockers_summary"][:10]
    )
    limitations = "\n".join(f"<li>{escape(item)}</li>" for item in data["limitations"])
    next_steps = "\n".join(
        f"<li>{step['priority']}. {escape(step['action'])}</li>" for step in data["next_steps"]
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <title>Demo Pipeline Report</title>
  <style>
    body {{ margin: 32px; color: #151816; font: 14px/1.5 Arial, sans-serif; background: #f5f6f4; }}
    main {{ max-width: 980px; margin: 0 auto; }}
    h1, h2 {{ margin-bottom: 8px; }}
    .hero, .company, .box {{ border: 1px solid #dfe4df; border-radius: 8px; background: #fff; padding: 16px; margin: 14px 0; }}
    .hero {{ background: #10201d; color: #fff; }}
    .muted {{ color: #66706a; }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <h1>Demo Pipeline Report</h1>
    <p>Безопасный demo/report слой: не персистит факты, не запускает valuation и не вызывает LLM.</p>
  </section>
  <section class="box">
    <h2>Executive Summary</h2>
    <p>{escape(data['executive_summary']['what_the_system_does'])}</p>
    <p class="muted">Система показывает READY/PARTIAL/BLOCKED и причины, а не выдумывает недостающие показатели.</p>
  </section>
  {company_rows}
  <section class="box">
    <h2>Top Blockers</h2>
    <ul>{blockers}</ul>
  </section>
  <section class="box">
    <h2>Next Engineering Actions</h2>
    <ol>{next_steps}</ol>
  </section>
  <section class="box">
    <h2>Limitations</h2>
    <ul>{limitations}</ul>
  </section>
</main>
</body>
</html>
"""
