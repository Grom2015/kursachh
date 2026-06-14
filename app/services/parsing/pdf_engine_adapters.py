from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.services.parsing.statement_table_extractor import (
    build_normalized_row_blocks,
    build_text_fallback_records,
    classify_primary_statement_page,
    classify_statement_family,
    detect_currency,
    detect_unit,
    detect_unit_multiplier,
    guess_title,
    is_short_note_reference,
    is_text_fallback_header_or_title_line,
    parse_html_tables,
    parseable_number,
    text_lines_to_rows,
)
from app.services.parsing.text_normalization import normalize_matching_text

ENGINE_STATUS_SUCCESS = "SUCCESS"
ENGINE_STATUS_PARTIAL = "PARTIAL"
ENGINE_STATUS_FAILED = "FAILED"
ENGINE_STATUS_UNAVAILABLE = "UNAVAILABLE"
ENGINE_STATUS_SKIPPED = "SKIPPED"
ENGINE_MAX_SAMPLE_PAGES = 8
ENGINE_CANDIDATE_CONTRACT_VERSION = "1.0"


@dataclass
class EngineArtifactPaths:
    raw_output_path: str | None = None
    debug_output_path: str | None = None
    rendered_pages_path: str | None = None
    normalized_candidate_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EngineExtractionResult:
    engine_name: str
    engine_status: str
    engine_priority: int
    pages_attempted: int
    table_candidates: int
    text_blocks: int
    confidence_scores: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    runtime_ms: int = 0
    artifact_paths: EngineArtifactPaths = field(default_factory=EngineArtifactPaths)
    blocker_reason: str | None = None
    recommended_setup_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact_paths"] = self.artifact_paths.to_dict()
        return payload


@dataclass
class EngineRunContext:
    root: Path
    stored_document_path: str | None = None
    statement_table_report: dict[str, Any] = field(default_factory=dict)
    parse_report: dict[str, Any] = field(default_factory=dict)
    extraction_coverage: dict[str, Any] = field(default_factory=dict)
    pages_total: int = 0
    prior_engine_results: list[EngineExtractionResult] = field(default_factory=list)


class ExtractionEngineAdapter(ABC):
    engine_name: str
    engine_priority: int

    @abstractmethod
    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        raise NotImplementedError


def dependency_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def binary_available(binary_name: str) -> bool:
    return resolved_binary_path(binary_name) is not None


def resolved_binary_path(binary_name: str) -> str | None:
    direct = shutil.which(binary_name)
    if direct:
        return direct
    for candidate in common_windows_binary_paths(binary_name):
        if candidate.exists():
            return str(candidate)
    return None


def common_windows_binary_paths(binary_name: str) -> list[Path]:
    program_files = [Path("C:/Program Files"), Path("C:/Program Files (x86)")]
    known: dict[str, list[Path]] = {
        "tesseract": [
            Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
            Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
        ],
        "gswin64c": [
            candidate / "bin" / "gswin64c.exe"
            for root in program_files
            for candidate in sorted(root.glob("gs/gs*")) if root.exists()
        ],
        "gs": [
            candidate / "bin" / "gswin64c.exe"
            for root in program_files
            for candidate in sorted(root.glob("gs/gs*")) if root.exists()
        ],
        "qpdf": [
            candidate / "bin" / "qpdf.exe"
            for root in program_files
            for candidate in sorted(root.glob("qpdf*")) if root.exists()
        ],
        "pdftoppm": [
            Path("C:/Program Files/MiKTeX/miktex/bin/x64/pdftoppm.exe"),
            Path("C:/Program Files (x86)/MiKTeX/miktex/bin/pdftoppm.exe"),
        ],
    }
    return known.get(binary_name, [])


