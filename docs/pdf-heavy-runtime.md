# PDF-Heavy Runtime

This is the target deployment profile for maximum automatic parsing of RU public-company IFRS/RAS PDF reports.

It keeps the legacy parser path and the canonical `machine_report.json` contract intact, while enabling a deeper extraction cascade on top of the base runtime.

## Stack

The production-oriented cascade is:

1. `pdfplumber`
   - born-digital text, geometry, and table candidates
2. `Camelot`
   - secondary table extraction for bordered and borderless layouts
3. `OCRmyPDF`
   - searchable OCR-layer PDF creation for image-heavy or weak-text PDFs
4. `pypdfium2` / raster backend
   - page rendering for OCR/table-vision stages
5. `PaddleOCR` / `PP-Structure`
   - OCR text, layout, and table-structure candidates
6. `Docling`
   - advanced document-layout and structured-document fallback
7. `Tesseract`
   - backup OCR text path when Paddle is unavailable

All of these engines feed candidate artifacts into the same normalized statement pipeline. None of them may bypass semantic validation.

## Install Targets

### Base runtime

The base runtime remains import-safe and test-safe:

- FastAPI app starts
- legacy parser path works
- canonical machine report works
- optional engines may report `UNAVAILABLE`

### PDF-heavy runtime

Install Python dependencies:

```bash
pip install -e .[pdf-heavy]
```

Or install both development and PDF-heavy extras:

```bash
pip install -e .[dev,pdf-heavy]
```

System dependencies expected by the reference container:

- Java runtime
- Poppler tools
- Ghostscript
- Tesseract with `rus` and `eng` language packs
- LibreOffice
- image/runtime libraries needed by OCR and rendering stacks

## Reference Container

Use [Dockerfile.pdf-heavy](/abs/path/C:/Users/Lenovo/Documents/New%20project/Dockerfile.pdf-heavy) as the canonical deployment artifact for the max-parse profile.

It installs:

- `.[dev,pdf-heavy]`
- Java
- Poppler
- Ghostscript
- LibreOffice
- Tesseract `rus` + `eng`
- required image/runtime libraries

## Runtime Guarantees

Heavy dependencies are optional at import time.

If `Camelot`, `OCRmyPDF`, `PaddleOCR`, `Tesseract`, `Docling`, or raster backends are missing:

- FastAPI import must still succeed
- pytest collection must still succeed
- legacy parser path must still work
- `machine_report.json` must still be emitted for safe readable PDFs
- the missing engine must report:
  - `engine_status = UNAVAILABLE`
  - blocker reason
  - recommended setup action

## Trust Policy

The PDF-heavy runtime improves recall, not truth shortcuts.

- OCR output never becomes a fact directly
- secondary engines never override conflicts by origin alone
- confirmed facts still require:
  - statement family resolution
  - eligible row kind
  - explicit label/value ownership
  - explicit period ownership
  - sector/interpreter consistency
  - provenance completeness

Everything else remains retained as evidence inside the canonical machine report.
