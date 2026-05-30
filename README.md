# MOEX AI Financial Research Assistant MVP

Backend/data pipeline MVP for an AI financial research assistant that prepares structured analytics payloads for public Russian companies traded on MOEX. The service resolves a company, loads demo/fixture financial statements, parses normalized facts, calculates deterministic financial metrics, loads fixture market candles, calculates basic technical indicators, builds peer comparison, and returns JSON for a later LLM layer.

Every response and LLM payload includes the disclaimer:

> Материал носит информационно-аналитический характер и не является индивидуальной инвестиционной рекомендацией.

## What It Does

- Company registry and search by ticker, Russian/English names, and aliases.
- Async-style analysis jobs via FastAPI `BackgroundTasks`.
- Fixture report manifest loading and CSV fact parsing.
- Normalized `StatementFact` storage with source traceability fields.
- Deterministic financial ratio calculation in Python.
- MOEX ISS runtime client with mocked-test-friendly design.
- Basic technical indicators from candle data.
- Fixture-backed peer analysis.
- Immutable analysis results, history, and extension flow with `delta_summary`.
- LLM-ready JSON payload with compliance instructions.

## What It Does Not Do

- It is not an investment adviser.
- It does not provide individual investment recommendations.
- It does not invent missing financial data.
- It does not call an LLM.
- It does not yet implement production-grade issuer IR/e-disclosure/news adapters.

## Local Setup

```powershell
python -m pip install -e .[dev]
copy .env.example .env
python -m app.db.init_db
uvicorn app.main:app --reload
```

SQLite is used by default via `DATABASE_URL=sqlite:///./local.db`. For PostgreSQL, start:

```powershell
docker compose up -d postgres
```

Then set `DATABASE_URL` accordingly, for example:

```text
postgresql+psycopg://moex:moex@localhost:5432/moex_research
```

## Tests And Lint

```powershell
python -m pytest -p no:cacheprovider
python -m ruff check .
```

The tests use fixtures and mocked network behavior. They do not require internet access.

## Smoke Test

Start the service:

```powershell
python -m pip install -e .[dev]
python -m app.db.init_db
uvicorn app.main:app --reload
```

In another PowerShell session:

```powershell
$base = "http://127.0.0.1:8000"

Invoke-RestMethod "$base/health"

Invoke-RestMethod "$base/companies/search?q=%D0%BB%D1%83%D0%BA%D0%BE%D0%B9%D0%BB"

$job = Invoke-RestMethod -Method Post "$base/analysis-jobs" `
  -ContentType "application/json" `
  -Body '{
    "company_query": "Лукойл",
    "period_from": "2021Q2",
    "period_to": "2021Q4",
    "reporting_standard": "IFRS",
    "include_market_data": true,
    "include_peers": true,
    "include_news": false,
    "output_language": "ru"
  }'

$job

do {
  Start-Sleep -Milliseconds 500
  $status = Invoke-RestMethod "$base/analysis-jobs/$($job.job_id)"
  $status
} while ($status.status -in @("queued", "running"))

$result = Invoke-RestMethod "$base/analysis-results/$($status.result_id)"
$result.result.company
$result.result.period
$result.result.financial_analysis
$result.result.market_analysis
$result.result.peer_analysis
$result.result.source_documents
$result.result.warnings
$result.disclaimer
$result.llm_payload

Invoke-RestMethod "$base/analysis-history?company=LKOH&limit=20"

$extend = Invoke-RestMethod -Method Post "$base/analysis-results/$($status.result_id)/extend" `
  -ContentType "application/json" `
  -Body '{"new_period_to": "2022Q1", "force_refetch": false}'

do {
  Start-Sleep -Milliseconds 500
  $extendStatus = Invoke-RestMethod "$base/analysis-jobs/$($extend.job_id)"
  $extendStatus
} while ($extendStatus.status -in @("queued", "running"))

Invoke-RestMethod "$base/analysis-results/$($extendStatus.result_id)"
```