def relative_to_root(root: Path, value: str | Path | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        return str(resolved.relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(resolved)


def ensure_engine_runtime_environment(root: Path) -> dict[str, str]:
    cache_root = (root / "data" / "_engine_runtime" / "cache").resolve()
    paddlex_cache = cache_root / "paddlex"
    huggingface_cache = cache_root / "huggingface"
    temp_cache = cache_root / "tmp"
    for path in (cache_root, paddlex_cache, huggingface_cache, temp_cache):
        path.mkdir(parents=True, exist_ok=True)
    values = {
        "PADDLE_PDX_CACHE_HOME": str(paddlex_cache),
        "HF_HOME": str(huggingface_cache),
        "HUGGINGFACE_HUB_CACHE": str(huggingface_cache / "hub"),
        "XDG_CACHE_HOME": str(cache_root),
        "TMP": str(temp_cache),
        "TEMP": str(temp_cache),
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        "FLAGS_use_onednn": "0",
        "FLAGS_enable_pir_api": "0",
    }
    binary_dirs = sorted(
        {
            str(Path(path).parent)
            for binary_name in ("tesseract", "gswin64c", "gs", "qpdf", "pdftoppm")
            if (path := resolved_binary_path(binary_name))
        }
    )
    if binary_dirs:
        existing_path = os.environ.get("PATH", "")
        path_parts = [part for part in existing_path.split(os.pathsep) if part]
        merged_path = [*binary_dirs, *[part for part in path_parts if part not in binary_dirs]]
        values["PATH"] = os.pathsep.join(merged_path)
        tesseract_path = resolved_binary_path("tesseract")
        if tesseract_path:
            tessdata = Path(tesseract_path).parent / "tessdata"
            if tessdata.exists():
                values["TESSDATA_PREFIX"] = str(tessdata)
    for key, value in values.items():
        os.environ[key] = value
    return values


def local_paddlex_model_dir(root: Path, model_name: str) -> str | None:
    model_dir = root / "data" / "_engine_runtime" / "cache" / "paddlex" / "official_models" / model_name
    if (model_dir / "inference.yml").exists():
        return str(model_dir.resolve())
    return None


def local_paddle_ocr_model_kwargs(root: Path) -> dict[str, str]:
    candidates = {
        "text_detection_model_dir": "PP-OCRv5_server_det",
        "text_recognition_model_dir": "eslav_PP-OCRv5_mobile_rec",
    }
    kwargs = {
        argument: model_dir
        for argument, model_name in candidates.items()
        if (model_dir := local_paddlex_model_dir(root, model_name))
    }
    if "text_detection_model_dir" in kwargs:
        kwargs["text_detection_model_name"] = "PP-OCRv5_server_det"
    if "text_recognition_model_dir" in kwargs:
        kwargs["text_recognition_model_name"] = "eslav_PP-OCRv5_mobile_rec"
    return kwargs


def paddle_cpu_static_runtime_kwargs() -> dict[str, Any]:
    return {
        "device": "cpu",
        "engine": "paddle_static",
        "enable_mkldnn": False,
        "cpu_threads": 2,
        "enable_hpi": False,
    }


def local_ppstructure_model_kwargs(root: Path) -> dict[str, str]:
    candidates = {
        "doc_orientation_classify_model_dir": "PP-LCNet_x1_0_doc_ori",
        "doc_unwarping_model_dir": "UVDoc",
        "layout_detection_model_dir": "PP-DocLayout_plus-L",
        "region_detection_model_dir": "PP-DocBlockLayout",
        "text_detection_model_dir": "PP-OCRv5_server_det",
        "text_recognition_model_dir": "eslav_PP-OCRv5_mobile_rec",
        "textline_orientation_model_dir": "PP-LCNet_x1_0_textline_ori",
        "table_classification_model_dir": "PP-LCNet_x1_0_table_cls",
        "wired_table_structure_recognition_model_dir": "SLANeXt_wired",
        "wireless_table_structure_recognition_model_dir": "SLANet_plus",
        "wired_table_cells_detection_model_dir": "RT-DETR-L_wired_table_cell_det",
        "wireless_table_cells_detection_model_dir": "RT-DETR-L_wireless_table_cell_det",
    }
    kwargs = {
        argument: model_dir
        for argument, model_name in candidates.items()
        if (model_dir := local_paddlex_model_dir(root, model_name))
    }
    model_name_arguments = {
        "doc_orientation_classify_model_name": "PP-LCNet_x1_0_doc_ori",
        "doc_unwarping_model_name": "UVDoc",
        "layout_detection_model_name": "PP-DocLayout_plus-L",
        "region_detection_model_name": "PP-DocBlockLayout",
        "text_detection_model_name": "PP-OCRv5_server_det",
        "text_recognition_model_name": "eslav_PP-OCRv5_mobile_rec",
        "textline_orientation_model_name": "PP-LCNet_x1_0_textline_ori",
        "table_classification_model_name": "PP-LCNet_x1_0_table_cls",
        "wired_table_structure_recognition_model_name": "SLANeXt_wired",
        "wireless_table_structure_recognition_model_name": "SLANet_plus",
        "wired_table_cells_detection_model_name": "RT-DETR-L_wired_table_cell_det",
        "wireless_table_cells_detection_model_name": "RT-DETR-L_wireless_table_cell_det",
    }
    for argument, model_name in model_name_arguments.items():
        dir_argument = argument.replace("_name", "_dir")
        if dir_argument in kwargs:
            kwargs[argument] = model_name
    return kwargs


class NativePdfTextEngineAdapter(ExtractionEngineAdapter):
    engine_name = "native_pdf_text_engine"
    engine_priority = 100

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        coverage = context.extraction_coverage or {}
        pages_with_text_layer = int(coverage.get("pages_with_text_layer") or 0)
        pages_attempted = int(coverage.get("pages_processed_by_native_extractor") or 0)
        status = (
            ENGINE_STATUS_SUCCESS
            if pages_with_text_layer
            else ENGINE_STATUS_PARTIAL
            if pages_attempted
            else ENGINE_STATUS_SKIPPED
        )
        result = EngineExtractionResult(
            engine_name=self.engine_name,
            engine_status=status,
            engine_priority=self.engine_priority,
            pages_attempted=pages_attempted,
            table_candidates=0,
            text_blocks=pages_with_text_layer,
            confidence_scores={"text_layer_coverage": _ratio(pages_with_text_layer, pages_attempted or context.pages_total)},
            artifact_paths=EngineArtifactPaths(
                raw_output_path=relative_to_root(context.root, context.stored_document_path),
                debug_output_path=relative_to_root(
                    context.root,
                    context.statement_table_report.get("artifact_path"),
                ),
            ),
        )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


class NativePdfTableEngineAdapter(ExtractionEngineAdapter):
    engine_name = "native_pdf_table_engine"
    engine_priority = 90

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        report = context.statement_table_report or {}
        tables = report.get("statement_tables") or []
        pages_attempted = int((context.extraction_coverage or {}).get("pages_processed_by_native_extractor") or 0)
        table_candidates = len(tables)
        statement_tables = sum(1 for table in tables if table.get("statement_type") != "unknown")
        status = (
            ENGINE_STATUS_SUCCESS
            if statement_tables and report.get("required_statement_tables_found")
            else ENGINE_STATUS_PARTIAL
            if table_candidates
            else ENGINE_STATUS_SKIPPED
        )
        result = EngineExtractionResult(
            engine_name=self.engine_name,
            engine_status=status,
            engine_priority=self.engine_priority,
            pages_attempted=pages_attempted,
            table_candidates=table_candidates,
            text_blocks=0,
            confidence_scores={
                "statement_table_ratio": _ratio(statement_tables, table_candidates),
                "required_statement_coverage": 1.0 if report.get("required_statement_tables_found") else 0.0,
            },
            warnings=list(report.get("warnings") or []),
            artifact_paths=EngineArtifactPaths(
                raw_output_path=relative_to_root(context.root, context.statement_table_report.get("artifact_path")),
                debug_output_path=relative_to_root(context.root, context.statement_table_report.get("artifact_path")),
                normalized_candidate_path=relative_to_root(context.root, context.statement_table_report.get("artifact_path")),
            ),
        )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


class WordLayoutRecoveryEngineAdapter(ExtractionEngineAdapter):
    engine_name = "word_layout_recovery_engine"
    engine_priority = 80

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        report = context.statement_table_report or {}
        tables = (report.get("normalized_statement_tables") or report.get("statement_tables") or [])
        recovered_tables = [
            table
            for table in tables
            if (
                "word_layout_recovery_used" in (table.get("warnings") or [])
                or str((table.get("diagnostics") or {}).get("text_recovery_source") or "") == "extract_words"
                or any(
                    bool((row or {}).get("word_layout_column_recovered"))
                    for row in (table.get("rows") or [])
                    if isinstance(row, dict)
                )
            )
        ]
        recovered_pages = {
            int(table.get("page_number"))
            for table in recovered_tables
            if table.get("page_number") is not None
        }
        structured_row_count = sum(
            1
            for table in recovered_tables
            for row in (table.get("rows") or [])
            if isinstance(row, dict)
            and any(str(key) != "line" and str(row.get(key) or "").strip() for key in row)
        )
        status = ENGINE_STATUS_SUCCESS if recovered_tables else ENGINE_STATUS_SKIPPED
        result = EngineExtractionResult(
            engine_name=self.engine_name,
            engine_status=status,
            engine_priority=self.engine_priority,
            pages_attempted=len(recovered_pages),
            table_candidates=len(recovered_tables),
            text_blocks=structured_row_count,
            confidence_scores={
                "recovered_table_ratio": _ratio(len(recovered_tables), len(tables)),
                "structured_row_recovery": float(structured_row_count > 0),
            },
            artifact_paths=EngineArtifactPaths(
                debug_output_path=relative_to_root(context.root, report.get("artifact_path")),
                normalized_candidate_path=relative_to_root(context.root, report.get("artifact_path")),
            ),
        )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


class OptionalDependencyEngineAdapter(ExtractionEngineAdapter):
    dependency_modules: tuple[str, ...] = ()
    dependency_binaries: tuple[str, ...] = ()
    setup_action: str = ""

    def _missing_dependency(self) -> str | None:
        for module_name in self.dependency_modules:
            if not dependency_available(module_name):
                return f"missing_module:{module_name}"
        for binary_name in self.dependency_binaries:
            if not binary_available(binary_name):
                return f"missing_binary:{binary_name}"
        return None


class SecondaryTableEngineAdapter(OptionalDependencyEngineAdapter):
    engine_name = "secondary_table_engine"
    engine_priority = 70
    dependency_modules = ("camelot",)
    dependency_binaries = ()
    setup_action = "Install camelot-py with its PDF table extraction dependencies for secondary lattice/stream table extraction."

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        ensure_engine_runtime_environment(context.root)
        missing = self._missing_dependency()
        if missing:
            result = EngineExtractionResult(
                engine_name=self.engine_name,
                engine_status=ENGINE_STATUS_UNAVAILABLE,
                engine_priority=self.engine_priority,
                pages_attempted=0,
                table_candidates=0,
                text_blocks=0,
                errors=[missing],
                blocker_reason=missing,
                recommended_setup_action=self.setup_action,
            )
        else:
            stored_document = preferred_document_path(context, allow_ocr_layer=True)
            if not stored_document or not stored_document.exists():
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=0,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["secondary_table_engine_missing_document_path"],
                    recommended_setup_action=self.setup_action,
                )
            else:
                try:
                    camelot = importlib.import_module("camelot")
                    tables = camelot.read_pdf(str(stored_document), pages="all")
                    table_count = len(tables)
                    candidate_payload = []
                    for table in list(tables):
                        dataframe = getattr(table, "df", None)
                        rows = dataframe.fillna("").astype(str).values.tolist() if dataframe is not None else []
                        page_number = int(getattr(table, "page", 0) or 0) or None
                        source_table_id = f"secondary:{page_number or 'na'}:{len(candidate_payload)}"
                        candidate_payload.append(
                            {
                                "page_number": page_number,
                                "rows": rows,
                                "table_title": None,
                                "nearby_text": None,
                                "unit": None,
                                "currency": None,
                                "unit_multiplier": None,
                                "effective_period": None,
                                "comparative_period": None,
                                "period_source": None,
                                "period_confidence": None,
                                "source_engine": self.engine_name,
                                "source_page": page_number,
                                "source_table_id": source_table_id,
                                "source_bbox": None,
                                "parsing_report": getattr(table, "parsing_report", {}),
                            }
                        )
                    debug_path = write_engine_candidate_json(context.root, self.engine_name, candidate_payload)
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_SUCCESS if table_count else ENGINE_STATUS_PARTIAL,
                        engine_priority=self.engine_priority,
                        pages_attempted=context.pages_total or int((context.extraction_coverage or {}).get("pages_total") or 0),
                        table_candidates=table_count,
                        text_blocks=0,
                        confidence_scores={"table_candidates_detected": float(table_count > 0)},
                        artifact_paths=EngineArtifactPaths(
                            raw_output_path=relative_to_root(context.root, stored_document),
                            debug_output_path=relative_to_root(context.root, debug_path),
                            normalized_candidate_path=relative_to_root(context.root, debug_path),
                        ),
                        recommended_setup_action=self.setup_action,
                    )
                except Exception as exc:
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_FAILED,
                        engine_priority=self.engine_priority,
                        pages_attempted=0,
                        table_candidates=0,
                        text_blocks=0,
                        errors=[f"secondary_table_engine_failed:{exc}"],
                        blocker_reason="secondary_table_engine_failed",
                        recommended_setup_action=self.setup_action,
                    )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


