import re
from dataclasses import dataclass

from app.services.parsing.text_normalization import normalize_financial_text, normalize_matching_text


@dataclass(frozen=True)
class PeriodDetection:
    period_coverage: str | None
    period_type: str
    ytd_months: int | None
    column_role: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportPeriodResolution:
    effective_report_period: str | None
    comparative_period: str | None
    period_confidence: float
    period_source: str
    period_warnings: tuple[str, ...] = ()
    period_type: str = "unknown"
    period_coverage: str | None = None
    ytd_months: int | None = None


MONTH_TO_QUARTER = {
    "march": 1,
    "марта": 1,
    "june": 2,
    "июня": 2,
    "september": 3,
    "сентября": 3,
    "december": 4,
    "декабря": 4,
}


def resolve_report_period(
    title_text: str = "",
    headers: list[str] | None = None,
    report_period: str | None = None,
    filename_hint: str | None = None,
) -> ReportPeriodResolution:
    header_text = " ".join(headers or [])
    candidates = [
        _resolve_from_text(header_text, source="table_headers"),
        _resolve_from_text(title_text, source="document_text"),
        _resolve_from_filename(filename_hint),
    ]
    candidates = [candidate for candidate in candidates if candidate]
    if candidates:
        best = max(candidates, key=_resolution_rank)
        return best
    fallback_period = _normalize_period(report_period)
    if fallback_period:
        return ReportPeriodResolution(
            effective_report_period=fallback_period,
            comparative_period=_comparative_period(fallback_period),
            period_confidence=0.1,
            period_source="upload_default",
            period_warnings=("period_defaulted_without_document_evidence",),
            period_type=_period_type_for_quarter(int(fallback_period[-1])),
            period_coverage=_coverage_for_quarter(int(fallback_period[-1])),
            ytd_months=int(fallback_period[-1]) * 3,
        )
    return ReportPeriodResolution(
        effective_report_period=None,
        comparative_period=None,
        period_confidence=0.0,
        period_source="unknown",
        period_warnings=("period_not_detected",),
    )


def detect_period(title_text: str = "", headers: list[str] | None = None, report_period: str | None = None) -> PeriodDetection:
    resolution = resolve_report_period(
        title_text=title_text,
        headers=headers,
        report_period=report_period,
    )
    return PeriodDetection(
        period_coverage=resolution.period_coverage,
        period_type=resolution.period_type,
        ytd_months=resolution.ytd_months,
        column_role="current_period",
        warnings=resolution.period_warnings,
    )


def _resolution_rank(resolution: ReportPeriodResolution) -> tuple[float, int]:
    source_rank = {
        "table_headers": 4,
        "document_text": 3,
        "filename": 2,
        "upload_default": 1,
        "unknown": 0,
    }
    return (resolution.period_confidence, source_rank.get(resolution.period_source, 0))


def _resolve_from_text(text: str | None, *, source: str) -> ReportPeriodResolution | None:
    normalized_financial = normalize_financial_text(text)
    normalized = normalized_financial.casefold().replace("ё", "е")
    if not normalized.strip():
        return None
    duration = _detect_duration(normalized)
    dated = _detect_explicit_date(normalized)
    if dated:
        year, quarter = dated
        period = f"{year}Q{quarter}"
        if duration:
            return ReportPeriodResolution(
                effective_report_period=period,
                comparative_period=_comparative_period(period),
                period_confidence=0.95 if source == "table_headers" else 0.9,
                period_source=source,
                period_type=_period_type_for_duration(duration),
                period_coverage=_coverage_for_duration(duration, quarter),
                ytd_months=duration,
            )
        return ReportPeriodResolution(
            effective_report_period=period,
            comparative_period=_comparative_period(period),
            period_confidence=0.9 if source == "table_headers" else 0.85,
            period_source=source,
            period_type="balance_sheet_snapshot",
            period_coverage=_coverage_for_quarter(quarter),
            ytd_months=quarter * 3,
        )
    years = sorted({int(value) for value in re.findall(r"\b(20\d{2})\b", normalized)})
    if years:
        quarter = _quarter_from_duration(duration) or 4
        period = f"{years[-1]}Q{quarter}"
        return ReportPeriodResolution(
            effective_report_period=period,
            comparative_period=f"{years[-2]}Q{quarter}" if len(years) >= 2 else _comparative_period(period),
            period_confidence=0.78 if source == "table_headers" else 0.72,
            period_source=source,
            period_warnings=("period_inferred_from_year_headers_only",),
            period_type=_period_type_for_duration(duration) if duration else _period_type_for_quarter(quarter),
            period_coverage=_coverage_for_duration(duration, quarter) if duration else _coverage_for_quarter(quarter),
            ytd_months=duration or quarter * 3,
        )
    return None