Expected result:

- Company search returns `LKOH`.
- Analysis job finishes with `status = succeeded`.
- Result contains `company`, `period`, `financial_analysis`, `market_analysis`, `peer_analysis`, `source_documents`, `warnings`, `disclaimer`, and `llm_payload`.
- History contains the created result.
- Extend creates a new immutable result version with `parent_result_id` and `delta_summary`.

## Database Initialization

```powershell
python -m app.db.init_db
```

This creates tables and seeds:

- `LKOH`, `ROSN`, `SIBN`, `TATN`, `NVTK`, `GAZP`
- LKOH peer group

Alembic scaffolding is present for future migrations. MVP schema creation is handled by `init_db`; before production, create explicit Alembic revisions for all model changes.

## API Endpoints

- `GET /health`
- `GET /companies/search?q=лукойл`
- `POST /analysis-jobs`
- `GET /analysis-jobs/{job_id}`
- `GET /analysis-results/{result_id}`
- `POST /analysis-results/{result_id}/extend`
- `GET /analysis-history?company=LKOH&limit=20`
- Backward-compatible aliases: `POST /analyze`, `POST /extend`, `GET /history`

Example:

```json
{
  "company_query": "Лукойл",
  "period_from": "2021Q2",
  "period_to": "2025Q4",
  "reporting_standard": "IFRS",
  "include_market_data": true,
  "include_peers": true,
  "include_news": false,
  "output_language": "ru"
}
```

## Fixture/Demo Mode

Demo data lives in:

- `data/manifests/lkoh.yml`
- `data/fixtures/lkoh_financial_facts.csv`
- `data/fixtures/lkoh_candles.csv`
- `data/fixtures/peers_financial_facts.csv`

Fixture facts are explicitly marked with `source_type = fixture` and/or `quality_flag = fixture`. They are synthetic demo numbers and are not masked as real financial data.
Any fixture-driven result also carries fixture warnings and `fixture` in `data_quality.flags` / `llm_payload.data_quality_flags`.

## Data Modes

Analysis jobs accept `data_mode`:

- `fixture`: default MVP mode. Uses only local demo fixtures.
- `real`: uses only real source adapters and manual real-source manifests. If no real documents are available, the job fails with a controlled error and does not silently fall back.
- `auto`: tries real sources first, then falls back to fixtures with the warning `Real data unavailable; fixture fallback used.`

Example real-mode request:

```json
{
  "company_query": "LKOH",
  "period_from": "2021Q1",
  "period_to": "2021Q4",
  "reporting_standard": "IFRS",
  "include_market_data": true,
  "include_peers": false,
  "include_news": false,
  "output_language": "ru",
  "data_mode": "real"
}
```

## Real LKOH Data

Stage 1.5 adds a conservative real-data adapter for LKOH only:

- `app/services/reports/adapters/lkoh_ir_adapter.py`
- `data/manifests/lkoh_real_sources.yml`

The adapter only uses trusted LKOH IR URLs from `LKOH_IR_REPORTS_URL` or the manual manifest, and only downloads from `REPORT_SOURCE_ALLOWED_DOMAINS`. It does not crawl the public internet. Real parsing is best-effort; parser confidence matters, missing values are expected, and no values are invented.

The repository manifest currently contains official `www.lukoil.com` 2021 IFRS financial-results press-release URLs for `2021Q1`, `2021Q2`, `2021Q3`, and `2021Q4`. These are trusted because they are on the allowlisted LUKOIL corporate domain. They are not treated as full manually verified IFRS statement packages:

- `2021Q1`: official press-release export URL, 3M 2021 IFRS financial results.
- `2021Q2`: official press-release HTML page, 2Q/6M 2021 IFRS financial results; direct export/file URL is not verified.
- `2021Q3`: official press-release export URL, 3Q/9M 2021 IFRS financial results.
- `2021Q4`: official press-release export URL, FY/12M 2021 IFRS financial results.

To run real LKOH analysis:

```powershell
python -m app.db.init_db
# optionally fill data/manifests/lkoh_real_sources.yml with trusted LKOH IR URLs
uvicorn app.main:app --reload
```

Then submit `POST /analysis-jobs` with `data_mode = "real"`. If the manifest is empty and `LKOH_IR_REPORTS_URL` is unset/unavailable, the job fails clearly instead of using fixtures.

## Source Documents And Parse Audit

Inspect downloaded/parsed reports:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/reports/documents?company=LKOH&period_from=2021Q1&period_to=2021Q4&data_mode=real"
Invoke-RestMethod "http://127.0.0.1:8000/reports/documents/1/parse-audit"
```

Parse audit files are written under `data/parsed/{ticker}/{period}/{document_id}_parse_audit.json`.

## Comparing Fixture And Real Pipeline

Developer utility:

```powershell
python -m app.tools.compare_fixture_real LKOH 2021Q1 2021Q4
```

It runs fixture and real pipelines and prints metric differences. If real documents cannot be discovered or downloaded, it exits with a controlled message rather than a stacktrace.

## LKOH Real Extraction Validation

Stage 1.6 adds a developer/analyst validation report for LKOH real extraction. It checks whether the system can find/download/parse real LKOH IFRS documents, create only traceable real `StatementFact` rows, calculate only supported metrics, and clearly report missing, low-confidence, and conflicting data.

Run:

```powershell
python -m app.tools.validate_lkoh_real_extraction 2021Q1 2021Q4
```

The JSON report is saved to:

```text
data/validation/LKOH/2021Q1_2021Q4_real_validation_report.json
```

Status meaning:

- `PASS`: real documents and traceable facts were extracted, no fixture data was used, and no critical validation problems were found.
- `PARTIAL`: real documents/facts exist, but extraction is incomplete, low-confidence, or metrics are only partially available. This can be acceptable for parser development, but requires analyst review.
- `FAIL`: no real documents were found/downloaded, parser extracted zero facts, traceability is missing, or fixture fallback happened in `data_mode=real`.

Real parser output is best-effort. Do not trust any number without `source_url`, `document_id`, and page/table/line traceability. Fixture data is synthetic demo data and is not real financial reporting data.

For live validation, fill `data/manifests/lkoh_real_sources.yml` with verified LKOH IR URLs or configure `LKOH_IR_REPORTS_URL`. If neither is available, validation will end with a controlled `FAIL` report rather than fake success.

Known validation status: `PARTIAL`, not production-ready. Replay-cache validation over cached official LUKOIL 2021 IFRS source documents extracts 52 canonical facts from `financial_statements`, with 52 high-confidence facts, 0 press-release canonical facts, and 0 conflicts. The LKOH parser has a manually verified 20-fact review-pack sample with `review_pack_status = PASS` and `review_pack_accuracy = 1.0`. The full golden dataset is still `IN_PROGRESS` because seed checks remain pending; do not treat the full extraction set as completely manually verified.

Manual QA artifacts:

```powershell
python -m app.tools.generate_lkoh_manual_review_pack 2021Q1 2021Q4 --limit 20
python -m app.tools.sync_lkoh_golden_from_review_pack 2021Q1 2021Q4
python -m app.tools.verify_lkoh_golden_facts 2021Q1 2021Q4
```

The golden verification report separates `review_pack_sample_status` from `full_golden_dataset_status` so a verified review-pack sample is not confused with full dataset completion.

## LKOH Metric Methodology Status

Stage 1.13 audits LKOH 2021Q1-2021Q4 real metrics after parser validation. Current replay-cache audit status:

- `metrics_valid = 24`
- `metrics_questionable = 5`
- `metrics_invalid = 0`
- `metrics_missing = 27`

Usable metrics include `operating_margin`, `net_margin`, `current_ratio`, `fcf`, and `fcf_margin` where inputs share compatible YTD/annual or balance-sheet snapshot semantics. `debt_to_equity` is usable with warning because `total_debt` is derived from borrowings components with explicit source references.

Questionable metrics are not production-ready: `revenue_growth` uses cumulative YTD/annual disclosures rather than standalone quarters, and `roe`/`roa` require a policy for TTM income and average balance sheet snapshots. EBITDA-based metrics (`ebitda_margin`, `net_debt_to_ebitda`, `ev_to_ebitda`) remain missing until EBITDA is explicitly disclosed and parsed. Valuation metrics (`pe_ratio`, `ev_to_ebitda`, `dividend_yield`) remain missing without market capitalization, enterprise value, price, and dividend inputs.

The 20-fact LKOH review-pack sample is manually verified, but the metrics layer remains partial and should not be treated as production-ready analytics.

## LKOH Market Data Audit

Stage 1.14 validates LKOH/TQBR daily candles through MOEX ISS separately from offline fixture candles. The audit enforces period alignment: fixture candles dated outside the requested period are marked `fixture_candles_unaligned` and their indicators are not applicable to the requested period.

Run live MOEX ISS audit:

```powershell
python -m app.tools.audit_lkoh_market_data 2021Q1 2021Q4 --mode live
```

Run cached replay without network:

```powershell
python -m app.tools.audit_lkoh_market_data 2021Q1 2021Q4 --mode replay-cache
```

Current live LKOH 2021 market audit status:

- `market_data_source = MOEX ISS`
- `market_period_aligned = true`
- `candles_count = 255`
- `first_trade_date = 2021-01-04`
- `last_trade_date = 2021-12-30`
- `technical_indicators_valid_count = 10`
- `technical_indicators_missing_count = 0`
- `liquidity_metrics_valid_count = 5`
- `liquidity_metrics_missing_count = 1`

The live run saves replay cache to:

```text
data/market_cache/LKOH/TQBR/2021-01-01_2021-12-31_candles.csv
```

`bid_ask_spread` remains missing because MOEX daily candles do not contain bid/ask or order-book data. Valuation inputs (`market_cap`, `enterprise_value`, `dividends`) remain missing, so P/E, EV/EBITDA, and dividend yield must not be calculated from this audit alone.

## TATN Real Peer Pilot

Stage 1.15 adds a conservative second-issuer pilot for `TATN` / PJSC Tatneft. This is not a production peer universe; it is a portability check for the LKOH approach.

Official TATNEFT source package status for 2021Q1-2021Q4 is currently `PARTIAL`, not `READY`:

- `2021Q1`: official TATNEFT IFRS consolidated interim condensed financial statements for 1Q 2021.
- `2021Q2`: official TATNEFT IFRS consolidated interim condensed financial statements for 3M/6M ended 30 June 2021.
- `2021Q3`: official TATNEFT IFRS consolidated interim condensed financial statements for 3M/9M ended 30 September 2021.
- `2021Q4`: trusted FY/12M 2021 financial statement or annual report URL was not verified and is intentionally left missing.

The source package is defined in:

```text
data/manifests/tatn_real_sources.yml
```

Generic tools:

```powershell
python -m app.tools.verify_source_package TATN 2021Q1 2021Q4
python -m app.tools.validate_real_extraction TATN 2021Q1 2021Q4 --live
python -m app.tools.validate_real_extraction TATN 2021Q1 2021Q4 --replay-cache
python -m app.tools.audit_market_data TATN 2021Q1 2021Q4 --mode live
python -m app.tools.audit_market_data TATN 2021Q1 2021Q4 --mode replay-cache
python -m app.tools.generate_manual_review_pack TATN 2021Q1 2021Q4 --limit 20
python -m app.tools.compare_real_peers LKOH TATN 2021Q1 2021Q4
```

Current TATN validation status is `FAIL` in this environment because live downloads and MOEX live market fetches were blocked by sandbox networking, and no TATN raw/cache files are available for replay. Manual review pack generation therefore selects 0 facts. Do not claim TATN peer validation or manual verification until official files are downloaded, parsed, and reviewed.

## LKOH Source Package Verification

Stage 1.8 adds source package verification before treating real extraction as meaningful. A `press_release` is not the same thing as full `financial_statements`: press releases can support analysis, but they are not sufficient to validate full statement extraction. For production-grade extraction, each period should have a verified `financial_statements` PDF or `financial_supplement` XLSX source.

Run offline manifest verification:

```powershell
python -m app.tools.verify_lkoh_source_package 2021Q1 2021Q4
```

Run optional live metadata checks with timeout:

```powershell
python -m app.tools.verify_lkoh_source_package 2021Q1 2021Q4 --live
```

The JSON report is saved to:

```text
data/validation/LKOH/2021Q1_2021Q4_source_package_report.json
```

Status meaning:

- `READY`: every requested period has `financial_statements` or `financial_supplement`.
- `PARTIAL`: at least one proper financial source exists, but some periods still rely only on press releases or have no proper source.
- `NOT_READY`: all available sources are press releases or sources are missing.

Current manifest status: `READY` for source package coverage. It contains official LUKOIL `financial_statements` PDFs for 3M, 6M, 9M, and 12M 2021 plus press-release sources. This means the source package is adequate for parser development, not that real extraction is analytically validated.

## Live Tests

Offline tests are the default and do not require internet. Optional live tests should be marked with `@pytest.mark.live`.

```powershell
pytest -m live
```

## Data Quality Flags

Supported flags:

- `exact`
- `derived`
- `missing`
- `conflicting_sources`
- `low_confidence_parse`
- `fixture`
- `unavailable`
- `manual_review_required`

Missing inputs produce warnings and `quality_flag = "missing"` rather than silent nulls.

## MVP Limitations

- Financial reports are fixture/demo-first.
- Real LKOH parsing is best-effort and issuer-specific; low-confidence facts are marked `low_confidence_parse`.
- PDF parsing is best-effort and does not invent facts.
- P/E, EV/EBITDA, and dividend yield return `missing` unless market valuation inputs exist.
- Jobs run in-process via FastAPI `BackgroundTasks`.
- MOEX ISS connector exists, but default analysis path uses fixture candles for reproducible tests.
- CORS is disabled by default. Set `CORS_ALLOWED_ORIGINS` explicitly for a local frontend.

## Environment Variables

- `DATABASE_URL`: SQLAlchemy database URL. Defaults to `sqlite:///./local.db`.
- `APP_ENV`: environment label. Defaults to `local`.
- `MOEX_BASE_URL`: MOEX ISS base URL.
- `CORS_ALLOWED_ORIGINS`: comma-separated allowed origins. Empty by default.
- `DATA_MODE_DEFAULT`: default data mode for future callers. Current API default is `fixture`.
- `REPORT_DOWNLOAD_DIR`: raw report storage directory. Default `data/raw`.
- `REPORT_PARSED_DIR`: parse-audit storage directory. Default `data/parsed`.
- `MAX_REPORT_DOWNLOAD_MB`: max report download size. Default `50`.
- `REPORT_TLS_CA_BUNDLE`: optional PEM CA bundle file for report downloads and source verification.
- `REPORT_SOURCE_ALLOWED_DOMAINS`: comma-separated report-source allowlist.
- `ENABLE_LIVE_SOURCE_TESTS`: live test flag. Default `false`.
- `LKOH_IR_REPORTS_URL`: trusted LKOH IR reports page URL, optional.