class OcrmypdfEngineAdapter(OptionalDependencyEngineAdapter):
    engine_name = "ocrmypdf_engine"
    engine_priority = 65
    dependency_modules = ("ocrmypdf",)
    setup_action = (
        "Install OCRmyPDF with its rasterizer/OCR dependencies to create searchable OCR-layer PDFs "
        "for image-heavy statements."
    )

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        ensure_engine_runtime_environment(context.root)
        missing = self._missing_dependency()
        pages_requiring_ocr = int((context.extraction_coverage or {}).get("pages_requiring_ocr") or 0)
        if missing:
            result = EngineExtractionResult(
                engine_name=self.engine_name,
                engine_status=ENGINE_STATUS_UNAVAILABLE,
                engine_priority=self.engine_priority,
                pages_attempted=0,
                table_candidates=0,
                text_blocks=0,
                errors=[missing],
                blocker_reason=missing,
                recommended_setup_action=self.setup_action,
            )
        else:
            stored_document = preferred_document_path(context, allow_ocr_layer=True)
            if not stored_document or not stored_document.exists():
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=0,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["ocrmypdf_engine_missing_document_path"],
                    recommended_setup_action=self.setup_action,
                )
            elif pages_requiring_ocr <= 0:
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=0,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["ocrmypdf_engine_not_needed_for_current_document"],
                    recommended_setup_action=self.setup_action,
                )
            else:
                try:
                    ocrmypdf_module = importlib.import_module("ocrmypdf")
                    output_dir = engine_artifact_dir(context.root, self.engine_name)
                    output_pdf = output_dir / "ocr_layer.pdf"
                    ocr_hook = getattr(ocrmypdf_module, "ocr", None)
                    if callable(ocr_hook):
                        ocr_hook(
                            str(stored_document),
                            str(output_pdf),
                            language="rus+eng",
                            skip_text=True,
                            output_type="pdf",
                        )
                    debug_path = write_engine_debug_json(
                        context.root,
                        self.engine_name,
                        {
                            "engine_name": self.engine_name,
                            "input_pdf": str(stored_document),
                            "output_pdf": str(output_pdf),
                            "pages_requiring_ocr": pages_requiring_ocr,
                            "hook_invoked": bool(callable(ocr_hook)),
                        },
                    )
                    output_exists = output_pdf.exists()
                    warnings = [] if output_exists else ["ocrmypdf_engine_no_output_pdf_created"]
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_SUCCESS if output_exists else ENGINE_STATUS_PARTIAL,
                        engine_priority=self.engine_priority,
                        pages_attempted=min(pages_requiring_ocr, ENGINE_MAX_SAMPLE_PAGES),
                        table_candidates=0,
                        text_blocks=0,
                        confidence_scores={"ocr_layer_pdf_created": float(output_exists)},
                        artifact_paths=EngineArtifactPaths(
                            raw_output_path=relative_to_root(context.root, output_pdf if output_exists else stored_document),
                            debug_output_path=relative_to_root(context.root, debug_path),
                        ),
                        warnings=warnings,
                        recommended_setup_action=self.setup_action,
                    )
                except Exception as exc:
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_FAILED,
                        engine_priority=self.engine_priority,
                        pages_attempted=min(pages_requiring_ocr, ENGINE_MAX_SAMPLE_PAGES),
                        table_candidates=0,
                        text_blocks=0,
                        errors=[f"ocrmypdf_engine_failed:{exc}"],
                        blocker_reason="ocrmypdf_engine_failed",
                        recommended_setup_action=self.setup_action,
                    )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


class PageRasterizationEngineAdapter(OptionalDependencyEngineAdapter):
    engine_name = "page_rasterization_engine"
    engine_priority = 60
    dependency_modules = ()
    dependency_binaries = ()
    setup_action = "Install pypdfium2 or Poppler tools for page rasterization."

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        ensure_engine_runtime_environment(context.root)
        pdfium_available = dependency_available("pypdfium2")
        poppler_available = binary_available("pdftoppm")
        pages_requiring_ocr = int((context.extraction_coverage or {}).get("pages_requiring_ocr") or 0)
        target_pages = ocr_target_page_numbers(context)
        if not pdfium_available and not poppler_available:
            result = EngineExtractionResult(
                engine_name=self.engine_name,
                engine_status=ENGINE_STATUS_UNAVAILABLE,
                engine_priority=self.engine_priority,
                pages_attempted=0,
                table_candidates=0,
                text_blocks=0,
                errors=["missing_raster_runtime:pypdfium2_or_pdftoppm"],
                blocker_reason="missing_raster_runtime:pypdfium2_or_pdftoppm",
                recommended_setup_action=self.setup_action,
            )
        else:
            stored_document = preferred_document_path(context, allow_ocr_layer=True)
            if not stored_document or not stored_document.exists():
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=0,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["page_rasterization_engine_missing_document_path"],
                    recommended_setup_action=self.setup_action,
                )
            elif pdfium_available:
                try:
                    pdfium = importlib.import_module("pypdfium2")
                    document = pdfium.PdfDocument(str(stored_document))
                    page_count = len(document)
                    pages_to_render = bounded_render_pages(target_pages, page_count, pages_requiring_ocr)
                    render_dir = engine_artifact_dir(context.root, self.engine_name) / "rendered_pages"
                    render_dir.mkdir(parents=True, exist_ok=True)
                    for page_number in pages_to_render:
                        page = document[page_number - 1]
                        rendered = page.render(scale=1)
                        target = render_dir / f"page_{page_number}.png"
                        save_rendered_page(rendered, target)
                    pages_attempted = len(pages_to_render)
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_SUCCESS if pages_attempted else ENGINE_STATUS_PARTIAL,
                        engine_priority=self.engine_priority,
                        pages_attempted=pages_attempted,
                        table_candidates=0,
                        text_blocks=0,
                        confidence_scores={"rendered_page_ratio": _ratio(pages_attempted, page_count)},
                        artifact_paths=EngineArtifactPaths(
                            raw_output_path=relative_to_root(context.root, stored_document),
                            rendered_pages_path=relative_to_root(context.root, render_dir),
                        ),
                        warnings=[] if not target_pages else [f"targeted_ocr_pages:{','.join(map(str, pages_to_render))}"],
                        recommended_setup_action=self.setup_action,
                    )
                except Exception as exc:
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_FAILED,
                        engine_priority=self.engine_priority,
                        pages_attempted=0,
                        table_candidates=0,
                        text_blocks=0,
                        errors=[f"page_rasterization_engine_failed:{exc}"],
                        blocker_reason="page_rasterization_engine_failed",
                        recommended_setup_action=self.setup_action,
                    )
            else:
                try:
                    render_dir = engine_artifact_dir(context.root, self.engine_name) / "rendered_pages"
                    render_dir.mkdir(parents=True, exist_ok=True)
                    pages_to_render = bounded_render_pages(target_pages, context.pages_total or 0, pages_requiring_ocr)
                    if not pages_to_render:
                        pages_to_render = list(range(1, min(max(1, pages_requiring_ocr), ENGINE_MAX_SAMPLE_PAGES) + 1))
                    for page_number in pages_to_render:
                        output_prefix = render_dir / f"page_{page_number}"
                        command = [
                            "pdftoppm",
                            "-png",
                            "-f",
                            str(page_number),
                            "-l",
                            str(page_number),
                            str(stored_document),
                            str(output_prefix),
                        ]
                        completed = subprocess.run(command, check=False, capture_output=True, text=True)
                        if completed.returncode != 0:
                            raise RuntimeError((completed.stderr or completed.stdout or "pdftoppm failed").strip())
                        for rendered in render_dir.glob(f"page_{page_number}-*.png"):
                            normalized = render_dir / f"page_{page_number}.png"
                            if rendered != normalized:
                                rendered.replace(normalized)
                    rendered_pages = sorted(render_dir.glob("*.png"))
                    pages_attempted = len(rendered_pages)
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_SUCCESS if pages_attempted else ENGINE_STATUS_PARTIAL,
                        engine_priority=self.engine_priority,
                        pages_attempted=pages_attempted,
                        table_candidates=0,
                        text_blocks=0,
                        confidence_scores={
                            "rendered_page_ratio": _ratio(
                                pages_attempted,
                                len(pages_to_render),
                            )
                        },
                        artifact_paths=EngineArtifactPaths(
                            raw_output_path=relative_to_root(context.root, stored_document),
                            rendered_pages_path=relative_to_root(context.root, render_dir),
                        ),
                        warnings=(
                            [f"targeted_ocr_pages:{','.join(map(str, pages_to_render))}"]
                            if pages_attempted and target_pages
                            else []
                            if pages_attempted
                            else ["pdftoppm_rendered_no_pages"]
                        ),
                        recommended_setup_action=self.setup_action,
                    )
                except Exception as exc:
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_FAILED,
                        engine_priority=self.engine_priority,
                        pages_attempted=min(max(1, pages_requiring_ocr), ENGINE_MAX_SAMPLE_PAGES) if pages_requiring_ocr else 0,
                        table_candidates=0,
                        text_blocks=0,
                        errors=[f"page_rasterization_engine_failed:{exc}"],
                        blocker_reason="page_rasterization_engine_failed",
                        recommended_setup_action=self.setup_action,
                    )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


