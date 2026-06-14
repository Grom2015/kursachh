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


def test_resolve_report_period_prefers_explicit_report_year_phrase_over_later_noise_year():
    resolution = resolve_report_period(
        "РљРѕРЅСЃРѕР»РёРґРёСЂРѕРІР°РЅРЅС‹Р№ РѕС‚С‡РµС‚ Рѕ РїСЂРёР±С‹Р»СЏС… Рё СѓР±С‹С‚РєР°С…\n"
        "Р—Рђ 2025 Р“РћР”\n"
        "РѕС‚С‡РµС‚ СѓС‚РІРµСЂР¶РґРµРЅ РІ 2026 РіРѕРґСѓ",
        report_period="2026Q4",
    )

    assert resolution.effective_report_period == "2025Q4"
    assert resolution.comparative_period == "2024Q4"
    assert resolution.period_resolution_source_detail in {
        "explicit_report_year_phrase",
        "single_year_statement_line",
    }


def test_resolve_report_period_prefers_report_period_year_when_present_in_plain_year_headers():
    resolution = resolve_report_period(
        title_text="some nearby 2026 text",
        headers=["line", "2025", "2024"],
        report_period="2025Q4",
    )

    assert resolution.effective_report_period == "2025Q4"
    assert resolution.comparative_period == "2024Q4"
    assert resolution.period_source == "table_headers"