## Custom CA Bundle For Report Downloads

Report downloads and live source verification use normal TLS verification by default. The service does not use
`verify=False` in the normal downloader or verifier path.

If a browser or Windows system trust store can open an issuer PDF, but Python/certifi cannot validate the chain, export
the required CA chain to a local PEM file and set:

```powershell
$env:REPORT_TLS_CA_BUNDLE="C:\path\to\custom-ca-bundle.pem"
python -m app.tools.verify_source_package GAZP 2021Q1 2021Q4 --live --tls-diagnostics
```

`REPORT_TLS_CA_BUNDLE` must point to an existing non-empty PEM file. Missing or empty files are reported as controlled
TLS configuration errors. Do not commit local/private CA bundles to the repository, and do not place them under
`data/validation`, `data/raw`, or other user-facing artifacts.

## Financial Report Discovery And Statement Table Extraction

The real-data report pipeline is split into explicit stages:

- Company Source Discovery finds official source pages and identity metadata. It does not verify financial statements.
- Financial Report Discovery finds candidate report URLs for a company, period range, and reporting standard. `READY`
  means candidate URLs were found only; it does not mean the company is analysis-ready.
- Report Download Validation downloads selected candidates with the existing downloader, runs `DocumentValidator`, and
  keeps rejected files in the audit trail as `ReportDocument(status="rejected")`.
