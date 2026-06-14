import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, ReportDocument
from app.services.company_source_discovery import CompanySourceDiscoveryService
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser
from app.services.periods import period_in_range, periods_between
from app.services.reports.financial_report_discovery import (
    FinancialReportDiscoveryRequest,
    FinancialReportDiscoveryService,
)

SOURCE_BLOCKERS = {
    "company_not_resolved",
    "ambiguous_company_identity",
    "no_trusted_source_candidates",
    "missing_period_report",
    "missing_fy_report",
    "source_package_not_ready",
    "tls_download_failed",
    "document_validation_failed",
    "wrong_document_role",
}
PARSER_BLOCKERS = {
    "no_cached_documents",
    "no_primary_statement_tables",
    "image_only_primary_statement_pages",
    "table_extraction_failed",
    "toc_or_notes_only",
    "no_safe_fact_candidates",
    "text_fallback_requires_gate",
    "notes_rejected",
    "ambiguous_period_column",
    "missing_source_location",
    "low_confidence_labels",
    "missing_expected_statement_facts",
}


@dataclass
class CoverageScanRequest:
    market: str = "MOEX"
    tickers: list[str] | None = None
    from_registry: bool = True
    limit: int | None = None
    max_companies: int | None = None
    period_from: str = "2021Q1"
    period_to: str = "2021Q4"
    reporting_standard: str = "IFRS"
    live: bool = False
    replay_cache: bool = True
    allow_text_fallback_semantic_gate: bool = False
    run_missing_table_extraction: bool = False


