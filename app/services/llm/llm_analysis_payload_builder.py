import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings

COMPLIANCE_DISCLAIMER = (
    "Материал носит информационно-аналитический характер и не является индивидуальной инвестиционной рекомендацией."
)


@dataclass
class LLMAnalysisPayloadRequest:
    company_ticker: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    include_financial_ratios: bool = True
    include_source_documents: bool = True
    include_coverage_status: bool = True
    include_manual_uploads: bool = True
    include_provider_strategy: bool = False


@dataclass
class LLMAnalysisPayload:
    company: dict[str, Any]
    period: dict[str, Any]
    generated_at: str
    analysis_scope: dict[str, bool]
    source_documents: dict[str, Any]
    financial_ratios: dict[str, list[dict[str, Any]]]
    unavailable_metrics: list[dict[str, Any]]
    data_quality: dict[str, Any]
    blockers: list[str]
    limitations: list[str]
    llm_instructions: list[str]
    compliance_disclaimer: str
    evidence_paths: dict[str, str | None]
    normalized_facts: list[dict[str, Any]] = field(default_factory=list)
    derived_facts: list[dict[str, Any]] = field(default_factory=list)
    rejected_rows: list[dict[str, Any]] = field(default_factory=list)
    unresolved_numeric_evidence: list[dict[str, Any]] = field(default_factory=list)
    unresolved_table_evidence: list[dict[str, Any]] = field(default_factory=list)
    evidence_pack_warnings: list[str] = field(default_factory=list)
    market_technical_analysis: dict[str, Any] = field(default_factory=dict)
    source_pdf_attachments: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider_strategy: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LLMAnalysisPayloadBuilder:
    def __init__(self, root: Path | None = None):
        self.root = (root or get_settings().root_dir).resolve()
        self.warnings: list[str] = []

    def build(self, request: LLMAnalysisPayloadRequest) -> LLMAnalysisPayload:
        request = LLMAnalysisPayloadRequest(
            company_ticker=request.company_ticker.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            include_financial_ratios=request.include_financial_ratios,
            include_source_documents=request.include_source_documents,
            include_coverage_status=request.include_coverage_status,
            include_manual_uploads=request.include_manual_uploads,
            include_provider_strategy=request.include_provider_strategy,
        )
        self.warnings = []
        coverage = self._coverage_item(request) if request.include_coverage_status else {}
        ratios = (
            self._load_json(self._company_report_path(request, "financial_ratios"))
            if request.include_financial_ratios
            else {}
        )
        extraction = self._load_json(self._company_report_path(request, "statement_table_extraction"))
        fact_parse = self._load_json(self._company_report_path(request, "dataframe_statement_fact_parse"))
        comparison = self._load_json(self._company_report_path(request, "dataframe_vs_existing_fact_comparison"))
        market_technical = self._load_json(self._company_report_path(request, "market_technical_report"))
        peer_report = self._load_json(self._peer_report_path(request))
        manual = self._manual_report(request) if request.include_manual_uploads else {}
        provider = self._provider_strategy() if request.include_provider_strategy else None
        evidence_paths = self._evidence_paths(request)
        financial_ratios = self._financial_ratios_payload(ratios)
        parse_evidence = self._parse_evidence_payload(fact_parse)
        blockers = self._blockers(coverage, ratios)
        data_quality = self._data_quality(coverage, extraction, fact_parse, comparison, ratios, manual, market_technical)
        market_available = int((market_technical.get("summary") or {}).get("candles_count") or 0) > 0
        peer_available = peer_report.get("comparison_readiness") in {"READY_WITH_LIMITATIONS", "PARTIAL"}
        return LLMAnalysisPayload(
            company={
                "ticker": request.company_ticker,
                "name": coverage.get("company_name") or request.company_ticker,
                "coverage_level": coverage.get("coverage_level", "UNKNOWN"),
            },
            period={
                "from": request.period_from,
                "to": request.period_to,
                "reporting_standard": request.reporting_standard,
            },
            generated_at=datetime.now(UTC).isoformat(),
            analysis_scope={
                "statement_based_financials": coverage.get("coverage_level") == "FULL_STATEMENT_READY",
                "valuation_metrics": False,
                "peer_comparison": peer_available,
                "market_technical_analysis": market_available,
            },
            source_documents=self._source_documents_payload(
                extraction, manual, request.include_source_documents, market_technical, peer_report
            ),
            financial_ratios=financial_ratios,
            unavailable_metrics=financial_ratios["missing"] + financial_ratios["unsupported"] + financial_ratios["blocked"],
            data_quality=data_quality,
            blockers=blockers,
            limitations=self._limitations(data_quality, blockers),
            llm_instructions=self._llm_instructions(),
            compliance_disclaimer=COMPLIANCE_DISCLAIMER,
            evidence_paths=evidence_paths,
            normalized_facts=parse_evidence["normalized_facts"],
            derived_facts=parse_evidence["derived_facts"],
            rejected_rows=parse_evidence["rejected_rows"],
            unresolved_numeric_evidence=parse_evidence["unresolved_numeric_evidence"],
            unresolved_table_evidence=parse_evidence["unresolved_table_evidence"],
            evidence_pack_warnings=parse_evidence["evidence_pack_warnings"],
            market_technical_analysis=self._market_technical_payload(market_technical),
            source_pdf_attachments=self._source_pdf_attachments(manual),
            warnings=self.warnings,
            provider_strategy=provider,
        )

    def save_payload(self, payload: LLMAnalysisPayload) -> Path:
        ticker = payload.company["ticker"]
        period = payload.period
        root = self.root / "data" / "validation" / ticker
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{period['from']}_{period['to']}_llm_analysis_payload.json"
        path.write_text(json.dumps(payload.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _financial_ratios_payload(self, ratios: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        if not ratios:
            self.warnings.append("financial_ratios_report_missing")
            return {"calculated": [], "missing": [], "unsupported": [], "blocked": []}
        groups = {"calculated": [], "missing": [], "unsupported": [], "blocked": []}
        for metric in ratios.get("metrics", []):
            item = self._metric_payload(metric)
            status = item.get("status")
            if status == "calculated":
                groups["calculated"].append(item)
            elif status == "unsupported_by_policy":
                groups["unsupported"].append(item)
            elif str(status).startswith("blocked"):
                groups["blocked"].append(item)
            else:
                groups["missing"].append(item)
        return groups

    def _metric_payload(self, metric: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "metric_code": metric.get("metric_code"),
            "metric_name": metric.get("metric_name"),
            "period": metric.get("period"),
            "value": metric.get("value"),
            "display_value": metric.get("display_value"),
            "unit": metric.get("unit"),
            "formula": metric.get("formula"),
            "status": metric.get("status"),
            "warnings": metric.get("warnings", []),
            "methodology_notes": metric.get("methodology_notes", []),
        }
        if metric.get("status") == "calculated":
            payload["inputs"] = self._inputs_summary(metric.get("inputs") or {})
        else:
            payload["reason"] = metric.get("reason")
            payload["inputs_missing"] = metric.get("inputs_missing", [])
        if metric.get("trust_warning"):
            payload["trust_warning"] = metric.get("trust_warning")
        return payload

    def _inputs_summary(self, inputs: dict[str, Any]) -> dict[str, Any]:
        summary = {}
        for code, item in inputs.items():
            summary[code] = {
                "value": item.get("value"),
                "source_fact_id": item.get("source_fact_id")
                or item.get("current_source_fact_id")
                or item.get("previous_source_fact_id"),
                "source_location": item.get("source_location"),
                "quality_flag": item.get("quality_flag"),
                "period_type": item.get("period_type"),
                "raw_label": item.get("raw_label"),
            }
        return summary

    def _data_quality(
        self,
        coverage: dict[str, Any],
        extraction: dict[str, Any],
        fact_parse: dict[str, Any],
        comparison: dict[str, Any],
        ratios: dict[str, Any],
        manual: dict[str, Any],
        market_technical: dict[str, Any],
    ) -> dict[str, Any]:
        coverage_level = coverage.get("coverage_level", "UNKNOWN")
        manual_used = bool(manual)
        warnings = list(self.warnings)
        if manual_used:
            warnings.append("manual_upload_lower_trust_fallback")
        main_blocker = coverage.get("main_blocker")
        if main_blocker:
            warnings.append(main_blocker)
        return {
            "coverage_level": coverage_level,
            "main_status": self._main_status(coverage_level),
            "main_blocker": None if coverage_level == "FULL_STATEMENT_READY" else main_blocker,
            "sector_policy": ratios.get("sector_policy") or {},
            "source_package_ready": coverage.get("source_package_status") == "READY",
            "official_source_verified": not manual_used and coverage_level == "FULL_STATEMENT_READY",
            "manual_upload_used": manual_used,
            "source_trust_bucket": manual.get("source_trust_bucket")
            or ("verified_official_pipeline" if coverage_level == "FULL_STATEMENT_READY" else "unknown"),
            "manual_official_source_verified": manual.get("official_source_verified") if manual_used else None,
            "source_package_ready_contribution": manual.get("source_package_ready_contribution") if manual_used else None,
            "statement_tables_available": int(extraction.get("statement_tables_count") or 0) > 0,
            "fact_candidates_available": int(
                fact_parse.get("canonical_fact_candidates") or fact_parse.get("canonical_facts_created") or 0
            )
            > 0,
            "structured_facts_count": int(len(fact_parse.get("structured_facts") or [])),
            "derived_safe_facts_count": int(len(fact_parse.get("derived_safe_facts") or [])),
            "rejected_rows_count": int(len(fact_parse.get("rejected_rows") or [])),
            "unmapped_numeric_evidence_count": int(len(fact_parse.get("unmapped_numeric_evidence") or [])),
            "unmapped_table_evidence_count": int(len(fact_parse.get("unmapped_table_evidence") or [])),
            "llm_ready_evidence_pack_available": bool(fact_parse.get("llm_ready_evidence_pack")),
            "ratios_report_available": bool(ratios),
            "market_technical_report_available": bool(market_technical),
            "conflicts_count": int(comparison.get("conflict_count") or 0),
            "warnings": sorted(set(warnings)),
        }

    def _source_documents_payload(
        self,
        extraction: dict[str, Any],
        manual: dict[str, Any],
        include_source_documents: bool,
        market_technical: dict[str, Any] | None = None,
        peer_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not include_source_documents:
            return {"included": False}
        return {
            "included": True,
            "statement_table_extraction": {
                "available": bool(extraction),
                "documents_processed": extraction.get("documents_processed"),
                "statement_tables_count": extraction.get("statement_tables_count"),
                "statement_coverage": extraction.get("statement_coverage"),
                "facts_extracted": extraction.get("facts_extracted"),
                "fact_parser_status": extraction.get("fact_parser_status"),
            },
            "manual_upload": {
                "available": bool(manual),
                "source_trust_bucket": manual.get("source_trust_bucket"),
                "official_source_verified": manual.get("official_source_verified"),
                "source_package_ready_contribution": manual.get("source_package_ready_contribution"),
                "report_document_id": manual.get("report_document_id"),
                "source_pdf": self._manual_source_pdf_payload(manual),
            },
            "market_technical_report": {
                "available": bool(market_technical),
                "status": (market_technical or {}).get("status"),
                "market_data_mode": (market_technical or {}).get("market_data_mode"),
                "provider": (market_technical or {}).get("provider"),
                "latest_summary": (market_technical or {}).get("latest_summary"),
            },
            "peer_analysis_report": {
                "available": bool(peer_report),
                "peer_selection_status": (peer_report or {}).get("peer_selection_status"),
                "comparison_data_status": (peer_report or {}).get("comparison_data_status"),
                "comparison_readiness": (peer_report or {}).get("comparison_readiness"),
                "valuation_status": (peer_report or {}).get("valuation_status"),
            },
        }

    def _manual_source_pdf_payload(self, manual: dict[str, Any]) -> dict[str, Any]:
        stored_path = str(manual.get("stored_document_path") or "").strip()
        if not stored_path:
            return {"available": False, "reason": "manual_upload_pdf_path_missing"}
        path = Path(stored_path)
        if not path.is_absolute() and not path.exists():
            path = self.root / path
        is_pdf = path.suffix.casefold() == ".pdf"
        exists = path.exists() and path.is_file()
        return {
            "available": bool(exists and is_pdf),
            "path": str(path),
            "file_name": manual.get("input_file") or path.name,
            "source_document_id": manual.get("report_document_id"),
            "media_type": "application/pdf" if is_pdf else None,
            "llm_attachment_enabled": bool(exists and is_pdf),
            "trust_warning": "manual_upload_lower_trust_fallback",
            "reason": None if exists and is_pdf else "manual_upload_pdf_not_available_or_not_pdf",
        }

    def _source_pdf_attachments(self, manual: dict[str, Any]) -> list[dict[str, Any]]:
        pdf = self._manual_source_pdf_payload(manual)
        if not pdf.get("llm_attachment_enabled"):
            return []
        return [pdf]

    def _market_technical_payload(self, market_technical: dict[str, Any]) -> dict[str, Any]:
        if not market_technical:
            return {"available": False}
        return {
            "available": True,
            "ticker": market_technical.get("ticker"),
            "board": market_technical.get("board"),
            "provider": market_technical.get("provider"),
            "market_data_source": market_technical.get("market_data_source"),
            "market_data_mode": market_technical.get("market_data_mode"),
            "status": market_technical.get("status"),
            "requested_date_range": market_technical.get("requested_date_range"),
            "actual_candle_date_range": market_technical.get("actual_candle_date_range"),
            "market_period_aligned": market_technical.get("market_period_aligned"),
            "coverage": market_technical.get("coverage"),
            "summary": market_technical.get("summary") or {},
            "latest_summary": market_technical.get("latest_summary") or {},
            "technical_indicators": market_technical.get("technical_indicators") or [],
            "liquidity_metrics": market_technical.get("liquidity_metrics") or [],
            "valuation_inputs": market_technical.get("valuation_inputs") or [],
            "warnings": market_technical.get("warnings") or [],
            "instructions": [
                "Use market_technical_analysis only as market context, not as financial statement facts.",
                "Daily candles do not provide bid/ask spread unless explicitly present in liquidity_metrics.",
                "Do not infer valuation metrics when valuation_inputs are missing.",
            ],
        }

    def _blockers(self, coverage: dict[str, Any], ratios: dict[str, Any]) -> list[str]:
        blockers = set(coverage.get("blockers") or [])
        if coverage.get("main_blocker"):
            blockers.add(coverage["main_blocker"])
        if ratios:
            for metric in ratios.get("metrics", []):
                if str(metric.get("status", "")).startswith("blocked"):
                    blockers.add(metric.get("reason") or metric.get("status"))
        return sorted(blocker for blocker in blockers if blocker)

    def _limitations(self, data_quality: dict[str, Any], blockers: list[str]) -> list[str]:
        limitations = [
            "Payload is assembled from existing reports only; no ratios or facts were recalculated.",
            "Valuation metrics are not included and must not be inferred.",
            "Peer comparison is outside this payload unless a peer analysis report is explicitly present.",
            "Missing, unsupported, or blocked metrics must remain unavailable in downstream analysis.",
            "This payload does not claim universal issuer support.",
        ]
        if data_quality.get("manual_upload_used"):
            limitations.append("Manual upload is lower-trust fallback and not a verified official source package.")
        if "image_only_primary_statement_pages" in blockers:
            limitations.append("Primary statements are image-only or unavailable to text/table extraction.")
        if "missing_fy_report" in blockers:
            limitations.append("Full-year source report is missing for the requested period.")
        return limitations

    def _llm_instructions(self) -> list[str]:
        return [
            "Use only numbers present in this JSON.",
            "Do not invent missing metrics.",
            "Do not calculate new ratios unless formula inputs are explicitly present in this JSON.",
            "If a metric is missing, unsupported, or blocked, mention that limitation.",
            "Do not treat manual_upload as verified official source.",
            "Do not make investment recommendations.",
            "Use cautious analytical wording.",
            "Distinguish calculated facts from unavailable metrics.",
            "Mention data quality blockers when relevant.",
            "Use structured_facts as normalized facts.",
            "Use derived_safe_facts only with caution.",
            "Do not treat unresolved evidence as confirmed financial facts.",
            "Do not calculate ratios from unresolved evidence.",
            "Unresolved evidence may be mentioned only as unverified supporting material.",
        ]

    def _parse_evidence_payload(self, fact_parse: dict[str, Any]) -> dict[str, Any]:
        pack = fact_parse.get("llm_ready_evidence_pack") or {}
        if pack:
            return {
                "normalized_facts": pack.get("normalized_facts") or fact_parse.get("structured_facts") or [],
                "derived_facts": pack.get("derived_safe_facts") or fact_parse.get("derived_safe_facts") or [],
                "rejected_rows": pack.get("rejected_rows") or fact_parse.get("rejected_rows") or [],
                "unresolved_numeric_evidence": pack.get("unresolved_numeric_evidence")
                or fact_parse.get("unmapped_numeric_evidence")
                or [],
                "unresolved_table_evidence": pack.get("unresolved_table_evidence")
                or fact_parse.get("unmapped_table_evidence")
                or [],
                "evidence_pack_warnings": pack.get("evidence_warnings") or [],
            }
        return {
            "normalized_facts": fact_parse.get("structured_facts") or [],
            "derived_facts": fact_parse.get("derived_safe_facts") or [],
            "rejected_rows": fact_parse.get("rejected_rows") or [],
            "unresolved_numeric_evidence": fact_parse.get("unmapped_numeric_evidence") or [],
            "unresolved_table_evidence": fact_parse.get("unmapped_table_evidence") or [],
            "evidence_pack_warnings": [],
        }

    def _coverage_item(self, request: LLMAnalysisPayloadRequest) -> dict[str, Any]:
        coverage = self._load_json(self._coverage_path(request), required=False)
        for item in coverage.get("company_items", []):
            if item.get("ticker") == request.company_ticker:
                return item
        self.warnings.append("coverage_item_missing")
        return {}

    def _manual_report(self, request: LLMAnalysisPayloadRequest) -> dict[str, Any]:
        path = self.root / "data" / "validation" / request.company_ticker / f"{request.period_to}_manual_report_ingestion.json"
        return self._load_json(path)

    def _provider_strategy(self) -> dict[str, Any]:
        provider = self._load_json(self.root / "data" / "validation" / "providers" / "provider_feasibility_scan.json")
        if not provider:
            return {"available": False}
        best = provider.get("best_candidates_by_data_type") or {}
        return {
            "available": True,
            "production_ready_providers": (provider.get("summary") or {}).get("production_ready_count", 0),
            "ifrs_report_metadata_candidate": best.get("IFRS report discovery"),
            "ras_financials_candidate": best.get("RAS financial statements"),
            "market_identity_candidate": best.get("company identity"),
            "valuation_candidate": best.get("valuation inputs"),
            "warning": "Provider strategy is feasibility-only and not production-ready integration.",
        }

    def _main_status(self, coverage_level: str) -> str:
        if coverage_level == "FULL_STATEMENT_READY":
            return "READY"
        if coverage_level == "PARTIAL_STATEMENT_READY":
            return "PARTIAL"
        if coverage_level in {"UNKNOWN", "UNSUPPORTED"}:
            return "BLOCKED"
        return "BLOCKED"

    def _coverage_path(self, request: LLMAnalysisPayloadRequest) -> Path:
        year = request.period_from[:4]
        filename = f"moex_{request.reporting_standard.lower()}_{year}_universal_coverage_scan.json"
        return self.root / "data" / "validation" / "coverage" / filename

    def _company_report_path(self, request: LLMAnalysisPayloadRequest, suffix: str) -> Path:
        filename = f"{request.period_from}_{request.period_to}_{suffix}.json"
        return self.root / "data" / "validation" / request.company_ticker / filename

    def _evidence_paths(self, request: LLMAnalysisPayloadRequest) -> dict[str, str | None]:
        manual_filename = f"{request.period_to}_manual_report_ingestion.json"
        manual_path = self.root / "data" / "validation" / request.company_ticker / manual_filename
        paths = {
            "coverage_report": self._coverage_path(request),
            "financial_ratios_report": self._company_report_path(request, "financial_ratios"),
            "statement_table_extraction_report": self._company_report_path(request, "statement_table_extraction"),
            "dataframe_fact_parse_report": self._company_report_path(request, "dataframe_statement_fact_parse"),
            "comparison_report": self._company_report_path(request, "dataframe_vs_existing_fact_comparison"),
            "market_technical_report": self._company_report_path(request, "market_technical_report"),
            "peer_analysis_report": self._peer_report_path(request),
            "manual_ingestion_report": manual_path,
            "demo_pipeline_report": self.root / "data" / "validation" / "demo" / "demo_pipeline_report.json",
        }
        return {name: str(path) if path.exists() else None for name, path in paths.items()}

    def _peer_report_path(self, request: LLMAnalysisPayloadRequest) -> Path:
        filename = f"{request.company_ticker}_{request.period_from}_{request.period_to}_peer_analysis_report.json"
        return self.root / "data" / "validation" / "PEERS" / filename

    def _load_json(self, path: Path, required: bool = False) -> dict[str, Any]:
        if not path.exists():
            if required:
                self.warnings.append(f"report_missing:{path}")
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.warnings.append(f"invalid_json:{path}:{exc}")
            return {}
