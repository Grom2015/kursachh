"""Web-facing endpoints for the PDF analysis app.

Reuses the existing AnalysisResult history/versioning (see PdfAnalysisService):
* POST /pdf-analysis            — upload a PDF -> new analysis (version 1)
* POST /pdf-analysis/{id}/augment — add a second PDF -> child version
* GET  /pdf-analysis            — server-side history of PDF analyses
* GET  /pdf-analysis/{id}       — full analytics of one analysis
"""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import get_db
from app.core.config import get_settings
from app.db.models import AnalysisResult
from app.services.analysis.pdf_analysis_service import (
    PdfAnalysisError,
    PdfAnalysisService,
    history_items,
)
from app.services.analysis.pdf_insight_service import PdfInsightError, generate_pdf_insight
from app.services.reports.manual_report_ingestion import safe_filename_for_upload

router = APIRouter(prefix="/pdf-analysis", tags=["pdf-analysis"])


def _stage_root() -> Path:
    root = get_settings().root_dir.resolve() / "data" / "tmp" / "pdf_analysis_stage"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _stage_manifest_path(stage_id: str) -> Path:
    return _stage_root() / f"{stage_id}.json"


def _write_stage_manifest(
    *,
    stage_id: str,
    temp_path: Path,
    original_filename: str,
    safe_name: str,
    size_bytes: int,
) -> dict:
    payload = {
        "stage_id": stage_id,
        "path": str(temp_path),
        "original_filename": original_filename,
        "safe_name": safe_name,
        "size_bytes": int(size_bytes),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _stage_manifest_path(stage_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _load_stage_manifest(stage_id: str) -> dict:
    path = _stage_manifest_path(stage_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="staged_file_not_found")
    return json.loads(path.read_text(encoding="utf-8"))


def _delete_stage_manifest(stage_id: str) -> None:
    manifest_path = _stage_manifest_path(stage_id)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    except json.JSONDecodeError:
        payload = {}
    file_path = Path(str(payload.get("path") or ""))
    if file_path.exists():
        file_path.unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)


async def _spool_upload(file: UploadFile) -> tuple[Path, Path]:
    settings = get_settings()
    root = settings.root_dir.resolve()
    temp_root = root / "data" / "tmp" / "pdf_analysis_api"
    temp_root.mkdir(parents=True, exist_ok=True)
    safe_name = safe_filename_for_upload(file.filename or "upload.pdf")
    suffix = Path(safe_name).suffix.casefold()
    size_limit = settings.max_report_download_mb * 1024 * 1024
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="upload_", dir=temp_root) as tmp:
        temp_path = Path(tmp.name).resolve()
        written = 0
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > size_limit:
                tmp.close()
                temp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="file_too_large")
            tmp.write(chunk)
    return temp_path, Path(safe_name)


async def _spool_uploads(files: list[UploadFile]) -> list[tuple[Path, Path, UploadFile]]:
    spooled: list[tuple[Path, Path, UploadFile]] = []
    for file in files:
        temp_path, safe_name = await _spool_upload(file)
        spooled.append((temp_path, safe_name, file))
    return spooled


@router.post("/staged-file")
async def stage_pdf_file(file: UploadFile = File(...)) -> dict:
    temp_path, safe_name = await _spool_upload(file)
    try:
        stage_id = uuid4().hex
        payload = _write_stage_manifest(
            stage_id=stage_id,
            temp_path=temp_path,
            original_filename=file.filename or safe_name.name,
            safe_name=safe_name.name,
            size_bytes=temp_path.stat().st_size,
        )
        return {
            "stage_id": payload["stage_id"],
            "file_name": payload["original_filename"],
            "size_bytes": payload["size_bytes"],
        }
    finally:
        await file.close()