class OcrTextEngineAdapter(OptionalDependencyEngineAdapter):
    engine_name = "ocr_text_engine"
    engine_priority = 50
    setup_action = "Install PaddleOCR or Tesseract to enable OCR text extraction."

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        ensure_engine_runtime_environment(context.root)
        paddle = dependency_available("paddleocr")
        tesseract = dependency_available("pytesseract") and binary_available("tesseract")
        pages_requiring_ocr = int((context.extraction_coverage or {}).get("pages_requiring_ocr") or 0)
        if not paddle and not tesseract:
            result = EngineExtractionResult(
                engine_name=self.engine_name,
                engine_status=ENGINE_STATUS_UNAVAILABLE,
                engine_priority=self.engine_priority,
                pages_attempted=0,
                table_candidates=0,
                text_blocks=0,
                errors=["missing_ocr_runtime:paddleocr_or_tesseract"],
                blocker_reason="missing_ocr_runtime:paddleocr_or_tesseract",
                recommended_setup_action=self.setup_action,
            )
        else:
            rendered_dir = rendered_pages_dir_from_context(context)
            if pages_requiring_ocr <= 0:
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=0,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["ocr_text_engine_not_needed_for_current_document"],
                    recommended_setup_action=self.setup_action,
                )
            elif rendered_dir is None or not rendered_dir.exists():
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=pages_requiring_ocr,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["ocr_text_engine_missing_rendered_pages"],
                    recommended_setup_action=self.setup_action,
                )
            else:
                try:
                    candidates: list[dict[str, Any]] = []
                    ocr_warnings: list[str] = []
                    if paddle:
                        paddle_module = importlib.import_module("paddleocr")
                        hook = getattr(paddle_module, "extract_statement_candidates", None)
                        try:
                            if callable(hook):
                                source_document = preferred_document_path(context, allow_ocr_layer=True)
                                candidates = list(
                                    hook(
                                        document_path=str(source_document or ""),
                                        rendered_pages_path=str(rendered_dir),
                                        engine_name=self.engine_name,
                                    )
                                    or []
                                )
                            elif hasattr(paddle_module, "PaddleOCR"):
                                candidates = build_paddle_text_candidates(
                                    paddle_module=paddle_module,
                                    rendered_dir=rendered_dir,
                                    context=context,
                                    source_engine=self.engine_name,
                                )
                        except Exception as exc:
                            ocr_warnings.append(f"paddle_ocr_text_failed_falling_back:{exc}")
                    if not candidates and tesseract:
                        pytesseract = importlib.import_module("pytesseract")
                        if tesseract_path := resolved_binary_path("tesseract"):
                            pytesseract_config = getattr(pytesseract, "pytesseract", None)
                            if pytesseract_config is not None:
                                pytesseract_config.tesseract_cmd = tesseract_path
                        text_by_page: dict[int, str] = {}
                        for page_path in sorted(rendered_dir.glob("*.png"))[:ENGINE_MAX_SAMPLE_PAGES]:
                            page_number = page_number_from_path(page_path)
                            text = pytesseract.image_to_string(str(page_path), lang="rus+eng") or ""
                            if text.strip():
                                text_by_page[page_number] = text
                        candidates = build_ocr_text_candidates(
                            text_by_page=text_by_page,
                            context=context,
                            source_engine=self.engine_name,
                        )
                    debug_path = write_engine_candidate_json(context.root, self.engine_name, candidates)
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_SUCCESS if candidates else ENGINE_STATUS_PARTIAL,
                        engine_priority=self.engine_priority,
                        pages_attempted=min(pages_requiring_ocr, ENGINE_MAX_SAMPLE_PAGES),
                        table_candidates=len(candidates),
                        text_blocks=sum(len(item.get("row_blocks") or []) for item in candidates),
                        confidence_scores={"ocr_candidate_tables_detected": float(bool(candidates))},
                        artifact_paths=EngineArtifactPaths(
                            raw_output_path=relative_to_root(
                                context.root,
                                preferred_document_path(context, allow_ocr_layer=True),
                            ),
                            debug_output_path=relative_to_root(context.root, debug_path),
                            rendered_pages_path=relative_to_root(context.root, rendered_dir),
                            normalized_candidate_path=relative_to_root(context.root, debug_path),
                        ),
                        warnings=ocr_warnings
                        if candidates
                        else [*ocr_warnings, "ocr_text_engine_no_statement_candidates_detected"],
                        recommended_setup_action=self.setup_action,
                    )
                except Exception as exc:
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_FAILED,
                        engine_priority=self.engine_priority,
                        pages_attempted=pages_requiring_ocr,
                        table_candidates=0,
                        text_blocks=0,
                        errors=[f"ocr_text_engine_failed:{exc}"],
                        blocker_reason="ocr_text_engine_failed",
                        recommended_setup_action=self.setup_action,
                    )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


