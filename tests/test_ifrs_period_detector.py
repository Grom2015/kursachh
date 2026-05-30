from app.services.parsing.ifrs.period_detector import detect_period, resolve_report_period


def test_6m_column_creates_ytd_period_type():
    detected = detect_period("Six months ended 30 June 2021", report_period="2021Q2")

    assert detected.period_coverage == "6M"
    assert detected.period_type == "ytd"
    assert detected.ytd_months == 6


def test_balance_sheet_date_creates_snapshot_period_type():
    detected = detect_period("As of 31 December 2021")

    assert detected.period_coverage == "as_at_date"
    assert detected.period_type == "balance_sheet_snapshot"


def test_fy_creates_annual_period_type():
    detected = detect_period("Year ended 31 December 2021", report_period="2021Q4")

    assert detected.period_coverage == "FY"
    assert detected.period_type == "annual"


def test_resolve_report_period_detects_russian_fy():
    resolution = resolve_report_period("за год, закончившийся 31 декабря 2025 г.")

    assert resolution.effective_report_period == "2025Q4"
    assert resolution.comparative_period == "2024Q4"
    assert resolution.period_source == "document_text"
    assert resolution.period_confidence >= 0.85


def test_resolve_report_period_detects_russian_half_year():
    resolution = resolve_report_period("за 6 месяцев, закончившихся 30 июня 2025 г.")

    assert resolution.effective_report_period == "2025Q2"
    assert resolution.comparative_period == "2024Q2"
    assert resolution.period_type == "ytd"