@router.post("/staged-files-form", response_class=HTMLResponse)
async def stage_pdf_files_form(request: Request, files: list[UploadFile] = File(...)) -> HTMLResponse:
    staged: list[dict] = []
    error_message: str | None = None
    try:
        spooled = await _spool_uploads(files)
        for temp_path, safe_name, file in spooled:
            stage_id = uuid4().hex
            payload = _write_stage_manifest(
                stage_id=stage_id,
                temp_path=temp_path,
                original_filename=file.filename or safe_name.name,
                safe_name=safe_name.name,
                size_bytes=temp_path.stat().st_size,
            )
            staged.append(
                {
                    "stage_id": payload["stage_id"],
                    "file_name": payload["original_filename"],
                    "size_bytes": payload["size_bytes"],
                }
            )
    except HTTPException as exc:
        error_message = str(exc.detail)
    except Exception as exc:  # pragma: no cover - defensive
        error_message = str(exc)
    finally:
        for file in files:
            await file.close()

    event = {"source": "pdf-analysis-stage", "ok": error_message is None, "files": staged, "error": error_message}
    html = f"""<!doctype html>
<html>
  <body>
    <script>
      window.parent.postMessage({json.dumps(event, ensure_ascii=False)}, window.location.origin);
    </script>
  </body>
</html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.delete("/staged-file/{stage_id}")
def delete_staged_pdf_file(stage_id: str) -> dict:
    _delete_stage_manifest(stage_id)
    return {"ok": True}


def _analysis_payload(result: AnalysisResult) -> dict:
    snapshot = result.data_snapshot_json or {}
    return {
        "id": result.id,
        "version": result.version,
        "parent_result_id": result.parent_result_id,
        "created_at": result.created_at.isoformat() if result.created_at else None,
        "source": snapshot.get("source"),
        "analytics": result.result_json or {},
    }


@router.post("")
async def create_pdf_analysis(
    file: UploadFile = File(...),
    company_ticker: str = Form(...),
    period: str | None = Form(None),
    reporting_standard: str = Form("IFRS"),
    db: Session = Depends(get_db),
) -> dict:
    temp_path, safe_name = await _spool_upload(file)
    try:
        result = PdfAnalysisService(db).analyze(
            local_file_path=str(temp_path),
            company_ticker=company_ticker,
            period=period,
            reporting_standard=reporting_standard,
            original_filename=file.filename or safe_name.name,
        )
        return _analysis_payload(result)
    except PdfAnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()
        temp_path.unlink(missing_ok=True)


@router.post("/batch")
async def create_pdf_analysis_batch(
    files: list[UploadFile] = File(...),
    company_ticker: str = Form(...),
    period: str | None = Form(None),
    reporting_standard: str = Form("IFRS"),
    db: Session = Depends(get_db),
) -> dict:
    if not files:
        raise HTTPException(status_code=422, detail="At least one file is required")
    spooled = await _spool_uploads(files)
    try:
        service = PdfAnalysisService(db)
        first_path, first_name, first_file = spooled[0]
        result = service.analyze(
            local_file_path=str(first_path),
            company_ticker=company_ticker,
            period=period,
            reporting_standard=reporting_standard,
            original_filename=first_file.filename or first_name.name,
        )
        for temp_path, safe_name, file in spooled[1:]:
            result = service.augment(
                parent_result_id=result.id,
                local_file_path=str(temp_path),
                period=None,
                original_filename=file.filename or safe_name.name,
            )
        analytics = dict(result.result_json or {})
        analytics["batch_upload_count"] = len(files)
        analytics["batch_uploaded_filenames"] = [file.filename or safe_name.name for _, safe_name, file in spooled]
        result.result_json = analytics
        if hasattr(result, "_sa_instance_state"):
            flag_modified(result, "result_json")
            db.commit()
        payload = _analysis_payload(result)
        return payload
    except PdfAnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        for temp_path, _, file in spooled:
            await file.close()
            temp_path.unlink(missing_ok=True)


@router.post("/staged-batch")
def create_pdf_analysis_from_staged_batch(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    stage_ids = list(payload.get("stage_ids") or [])
    company_ticker = str(payload.get("company_ticker") or "").strip()
    period = payload.get("period")
    reporting_standard = str(payload.get("reporting_standard") or "IFRS").strip() or "IFRS"
    if not company_ticker:
        raise HTTPException(status_code=422, detail="company_ticker_required")
    if not stage_ids:
        raise HTTPException(status_code=422, detail="At least one staged file is required")

    manifests = [_load_stage_manifest(str(stage_id)) for stage_id in stage_ids]
    try:
        service = PdfAnalysisService(db)
        first = manifests[0]
        result = service.analyze(
            local_file_path=str(first["path"]),
            company_ticker=company_ticker,
            period=period,
            reporting_standard=reporting_standard,
            original_filename=first.get("original_filename") or first.get("safe_name"),
        )
        for manifest in manifests[1:]:
            result = service.augment(
                parent_result_id=result.id,
                local_file_path=str(manifest["path"]),
                period=None,
                original_filename=manifest.get("original_filename") or manifest.get("safe_name"),
            )
        analytics = dict(result.result_json or {})
        analytics["batch_upload_count"] = len(manifests)
        analytics["batch_uploaded_filenames"] = [
            str(manifest.get("original_filename") or manifest.get("safe_name") or "upload.pdf")
            for manifest in manifests
        ]
        result.result_json = analytics
        if hasattr(result, "_sa_instance_state"):
            flag_modified(result, "result_json")
            db.commit()
        return _analysis_payload(result)
    except PdfAnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        for stage_id in stage_ids:
            _delete_stage_manifest(str(stage_id))


@router.post("/{result_id}/augment")
async def augment_pdf_analysis(
    result_id: str,
    file: UploadFile = File(...),
    period: str | None = Form(None),
    db: Session = Depends(get_db),
) -> dict:
    temp_path, safe_name = await _spool_upload(file)
    try:
        result = PdfAnalysisService(db).augment(
            parent_result_id=result_id,
            local_file_path=str(temp_path),
            period=period,
            original_filename=file.filename or safe_name.name,
        )
        return _analysis_payload(result)
    except PdfAnalysisError as exc:
        status_code = 404 if "not found" in str(exc).casefold() else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    finally:
        await file.close()
        temp_path.unlink(missing_ok=True)


@router.get("")
def list_pdf_analyses(limit: int = 50, db: Session = Depends(get_db)) -> dict:
    return {"items": history_items(db, limit=limit)}


@router.get("/{result_id}")
def get_pdf_analysis(result_id: str, db: Session = Depends(get_db)) -> dict:
    result = db.get(AnalysisResult, result_id)
    if not result:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return _analysis_payload(result)


@router.post("/{result_id}/insight")
def generate_insight(result_id: str, regenerate: bool = False, db: Session = Depends(get_db)) -> dict:
    """Generate or return cached LLM narrative for a PDF analysis."""
    try:
        return generate_pdf_insight(db, result_id, regenerate=regenerate)
    except PdfInsightError as exc:
        message = str(exc)
        status_code = 404 if "not found" in message.casefold() else 422
        raise HTTPException(status_code=status_code, detail=message) from exc