class OcrTableStructureEngineAdapter(OptionalDependencyEngineAdapter):
    engine_name = "ocr_table_structure_engine"
    engine_priority = 40
    dependency_modules = ("paddleocr",)
    setup_action = "Install PaddleOCR/PP-Structure to enable OCR table structure extraction."

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        ensure_engine_runtime_environment(context.root)
        missing = self._missing_dependency()
        pages_requiring_ocr = int((context.extraction_coverage or {}).get("pages_requiring_ocr") or 0)
        if missing:
            result = EngineExtractionResult(
                engine_name=self.engine_name,
                engine_status=ENGINE_STATUS_UNAVAILABLE,
                engine_priority=self.engine_priority,
                pages_attempted=0,
                table_candidates=0,
                text_blocks=0,
                errors=[missing],
                blocker_reason=missing,
                recommended_setup_action=self.setup_action,
            )
        else:
            ocr_text_result = prior_engine_result(context, "ocr_text_engine")
            force_table_structure = os.environ.get("PDF_PARSE_FORCE_OCR_TABLE_STRUCTURE", "").lower() in {"1", "true", "yes"}
            if (
                not force_table_structure
                and ocr_text_result
                and ocr_text_result.engine_status == ENGINE_STATUS_SUCCESS
                and ocr_text_result.table_candidates > 0
            ):
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=0,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["ocr_table_structure_skipped_after_ocr_text_success"],
                    recommended_setup_action=self.setup_action,
                )
                result.runtime_ms = int((time.perf_counter() - started) * 1000)
                return result
            rendered_dir = rendered_pages_dir_from_context(context)
            if pages_requiring_ocr <= 0:
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=0,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["ocr_table_structure_engine_not_needed_for_current_document"],
                    recommended_setup_action=self.setup_action,
                )
            elif rendered_dir is None or not rendered_dir.exists():
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=pages_requiring_ocr,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["ocr_table_structure_engine_missing_rendered_pages"],
                    recommended_setup_action=self.setup_action,
                )
            else:
                try:
                    paddle_module = importlib.import_module("paddleocr")
                    hook = getattr(paddle_module, "extract_statement_candidates", None)
                    source_document = preferred_document_path(context, allow_ocr_layer=True)
                    candidates = list(
                        hook(
                            document_path=str(source_document or ""),
                            rendered_pages_path=str(rendered_dir),
                            engine_name=self.engine_name,
                        )
                        or []
                    ) if callable(hook) else []
                    if not candidates and hasattr(paddle_module, "PPStructureV3"):
                        candidates = build_paddle_structure_candidates(
                            paddle_module=paddle_module,
                            rendered_dir=rendered_dir,
                            context=context,
                            source_engine=self.engine_name,
                        )
                    debug_path = write_engine_candidate_json(context.root, self.engine_name, candidates)
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_SUCCESS if candidates else ENGINE_STATUS_PARTIAL,
                        engine_priority=self.engine_priority,
                        pages_attempted=min(pages_requiring_ocr, ENGINE_MAX_SAMPLE_PAGES),
                        table_candidates=len(candidates),
                        text_blocks=sum(len(item.get("row_blocks") or []) for item in candidates),
                        confidence_scores={"ocr_table_candidates_detected": float(bool(candidates))},
                        artifact_paths=EngineArtifactPaths(
                            raw_output_path=relative_to_root(context.root, source_document),
                            debug_output_path=relative_to_root(context.root, debug_path),
                            rendered_pages_path=relative_to_root(context.root, rendered_dir),
                            normalized_candidate_path=relative_to_root(context.root, debug_path),
                        ),
                        warnings=[] if candidates else ["ocr_table_structure_engine_no_statement_candidates_detected"],
                        recommended_setup_action=self.setup_action,
                    )
                except Exception as exc:
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_FAILED,
                        engine_priority=self.engine_priority,
                        pages_attempted=pages_requiring_ocr,
                        table_candidates=0,
                        text_blocks=0,
                        errors=[f"ocr_table_structure_engine_failed:{exc}"],
                        blocker_reason="ocr_table_structure_engine_failed",
                        recommended_setup_action=self.setup_action,
                    )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


class DoclingDocumentEngineAdapter(OptionalDependencyEngineAdapter):
    engine_name = "docling_document_engine"
    engine_priority = 35
    dependency_modules = ("docling",)
    setup_action = "Install Docling to add advanced PDF layout/table fallback candidates into the normalized statement pipeline."

    def run(self, context: EngineRunContext) -> EngineExtractionResult:
        started = time.perf_counter()
        ensure_engine_runtime_environment(context.root)
        missing = self._missing_dependency()
        if missing:
            result = EngineExtractionResult(
                engine_name=self.engine_name,
                engine_status=ENGINE_STATUS_UNAVAILABLE,
                engine_priority=self.engine_priority,
                pages_attempted=0,
                table_candidates=0,
                text_blocks=0,
                errors=[missing],
                blocker_reason=missing,
                recommended_setup_action=self.setup_action,
            )
        else:
            stored_document = preferred_document_path(context, allow_ocr_layer=True)
            if not stored_document or not stored_document.exists():
                result = EngineExtractionResult(
                    engine_name=self.engine_name,
                    engine_status=ENGINE_STATUS_SKIPPED,
                    engine_priority=self.engine_priority,
                    pages_attempted=0,
                    table_candidates=0,
                    text_blocks=0,
                    warnings=["docling_document_engine_missing_document_path"],
                    recommended_setup_action=self.setup_action,
                )
            else:
                try:
                    docling_module = importlib.import_module("docling")
                    hook = getattr(docling_module, "extract_statement_candidates", None)
                    candidates = list(
                        hook(
                            document_path=str(stored_document),
                            engine_name=self.engine_name,
                            effective_period=statement_period_context(context)[0],
                            comparative_period=statement_period_context(context)[1],
                        )
                        or []
                    ) if callable(hook) else []
                    if not candidates:
                        candidates = build_docling_candidates(
                            stored_document=stored_document,
                            context=context,
                            source_engine=self.engine_name,
                        )
                    debug_path = write_engine_candidate_json(context.root, self.engine_name, candidates)
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_SUCCESS if candidates else ENGINE_STATUS_PARTIAL,
                        engine_priority=self.engine_priority,
                        pages_attempted=context.pages_total or int((context.extraction_coverage or {}).get("pages_total") or 0),
                        table_candidates=len(candidates),
                        text_blocks=sum(len(item.get("row_blocks") or []) for item in candidates),
                        confidence_scores={"docling_candidates_detected": float(bool(candidates))},
                        artifact_paths=EngineArtifactPaths(
                            raw_output_path=relative_to_root(context.root, stored_document),
                            debug_output_path=relative_to_root(context.root, debug_path),
                            normalized_candidate_path=relative_to_root(context.root, debug_path),
                        ),
                        warnings=[] if candidates else ["docling_document_engine_no_statement_candidates_detected"],
                        recommended_setup_action=self.setup_action,
                    )
                except Exception as exc:
                    result = EngineExtractionResult(
                        engine_name=self.engine_name,
                        engine_status=ENGINE_STATUS_FAILED,
                        engine_priority=self.engine_priority,
                        pages_attempted=context.pages_total or int((context.extraction_coverage or {}).get("pages_total") or 0),
                        table_candidates=0,
                        text_blocks=0,
                        errors=[f"docling_document_engine_failed:{exc}"],
                        blocker_reason="docling_document_engine_failed",
                        recommended_setup_action=self.setup_action,
                    )
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result


def default_engine_adapters() -> list[ExtractionEngineAdapter]:
    return [
        NativePdfTextEngineAdapter(),
        NativePdfTableEngineAdapter(),
        WordLayoutRecoveryEngineAdapter(),
        SecondaryTableEngineAdapter(),
        OcrmypdfEngineAdapter(),
        PageRasterizationEngineAdapter(),
        OcrTextEngineAdapter(),
        OcrTableStructureEngineAdapter(),
        DoclingDocumentEngineAdapter(),
    ]


def runtime_profiles() -> dict[str, Any]:
    secondary_missing = SecondaryTableEngineAdapter()._missing_dependency()
    ocrmypdf_missing = OcrmypdfEngineAdapter()._missing_dependency()
    raster_missing = PageRasterizationEngineAdapter()._missing_dependency()
    ocr_text = OcrTextEngineAdapter().run(EngineRunContext(root=Path(".")))
    ocr_table = OcrTableStructureEngineAdapter().run(EngineRunContext(root=Path(".")))
    docling_missing = DoclingDocumentEngineAdapter()._missing_dependency()
    return {
        "base_runtime": {
            "legacy_parser_path": True,
            "machine_report_contract": True,
            "native_pdfplumber_path": dependency_available("pdfplumber"),
            "word_layout_recovery": True,
        },
        "pdf_heavy_runtime": {
            "secondary_table_engine_ready": secondary_missing is None,
            "ocrmypdf_ready": ocrmypdf_missing is None,
            "page_rasterization_ready": raster_missing is None,
            "ocr_text_ready": ocr_text.engine_status != ENGINE_STATUS_UNAVAILABLE,
            "ocr_table_structure_ready": ocr_table.engine_status != ENGINE_STATUS_UNAVAILABLE,
            "docling_document_engine_ready": docling_missing is None,
        },
    }


def resolved_document_path(context: EngineRunContext) -> Path | None:
    if not context.stored_document_path:
        return None
    path = Path(context.stored_document_path)
    return path if path.is_absolute() else (context.root / path).resolve()


def preferred_ocr_document_path(context: EngineRunContext) -> Path | None:
    ocr_result = prior_engine_result(context, "ocrmypdf_engine")
    if not ocr_result or not ocr_result.artifact_paths.raw_output_path:
        return None
    path = Path(ocr_result.artifact_paths.raw_output_path)
    resolved = path if path.is_absolute() else (context.root / path).resolve()
    return resolved if resolved.exists() else None


