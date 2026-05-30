from app.db.models import StatementFact


def derive_standalone_quarter(current_ytd: StatementFact, previous_ytd: StatementFact) -> StatementFact | None:
    if current_ytd.metric_code != previous_ytd.metric_code:
        return None
    if current_ytd.period_type != "ytd" or previous_ytd.period_type != "ytd":
        return None
    current_months = (current_ytd.source_location or {}).get("ytd_months")
    previous_months = (previous_ytd.source_location or {}).get("ytd_months")
    if not isinstance(current_months, int) or not isinstance(previous_months, int):
        return None
    if current_months - previous_months != 3:
        return None
    if current_ytd.value is None or previous_ytd.value is None:
        return None
    return StatementFact(
        company_id=current_ytd.company_id,
        report_document_id=current_ytd.report_document_id,
        period=current_ytd.period,
        reporting_standard=current_ytd.reporting_standard,
        statement_type=current_ytd.statement_type,
        metric_code=current_ytd.metric_code,
        metric_name_original=current_ytd.metric_name_original,
        value=current_ytd.value - previous_ytd.value,
        currency=current_ytd.currency,
        unit_multiplier=current_ytd.unit_multiplier,
        period_type="quarter",
        source_location={
            "derived_from": [
                current_ytd.source_location,
                previous_ytd.source_location,
            ],
            "derivation": f"{current_ytd.period} YTD minus {previous_ytd.period} YTD",
        },
        quality_flag="derived",
        confidence_score=min(current_ytd.confidence_score or 0.8, previous_ytd.confidence_score or 0.8),
    )