- Statement Table Extraction reads only cached/validated documents in replay-cache mode and writes balance sheet,
  income statement, cash flow, and unknown table artifacts as DataFrame-compatible JSON.

Annual reports, issuer reports, presentations, and press releases are not treated as financial statements unless
validation proves a full embedded financial statement package. IFRS and RAS flows are kept separate. Table extraction
does not imply fact extraction or metric coverage.

```powershell
python -m app.tools.discover_financial_reports LKOH 2021Q1 2021Q4 --reporting-standard IFRS --live
python -m app.tools.extract_statement_tables LKOH 2021Q1 2021Q4 --reporting-standard IFRS --replay-cache
```

### Statement Table Pipeline Smoke

Use the smoke command to verify the narrow report-table pipeline end to end:

```powershell
python -m app.tools.smoke_statement_table_pipeline LKOH 2021Q1 2021Q4 --reporting-standard IFRS --replay-cache
```

The smoke path resolves the company, runs candidate discovery, uses cached report documents, extracts statement tables,
and loads balance sheet / income statement tables as pandas-compatible DataFrames. Discovery `READY` still means only
that candidate URLs were found. Facts and metrics are downstream stages. The minimum table-pipeline acceptance is
`balance_sheet` plus `income_statement` found and loadable as DataFrames; cash-flow tables are reported when supported.
Text fallback tables are marked as `raw_text_table` and are not valid canonical-fact inputs without a separate semantic
parser quality gate.

