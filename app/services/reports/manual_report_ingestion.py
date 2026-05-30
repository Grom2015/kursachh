import hashlib
import json
import mimetypes
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, ReportDocument
from app.services.company_registry import CompanyRegistry, normalize_query
from app.services.company_source_discovery import CompanySourceDiscoveryReport, CompanySourceDiscoveryService
from app.services.parsing.dataframe_statement_parser import DataFrameStatementParser
from app.services.parsing.statement_table_extractor import StatementTableExtractor
from app.services.reports.document_validator import DocumentValidator

ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".html", ".htm", ".zip"}
ZIP_ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".html", ".htm"}
MANUAL_UPLOAD_REASONS = {
    "api_unavailable",
    "source_discovery_failed",
    "missing_fy_report",
    "manual_test",
    "user_requested",
}


@dataclass
class ManualReportIngestionRequest:
    company_ticker: str
    reporting_standard: str
    period: str
    local_file_path: str
    original_source_url: str | None = None
    document_type: str = "financial_statements"
    source_role: str | None = None
    uploaded_by: str | None = None
    manual_upload_reason: str = "user_requested"
    run_table_extraction: bool = True
    run_dataframe_fact_parser: bool = False
    allow_text_fallback_semantic_gate: bool = False
    persist_facts: bool = False