def preferred_document_path(context: EngineRunContext, *, allow_ocr_layer: bool = False) -> Path | None:
    if allow_ocr_layer:
        ocr_path = preferred_ocr_document_path(context)
        if ocr_path is not None:
            return ocr_path
    return resolved_document_path(context)


def ocr_target_page_numbers(context: EngineRunContext) -> list[int]:
    coverage = dict(context.extraction_coverage or {})
    explicit_pages = coverage.get("pages_requiring_ocr_numbers") or coverage.get("ocr_required_pages")
    pages: set[int] = set()
    if isinstance(explicit_pages, list):
        for value in explicit_pages:
            try:
                page = int(value)
            except (TypeError, ValueError):
                continue
            if page > 0:
                pages.add(page)
    for warning in list((context.statement_table_report or {}).get("warnings") or []):
        text = str(warning or "")
        if not text.startswith("primary_statement_page_image_only_or_no_extractable_text"):
            continue
        match = re.search(r"(?:page=|page:)(\d+)", text)
        if match:
            pages.add(int(match.group(1)))
    for table in list((context.statement_table_report or {}).get("statement_tables") or []):
        diagnostics = dict(table.get("diagnostics") or {})
        if diagnostics.get("requires_ocr") and table.get("page_number"):
            try:
                pages.add(int(table["page_number"]))
            except (TypeError, ValueError):
                continue
    return sorted(pages)


def bounded_render_pages(target_pages: list[int], page_count: int, pages_requiring_ocr: int) -> list[int]:
    if target_pages:
        upper = page_count if page_count > 0 else max(target_pages)
        return [page for page in target_pages if 1 <= page <= upper][:ENGINE_MAX_SAMPLE_PAGES]
    if page_count > 0:
        return list(range(1, min(page_count, max(1, pages_requiring_ocr or page_count), ENGINE_MAX_SAMPLE_PAGES) + 1))
    if pages_requiring_ocr > 0:
        return list(range(1, min(pages_requiring_ocr, ENGINE_MAX_SAMPLE_PAGES) + 1))
    return []


def engine_artifact_dir(root: Path, engine_name: str) -> Path:
    temp_name = Path(tempfile.mkdtemp(prefix="", dir=tempfile.gettempdir())).name
    base = root / "data" / "_engine_runtime" / engine_name / temp_name
    base.mkdir(parents=True, exist_ok=True)
    return base


def write_engine_debug_json(root: Path, engine_name: str, payload: dict[str, Any]) -> Path:
    directory = engine_artifact_dir(root, engine_name)
    path = directory / "debug.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def write_engine_candidate_json(root: Path, engine_name: str, tables: list[dict[str, Any]]) -> Path:
    return write_engine_debug_json(
        root,
        engine_name,
        {
            "engine_name": engine_name,
            "candidate_contract_version": ENGINE_CANDIDATE_CONTRACT_VERSION,
            "tables": tables,
        },
    )


def save_rendered_page(rendered: Any, target: Path) -> None:
    if hasattr(rendered, "to_pil"):
        rendered.to_pil().save(target)
        return
    if hasattr(rendered, "save"):
        rendered.save(target)
        return
    target.write_bytes(b"")


def prior_engine_result(context: EngineRunContext, engine_name: str) -> EngineExtractionResult | None:
    for result in reversed(context.prior_engine_results):
        if result.engine_name == engine_name:
            return result
    return None


def rendered_pages_dir_from_context(context: EngineRunContext) -> Path | None:
    raster_result = prior_engine_result(context, "page_rasterization_engine")
    if not raster_result or not raster_result.artifact_paths.rendered_pages_path:
        return None
    rendered_path = Path(raster_result.artifact_paths.rendered_pages_path)
    return rendered_path if rendered_path.is_absolute() else (context.root / rendered_path).resolve()


def statement_period_context(context: EngineRunContext) -> tuple[str | None, str | None, str | None, float | None]:
    period_resolution = dict(context.statement_table_report.get("period_resolution") or {})
    return (
        period_resolution.get("effective_report_period") or context.statement_table_report.get("period"),
        period_resolution.get("comparative_period"),
        period_resolution.get("period_source"),
        period_resolution.get("period_confidence"),
    )


def build_ocr_text_candidates(
    *,
    text_by_page: dict[int, str],
    context: EngineRunContext,
    source_engine: str,
) -> list[dict[str, Any]]:
    effective_period, comparative_period, period_source, period_confidence = statement_period_context(context)
    candidates: list[dict[str, Any]] = []
    for page_number, text in text_by_page.items():
        if not str(text or "").strip():
            continue
        for fragment_index, fragment_text in enumerate(ocr_text_fragments(text)):
            reconstructed_fragment_text = reconstruct_sequential_ocr_statement_text(fragment_text)
            rows = text_lines_to_rows(reconstructed_fragment_text)
            if len(rows) < 2:
                continue
            columns, records = build_text_fallback_records(
                rows,
                current_period=effective_period,
                comparative_period=comparative_period,
            )
            statement_family = infer_statement_family_from_ocr_fragment(reconstructed_fragment_text, records)
            if statement_family == "unknown":
                continue
            row_blocks, row_diagnostics = build_normalized_row_blocks(
                records=records,
                columns=columns,
                statement_family=statement_family,
                source_engine=source_engine,
                source_page=page_number,
                source_table_id=f"{source_engine}:{page_number}:{fragment_index}",
            )
            if not has_usable_ocr_row_blocks(row_blocks):
                continue
            candidates.append(
                {
                    "page_number": page_number,
                    "statement_family": statement_family,
                    "statement_type": statement_family,
                    "table_role": "primary_statement_degraded_but_usable",
                    "table_title": guess_title(reconstructed_fragment_text) or guess_title(text),
                    "nearby_text": reconstructed_fragment_text,
                    "unit": detect_unit(text),
                    "currency": detect_currency(text),
                    "unit_multiplier": detect_unit_multiplier(text),
                    "effective_period": effective_period,
                    "comparative_period": comparative_period,
                    "period_source": period_source or "document_text",
                    "period_confidence": period_confidence or 0.6,
                    "header_columns": columns,
                    "row_blocks": row_blocks,
                    "header_row_count": 1,
                    "table_structure_quality": "degraded",
                    "quality_flag": "ocr_candidate_table",
                    "warnings": ["ocr_only_lower_trust"],
                    "source_engine": source_engine,
                    "source_page": page_number,
                    "source_table_id": f"{source_engine}:{page_number}:{fragment_index}",
                    "source_bbox": None,
                    "diagnostics": row_diagnostics,
                    "confidence_score": 0.72,
                }
            )
    return candidates