## Universal Coverage Scanner

The universal coverage scanner measures what the real-data pipeline currently supports for each company. It is a
coverage map, not an analysis result.

By default the scanner is read-only over existing registry rows, reports, cached document metadata, table artifacts,
fact-parse reports, and readiness reports. It does not download files, mutate manifests, persist facts, run metrics,
perform valuation, run peer comparison, or call an LLM.

```powershell
python -m app.tools.scan_universal_coverage --tickers LKOH,TATN,GAZP --period-from 2021Q1 --period-to 2021Q4 --reporting-standard IFRS --replay-cache
```

The report separates current coverage from scanner actions:

- `scan_mode="existing_artifacts_only"` means no artifacts were created.
- `scan_mode="replay_cache_with_table_extraction"` is used only with `--run-missing-table-extraction`.
- `coverage_level` summarizes each company as `FULL_STATEMENT_READY`, `PARTIAL_STATEMENT_READY`, `MARKET_ONLY`,
  `SOURCE_BLOCKED`, `PARSER_BLOCKED`, or `UNSUPPORTED`.

Use blocker codes such as `missing_fy_report`, `image_only_primary_statement_pages`, and
`missing_expected_statement_facts` to decide the next engineering work. Valuation and EBITDA policy blockers are
reported separately from parser blockers.

## Demo Pipeline

The demo pipeline is a report-only orchestration layer for presentations and coursework defense. It reads existing
coverage, extraction, fact-parse, comparison, manual-ingestion, and provider feasibility reports, then writes a compact
machine-readable JSON report and a human-readable Markdown report. It is not a new analysis engine.