@dataclass
class ManualReportIngestionReport:
    company_ticker: str
    period: str
    original_period: str | None
    reporting_standard: str
    input_file: str
    stored_document_path: str | None
    report_document_id: int | None
    duplicate_detected: bool
    sha256: str | None
    file_size_bytes: int | None
    content_type: str | None
    manual_upload_reason: str
    source_trust_bucket: str
    official_source_verified: bool
    source_package_ready_contribution: bool
    document_validation_status: str
    detected_document_role: str | None
    statement_tables_extracted: int
    statement_coverage: dict[str, Any]
    dataframe_artifacts: list[str]
    fact_parse_status: str
    canonical_fact_candidates: int
    db_persisted: bool
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    status: str = "VALIDATION_FAILED"
    validation_result: dict[str, Any] | None = None
    fact_parse_report: dict[str, Any] | None = None
    ingestion_status: str = "INVALID_UPLOAD"
    identity_status: str = "unresolved"
    document_classification: str = "blocked_invalid_upload"
    evidence_pack_available: bool = False
    structured_facts_count: int = 0
    rejected_rows_count: int = 0
    unmapped_numeric_evidence_count: int = 0
    unmapped_table_evidence_count: int = 0
    recommended_next_action: str | None = None
    identity_report_path: str | None = None
    effective_report_period: str | None = None
    comparative_period: str | None = None
    period_source: str | None = None
    period_confidence: float | None = None
    period_warnings: list[str] = field(default_factory=list)
    degraded_primary_statements_found: int = 0
    top_structural_blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ManualReportIngestionService:
    def __init__(self, db: Session, root: Path | None = None):
        self.db = db
        self.root = (root or get_settings().root_dir).resolve()
        self.settings = get_settings()

    def ingest(self, request: ManualReportIngestionRequest) -> ManualReportIngestionReport:
        warnings: list[str] = []
        blockers: list[str] = []
        company, identity_status, identity_report_path, identity_warnings, identity_blockers = self._resolve_company(
            request.company_ticker
        )
        warnings.extend(identity_warnings)
        blockers.extend(identity_blockers)
        if not company:
            return self._failed_report(
                request,
                blockers or ["company_not_found_and_not_bootstrappable"],
                warnings or [f"Company not found: {request.company_ticker}"],
                ingestion_status="IDENTITY_BLOCKED",
                identity_status=identity_status,
                document_classification="blocked_invalid_upload",
                recommended_next_action="resolve_company_identity_manually",
                identity_report_path=identity_report_path,
            )
        reason = request.manual_upload_reason or "user_requested"
        if reason not in MANUAL_UPLOAD_REASONS:
            return self._failed_report(
                request,
                ["invalid_manual_upload_reason"],
                [f"Invalid manual upload reason: {reason}"],
                ingestion_status="INVALID_UPLOAD",
                identity_status=identity_status,
                document_classification="blocked_invalid_upload",
                recommended_next_action="use_supported_manual_upload_reason",
                identity_report_path=identity_report_path,
            )
        try:
            source_path = self._validate_input_file(request.local_file_path)
            file_size = source_path.stat().st_size
            sha256 = self._sha256(source_path)
            safe_filename = safe_filename_for_upload(source_path.name)
            stored_path = self._storage_path(company.ticker, request.reporting_standard, request.period, sha256, safe_filename)
        except ValueError as exc:
            return self._failed_report(
                request,
                [str(exc)],
                [str(exc)],
                ingestion_status="INVALID_UPLOAD",
                identity_status=identity_status,
                document_classification="blocked_invalid_upload",
                recommended_next_action="upload_supported_reporting_pdf",
                identity_report_path=identity_report_path,
            )

        duplicate = self._existing_manual_document(company, request, sha256)
        if duplicate:
            document = duplicate
            duplicate_detected = True
            stored_path = self._document_storage_path(document)
            warnings.append("duplicate_manual_upload_detected")
        else:
            stored_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, stored_path)
            document = ReportDocument(
                company_id=company.id,
                company=company,
                report_period=request.period,
                reporting_standard=request.reporting_standard.upper(),
                document_type=request.document_type,
                source_role=request.source_role or request.document_type,
                source_type="manual_upload",
                source_url=request.original_source_url,
                storage_path=str(stored_path),
                file_name=safe_filename,
                file_hash=sha256,
                status="downloaded",
                trust_level="user_provided_unverified",
                manual_upload_reason=reason,
                source_trust_bucket="manual_upload_unverified",
                official_source_verified=False,
                source_package_ready_contribution=False,
                validation_warnings_json=[],
            )
            self.db.add(document)
            self.db.commit()
            self.db.refresh(document)
            duplicate_detected = False

        validation = DocumentValidator().validate(
            document,
            expected_file_type=source_path.suffix.casefold().lstrip("."),
            max_text_pages=15,
        )
        validation_payload = validation.to_dict()
        resolved_period = self._apply_validation_period_resolution(document, validation_payload, warnings)
        source_trust_bucket = "manual_upload_validated" if validation.validation_status == "pass" else "manual_upload_unverified"
        self._prepare_document_for_reprocessing(document)
        document.validation_warnings_json = validation.warnings
        document.source_trust_bucket = source_trust_bucket
        document.official_source_verified = False
        document.source_package_ready_contribution = False
        document.trust_level = source_trust_bucket if validation.validation_status == "pass" else "user_provided_unverified"
        if validation.validation_status != "pass":
            evidence_report = self._build_evidence_only_report(
                company=company,
                request=request,
                source_path=source_path,
                stored_path=stored_path,
                document=document,
                duplicate_detected=duplicate_detected,
                sha256=sha256,
                file_size=file_size,
                reason=reason,
                warnings=warnings,
                blockers=blockers,
                validation=validation_payload,
                identity_status=identity_status,
                identity_report_path=identity_report_path,
            )
            self.save_report(evidence_report)
            return evidence_report
        document.status = "validated"
        document.rejection_reason = None
        self.db.commit()

        table_report = None
        dataframe_artifacts: list[str] = []
        statement_tables_extracted = 0
        coverage: dict[str, Any] = {}
        status = "INGESTED"
        if request.run_table_extraction:
            table_report = StatementTableExtractor(root=self.root).extract(document)
            resolved_period = self._apply_table_period_resolution(document, table_report, resolved_period, warnings)
            statement_tables_extracted = int(table_report.get("statement_tables_count") or 0)
            coverage = table_report.get("statement_coverage") or {}
            artifact_path = table_report.get("artifact_path")
            if artifact_path:
                dataframe_artifacts.append(artifact_path)
            if not table_report.get("required_statement_tables_found"):
                status = "TABLE_EXTRACTION_PARTIAL" if statement_tables_extracted else "TABLE_EXTRACTION_FAILED"
                blockers.append("manual_upload_table_extraction_incomplete")

        fact_parse_status = "not_invoked"
        canonical_fact_candidates = 0
        fact_parse_report = None
        if request.run_dataframe_fact_parser:
            parser_period_from, parser_period_to = self._parser_period_range(request.period)
            if resolved_period:
                parser_period_from, parser_period_to = self._parser_period_range(resolved_period)
            result = DataFrameStatementParser(
                self.db,
                root=self.root,
                allow_text_fallback_semantic_gate=request.allow_text_fallback_semantic_gate,
            ).parse(
                company.ticker,
                parser_period_from,
                parser_period_to,
                request.reporting_standard,
                document_ids=[document.id],
            )
            fact_parse_report = result.to_dict()
            fact_parse_status = result.status
            canonical_fact_candidates = result.canonical_facts_created
            if result.status not in {"SUCCESS"}:
                status = "FACT_PARSE_PARTIAL" if canonical_fact_candidates else "FACT_PARSE_FAILED"

        report = ManualReportIngestionReport(
            company_ticker=company.ticker,
            period=resolved_period or request.period,
            original_period=request.period,
            reporting_standard=request.reporting_standard.upper(),
            input_file=str(source_path),
            stored_document_path=str(stored_path),
            report_document_id=document.id,
            duplicate_detected=duplicate_detected,
            sha256=sha256,
            file_size_bytes=file_size,
            content_type=mimetypes.guess_type(str(source_path))[0],
            manual_upload_reason=reason,
            source_trust_bucket=source_trust_bucket,
            official_source_verified=False,
            source_package_ready_contribution=False,
            document_validation_status=validation.validation_status,
            detected_document_role=validation.detected_document_role,
            statement_tables_extracted=statement_tables_extracted,
            statement_coverage=coverage,
            dataframe_artifacts=dataframe_artifacts,
            fact_parse_status=fact_parse_status,
            canonical_fact_candidates=canonical_fact_candidates,
            db_persisted=False,
            warnings=warnings + validation.warnings + list((table_report or {}).get("warnings") or []),
            blockers=blockers,
            status=status,
            validation_result=validation_payload,
            fact_parse_report=fact_parse_report,
            ingestion_status="VALIDATED_FINANCIAL_STATEMENT",
            identity_status=identity_status,
            document_classification="validated_financial_statement",
            evidence_pack_available=bool((fact_parse_report or {}).get("llm_ready_evidence_pack")),
            structured_facts_count=int(len((fact_parse_report or {}).get("structured_facts") or [])),
            rejected_rows_count=int(len((fact_parse_report or {}).get("rejected_rows") or [])),
            unmapped_numeric_evidence_count=int(len((fact_parse_report or {}).get("unmapped_numeric_evidence") or [])),
            unmapped_table_evidence_count=int(len((fact_parse_report or {}).get("unmapped_table_evidence") or [])),
            recommended_next_action=None if validation.validation_status == "pass" else "review_evidence_only_output",
            identity_report_path=identity_report_path,
            effective_report_period=resolved_period or validation_payload.get("detected_period"),
            comparative_period=validation_payload.get("comparative_period"),
            period_source=validation_payload.get("period_source"),
            period_confidence=validation_payload.get("period_confidence"),
            period_warnings=list(validation_payload.get("period_warnings") or []),
            degraded_primary_statements_found=int((table_report or {}).get("degraded_primary_statement_count") or 0),
            top_structural_blockers=top_structural_blockers(fact_parse_report or {}),
        )
        self.save_report(report)
        return report

    def save_report(self, report: ManualReportIngestionReport) -> Path:
        root = self.root / "data" / "validation" / report.company_ticker.upper()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{report.period}_manual_report_ingestion.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _company(self, ticker: str) -> Company | None:
        exact = self.db.scalar(select(Company).where(Company.ticker == ticker.upper()))
        if exact:
            return exact
        return CompanyRegistry(self.db).resolve_one(ticker)

    def _resolve_company(self, query: str) -> tuple[Company | None, str, str | None, list[str], list[str]]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return None, "unresolved", None, ["company_query_empty"], ["company_not_found_and_not_bootstrappable"]
        company = self._company(normalized_query)
        if company:
            if not getattr(company, "identity_status", None):
                company.identity_status = "resolved_registry"
            if not getattr(company, "verification_scope", None):
                company.verification_scope = "registry"
            self.db.commit()
            return company, company.identity_status, None, [], []

        source_service = CompanySourceDiscoveryService(self.db, root=self.root)
        ticker_hint = normalized_query.upper() if _looks_like_ticker(normalized_query) else None
        source_report = source_service.discover(normalized_query, ticker=ticker_hint, live=False)
        source_report_path = source_service.save_report(source_report)
        company = self._bootstrap_company_from_identity(ticker_hint or normalized_query.upper(), source_report)
        if company:
            return (
                company,
                company.identity_status,
                self._relative(source_report_path),
                ["identity_bootstrapped_for_manual_upload"],
                [],
            )
        pending = self._create_pending_company(normalized_query, source_report)
        return (
            pending,
            pending.identity_status,
            self._relative(source_report_path),
            ["pending_company_identity_created"],
            [],
        )

    def _bootstrap_company_from_identity(
        self,
        ticker: str,
        source_report: CompanySourceDiscoveryReport,
    ) -> Company | None:
        exact_candidates = [
            candidate
            for candidate in source_report.recommended_candidates
            if candidate.source_provider == "moex_iss"
            and candidate.verification_scope == "identity_only"
            and (candidate.ticker or "").upper() == ticker.upper()
        ]
        if not exact_candidates:
            return None
        preferred = next(
            (
                candidate
                for candidate in exact_candidates
                if (candidate.provenance or {}).get("board") == "TQBR"
            ),
            exact_candidates[0],
        )
        provenance = preferred.provenance or {}
        board = provenance.get("board") or "TQBR"
        existing = self.db.scalar(select(Company).where(Company.ticker == ticker.upper(), Company.board == board))
        if existing:
            existing.identity_status = "bootstrapped_from_moex_iss"
            existing.verification_scope = "identity_only"
            self.db.commit()
            return existing
        company = Company(
            ticker=ticker.upper(),
            isin=provenance.get("isin"),
            board=board,
            short_name=provenance.get("short_name") or preferred.matched_name or ticker.upper(),
            full_name=provenance.get("name") or preferred.matched_name or ticker.upper(),
            aliases_json=[ticker.upper()],
            sector=None,
            subsector=None,
            ir_url=None,
            disclosure_id=None,
            identity_status="bootstrapped_from_moex_iss",
            verification_scope="identity_only",
            is_active=True,
        )
        self.db.add(company)
        self.db.commit()
        self.db.refresh(company)
        return company

    def _create_pending_company(self, query: str, source_report: CompanySourceDiscoveryReport) -> Company:
        suggested_name = next(
            (candidate.matched_name for candidate in source_report.recommended_candidates if candidate.matched_name),
            query,
        )
        ticker = self._next_pending_ticker(query)
        company = Company(
            ticker=ticker,
            board="PENDING",
            short_name=str(suggested_name)[:255],
            full_name=str(suggested_name)[:512],
            aliases_json=sorted({ticker, query.strip()}),
            sector=None,
            subsector=None,
            ir_url=None,
            disclosure_id=None,
            identity_status="pending_manual_resolution",
            verification_scope="identity_only_unverified",
            is_active=True,
        )
        self.db.add(company)
        self.db.commit()
        self.db.refresh(company)
        return company

    def _next_pending_ticker(self, query: str) -> str:
        base = normalize_query(query).upper()
        base = re.sub(r"[^A-Z0-9]+", "", base)[:24] or "MANUAL"
        candidate = base
        suffix = 1
        while self.db.scalar(select(Company).where(Company.ticker == candidate, Company.board == "PENDING")):
            suffix += 1
            candidate = f"{base[: max(1, 24 - len(str(suffix)) - 1)]}-{suffix}"
        return candidate

    def _relative(self, path: Path | None) -> str | None:
        if not path:
            return None
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _parser_period_range(self, period: str) -> tuple[str, str]:
        normalized = str(period or "").strip().upper()
        if normalized.isdigit() and len(normalized) == 4:
            return f"{normalized}Q1", f"{normalized}Q4"
        return normalized, normalized

    def _build_evidence_only_report(
        self,
        *,
        company: Company,
        request: ManualReportIngestionRequest,
        source_path: Path,
        stored_path: Path,
        document: ReportDocument,
        duplicate_detected: bool,
        sha256: str,
        file_size: int,
        reason: str,
        warnings: list[str],
        blockers: list[str],
        validation: dict[str, Any],
        identity_status: str,
        identity_report_path: str | None,
    ) -> ManualReportIngestionReport:
        table_report = {
            "statement_tables_count": 0,
            "statement_coverage": {},
            "artifact_path": None,
            "warnings": [],
        }
        fact_parse_report: dict[str, Any] = {}
        parser_period_from, parser_period_to = self._parser_period_range(request.period)
        resolved_period = validation.get("detected_period") or request.period
        if resolved_period:
            parser_period_from, parser_period_to = self._parser_period_range(resolved_period)
        fact_parse = None
        try:
            extractor = StatementTableExtractor(root=self.root)
            table_report = extractor.extract(document)
            resolved_period = self._evidence_only_table_period(table_report, resolved_period)
            if resolved_period:
                parser_period_from, parser_period_to = self._parser_period_range(resolved_period)
        except Exception as exc:
            table_report["warnings"] = [f"evidence_only_table_extraction_failed: {exc}"]
        try:
            fact_parse = DataFrameStatementParser(
                self.db,
                root=self.root,
                allow_text_fallback_semantic_gate=True,
            ).parse(
                company.ticker,
                parser_period_from,
                parser_period_to,
                request.reporting_standard,
                document_ids=[document.id],
            )
            fact_parse_report = fact_parse.to_dict()
        except Exception as exc:
            fact_parse_report = {
                "status": "NO_EVIDENCE_PACK",
                "canonical_facts_created": 0,
                "structured_facts": [],
                "rejected_rows": [],
                "unmapped_numeric_evidence": [],
                "unmapped_table_evidence": [],
                "llm_ready_evidence_pack": {},
                "warnings": [f"evidence_only_fact_parse_failed: {exc}"],
            }
        structured_facts_count = len(fact_parse_report.get("structured_facts") or [])
        rejected_rows_count = len(fact_parse_report.get("rejected_rows") or [])
        unmapped_numeric_evidence_count = len(fact_parse_report.get("unmapped_numeric_evidence") or [])
        unmapped_table_evidence_count = len(fact_parse_report.get("unmapped_table_evidence") or [])
        statement_tables_extracted = int(table_report.get("statement_tables_count") or 0)
        useful_evidence = any(
            [
                statement_tables_extracted,
                structured_facts_count,
                rejected_rows_count,
                unmapped_numeric_evidence_count,
                unmapped_table_evidence_count,
            ]
        )
        content_warnings = (
            warnings
            + list(validation.get("warnings") or [])
            + list(table_report.get("warnings") or [])
            + list(fact_parse_report.get("warnings") or [])
        )
        if useful_evidence:
            document.status = "evidence_only"
            document.rejection_reason = "manual_upload_evidence_only_after_validation_failure"
            blockers.append("document_validation_failed_evidence_only")
            status = "EVIDENCE_ONLY"
            ingestion_status = "EVIDENCE_ONLY"
            document_classification = "evidence_only_report"
            recommended_next_action = "review_evidence_only_output"
        else:
            document.status = "rejected"
            document.rejection_reason = "manual_upload_document_validation_failed"
            blockers.append("document_validation_failed")
            status = "INVALID_UPLOAD"
            ingestion_status = "INVALID_UPLOAD"
            document_classification = "blocked_invalid_upload"
            recommended_next_action = "upload_financial_statement_pdf"
        self.db.commit()
        artifact_path = table_report.get("artifact_path")
        dataframe_artifacts = [artifact_path] if artifact_path else []
        return ManualReportIngestionReport(
            company_ticker=company.ticker,
            period=resolved_period or request.period,
            original_period=request.period,
            reporting_standard=request.reporting_standard.upper(),
            input_file=str(source_path),
            stored_document_path=str(stored_path),
            report_document_id=document.id,
            duplicate_detected=duplicate_detected,
            sha256=sha256,
            file_size_bytes=file_size,
            content_type=mimetypes.guess_type(str(source_path))[0],
            manual_upload_reason=reason,
            source_trust_bucket="manual_upload_unverified",
            official_source_verified=False,
            source_package_ready_contribution=False,
            document_validation_status=str(validation.get("validation_status") or "fail"),
            detected_document_role=validation.get("detected_document_role"),
            statement_tables_extracted=statement_tables_extracted,
            statement_coverage=table_report.get("statement_coverage") or {},
            dataframe_artifacts=dataframe_artifacts,
            fact_parse_status=(fact_parse.status if fact_parse else fact_parse_report.get("status", "NO_EVIDENCE_PACK")),
            canonical_fact_candidates=(
                fact_parse.canonical_facts_created
                if fact_parse
                else int(fact_parse_report.get("canonical_facts_created") or 0)
            ),
            db_persisted=False,
            warnings=content_warnings,
            blockers=blockers,
            status=status,
            validation_result=validation,
            fact_parse_report=fact_parse_report,
            ingestion_status=ingestion_status,
            identity_status=identity_status,
            document_classification=document_classification,
            evidence_pack_available=bool(fact_parse_report.get("llm_ready_evidence_pack")),
            structured_facts_count=structured_facts_count,
            rejected_rows_count=rejected_rows_count,
            unmapped_numeric_evidence_count=unmapped_numeric_evidence_count,
            unmapped_table_evidence_count=unmapped_table_evidence_count,
            recommended_next_action=recommended_next_action,
            identity_report_path=identity_report_path,
            effective_report_period=resolved_period or validation.get("detected_period"),
            comparative_period=validation.get("comparative_period"),
            period_source=(table_report.get("period_resolution") or {}).get("period_source") or validation.get("period_source"),
            period_confidence=(table_report.get("period_resolution") or {}).get("period_confidence")
            or validation.get("period_confidence"),
            period_warnings=list(
                {
                    *list(validation.get("period_warnings") or []),
                    *list((table_report.get("period_resolution") or {}).get("period_warnings") or []),
                }
            ),
            degraded_primary_statements_found=int(table_report.get("degraded_primary_statement_count") or 0),
            top_structural_blockers=top_structural_blockers(fact_parse_report),
        )

    def _validate_input_file(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise ValueError("manual_upload_file_not_found")
        if not path.is_file():
            raise ValueError("manual_upload_path_not_regular_file")
        suffix = path.suffix.casefold()
        if suffix == ".xls":
            raise ValueError("xls_not_supported_in_v1")
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError("manual_upload_extension_not_allowed")
        size_limit = self.settings.max_report_download_mb * 1024 * 1024
        if suffix == ".zip":
            validate_zip_upload(path, size_limit)
        if path.stat().st_size > size_limit:
            raise ValueError("manual_upload_file_too_large")
        return path

    def _storage_path(
        self,
        ticker: str,
        reporting_standard: str,
        period: str,
        sha256: str,
        filename: str,
    ) -> Path:
        root = (
            self.root
            / "data"
            / "raw"
            / "manual_uploads"
            / ticker.upper()
            / reporting_standard.upper()
            / period
        ).resolve()
        path = (root / f"{sha256}_{filename}").resolve()
        if not path.is_relative_to(root):
            raise ValueError("manual_upload_storage_path_escape")
        return path

    def _existing_manual_document(
        self,
        company: Company,
        request: ManualReportIngestionRequest,
        sha256: str,
    ) -> ReportDocument | None:
        return self.db.scalar(
            select(ReportDocument).where(
                ReportDocument.company_id == company.id,
                ReportDocument.report_period == request.period,
                ReportDocument.reporting_standard == request.reporting_standard.upper(),
                ReportDocument.file_hash == sha256,
                ReportDocument.source_type == "manual_upload",
            )
        )

    def _document_storage_path(self, document: ReportDocument) -> Path:
        path = Path(document.storage_path or "")
        return path if path.is_absolute() else self.root / path

    def _prepare_document_for_reprocessing(self, document: ReportDocument) -> None:
        if document.status in {"rejected", "failed"}:
            document.status = "downloaded"
            self.db.commit()

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _failed_report(
        self,
        request: ManualReportIngestionRequest,
        blockers: list[str],
        warnings: list[str],
        *,
        ingestion_status: str,
        identity_status: str,
        document_classification: str,
        recommended_next_action: str | None,
        identity_report_path: str | None,
    ) -> ManualReportIngestionReport:
        return ManualReportIngestionReport(
            company_ticker=(request.company_ticker or "").upper(),
            period=request.period,
            original_period=request.period,
            reporting_standard=request.reporting_standard.upper(),
            input_file=request.local_file_path,
            stored_document_path=None,
            report_document_id=None,
            duplicate_detected=False,
            sha256=None,
            file_size_bytes=None,
            content_type=None,
            manual_upload_reason=request.manual_upload_reason,
            source_trust_bucket="manual_upload_unverified",
            official_source_verified=False,
            source_package_ready_contribution=False,
            document_validation_status="not_run",
            detected_document_role=None,
            statement_tables_extracted=0,
            statement_coverage={},
            dataframe_artifacts=[],
            fact_parse_status="not_invoked",
            canonical_fact_candidates=0,
            db_persisted=False,
            warnings=warnings,
            blockers=blockers,
            status=ingestion_status,
            ingestion_status=ingestion_status,
            identity_status=identity_status,
            document_classification=document_classification,
            recommended_next_action=recommended_next_action,
            identity_report_path=identity_report_path,
        )

    def _apply_validation_period_resolution(
        self,
        document: ReportDocument,
        validation_payload: dict[str, Any],
        warnings: list[str],
    ) -> str | None:
        detected_period = str(validation_payload.get("detected_period") or "").strip().upper()
        period_source = str(validation_payload.get("period_source") or "")
        confidence = float(validation_payload.get("period_confidence") or 0.0)
        if (
            detected_period
            and period_source != "upload_default"
            and confidence >= 0.7
            and detected_period != document.report_period
        ):
            warnings.append(f"manual_upload_period_overridden_from_{period_source}")
            document.report_period = detected_period
            self.db.commit()
            return detected_period
        return document.report_period

    def _apply_table_period_resolution(
        self,
        document: ReportDocument,
        table_report: dict[str, Any],
        current_period: str | None,
        warnings: list[str],
    ) -> str | None:
        resolution = table_report.get("period_resolution") or {}
        effective_period = (
            str(resolution.get("effective_report_period") or "").strip().upper()
        )
        period_source = str(resolution.get("period_source") or "")
        confidence = float(resolution.get("period_confidence") or 0.0)
        if (
            effective_period
            and period_source != "upload_default"
            and confidence >= 0.75
            and effective_period != document.report_period
        ):
            warnings.append(f"manual_upload_period_refined_from_{period_source}")
            document.report_period = effective_period
            self.db.commit()
            return effective_period
        return current_period or document.report_period

    def _evidence_only_table_period(self, table_report: dict[str, Any], fallback_period: str | None) -> str | None:
        resolution = table_report.get("period_resolution") or {}
        effective_period = str(resolution.get("effective_report_period") or "").strip().upper()
        confidence = float(resolution.get("period_confidence") or 0.0)
        if effective_period and confidence >= 0.75:
            return effective_period
        return fallback_period


def top_structural_blockers(fact_parse_report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    structural = fact_parse_report.get("structural_blockers") or {}
    for source in (
        structural.get("rejected_rows_by_reason") or {},
        structural.get("numeric_evidence_by_reason") or {},
        structural.get("table_evidence_by_reason") or {},
    ):
        for key, count in source.items():
            if count:
                blockers.append(str(key))
    for key in fact_parse_report.get("period_blockers") or []:
        blockers.append(str(key))
    return sorted(dict.fromkeys(blockers))


def safe_filename_for_upload(value: str) -> str:
    name = Path(value).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._-") or "report"
    suffix = Path(name).suffix.casefold()
    return f"{stem[:120]}{suffix}"


def validate_zip_upload(path: Path, size_limit: int) -> None:
    try:
        with ZipFile(path) as archive:
            total_size = 0
            for member in archive.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError("manual_upload_zip_path_traversal")
                suffix = member_path.suffix.casefold()
                if suffix == ".xls":
                    raise ValueError("xls_not_supported_in_v1")
                if suffix and suffix not in ZIP_ALLOWED_EXTENSIONS:
                    raise ValueError("manual_upload_zip_unsupported_internal_file")
                total_size += int(member.file_size or 0)
                if total_size > size_limit:
                    raise ValueError("manual_upload_zip_decompressed_too_large")
    except BadZipFile as exc:
        raise ValueError("manual_upload_zip_invalid") from exc


def _looks_like_ticker(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,16}", str(value or "").strip()))
