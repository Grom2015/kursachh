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
    period_resolution_source_detail: str | None = None
    period_conflict_sources: tuple[str, ...] = ()
    period_warnings: tuple[str, ...] = ()
    period_type: str = "unknown"
    period_coverage: str | None = None
    ytd_months: int | None = None


MONTH_TO_QUARTER = {
    "march": 1,
    "РјР°СЂС‚Р°": 1,
    "june": 2,
    "РёСЋРЅСЏ": 2,
    "september": 3,
    "СЃРµРЅС‚СЏР±СЂСЏ": 3,
    "december": 4,
    "РґРµРєР°Р±СЂСЏ": 4,
}


def resolve_report_period(
    title_text: str = "",
    headers: list[str] | None = None,
    report_period: str | None = None,
    filename_hint: str | None = None,
) -> ReportPeriodResolution:
    header_text = " ".join(headers or [])
    normalized_report_period = _normalize_period(report_period)
    candidates = [
        _resolve_from_text(header_text, source="table_headers", preferred_report_period=normalized_report_period),
        _resolve_from_text(title_text, source="document_text", preferred_report_period=normalized_report_period),
        _resolve_from_filename(filename_hint),
    ]
    candidates = [candidate for candidate in candidates if candidate]
    if candidates:
        return _select_best_resolution(candidates, normalized_report_period)
    if normalized_report_period:
        return ReportPeriodResolution(
            effective_report_period=normalized_report_period,
            comparative_period=_comparative_period(normalized_report_period),
            period_confidence=0.1,
            period_source="upload_default",
            period_resolution_source_detail="upload_default_report_period",
            period_warnings=("period_defaulted_without_document_evidence",),
            period_type=_period_type_for_quarter(int(normalized_report_period[-1])),
            period_coverage=_coverage_for_quarter(int(normalized_report_period[-1])),
            ytd_months=int(normalized_report_period[-1]) * 3,
        )
    return ReportPeriodResolution(
        effective_report_period=None,
        comparative_period=None,
        period_confidence=0.0,
        period_source="unknown",
        period_resolution_source_detail="period_not_detected",
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


def _select_best_resolution(
    candidates: list[ReportPeriodResolution],
    preferred_report_period: str | None,
) -> ReportPeriodResolution:
    by_source = {candidate.period_source: candidate for candidate in candidates}
    header_candidate = by_source.get("table_headers")
    document_candidate = by_source.get("document_text")
    preferred_year = _period_year(preferred_report_period)
    if (
        header_candidate
        and document_candidate
        and header_candidate.effective_report_period != document_candidate.effective_report_period
    ):
        if preferred_year and _period_year(header_candidate.effective_report_period) == preferred_year:
            warnings = tuple(dict.fromkeys((*header_candidate.period_warnings, "period_conflict_warning")))
            return ReportPeriodResolution(
                **{
                    **header_candidate.__dict__,
                    "period_conflict_sources": ("table_headers", "document_text"),
                    "period_warnings": warnings,
                    "period_confidence": max(header_candidate.period_confidence, 0.84),
                }
            )
    return max(candidates, key=_resolution_rank)


def _resolve_from_text(
    text: str | None,
    *,
    source: str,
    preferred_report_period: str | None = None,
) -> ReportPeriodResolution | None:
    raw_text = str(text or "")
    normalized_financial = normalize_financial_text(text)
    normalized = normalized_financial.casefold().replace("С‘", "Рµ")
    normalized_matching = normalize_matching_text(text)
    if not normalized.strip():
        return None
    duration = _detect_duration(normalized)
    explicit_report_year = _detect_explicit_report_year(normalized)
    if explicit_report_year:
        period = f"{explicit_report_year}Q4"
        return ReportPeriodResolution(
            effective_report_period=period,
            comparative_period=f"{explicit_report_year - 1}Q4",
            period_confidence=0.94 if source == "table_headers" else 0.9,
            period_source=source,
            period_resolution_source_detail="explicit_report_year_phrase",
            period_type="annual",
            period_coverage="FY",
            ytd_months=12,
        )
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
                period_resolution_source_detail="explicit_date_with_duration",
                period_type=_period_type_for_duration(duration),
                period_coverage=_coverage_for_duration(duration, quarter),
                ytd_months=duration,
            )
        return ReportPeriodResolution(
            effective_report_period=period,
            comparative_period=_comparative_period(period),
            period_confidence=0.9 if source == "table_headers" else 0.85,
            period_source=source,
            period_resolution_source_detail="explicit_snapshot_date",
            period_type="balance_sheet_snapshot",
            period_coverage=_coverage_for_quarter(quarter),
            ytd_months=quarter * 3,
        )
    numeric_date_hint = _detect_numeric_only_date_hint(normalized_matching)
    if numeric_date_hint:
        year, quarter, inferred_duration = numeric_date_hint
        period = f"{year}Q{quarter}"
        return ReportPeriodResolution(
            effective_report_period=period,
            comparative_period=_comparative_period(period),
            period_confidence=0.88 if source == "table_headers" else 0.85,
            period_source=source,
            period_resolution_source_detail="numeric_only_date_hint",
            period_type=_period_type_for_duration(inferred_duration)
            if inferred_duration
            else _period_type_for_quarter(quarter),
            period_coverage=_coverage_for_duration(inferred_duration, quarter)
            if inferred_duration
            else _coverage_for_quarter(quarter),
            ytd_months=inferred_duration or quarter * 3,
        )
    line_year = _detect_single_year_line(raw_text)
    if line_year is not None:
        period = f"{line_year}Q4"
        return ReportPeriodResolution(
            effective_report_period=period,
            comparative_period=f"{line_year - 1}Q4",
            period_confidence=0.9 if source == "table_headers" else 0.86,
            period_source=source,
            period_resolution_source_detail="single_year_statement_line",
            period_type="annual",
            period_coverage="FY",
            ytd_months=12,
        )
    years = sorted({int(value) for value in re.findall(r"\b(20\d{2})\b", normalized)})
    if years:
        quarter = _quarter_from_duration(duration) or 4
        chosen_year, warnings, detail = _choose_current_year(years, preferred_report_period, source=source)
        period = f"{chosen_year}Q{quarter}"
        comparative_year = _comparative_year_from_set(years, chosen_year)
        return ReportPeriodResolution(
            effective_report_period=period,
            comparative_period=f"{comparative_year}Q{quarter}" if comparative_year is not None else _comparative_period(period),
            period_confidence=_period_confidence_from_years(source, preferred_report_period, chosen_year),
            period_source=source,
            period_resolution_source_detail=detail,
            period_warnings=warnings,
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
            period_resolution_source_detail="explicit_date_in_filename",
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
        period_resolution_source_detail="year_token_in_filename",
        period_warnings=("period_inferred_from_filename",),
        period_type=_period_type_for_duration(duration) if duration else _period_type_for_quarter(quarter),
        period_coverage=_coverage_for_duration(duration, quarter) if duration else _coverage_for_quarter(quarter),
        ytd_months=duration or quarter * 3,
    )