```powershell
python -m app.tools.run_demo_pipeline LKOH,TATN,GAZP --period-from 2021Q1 --period-to 2021Q4 --reporting-standard IFRS --replay-cache
```

Outputs:

- `data/validation/demo/demo_pipeline_report.json`
- `data/validation/demo/demo_pipeline_report.md`

Interpret company statuses as scoped demo coverage:

- `READY` means the existing scoped statement pipeline is ready for that company.
- `PARTIAL` means usable coverage exists but the scoped statement pipeline is not fully ready.
- `BLOCKED` means source acquisition, validation, parsing, or unsupported coverage blocks the requested output.

The demo report does not download documents, mutate manifests, persist facts, run metrics, perform valuation, run peer
comparison, or call an LLM. It explicitly preserves limitations such as no universal support, no OCR for image-only
primary statement pages, no production provider API access, and lower trust for manual uploads.

## Provider Feasibility Scanner

The provider feasibility scanner is a research/report-only stage for evaluating external official and commercial data
providers. It does not integrate providers into the production pipeline, download reports, mutate manifests, persist
facts, run metrics, perform valuation, run peer comparison, or call an LLM.

```powershell
python -m app.tools.scan_provider_feasibility --market MOEX --offline-only
```

The default offline scan uses a curated catalog with official source URLs and conservative capability labels. For
example, MOEX ISS is classified as company identity and market data, not a financial-report source. FNS GIR BO is
classified as a RAS accounting-report source, with free per-company download separated from paid/API subscription
access. E-Disclosure is marked as having an authenticated API gateway, but remains not production-ready until contract
terms, limits, archive coverage, and data-use rights are verified.

The report is written to `data/validation/providers/provider_feasibility_scan.json` and includes provider roles,
best candidates by data type, access and license risks, and recommended next experiments.

## No-API Fallback / Manual Report Ingestion

Manual report ingestion is a controlled fallback for cases where API access is unavailable or source discovery has not
found a required report. It accepts a local `.pdf`, `.xlsx`, `.html`, `.htm`, or `.zip` file, copies it to
`data/raw/manual_uploads`, validates it with the normal `DocumentValidator`, and can extract statement tables into
DataFrame-compatible artifacts.

```powershell
python -m app.tools.ingest_manual_report LKOH 2021Q4 --reporting-standard IFRS --file "C:\path\to\report.pdf"
```

Manual uploads are not official source discovery. Even after validation, reports preserve
`source_trust_bucket="manual_upload_validated"`, `official_source_verified=false`, and
`source_package_ready_contribution=false`. The CLI does not expose fact persistence in V1; DataFrame fact parsing is
report-only and must be requested explicitly. Metrics, valuation, peer comparison, manifests, and LLM payloads are not
updated by this fallback stage.

### E-Disclosure Proof Of Access

The E-Disclosure proof stage checks what is known about API gateway access and whether the local registry has enough
issuer identifiers for a future authenticated proof of concept. It does not integrate E-Disclosure into production,
does not use secrets, does not make authenticated or paid calls, does not download financial PDFs, does not create
`ReportDocument` rows, and does not run parsers, metrics, valuation, peer comparison, or LLM paths.

```powershell
python -m app.tools.prove_edisclosure_access --offline-only
```

Offline mode records catalog-known API documentation as `api_documentation_status="found_in_catalog"` and keeps
`api_documentation_live_verified=false`. Optional `--live-public-docs-check` only checks public documentation page
reachability; it does not prove usable API access. Production integration still requires credentials, contract terms,
archive coverage checks, document-link access verification, redistribution/license review, and data-quality tests.

## Before Production

- Verify licenses and usage terms for MOEX, news, issuer IR, and disclosure data.
- Replace in-process jobs with Celery, RQ, Temporal, or another production queue.
- Add robust issuer IR and e-disclosure adapters.
- Add source-specific parsing maps and reconciliation for conflicting sources.
- Add parser quality monitoring and observability.
- Add authentication, rate limits, and structured audit logs.
