import json
import mimetypes
import tempfile
from datetime import datetime
from pathlib import Path
from traceback import format_exception

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.db.models import Company, ReportDocument
from app.services.parsing.audit import audit_path
from app.services.parsing.statement_table_extractor import statement_tables_path
from app.services.periods import period_in_range
from app.services.reports.financial_report_discovery import (
    FinancialReportDiscoveryRequest,
    FinancialReportDiscoveryService,
    detect_period,
)
from app.services.reports.manual_report_ingestion import (
    ManualReportIngestionRequest,
    ManualReportIngestionService,
    safe_filename_for_upload,
)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("")
def list_reports() -> dict:
    return {"items": [], "warnings": ["Report listing is not implemented in MVP"]}


@router.post("/manual-upload")
async def manual_upload_report(
    company_ticker: str = Form(...),
    period: str | None = Form(None),
    reporting_standard: str = Form("IFRS"),
    document_type: str = Form("financial_statements"),
    source_role: str | None = Form(None),
    manual_upload_reason: str = Form("user_requested"),
    original_source_url: str | None = Form(None),
    run_table_extraction: bool = Form(False),
    run_dataframe_fact_parser: bool = Form(False),
    allow_text_fallback_semantic_gate: bool = Form(False),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    root = settings.root_dir.resolve()
    temp_root = root / "data" / "tmp" / "manual_upload_api"
    temp_root.mkdir(parents=True, exist_ok=True)
    safe_name = safe_filename_for_upload(file.filename or "manual_upload")
    temp_path: Path | None = None
    inferred_period_warning: str | None = None
    try:
        suffix = Path(safe_name).suffix.casefold()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="upload_", dir=temp_root) as tmp:
            temp_path = Path(tmp.name).resolve()
            if not temp_path.is_relative_to(temp_root.resolve()):
                raise HTTPException(status_code=400, detail="manual_upload_temp_path_escape")
            size_limit = settings.max_report_download_mb * 1024 * 1024
            written = 0
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > size_limit:
                    raise HTTPException(status_code=413, detail="manual_upload_file_too_large")
                tmp.write(chunk)
        inferred_period = str(period or "").strip().upper()
        if not inferred_period:
            inferred_period = detect_period(file.filename or safe_name) or f"{datetime.now().year}Q4"
            inferred_period_warning = (
                "manual_upload_period_inferred_from_filename"
                if detect_period(file.filename or safe_name)
                else "manual_upload_period_defaulted_to_current_year_q4"
            )
        service = ManualReportIngestionService(db)
        report = service.ingest(
            ManualReportIngestionRequest(
                company_ticker=company_ticker,
                period=inferred_period,
                reporting_standard=reporting_standard,
                local_file_path=str(temp_path),
                original_source_url=original_source_url,
                document_type=document_type,
                source_role=source_role,
                manual_upload_reason=manual_upload_reason,
                run_table_extraction=run_table_extraction,
                run_dataframe_fact_parser=run_dataframe_fact_parser,
                allow_text_fallback_semantic_gate=allow_text_fallback_semantic_gate,
                persist_facts=False,
            )
        )
        payload = report.to_dict()
        if inferred_period_warning:
            payload.setdefault("warnings", []).append(inferred_period_warning)
            payload["period_inferred"] = True
        payload["db_persisted"] = False
        payload["official_source_verified"] = False
        payload["source_package_ready_contribution"] = False
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        error_root = root / "data" / "validation" / company_ticker.upper()
        error_root.mkdir(parents=True, exist_ok=True)
        error_payload = {
            "company_ticker": company_ticker.upper(),
            "period": str(period or "").strip().upper(),
            "reporting_standard": reporting_standard.upper(),
            "status": "UPLOAD_FAILED",
            "ingestion_status": "INVALID_UPLOAD",
            "identity_status": "unresolved",
            "document_classification": "blocked_invalid_upload",
            "document_validation_status": "not_run",
            "statement_tables_extracted": 0,
            "fact_parse_status": "not_invoked",
            "canonical_fact_candidates": 0,
            "db_persisted": False,
            "source_trust_bucket": "manual_upload_unverified",
            "official_source_verified": False,
            "source_package_ready_contribution": False,
            "evidence_pack_available": False,
            "structured_facts_count": 0,
            "rejected_rows_count": 0,
            "unmapped_numeric_evidence_count": 0,
            "unmapped_table_evidence_count": 0,
            "warnings": [str(exc)],
            "blockers": ["manual_upload_endpoint_exception"],
            "traceback_tail": "".join(format_exception(type(exc), exc, exc.__traceback__))[-4000:],
        }
        error_path = error_root / f"{period}_manual_report_ingestion_error.json"
        error_path.write_text(json.dumps(error_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        error_payload["error_report_path"] = str(error_path.relative_to(root)).replace("\\", "/")
        return error_payload
    finally:
        await file.close()
        if temp_path and temp_path.exists():
            temp_path.unlink()


@router.get("/documents")
def list_documents(
    company: str,
    period_from: str,
    period_to: str,
    data_mode: str = "real",
    db: Session = Depends(get_db),
) -> dict:
    company_obj = db.scalar(select(Company).where(Company.ticker == company.upper()))
    if not company_obj:
        return {"items": [], "warnings": [f"Company not found: {company}"]}
    stmt = select(ReportDocument).where(ReportDocument.company_id == company_obj.id)
    if data_mode == "real":
        stmt = stmt.where(ReportDocument.source_type != "fixture")
    elif data_mode == "fixture":
        stmt = stmt.where(ReportDocument.source_type == "fixture")
    docs = [
        doc
        for doc in db.scalars(stmt).all()
        if period_in_range(doc.report_period, period_from, period_to)
    ]
    return {
        "items": [
            {
                "id": doc.id,
                "company": company_obj.ticker,
                "period": doc.report_period,
                "reporting_standard": doc.reporting_standard,
                "document_type": doc.document_type,
                "source_role": doc.source_role,
                "source_type": doc.source_type,
                "source_url": doc.source_url,
                "file_name": doc.file_name,
                "file_hash": doc.file_hash,
                "status": doc.status,
                "rejection_reason": doc.rejection_reason,
                "manual_upload_reason": doc.manual_upload_reason,
                "source_trust_bucket": doc.source_trust_bucket,
                "official_source_verified": doc.official_source_verified,
                "source_package_ready_contribution": doc.source_package_ready_contribution,
            }
            for doc in docs
        ],
        "warnings": [],
    }


@router.get("/discover")
def discover_reports(
    company: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    live: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    service = FinancialReportDiscoveryService(db)
    report = service.discover(
        FinancialReportDiscoveryRequest(
            company_query=company,
            ticker=company.upper(),
            period_from=period_from,
            period_to=period_to,
            reporting_standard=reporting_standard,
            live=live,
        )
    )
    return report.to_dict()


@router.get("/documents/{document_id}/parse-audit")
def get_parse_audit(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(ReportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="ReportDocument not found")
    path = audit_path(document)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Parse audit not found for document")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/documents/{document_id}/statement-tables")
def get_statement_tables(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(ReportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="ReportDocument not found")
    path = statement_tables_path(document)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Statement table artifact not found for document")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "document_id": document_id,
        "items": payload.get("statement_tables", []),
        "warnings": payload.get("warnings", []),
    }


@router.get("/artifact/{artifact_path:path}")
def open_artifact_file(artifact_path: str) -> FileResponse:
    settings = get_settings()
    root = settings.root_dir.resolve()
    relative = Path(artifact_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=400, detail="artifact_path_not_allowed")
    full_path = (root / relative).resolve()
    allowed_roots = [
        (root / "data" / "validation").resolve(),
        (root / "data" / "parsed").resolve(),
    ]
    if not any(full_path.is_relative_to(base) for base in allowed_roots):
        raise HTTPException(status_code=403, detail="artifact_path_outside_allowed_roots")
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="artifact_not_found")
    media_type = mimetypes.guess_type(str(full_path))[0] or "application/octet-stream"
    return FileResponse(full_path, media_type=media_type, filename=full_path.name)