def _detect_explicit_date(normalized_text: str) -> tuple[int, int] | None:
    match = re.search(
        r"\b(31|30)\s+(march|РјР°СЂС‚Р°|june|РёСЋРЅСЏ|september|СЃРµРЅС‚СЏР±СЂСЏ|december|РґРµРєР°Р±СЂСЏ)\s+(20\d{2})\b",
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
        12: [r"\byear ended\b", r"\b12 months\b", r"\bР·Р° РіРѕРґ\b", r"\b12 РјРµСЃСЏС†РµРІ\b"],
        9: [r"\bnine months\b", r"\b9 months\b", r"\bР·Р° 9 РјРµСЃСЏС†РµРІ\b", r"\b9 РјРµСЃСЏС†РµРІ\b"],
        6: [r"\bsix months\b", r"\b6 months\b", r"\bР·Р° 6 РјРµСЃСЏС†РµРІ\b", r"\b6 РјРµСЃСЏС†РµРІ\b"],
        3: [r"\bthree months\b", r"\b3 months\b", r"\bР·Р° 3 РјРµСЃСЏС†Р°\b", r"\b3 РјРµСЃСЏС†Р°\b"],
    }
    for months, patterns in duration_patterns.items():
        if any(re.search(pattern, normalized_text) for pattern in patterns):
            return months
    return None


def _detect_explicit_report_year(normalized_text: str) -> int | None:
    patterns = [
        r"\bза\s+(20\d{2})\s+год\b",
        r"\bfor\s+(20\d{2})\b",
        r"\byear\s+(20\d{2})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_text)
        if match:
            return int(match.group(1))
    return None


def _detect_single_year_line(raw_text: str) -> int | None:
    lines = [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    line_years = []
    for line in lines:
        years = re.findall(r"\b(20\d{2})\b", line)
        if len(years) == 1:
            line_years.append(int(years[0]))
    if not line_years:
        return None
    all_years = {int(year) for year in re.findall(r"\b(20\d{2})\b", raw_text)}
    if len(all_years) > 1:
        return min(line_years)
    return line_years[0]


def _detect_numeric_only_date_hint(normalized_matching: str) -> tuple[int, int, int | None] | None:
    years = [int(year) for year in re.findall(r"\b(20\d{2})\b", normalized_matching)]
    if len(set(years)) != 1:
        return None
    year = years[0]
    tokens = [token for token in normalized_matching.split() if token]
    token_set = set(tokens)
    if "9" in token_set and "30" in token_set:
        return year, 3, 9
    if "6" in token_set and "30" in token_set:
        return year, 2, 6
    if "3" in token_set and "31" in token_set:
        return year, 1, 3
    if "31" in token_set:
        return year, 4, 12
    return None


def _choose_current_year(
    years: list[int],
    preferred_report_period: str | None,
    *,
    source: str,
) -> tuple[int, tuple[str, ...], str]:
    preferred_year = _period_year(preferred_report_period)
    if preferred_year and preferred_year in years:
        warnings: tuple[str, ...] = ()
        detail = "preferred_report_period_matched_year_headers"
        if preferred_year != years[-1]:
            warnings = ("period_inferred_from_year_headers_only", "period_conflict_warning")
            detail = "preferred_report_period_overrode_max_detected_year"
        return preferred_year, warnings, detail
    return years[-1], ("period_inferred_from_year_headers_only",), (
        "table_header_year_set_max_year" if source == "table_headers" else "document_year_set_max_year"
    )


def _comparative_year_from_set(years: list[int], chosen_year: int) -> int | None:
    prior_years = [year for year in years if year < chosen_year]
    if prior_years:
        return max(prior_years)
    return None


def _period_confidence_from_years(source: str, preferred_report_period: str | None, chosen_year: int) -> float:
    preferred_year = _period_year(preferred_report_period)
    base = 0.78 if source == "table_headers" else 0.72
    if preferred_year and preferred_year == chosen_year:
        return base + 0.08
    return base


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


def _period_year(period: str | None) -> int | None:
    normalized = _normalize_period(period)
    if not normalized:
        return None
    return int(normalized[:4])


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