def _resolve_from_filename(filename_hint: str | None) -> ReportPeriodResolution | None:
    normalized = normalize_matching_text(filename_hint)
    if not normalized:
        return None
    explicit = _detect_explicit_date(normalized)
    duration = _detect_duration(normalized)
    if explicit:
        year, quarter = explicit
        period = f"{year}Q{quarter}"
        return ReportPeriodResolution(
            effective_report_period=period,
            comparative_period=_comparative_period(period),
            period_confidence=0.65,
            period_source="filename",
            period_warnings=("period_inferred_from_filename",),
            period_type=_period_type_for_duration(duration) if duration else _period_type_for_quarter(quarter),
            period_coverage=_coverage_for_duration(duration, quarter) if duration else _coverage_for_quarter(quarter),
            ytd_months=duration or quarter * 3,
        )
    year_match = re.search(r"\b(20\d{2})\b", normalized)
    if not year_match:
        return None
    quarter_match = re.search(r"\bq([1-4])\b", normalized)
    quarter = int(quarter_match.group(1)) if quarter_match else (_quarter_from_duration(duration) or 4)
    period = f"{year_match.group(1)}Q{quarter}"
    return ReportPeriodResolution(
        effective_report_period=period,
        comparative_period=_comparative_period(period),
        period_confidence=0.55,
        period_source="filename",
        period_warnings=("period_inferred_from_filename",),
        period_type=_period_type_for_duration(duration) if duration else _period_type_for_quarter(quarter),
        period_coverage=_coverage_for_duration(duration, quarter) if duration else _coverage_for_quarter(quarter),
        ytd_months=duration or quarter * 3,
    )


def _detect_explicit_date(normalized_text: str) -> tuple[int, int] | None:
    match = re.search(
        r"\b(31|30)\s+(march|марта|june|июня|september|сентября|december|декабря)\s+(20\d{2})\b",
        normalized_text,
    )
    if not match:
        return None
    month = match.group(2)
    year = int(match.group(3))
    quarter = MONTH_TO_QUARTER.get(month)
    if not quarter:
        return None
    return year, quarter


def _detect_duration(normalized_text: str) -> int | None:
    duration_patterns = {
        12: [r"\byear ended\b", r"\b12 months\b", r"\bза год\b", r"\b12 месяцев\b"],
        9: [r"\bnine months\b", r"\b9 months\b", r"\bза 9 месяцев\b", r"\b9 месяцев\b"],
        6: [r"\bsix months\b", r"\b6 months\b", r"\bза 6 месяцев\b", r"\b6 месяцев\b"],
        3: [r"\bthree months\b", r"\b3 months\b", r"\bза 3 месяца\b", r"\b3 месяца\b"],
    }
    for months, patterns in duration_patterns.items():
        if any(re.search(pattern, normalized_text) for pattern in patterns):
            return months
    return None


def _quarter_from_duration(duration: int | None) -> int | None:
    if not duration:
        return None
    return max(1, min(4, duration // 3))


def _normalize_period(period: str | None) -> str | None:
    text = str(period or "").strip().upper()
    if re.match(r"^\d{4}Q[1-4]$", text):
        return text
    if re.match(r"^\d{4}$", text):
        return f"{text}Q4"
    return None


def _comparative_period(period: str | None) -> str | None:
    normalized = _normalize_period(period)
    if not normalized:
        return None
    return f"{int(normalized[:4]) - 1}{normalized[4:]}"


def _period_type_for_duration(duration: int | None) -> str:
    return "annual" if duration == 12 else "ytd"


def _period_type_for_quarter(quarter: int) -> str:
    return "annual" if quarter == 4 else "ytd"


def _coverage_for_duration(duration: int | None, quarter: int) -> str | None:
    if duration == 12:
        return "FY"
    if duration:
        return f"{duration}M"
    return _coverage_for_quarter(quarter)


def _coverage_for_quarter(quarter: int) -> str | None:
    if quarter == 4:
        return "as_at_date"
    return f"{quarter * 3}M"
