# Demo Pipeline Report

## Executive Summary
Measures and demonstrates the real financial-report pipeline from company identity to scoped readiness.

**What worked**
- 0 company is READY in the scoped statement pipeline.
- Manual upload fallback can validate and extract statement tables while remaining lower-trust.
- Provider feasibility is cataloged without claiming production access.

**Blocked or unavailable**
- 1 companies are blocked by source or parser limitations.
- Valuation inputs are unavailable without a separate module.

**What the system does not claim**
- No universal issuer support is claimed.
- No metric improvement is claimed by this report.
- No provider integration is production-ready.

## Architecture
Company → source discovery → report discovery → validation → table extraction → DataFrame artifacts → fact candidates → scoped readiness

This report reads existing artifacts only. It does not download documents, persist facts, run valuation, run peer analysis, or call LLMs.

## Results by Company

### LKOH
- Status: BLOCKED / UNSUPPORTED
- Main blocker: coverage_item_missing
- Statement readiness: UNKNOWN
- Valuation readiness: UNAVAILABLE
- Documents: 0; tables: 0; balance sheet: 0; income statement: 0; cash flow: 0
- Fact candidates: 0; matched existing facts: 0; conflicts: 0
- Financial ratios calculated: 0; missing: 36; unsupported: 4; blocked: 0
- Recommended action: Package current official pipeline, comparison evidence, and scoped readiness for demo/API presentation.

## Manual Upload Fallback
Manual upload works as lower-trust fallback and does not make source package READY.


## Provider Strategy
- Production-ready providers: 0
- IFRS/report metadata candidate: E-Disclosure / Interfax
- RAS financials candidate: FNS GIR BO
- Market/identity candidate: MOEX ISS
- Valuation candidate: Cbonds / RU Data

## Blockers and Next Engineering Actions

Recommended next actions:
3. Enrich issuer identifiers for provider POC.
4. Build valuation input module separately from parser work.

## Limitations
- This demo report is a reporting layer, not a new analysis engine.
- The system does not claim universal support across all MOEX issuers.
- Valuation metrics remain unavailable without a separate valuation input module.
- Manual upload is lower-trust fallback and does not make source package READY.
- No OCR/table-image extraction is implemented for image-only primary statements.
- No production external provider API access is verified yet.