@dataclass
class CoverageScanReport:
    market: str
    reporting_standard: str
    period_from: str
    period_to: str
    generated_at: str
    scan_mode: str
    actions_executed: list[str]
    artifacts_created: bool
    companies_scanned: int
    summary: dict[str, Any]
    company_items: list[dict[str, Any]]
    blocker_summary: list[dict[str, Any]]
    recommended_next_actions: list[dict[str, Any]]
    recommended_next_engineering_action: dict[str, Any] | None
    recommended_not_to_prioritize: list[dict[str, Any]]
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UniversalCoverageScanner:
    def __init__(self, db: Session, root: Path | None = None):
        self.db = db
        self.root = root or get_settings().root_dir
        self.artifacts_created = False

    def scan(self, request: CoverageScanRequest) -> CoverageScanReport:
        companies = self._companies(request)
        actions = ["statement_table_extraction"] if request.run_missing_table_extraction else []
        items = []
        for company in companies:
            try:
                items.append(self._company_item(company, request))
            except Exception as exc:
                items.append(self._failed_company_item(company, request, exc))
        summary = self._summary(items)
        blockers = self._blocker_summary(items)
        next_engineering_action = recommended_next_engineering_action(blockers)
        return CoverageScanReport(
            market=request.market,
            reporting_standard=request.reporting_standard.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            generated_at=datetime.now(UTC).isoformat(),
            scan_mode="replay_cache_with_table_extraction"
            if request.run_missing_table_extraction
            else "existing_artifacts_only",
            actions_executed=actions,
            artifacts_created=self.artifacts_created,
            companies_scanned=len(items),
            summary=summary,
            company_items=items,
            blocker_summary=blockers,
            recommended_next_actions=self._recommended_next_actions(blockers),
            recommended_next_engineering_action=next_engineering_action,
            recommended_not_to_prioritize=recommended_not_to_prioritize(blockers, next_engineering_action),
            limitations=[
                "Coverage scanner is report-only and does not guarantee full analysis.",
                "Default mode reads existing artifacts only and does not create statement facts.",
                "Valuation and EBITDA policy blockers are reported separately from parser blockers.",
            ],
        )

    def save_report(self, report: CoverageScanReport) -> Path:
        root = self.root / "data" / "validation" / "coverage"
        root.mkdir(parents=True, exist_ok=True)
        year = report.period_from[:4]
        path = root / f"{report.market.lower()}_{report.reporting_standard.lower()}_{year}_universal_coverage_scan.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _companies(self, request: CoverageScanRequest) -> list[Company]:
        query = select(Company).where(Company.is_active.is_(True)).order_by(Company.ticker)
        if request.tickers:
            tickers = [ticker.strip().upper() for ticker in request.tickers if ticker.strip()]
            query = query.where(Company.ticker.in_(tickers))
        elif not request.from_registry:
            return []
        limit = request.max_companies or request.limit
        if limit:
            query = query.limit(limit)
        companies = list(self.db.scalars(query).all())
        if request.tickers:
            order = {ticker.strip().upper(): index for index, ticker in enumerate(request.tickers)}
            companies.sort(key=lambda company: order.get(company.ticker.upper(), 10_000))
        return companies

    def _company_item(self, company: Company, request: CoverageScanRequest) -> dict[str, Any]:
        blockers: list[str] = []
        reports: dict[str, str] = {}
        source_report = CompanySourceDiscoveryService(self.db, root=self.root).discover(
            company.ticker,
            ticker=company.ticker,
            market=request.market,
            live=False,
        )
        identity_status = "RESOLVED"
        trusted_candidates = [
            item for item in source_report.recommended_candidates if item.trust_status in {"trusted_candidate", "ambiguous"}
        ]
        source_discovery_status = "READY" if trusted_candidates else "NOT_FOUND"
        if not trusted_candidates:
            blockers.append("no_trusted_source_candidates")

        financial_report = FinancialReportDiscoveryService(self.db, root=self.root).discover(
            FinancialReportDiscoveryRequest(
                company_query=company.ticker,
                ticker=company.ticker,
                period_from=request.period_from,
                period_to=request.period_to,
                reporting_standard=request.reporting_standard,
                live=False,
            )
        )
        report_discovery_status = financial_report.status
        if financial_report.missing_periods:
            blockers.append("missing_period_report")
            if request.period_to.endswith("Q4") and request.period_to in financial_report.missing_periods:
                blockers.append("missing_fy_report")

        source_package_report = self._load_json(self._source_package_report_path(company.ticker, request))
        source_package_status = source_package_report.get("status")
        source_summary = source_package_report.get("summary") or {}
        missing_source_periods = source_summary.get("missing_periods") or []
        if request.period_to.endswith("Q4") and request.period_to in missing_source_periods:
            blockers.append("missing_fy_report")
        if source_package_status and source_package_status != "READY":
            blockers.append("source_package_not_ready")

        documents = self._cached_documents(company, request)
        documents_status = self._documents_status(documents, request)
        if documents_status == "NOT_CACHED":
            blockers.append("no_cached_documents")
        manual_documents = [doc for doc in documents if getattr(doc, "source_type", None) == "manual_upload"]
        if manual_documents:
            blockers.append("manual_upload_available")
            if report_discovery_status != "READY" or source_package_status != "READY":
                blockers.append("official_source_missing")

        table_report = self._statement_table_report(company.ticker, request)
        if request.run_missing_table_extraction and documents and not table_report:
            table_report = self._run_table_extraction(company.ticker, request)
        if not table_report:
            table_report = self._fallback_table_report_from_artifacts(company.ticker, request)
        table_status, table_blockers = self._table_status(table_report)
        blockers.extend(table_blockers)

        validation_report = self._load_json(self._validation_report_path(company.ticker, request))
        fact_status, fact_blockers, fact_report = self._fact_status(company, request, bool(table_report), validation_report)
        blockers.extend(fact_blockers)

        scopes = ((validation_report.get("automated_support_status") or {}).get("scope_statuses") or {})
        metric_scopes = self._metric_scope_statuses(scopes)
        if "valuation_metrics" in metric_scopes.get("unsupported_output_scopes", []):
            blockers.append("unavailable_valuation_inputs")
        blockers.append("unavailable_valuation_inputs")

        blockers = sorted(set(blockers), key=blocker_priority)
        coverage_level = self._coverage_level(metric_scopes, documents, table_status, fact_status, blockers)
        main_blocker = None if coverage_level == "FULL_STATEMENT_READY" else blockers[0] if blockers else None
        return {
            "ticker": company.ticker,
            "company_name": company.full_name,
            "sector": company.sector,
            "coverage_level": coverage_level,
            "main_blocker": main_blocker,
            "identity_status": identity_status,
            "source_discovery_status": source_discovery_status,
            "report_discovery_status": report_discovery_status,
            "source_package_status": source_package_status or "UNKNOWN",
            "documents_status": documents_status,
            "table_extraction_status": table_status,
            "fact_extraction_status": fact_status,
            "metric_scope_statuses": metric_scopes,
            "blockers": blockers,
            "supported_outputs": self._supported_outputs(coverage_level, metric_scopes, blockers),
            "unsupported_outputs": self._unsupported_outputs(blockers, metric_scopes),
            "recommended_next_action": recommended_action_for_blockers(blockers),
            "reports_paths": {
                **reports,
                "source_package": str(self._source_package_report_path(company.ticker, request)),
                "statement_table_extraction": str(self._statement_table_report_path(company.ticker, request)),
                "dataframe_fact_parse": str(self._dataframe_parse_report_path(company.ticker, request)),
                "real_validation": str(self._validation_report_path(company.ticker, request)),
            },
            "counts": {
                "cached_documents": len(documents),
                "manual_upload_documents": len(manual_documents),
                "statement_tables": int((table_report or {}).get("statement_tables_count") or 0),
                "canonical_fact_candidates": int((fact_report or {}).get("canonical_facts_created") or 0),
            },
            "manual_upload": {
                "available": bool(manual_documents),
                "reasons": sorted(
                    {
                        getattr(doc, "manual_upload_reason", None)
                        for doc in manual_documents
                        if getattr(doc, "manual_upload_reason", None)
                    }
                ),
                "source_trust_buckets": sorted(
                    {
                        getattr(doc, "source_trust_bucket", None)
                        for doc in manual_documents
                        if getattr(doc, "source_trust_bucket", None)
                    }
                ),
                "official_source_verified": False,
                "source_package_ready_contribution": False,
            },
        }

    def _failed_company_item(self, company: Company, request: CoverageScanRequest, exc: Exception) -> dict[str, Any]:
        return {
            "ticker": company.ticker,
            "company_name": company.full_name,
            "sector": company.sector,
            "coverage_level": "UNSUPPORTED",
            "main_blocker": "scanner_company_failed",
            "identity_status": "RESOLVED",
            "source_discovery_status": "SOURCE_BLOCKED",
            "report_discovery_status": "SOURCE_BLOCKED",
            "source_package_status": "UNKNOWN",
            "documents_status": "NOT_CACHED",
            "table_extraction_status": "EXTRACTION_FAILED",
            "fact_extraction_status": "NOT_RUN",
            "metric_scope_statuses": {},
            "blockers": ["scanner_company_failed"],
            "warnings": [str(exc)],
            "supported_outputs": [],
            "unsupported_outputs": ["financial_facts", "financial_metrics", "valuation_metrics", "peer_comparison"],
            "recommended_next_action": "Inspect scanner failure for this company.",
            "reports_paths": {},
            "counts": {},
        }

    def _cached_documents(self, company: Company, request: CoverageScanRequest) -> list[ReportDocument]:
        docs = self.db.scalars(
            select(ReportDocument).where(
                ReportDocument.company_id == company.id,
                ReportDocument.reporting_standard == request.reporting_standard.upper(),
                ReportDocument.source_type != "fixture",
                ReportDocument.status.in_(["downloaded", "parsed", "validated"]),
            )
        ).all()
        return [
            doc
            for doc in docs
            if period_in_range(doc.report_period, request.period_from, request.period_to)
            and _usable_statement_role(doc.source_role)
        ]

    def _documents_status(self, documents: list[ReportDocument], request: CoverageScanRequest) -> str:
        if not documents:
            return "NOT_CACHED"
        periods = {doc.report_period for doc in documents}
        expected = set(periods_between(request.period_from, request.period_to))
        if all(getattr(doc, "source_type", None) == "manual_upload" for doc in documents):
            return "CACHED_VALIDATED_MANUAL" if expected.issubset(periods) else "CACHED_PARTIAL_MANUAL"
        if any(getattr(doc, "source_type", None) == "manual_upload" for doc in documents):
            return "CACHED_VALIDATED_WITH_MANUAL" if expected.issubset(periods) else "CACHED_PARTIAL_WITH_MANUAL"
        return "CACHED_VALIDATED" if expected.issubset(periods) else "CACHED_PARTIAL"

    def _statement_table_report(self, ticker: str, request: CoverageScanRequest) -> dict[str, Any]:
        return self._load_json(self._statement_table_report_path(ticker, request))

    def _run_table_extraction(self, ticker: str, request: CoverageScanRequest) -> dict[str, Any]:
        before = {path for path in self._artifact_paths(ticker, request) if path.exists()}
        from app.tools.extract_statement_tables import extract_statement_tables

        report = extract_statement_tables(
            ticker,
            request.period_from,
            request.period_to,
            request.reporting_standard,
            replay_cache=True,
        )
        after = {path for path in self._artifact_paths(ticker, request) if path.exists()}
        self.artifacts_created = self.artifacts_created or bool(after - before)
        return report

    def _fallback_table_report_from_artifacts(self, ticker: str, request: CoverageScanRequest) -> dict[str, Any]:
        artifact_paths = self._artifact_paths(ticker, request)
        if not artifact_paths:
            return {}
        tables_extracted = 0
        statement_tables_count = 0
        cash_flow_tables_count = 0
        warnings: list[str] = []
        required_statement_tables_found = False
        for path in artifact_paths:
            payload = self._load_json(path)
            tables = list(payload.get("statement_tables") or payload.get("normalized_statement_tables") or [])
            tables_extracted += int(payload.get("tables_extracted") or len(tables))
            statement_tables_count += int(
                payload.get("statement_tables_count")
                or sum(1 for table in tables if str(table.get("statement_type") or "unknown") != "unknown")
            )
            cash_flow_tables_count += int(
                payload.get("cash_flow_tables_count")
                or sum(1 for table in tables if str(table.get("statement_type") or "") == "cash_flow")
            )
            warnings.extend(list(payload.get("warnings") or []))
            required_statement_tables_found = required_statement_tables_found or bool(
                payload.get("required_statement_tables_found")
            )
        if statement_tables_count and not required_statement_tables_found:
            required_statement_tables_found = True
        return {
            "tables_extracted": tables_extracted,
            "statement_tables_count": statement_tables_count,
            "cash_flow_tables_count": cash_flow_tables_count,
            "required_statement_tables_found": required_statement_tables_found,
            "warnings": sorted(set(warnings)),
        }

    def _artifact_paths(self, ticker: str, request: CoverageScanRequest) -> list[Path]:
        root = self.root / get_settings().report_parsed_dir / ticker.upper()
        if not root.exists():
            return []
        return [
            path
            for path in root.glob("*/*_statement_tables.json")
            if self._artifact_period_in_range(path.parent.name, request)
        ]

    def _artifact_period_in_range(self, period_value: str, request: CoverageScanRequest) -> bool:
        try:
            return period_in_range(period_value, request.period_from, request.period_to)
        except ValueError:
            return False

    def _table_status(self, report: dict[str, Any]) -> tuple[str, list[str]]:
        if not report:
            return "EXTRACTION_FAILED", ["no_primary_statement_tables"]
        blockers: list[str] = []
        warnings = report.get("warnings") or []
        if any("primary_statement_page_image_only_or_no_extractable_text" in warning for warning in warnings):
            blockers.append("image_only_primary_statement_pages")
        if int(report.get("tables_extracted") or 0) and int(report.get("statement_tables_count") or 0) == 0:
            blockers.append("toc_or_notes_only")
        if not report.get("required_statement_tables_found"):
            blockers.append("no_primary_statement_tables")
        if blockers:
            if "image_only_primary_statement_pages" in blockers:
                return "IMAGE_ONLY_PRIMARY_STATEMENTS", blockers
            return "NO_PRIMARY_STATEMENTS", blockers
        if report.get("cash_flow_tables_count", 0) == 0:
            return "TABLES_PARTIAL", []
        return "TABLES_READY", []

    def _fact_status(
        self,
        company: Company,
        request: CoverageScanRequest,
        has_table_report: bool,
        validation_report: dict[str, Any],
    ) -> tuple[str, list[str], dict[str, Any]]:
        summary = validation_report.get("summary") or {}
        canonical_facts = int(summary.get("canonical_facts_count") or 0)
        high_confidence = int(summary.get("high_confidence_fact_count") or 0)
        conflicts = int(summary.get("conflicting_fact_count") or 0)
        if canonical_facts and not conflicts:
            status = "FACTS_READY" if high_confidence == canonical_facts else "FACTS_PARTIAL"
            return status, [], {"source": "real_validation_report", "canonical_facts_count": canonical_facts}
        existing_parse_report = self._load_json(self._dataframe_parse_report_path(company.ticker, request))
        if existing_parse_report:
            existing_canonical_facts = int(existing_parse_report.get("canonical_facts_created") or 0)
            readiness = dict(existing_parse_report.get("analysis_readiness_summary") or {})
            if existing_canonical_facts > 0 and readiness.get("facts_ready") is True:
                return "FACTS_READY", [], existing_parse_report
            if existing_canonical_facts > 0:
                blockers = (
                    ["missing_expected_statement_facts"]
                    if existing_parse_report.get("missing_expected_facts")
                    else []
                )
                return "FACTS_PARTIAL", blockers, existing_parse_report
        if not has_table_report:
            return "NOT_RUN", [], {}
        result = DataFrameStatementParser(
            self.db,
            root=self.root,
            allow_text_fallback_semantic_gate=request.allow_text_fallback_semantic_gate,
        ).parse(company.ticker, request.period_from, request.period_to, request.reporting_standard)
        report = result.to_dict()
        if result.canonical_facts_created == 0:
            return "NO_FACTS", ["no_safe_fact_candidates"], report
        readiness = dict(report.get("analysis_readiness_summary") or {})
        if readiness.get("facts_ready") is True:
            return "FACTS_READY", [], report
        blockers = ["missing_expected_statement_facts"] if result.missing_expected_facts else []
        return ("FACTS_PARTIAL" if blockers else "FACTS_READY"), blockers, report

    def _metric_scope_statuses(self, scopes: dict[str, Any]) -> dict[str, Any]:
        return {
            "statement_based_financials": (scopes.get("statement_based_financials") or {}).get("status", "UNSUPPORTED"),
            "market_technical_analysis": (scopes.get("market_technical_analysis") or {}).get("status", "UNSUPPORTED"),
            "valuation_metrics": (scopes.get("valuation_metrics") or {}).get("status", "UNAVAILABLE"),
            "peer_comparison": (scopes.get("peer_comparison") or {}).get("status", "UNSUPPORTED"),
            "raw_scope_statuses": scopes,
        }

    def _coverage_level(
        self,
        metric_scopes: dict[str, Any],
        documents: list[ReportDocument],
        table_status: str,
        fact_status: str,
        blockers: list[str],
    ) -> str:
        if metric_scopes.get("statement_based_financials") == "AUTO_READY":
            return "FULL_STATEMENT_READY"
        if any(blocker in SOURCE_BLOCKERS for blocker in blockers):
            return "SOURCE_BLOCKED"
        if table_status in {"TABLES_READY", "TABLES_PARTIAL"} and fact_status == "FACTS_READY":
            return "FULL_STATEMENT_READY"
        if any(blocker in PARSER_BLOCKERS for blocker in blockers) or table_status in {
            "IMAGE_ONLY_PRIMARY_STATEMENTS",
            "NO_PRIMARY_STATEMENTS",
            "EXTRACTION_FAILED",
        }:
            return "PARSER_BLOCKED"
        if documents or fact_status in {"FACTS_READY", "FACTS_PARTIAL"}:
            return "PARTIAL_STATEMENT_READY"
        if metric_scopes.get("market_technical_analysis") in {"AUTO_READY", "AUTO_PARTIAL"}:
            return "MARKET_ONLY"
        return "UNSUPPORTED"

    def _supported_outputs(self, coverage_level: str, metric_scopes: dict[str, Any], blockers: list[str]) -> list[str]:
        outputs = []
        if coverage_level in {"FULL_STATEMENT_READY", "PARTIAL_STATEMENT_READY"}:
            outputs.extend(["financial_facts", "financial_metrics"])
        if metric_scopes.get("market_technical_analysis") in {"AUTO_READY", "AUTO_PARTIAL"}:
            outputs.append("market_analysis")
        if any(blocker == "manual_upload_available" for blocker in blockers):
            outputs.append("manual_document_analysis")
        return sorted(set(outputs))

    def _unsupported_outputs(self, blockers: list[str], metric_scopes: dict[str, Any]) -> list[str]:
        outputs = ["valuation_metrics", "peer_comparison", "llm_payload"]
        if any(blocker in SOURCE_BLOCKERS | PARSER_BLOCKERS for blocker in blockers):
            outputs.extend(["financial_facts", "financial_metrics"])
        if metric_scopes.get("market_technical_analysis") == "UNSUPPORTED":
            outputs.append("market_analysis")
        return sorted(set(outputs))

    def _summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        coverage = Counter(item["coverage_level"] for item in items)
        return {
            "companies_scanned": len(items),
            "identity_resolved_count": sum(1 for item in items if item["identity_status"] == "RESOLVED"),
            "source_ready_count": sum(1 for item in items if item["source_discovery_status"] == "READY"),
            "report_ready_count": sum(1 for item in items if item["report_discovery_status"] == "READY"),
            "cached_documents_ready_count": sum(1 for item in items if item["documents_status"] == "CACHED_VALIDATED"),
            "statement_tables_ready_count": sum(1 for item in items if item["table_extraction_status"] == "TABLES_READY"),
            "facts_ready_count": sum(1 for item in items if item["fact_extraction_status"] == "FACTS_READY"),
            "statement_scope_auto_ready_count": sum(
                1 for item in items if item["metric_scope_statuses"].get("statement_based_financials") == "AUTO_READY"
            ),
            "statement_scope_auto_partial_count": sum(
                1 for item in items if item["metric_scope_statuses"].get("statement_based_financials") == "AUTO_PARTIAL"
            ),
            "market_technical_ready_count": sum(
                1 for item in items if item["metric_scope_statuses"].get("market_technical_analysis") == "AUTO_READY"
            ),
            "valuation_unavailable_count": sum(
                1 for item in items if item["metric_scope_statuses"].get("valuation_metrics") in {"UNAVAILABLE", "UNSUPPORTED"}
            ),
            "unsupported_count": coverage.get("UNSUPPORTED", 0),
            "coverage_level_counts": dict(coverage),
        }

    def _blocker_summary(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tickers: dict[str, list[str]] = defaultdict(list)
        for item in items:
            for blocker in item.get("blockers", []):
                tickers[blocker].append(item["ticker"])
        return [
            {
                "blocker_code": blocker,
                "count": len(values),
                "tickers_sample": values[:5],
                "recommended_action": recommended_action_for_blockers([blocker]),
            }
            for blocker, values in sorted(tickers.items(), key=lambda entry: (-len(entry[1]), blocker_priority(entry[0])))
        ]

    def _recommended_next_actions(self, blocker_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "rank": index + 1,
                "blocker_code": item["blocker_code"],
                "affected_companies": item["count"],
                "recommended_action": item["recommended_action"],
            }
            for index, item in enumerate(blocker_summary[:5])
        ]

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _source_package_report_path(self, ticker: str, request: CoverageScanRequest) -> Path:
        return (
            self.root
            / "data"
            / "validation"
            / ticker.upper()
            / f"{request.period_from}_{request.period_to}_source_package_report.json"
        )

    def _statement_table_report_path(self, ticker: str, request: CoverageScanRequest) -> Path:
        return (
            self.root
            / "data"
            / "validation"
            / ticker.upper()
            / f"{request.period_from}_{request.period_to}_statement_table_extraction.json"
        )

    def _dataframe_parse_report_path(self, ticker: str, request: CoverageScanRequest) -> Path:
        return (
            self.root
            / "data"
            / "validation"
            / ticker.upper()
            / f"{request.period_from}_{request.period_to}_dataframe_statement_fact_parse.json"
        )

    def _validation_report_path(self, ticker: str, request: CoverageScanRequest) -> Path:
        return (
            self.root
            / "data"
            / "validation"
            / ticker.upper()
            / f"{request.period_from}_{request.period_to}_real_validation_report.json"
        )