def reconstruct_sequential_ocr_statement_text(text: str) -> str:
    lines = [normalize_ocr_statement_line(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    reconstructed: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if is_standalone_change_value_line(line):
            index += 1
            continue
        if (
            not line_has_letters(line)
            or is_text_fallback_header_or_title_line(line)
            or line_has_statement_values(line)
        ):
            reconstructed.append(line)
            index += 1
            continue
        values: list[str] = []
        consumed_until = index
        for lookahead_index in range(index + 1, min(len(lines), index + 8)):
            candidate = lines[lookahead_index]
            if not candidate:
                continue
            if line_has_letters(candidate) and not is_short_note_reference(candidate):
                break
            if is_short_note_reference(candidate) and not values:
                consumed_until = lookahead_index
                continue
            if is_ocr_statement_value_token(candidate):
                values.append(candidate)
                consumed_until = lookahead_index
                if len(values) == 2:
                    break
                continue
            if values:
                break
        if len(values) >= 2:
            reconstructed.append(" ".join([line, *values[:2]]))
            index = consumed_until + 1
            continue
        reconstructed.append(line)
        index += 1
    return "\n".join(reconstructed)


def normalize_ocr_statement_line(line: str) -> str:
    return re.sub(r"\s+", " ", str(line or "").replace("\u00a0", " ")).strip()


def line_has_letters(line: str) -> bool:
    return bool(re.search(r"[A-Za-zА-Яа-я]", str(line or "")))


def is_standalone_change_value_line(line: str) -> bool:
    tokens = [token for token in str(line or "").split() if token]
    if not tokens:
        return False
    return all("%" in token or token in {"-", "—", "н.п.", "н.п", "n.m.", "n/a"} for token in tokens)


def line_has_statement_values(line: str) -> bool:
    return split_statement_value_tokens(line) >= 2


def split_statement_value_tokens(line: str) -> int:
    return sum(1 for token in re.findall(r"[-−]?\(?\d[\d\s.,]*\)?%?", str(line or "")) if is_ocr_statement_value_token(token))


def is_ocr_statement_value_token(token: str) -> bool:
    cleaned = str(token or "").strip()
    if not cleaned or "%" in cleaned:
        return False
    if is_short_note_reference(cleaned):
        return False
    return parseable_number(cleaned) is not None


def build_paddle_text_candidates(
    *,
    paddle_module: Any,
    rendered_dir: Path,
    context: EngineRunContext,
    source_engine: str,
) -> list[dict[str, Any]]:
    predictor_cls = getattr(paddle_module, "PaddleOCR", None)
    if predictor_cls is None:
        return []
    predictor = predictor_cls(
        lang="ru",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        **paddle_cpu_static_runtime_kwargs(),
        **local_paddle_ocr_model_kwargs(context.root),
    )
    text_by_page: dict[int, str] = {}
    for page_path in sorted(rendered_dir.glob("*.png"))[:ENGINE_MAX_SAMPLE_PAGES]:
        page_number = page_number_from_path(page_path)
        prediction = invoke_predictor(predictor, str(page_path))
        page_text = extract_text_from_prediction(prediction)
        if page_text.strip():
            text_by_page[page_number] = page_text
    return build_ocr_text_candidates(
        text_by_page=text_by_page,
        context=context,
        source_engine=source_engine,
    )


def build_paddle_structure_candidates(
    *,
    paddle_module: Any,
    rendered_dir: Path,
    context: EngineRunContext,
    source_engine: str,
) -> list[dict[str, Any]]:
    structure_cls = getattr(paddle_module, "PPStructureV3", None)
    if structure_cls is None:
        return []
    predictor = structure_cls(
        use_table_recognition=True,
        use_formula_recognition=False,
        use_chart_recognition=False,
        use_region_detection=True,
        lang="ru",
        **paddle_cpu_static_runtime_kwargs(),
        **local_ppstructure_model_kwargs(context.root),
    )
    effective_period, comparative_period, period_source, period_confidence = statement_period_context(context)
    candidates: list[dict[str, Any]] = []
    for page_path in sorted(rendered_dir.glob("*.png"))[:ENGINE_MAX_SAMPLE_PAGES]:
        page_number = page_number_from_path(page_path)
        prediction = invoke_predictor(predictor, str(page_path))
        payload = to_builtin_payload(prediction)
        page_text = extract_text_from_prediction(payload)
        table_entries = extract_table_entries_from_payload(payload, default_page_number=page_number)
        if not table_entries:
            table_entries = [
                {
                    "page_number": page_number,
                    "row_matrix": matrix,
                    "table_title": None,
                    "nearby_text": page_text,
                    "table_bbox": None,
                    "page_bbox": None,
                    "column_boundaries": [],
                }
                for matrix in extract_row_matrices_from_payload(payload)
            ]
        for table_index, entry in enumerate(table_entries):
            candidate = candidate_from_row_matrix(
                row_matrix=entry["row_matrix"],
                page_number=entry.get("page_number"),
                source_engine=source_engine,
                source_table_id=f"{source_engine}:{page_number}:{table_index}",
                nearby_text=str(entry.get("nearby_text") or page_text or ""),
                effective_period=effective_period,
                comparative_period=comparative_period,
                period_source=period_source,
                period_confidence=period_confidence,
                warnings=["ocr_only_lower_trust"],
                quality_flag="ocr_candidate_table",
                table_title=entry.get("table_title"),
                table_bbox=entry.get("table_bbox"),
                page_bbox=entry.get("page_bbox"),
                column_boundaries=entry.get("column_boundaries"),
            )
            if candidate:
                candidates.append(candidate)
    return candidates


def build_docling_candidates(
    *,
    stored_document: Path,
    context: EngineRunContext,
    source_engine: str,
) -> list[dict[str, Any]]:
    try:
        from docling.document_converter import DocumentConverter
    except Exception:
        return []
    converter = DocumentConverter()
    conversion = converter.convert(str(stored_document), raises_on_error=False, max_num_pages=ENGINE_MAX_SAMPLE_PAGES)
    document = getattr(conversion, "document", None)
    if document is None:
        return []
    payload = to_builtin_payload(document)
    effective_period, comparative_period, period_source, period_confidence = statement_period_context(context)
    doc_text = extract_text_from_prediction(payload)
    candidates: list[dict[str, Any]] = []
    table_entries = extract_table_entries_from_payload(payload)
    if not table_entries:
        table_entries = [
            {
                "page_number": None,
                "row_matrix": matrix,
                "table_title": None,
                "nearby_text": doc_text,
                "table_bbox": None,
                "page_bbox": None,
                "column_boundaries": [],
            }
            for matrix in extract_row_matrices_from_payload(payload)
        ]
    for table_index, entry in enumerate(table_entries):
        candidate = candidate_from_row_matrix(
            row_matrix=entry["row_matrix"],
            page_number=entry.get("page_number"),
            source_engine=source_engine,
            source_table_id=f"{source_engine}:{entry.get('page_number') or 'na'}:{table_index}",
            nearby_text=str(entry.get("nearby_text") or doc_text or ""),
            effective_period=effective_period,
            comparative_period=comparative_period,
            period_source=period_source,
            period_confidence=period_confidence,
            warnings=[],
            quality_flag="docling_candidate_table",
            table_title=entry.get("table_title"),
            table_bbox=entry.get("table_bbox"),
            page_bbox=entry.get("page_bbox"),
            column_boundaries=entry.get("column_boundaries"),
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def invoke_predictor(predictor: Any, input_path: str) -> Any:
    predict = getattr(predictor, "predict", None)
    if not callable(predict):
        return None
    try:
        return predict(input_path)
    except TypeError:
        return predict([input_path])


def to_builtin_payload(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "tolist"):
        try:
            return to_builtin_payload(value.tolist())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): to_builtin_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_builtin_payload(item) for item in value]
    for attr in ("model_dump", "export_to_dict", "to_dict", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return to_builtin_payload(method())
            except TypeError:
                continue
    return str(value)


def extract_text_from_prediction(payload: Any) -> str:
    built = to_builtin_payload(payload)
    fragments: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, str):
            text = node.strip()
            if text:
                fragments.append(text)
            return
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = str(key).lower()
                if normalized_key in {
                    "text",
                    "rec_text",
                    "rec_texts",
                    "label",
                    "html",
                    "markdown",
                    "table_markdown",
                    "table_html",
                    "content",
                }:
                    visit(value)
                elif isinstance(value, (dict, list)):
                    visit(value)

    visit(built)
    return "\n".join(fragment for fragment in fragments if fragment).strip()


def extract_row_matrices_from_payload(payload: Any) -> list[list[list[str]]]:
    built = to_builtin_payload(payload)
    matrices: list[list[list[str]]] = []

    def visit(node: Any) -> None:
        matrix = coerce_row_matrix(node)
        if matrix:
            matrices.append(matrix)
            return
        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = str(key).lower()
                if normalized_key in {"html", "table_html", "markdown", "table_markdown"} and isinstance(value, str):
                    for html_matrix in row_matrices_from_markup(value):
                        matrices.append(html_matrix)
                elif isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(built)
    return matrices


def extract_table_entries_from_payload(
    payload: Any,
    *,
    default_page_number: int | None = None,
) -> list[dict[str, Any]]:
    built = to_builtin_payload(payload)
    entries: list[dict[str, Any]] = []

    def walk(node: Any, *, page_number: int | None, page_text_parts: list[str], page_bbox: Any) -> None:
        if isinstance(node, dict):
            local_page_number = page_number
            if local_page_number is None:
                for key in ("page_number", "page_no", "page", "page_index"):
                    if node.get(key) is not None:
                        try:
                            local_page_number = int(node.get(key))
                        except (TypeError, ValueError):
                            local_page_number = page_number
                        break
            local_page_bbox = node.get("page_bbox") or page_bbox
            local_text_parts = list(page_text_parts)
            for text_key in ("text", "rec_text", "rec_texts", "label", "content", "markdown"):
                text_value = node.get(text_key)
                if isinstance(text_value, str) and text_value.strip() and "<table" not in text_value.lower():
                    local_text_parts.append(text_value.strip())
                elif isinstance(text_value, list):
                    local_text_parts.extend(str(item).strip() for item in text_value if str(item).strip())
            matrix = coerce_row_matrix(node)
            if matrix:
                entries.append(
                    {
                        "page_number": local_page_number if local_page_number is not None else default_page_number,
                        "row_matrix": matrix,
                        "table_title": first_non_empty_string(
                            node.get("table_title"),
                            node.get("title"),
                            node.get("label"),
                        ),
                        "nearby_text": "\n".join(local_text_parts).strip(),
                        "table_bbox": node.get("table_bbox") or node.get("bbox"),
                        "page_bbox": local_page_bbox,
                        "column_boundaries": list(node.get("column_boundaries") or []),
                    }
                )
                return
            for key, value in node.items():
                normalized_key = str(key).lower()
                if normalized_key in {"html", "table_html", "markdown", "table_markdown"} and isinstance(value, str):
                    for matrix in row_matrices_from_markup(value):
                        entries.append(
                            {
                                "page_number": local_page_number if local_page_number is not None else default_page_number,
                                "row_matrix": matrix,
                                "table_title": first_non_empty_string(
                                    node.get("table_title"),
                                    node.get("title"),
                                    node.get("label"),
                                ),
                                "nearby_text": "\n".join(local_text_parts).strip(),
                                "table_bbox": node.get("table_bbox") or node.get("bbox"),
                                "page_bbox": local_page_bbox,
                                "column_boundaries": list(node.get("column_boundaries") or []),
                            }
                        )
                elif isinstance(value, (dict, list)):
                    walk(
                        value,
                        page_number=local_page_number,
                        page_text_parts=local_text_parts,
                        page_bbox=local_page_bbox,
                    )
        elif isinstance(node, list):
            for item in node:
                walk(item, page_number=page_number, page_text_parts=page_text_parts, page_bbox=page_bbox)

    walk(built, page_number=default_page_number, page_text_parts=[], page_bbox=None)
    return entries


def coerce_row_matrix(value: Any) -> list[list[str]] | None:
    if isinstance(value, list) and value:
        if all(isinstance(item, list) for item in value):
            matrix = [
                [str(cell or "").strip() for cell in row]
                for row in value
                if isinstance(row, list) and any(str(cell or "").strip() for cell in row)
            ]
            matrix_text = " ".join(" ".join(row) for row in matrix)
            if not re.search(r"[A-Za-zА-Яа-я]", matrix_text):
                return None
            return matrix or None
        if all(isinstance(item, dict) for item in value):
            keys: list[str] = []
            for item in value:
                for key in item:
                    if key not in keys:
                        keys.append(str(key))
            if keys:
                matrix = [keys]
                for item in value:
                    matrix.append([str(item.get(key, "") or "").strip() for key in keys])
                return matrix
    if isinstance(value, dict):
        for key in ("rows", "table_rows", "cells", "data", "table"):
            if key in value:
                return coerce_row_matrix(value[key])
    return None


def row_matrices_from_markup(markup: str) -> list[list[list[str]]]:
    text = str(markup or "").strip()
    if not text:
        return []
    if "<table" in text.lower():
        return parse_html_tables(text)
    return markdown_table_to_matrices(text)


def markdown_table_to_matrices(text: str) -> list[list[list[str]]]:
    lines = [line.strip() for line in str(text or "").splitlines() if "|" in line]
    if len(lines) < 2:
        return []
    matrix: list[list[str]] = []
    for line in lines:
        parts = [part.strip() for part in line.strip("|").split("|")]
        if not any(parts):
            continue
        if all(set(part) <= {"-", ":"} for part in parts):
            continue
        matrix.append(parts)
    return [matrix] if len(matrix) >= 2 else []


def candidate_from_row_matrix(
    *,
    row_matrix: list[list[str]],
    page_number: int | None,
    source_engine: str,
    source_table_id: str,
    nearby_text: str,
    effective_period: str | None,
    comparative_period: str | None,
    period_source: str | None,
    period_confidence: float | None,
    warnings: list[str],
    quality_flag: str,
    table_title: Any = None,
    table_bbox: Any = None,
    page_bbox: Any = None,
    column_boundaries: Any = None,
) -> dict[str, Any] | None:
    if len(row_matrix) < 2:
        return None
    title = first_non_empty_string(
        table_title,
        " ".join(cell for cell in row_matrix[0] if str(cell or "").strip()),
    )
    matrix_text = "\n".join(" ".join(str(cell or "").strip() for cell in row) for row in row_matrix)
    merged_text = "\n".join(fragment for fragment in [title, nearby_text, matrix_text] if fragment).strip()
    statement_family = classify_primary_statement_page(merged_text, "IFRS")
    if statement_family == "unknown":
        statement_family = classify_statement_family(merged_text, "IFRS")
    if statement_family not in {"income_statement", "balance_sheet", "cash_flow"}:
        return None
    return {
        "page_number": page_number,
        "rows": row_matrix,
        "table_title": title or guess_title(merged_text),
        "nearby_text": merged_text,
        "unit": detect_unit(merged_text),
        "currency": detect_currency(merged_text),
        "unit_multiplier": detect_unit_multiplier(merged_text),
        "effective_period": effective_period,
        "comparative_period": comparative_period,
        "period_source": period_source,
        "period_confidence": period_confidence or 0.6,
        "statement_family": statement_family,
        "statement_type": statement_family,
        "table_role": "primary_statement_degraded_but_usable",
        "source_engine": source_engine,
        "source_page": page_number,
        "source_table_id": source_table_id,
        "source_bbox": None,
        "table_bbox": table_bbox,
        "page_bbox": page_bbox,
        "column_boundaries": list(column_boundaries or []),
        "warnings": list(warnings),
        "quality_flag": quality_flag,
        "confidence_score": 0.74 if "ocr" in source_engine else 0.78,
    }


def page_number_from_path(path: Path) -> int:
    stem = path.stem
    digits = "".join(ch for ch in stem if ch.isdigit())
    return int(digits) if digits else 1


def ocr_text_fragments(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n")
    fragments = [fragment.strip() for fragment in normalized.split("\n\n") if fragment.strip()]
    return fragments or [normalized.strip()]


def infer_statement_family_from_ocr_fragment(fragment_text: str, records: list[dict[str, Any]]) -> str:
    direct = classify_primary_statement_page(fragment_text, "IFRS")
    if direct != "unknown":
        return direct
    broad = classify_statement_family(fragment_text, "IFRS")
    if broad in {"income_statement", "balance_sheet", "cash_flow"}:
        return broad
    normalized = normalize_matching_text(fragment_text)
    record_lines = " ".join(normalize_matching_text(str(record.get("line") or "")) for record in records)
    corpus = f"{normalized} {record_lines}".strip()
    income_markers = ("revenue", "sales", "operating profit", "net income", "profit for the year")
    balance_markers = ("total assets", "total equity", "current assets", "current liabilities", "cash and cash equivalents")
    cash_flow_markers = ("operating cash flow", "cash generated from operating activities", "net cash from operating activities")
    if sum(1 for marker in income_markers if marker in corpus) >= 2:
        return "income_statement"
    if sum(1 for marker in balance_markers if marker in corpus) >= 2:
        return "balance_sheet"
    if sum(1 for marker in cash_flow_markers if marker in corpus) >= 1:
        return "cash_flow"
    return "unknown"


def has_usable_ocr_row_blocks(row_blocks: list[dict[str, Any]]) -> bool:
    usable_count = 0
    for block in row_blocks:
        row_kind = str(block.get("row_kind") or "")
        if row_kind not in {"statement_line_item", "subtotal", "grand_total"}:
            continue
        if not block.get("label_text"):
            continue
        if not (block.get("value_cells") or {}):
            continue
        usable_count += 1
    return usable_count > 0


def _ratio(numerator: int, denominator: int | None) -> float:
    if not denominator:
        return 0.0
    return round(numerator / denominator, 4)


def first_non_empty_string(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None
