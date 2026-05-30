import json
from pathlib import Path

from app.core.config import get_settings
from app.db.models import ReportDocument, StatementFact


def audit_path(document: ReportDocument) -> Path:
    settings = get_settings()
    root = settings.root_dir / settings.report_parsed_dir
    return root / document.company.ticker.upper() / document.report_period / f"{document.id}_parse_audit.json"


def write_parse_audit(
    document: ReportDocument,
    parser_name: str,
    tables_found: int,
    facts: list[StatementFact],
    warnings: list[str],
) -> Path:
    path = audit_path(document)
    parsed_root = (get_settings().root_dir / get_settings().report_parsed_dir).resolve()
    path = path.resolve()
    if not path.is_relative_to(parsed_root):
        raise ValueError("Parse audit path escapes parsed directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "document_id": document.id,
        "source_url": document.source_url,
        "parser": parser_name,
        "period": document.report_period,
        "tables_found": tables_found,
        "facts_extracted": len(facts),
        "facts": [
            {
                "metric_code": fact.metric_code,
                "ifrs_concept_code": (fact.source_location or {}).get("ifrs_concept_code", fact.metric_code),
                "value": fact.value,
                "currency": fact.currency,
                "unit_multiplier": fact.unit_multiplier,
                "source_location": fact.source_location,
                "statement_context": (fact.source_location or {}).get("table_title"),
                "period_column_detected": (fact.source_location or {}).get("period_type") is not None,
                "period_coverage": (fact.source_location or {}).get("ytd_months")
                or (fact.source_location or {}).get("period_type"),
                "candidate_score_breakdown": (fact.source_location or {}).get("candidate_score_breakdown"),
                "applied_issuer_override": (fact.source_location or {}).get("applied_issuer_override"),
                "confidence_score": fact.confidence_score,
                "quality_flag": fact.quality_flag,
            }
            for fact in facts
        ],
        "warnings": warnings,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