def _usable_statement_role(source_role: str | None) -> bool:
    role = source_role or ""
    return role == "financial_statements" or role.endswith("_with_embedded_financial_statements")


def blocker_priority(blocker: str) -> int:
    priority = {
        "company_not_resolved": 0,
        "ambiguous_company_identity": 1,
        "missing_fy_report": 2,
        "missing_period_report": 3,
        "source_package_not_ready": 4,
        "image_only_primary_statement_pages": 5,
        "no_cached_documents": 6,
        "no_primary_statement_tables": 7,
        "no_safe_fact_candidates": 8,
        "missing_expected_statement_facts": 9,
        "official_source_missing": 10,
        "manual_upload_available": 11,
        "unavailable_valuation_inputs": 50,
    }
    return priority.get(blocker, 100)


def recommended_action_for_blockers(blockers: list[str]) -> str:
    if not blockers:
        return "Coverage is usable for the available scoped outputs."
    first = sorted(blockers, key=blocker_priority)[0]
    actions = {
        "missing_fy_report": "Improve official FY/12M report discovery or source acquisition.",
        "missing_period_report": "Improve financial report discovery for missing periods.",
        "source_package_not_ready": "Verify source package candidates before downstream parsing.",
        "no_cached_documents": "Run explicit download/validation stage for trusted candidates.",
        "image_only_primary_statement_pages": "Design an OCR/table-image extraction module.",
        "no_primary_statement_tables": "Improve statement table extraction for primary statements.",
        "no_safe_fact_candidates": "Improve DataFrame parser or semantic extraction gates after table artifacts are clean.",
        "missing_expected_statement_facts": "Extend statement fact mapping for expected statement inputs.",
        "official_source_missing": "Manual upload is available, but official source discovery/source package remains unresolved.",
        "manual_upload_available": "Use manual upload only as a fallback; keep official source acquisition as separate work.",
        "unavailable_valuation_inputs": "Build a separate valuation input module; do not treat as parser work.",
    }
    return actions.get(first, "Inspect blocker details and add the narrowest safe pipeline improvement.")


def recommended_next_engineering_action(blocker_summary: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not blocker_summary:
        return None
    prioritized = [
        item for item in blocker_summary if item["blocker_code"] not in {"unavailable_valuation_inputs"}
    ] or blocker_summary
    top = sorted(prioritized, key=lambda item: (-item["count"], blocker_priority(item["blocker_code"])))[0]
    return {
        "blocker_code": top["blocker_code"],
        "affected_companies": top["count"],
        "tickers_sample": top["tickers_sample"],
        "recommended_action": top["recommended_action"],
        "rationale": "Selected by blocker frequency, with valuation policy blockers separated from statement pipeline ROI.",
    }


def recommended_not_to_prioritize(
    blocker_summary: list[dict[str, Any]],
    selected: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    selected_code = (selected or {}).get("blocker_code")
    items = []
    for blocker in blocker_summary:
        code = blocker["blocker_code"]
        if code == selected_code:
            continue
        if code == "unavailable_valuation_inputs":
            items.append(
                {
                    "blocker_code": code,
                    "affected_companies": blocker["count"],
                    "reason": "Valuation inputs are a separate module and should not override statement pipeline blockers.",
                }
            )
    return items
