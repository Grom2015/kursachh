import re

PERIOD_RE = re.compile(r"^(?P<year>\d{4})Q(?P<quarter>[1-4])$")


def validate_period(period: str) -> None:
    if not PERIOD_RE.match(period):
        raise ValueError(f"Invalid period format: {period}. Expected YYYYQ1..YYYYQ4")


def period_key(period: str) -> tuple[int, int]:
    validate_period(period)
    year, quarter = period.split("Q")
    return int(year), int(quarter)


def ensure_period_range(period_from: str, period_to: str) -> None:
    validate_period(period_from)
    validate_period(period_to)
    if period_key(period_from) > period_key(period_to):
        raise ValueError("period_from must be less than or equal to period_to")


def ensure_extension_period(current_period_to: str, new_period_to: str) -> None:
    validate_period(new_period_to)
    if period_key(new_period_to) <= period_key(current_period_to):
        raise ValueError("new_period_to must be greater than current result period_to")


def period_in_range(period: str, period_from: str, period_to: str) -> bool:
    return period_key(period_from) <= period_key(period) <= period_key(period_to)


def period_or_year_in_range(period: str, period_from: str, period_to: str) -> bool:
    normalized = str(period or "").strip().upper()
    if normalized.isdigit() and len(normalized) == 4:
        start = period_key(period_from)
        end = period_key(period_to)
        year = int(normalized)
        return start <= (year, 4) and (year, 1) <= end
    return period_in_range(normalized, period_from, period_to)


def periods_between(period_from: str, period_to: str) -> list[str]:
    ensure_period_range(period_from, period_to)
    start_year, start_q = period_key(period_from)
    end_year, end_q = period_key(period_to)
    out: list[str] = []
    year, quarter = start_year, start_q
    while (year, quarter) <= (end_year, end_q):
        out.append(f"{year}Q{quarter}")
        quarter += 1
        if quarter == 5:
            year += 1
            quarter = 1
    return out


def previous_period(period: str) -> str | None:
    year, quarter = period_key(period)
    if quarter == 1:
        return f"{year - 1}Q4"
    return f"{year}Q{quarter - 1}"
