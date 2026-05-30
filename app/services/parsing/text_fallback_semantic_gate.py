import re
from dataclasses import dataclass, field
from typing import Any

from app.services.sectors.banking_policy import is_banking_ticker

CAUTION_WARNING = "derived_from_raw_text_table_requires_extra_caution"
FALLBACK_EXTRACTION_METHOD = "text_table_fallback_semantic_gate"


@dataclass
class TextFallbackAcceptedRow:
    metric_code: str
    value: float
    currency: str | None
    unit_multiplier: float
    period_type: str
    period: str | None
    raw_label: str
    raw_value: str
    source_line: str
    source_lines: list[str]
    column_name: str
    confidence_score: float
    quality_flag: str
    source_location: dict[str, Any]
    warnings: list[str] = field(default_factory=lambda: [CAUTION_WARNING])


@dataclass
class TextFallbackRejectedRow:
    raw_label: str | None
    metric_code: str | None
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TextFallbackGateResult:
    accepted_rows: list[TextFallbackAcceptedRow]
    rejected_rows: list[TextFallbackRejectedRow]
    warnings: list[str]
    confidence_score: float


def evaluate_text_fallback_table(
    table: dict[str, Any],
    reporting_standard: str,
    company_ticker: str,
    period: str,
    statement_type: str,
) -> TextFallbackGateResult:
    del reporting_standard
    rejected: list[TextFallbackRejectedRow] = []
    warnings: list[str] = []
    if table.get("extraction_method") != "text_table_fallback" or table.get("quality_flag") != "raw_text_table":
        return _table_rejected("not_text_fallback_raw_table")
    if statement_type not in FALLBACK_ALLOWED_BY_STATEMENT:
        return _table_rejected("unsupported_statement_type_for_text_fallback")
    if not _traceability_ok(table):
        return _table_rejected("missing_traceability")
    if _is_notes_like(table):
        return _table_rejected("notes_fallback_not_eligible_for_fact_normalization")
    if not _has_primary_statement_marker(table, statement_type):
        return _table_rejected("primary_statement_marker_not_detected")

    include_comparative_columns = is_banking_ticker(company_ticker)
    period_order = _period_order(table, period, statement_type)
    if period_order == "unknown":
        if statement_type == "income_statement":
            period_order = "current_first_inferred"
            warnings.append("current_period_column_inferred_from_statement_layout")
        else:
            warnings.append("current_period_header_not_detected")

    accepted: list[TextFallbackAcceptedRow] = []
    for source_lines, line in _candidate_lines(table):
        if not line:
            continue
        parsed = _parse_line(line)
        if not parsed:
            continue
        raw_label, numbers = parsed
        if _excluded_label(raw_label):
            rejected.append(
                TextFallbackRejectedRow(
                    raw_label=raw_label,
                    metric_code=None,
                    reason=_excluded_label(raw_label) or "excluded_label",
                    details={"source_line": line, "source_lines": source_lines},
                )
            )
            continue
        metric_code, label_confidence = _match_metric(raw_label, statement_type)
        if not metric_code:
            if _looks_like_blocked_metric(raw_label):
                rejected.append(
                    TextFallbackRejectedRow(
                        raw_label=raw_label,
                        metric_code=None,
                        reason="unsupported_metric_for_text_fallback",
                        details={"source_line": line},
                    )
                )
            continue
        selections = _select_statement_numbers(
            numbers,
            period_order,
            period,
            statement_type,
            include_comparative_columns=include_comparative_columns,
        )
        if not selections:
            selected = _select_current_number(numbers, period_order)
            rejected.append(
                TextFallbackRejectedRow(
                    raw_label=raw_label,
                    metric_code=metric_code,
                    reason=selected.reason or "ambiguous_period_column",
                    details={
                        "source_line": line,
                        "source_lines": source_lines,
                        "numbers_detected": [item.raw for item in numbers],
                    },
                )
            )
            continue
        if not _unit_basis_ok(table):
            rejected.append(
                TextFallbackRejectedRow(
                    raw_label=raw_label,
                    metric_code=metric_code,
                    reason="unit_or_currency_basis_missing",
                    details={"source_line": line, "source_lines": source_lines},
                )
            )
            continue
        unit_multiplier = table_unit_multiplier(table)
        for selected in selections:
            source_location = {
                "source_document_id": table.get("document_id"),
                "source_table_type": statement_type,
                "source_table_index": table.get("table_index"),
                "source_location": table.get("source_location"),
                "page_number": table.get("page_number"),
                "column_name": selected.column_name,
                "raw_label": raw_label,
                "raw_value": selected.raw,
                "source_line": line,
                "source_lines": source_lines,
                "extraction_method": FALLBACK_EXTRACTION_METHOD,
                "source_table_extraction_method": table.get("extraction_method"),
                "quality_gate": "strict_primary_statement_text_fallback",
                "fact_source_kind": FALLBACK_EXTRACTION_METHOD,
                "metric_input_allowed_by_default": False,
                "requires_downstream_quality_gate": True,
            }
            accepted.append(
                TextFallbackAcceptedRow(
                    metric_code=metric_code,
                    value=selected.value * unit_multiplier,
                    currency=table.get("currency"),
                    unit_multiplier=unit_multiplier,
                    period_type=period_type_for(statement_type, selected.period or period),
                    period=selected.period,
                    raw_label=raw_label,
                    raw_value=selected.raw,
                    source_line=line,
                    source_lines=source_lines,
                    column_name=selected.column_name,
                    confidence_score=label_confidence,
                    quality_flag="high_confidence_text_fallback",
                    source_location=source_location,
                    warnings=[*([CAUTION_WARNING]), *warnings],
                )
            )
    score = max((row.confidence_score for row in accepted), default=0.0)
    return TextFallbackGateResult(accepted, rejected, warnings, score)


@dataclass
class _NumberToken:
    raw: str
    value: float
    is_note_ref: bool


@dataclass
class _SelectedNumber:
    raw: str | None = None
    value: float | None = None
    column_name: str | None = None
    period: str | None = None
    reason: str | None = None


FALLBACK_ALLOWED_BY_STATEMENT = {
    "income_statement": {
        "revenue",
        "operating_profit",
        "net_income",
        "interest_income",
        "interest_expense",
        "fee_and_commission_income",
        "fee_and_commission_expense",
        "net_interest_income",
        "net_fee_commission_income",
        "net_trading_income",
        "operating_income",
        "operating_expenses",
        "impairment_charge",
        "profit_before_tax",
    },
    "balance_sheet": {
        "total_assets",
        "total_liabilities",
        "total_equity",
        "current_assets",
        "current_liabilities",
        "cash_and_equivalents",
        "loans_to_customers",
        "retail_customer_accounts",
        "corporate_customer_accounts",
        "customer_accounts",
    },
    "cash_flow": {"operating_cash_flow", "capex"},
}

LABELS = {
    "income_statement": {
        "revenue": [
            "revenue",
            "revenues",
            "total revenues",
            "sales",
            "sales and other operating revenues",
            "sales and other operating revenues net",
            "выручка",
            "продажи",
        ],
        "operating_profit": [
            "operating profit",
            "profit from operating activities",
            "прибыль от операционной деятельности",
        ],
        "net_income": [
            "profit for the period",
            "profit for the year",
            "profit loss for the period",
            "profit attributable to pjsc shareholders",
            "profit attributable to pjsc lukoil shareholders",
            "net income",
            "net profit",
            "прибыль за год",
            "прибыль за период",
            "чистая прибыль",
            "прибыль за отчетный период",
        ],
        "interest_income": [
            "interest income",
            "процентные доходы",
        ],
        "interest_expense": [
            "interest expense",
            "interest expenses",
            "процентные расходы",
        ],
        "fee_and_commission_income": [
            "fee and commission income",
            "fee commission income",
            "комиссионные доходы",
        ],
        "fee_and_commission_expense": [
            "fee and commission expense",
            "fee and commission expenses",
            "комиссионные расходы",
        ],
        "net_interest_income": [
            "net interest income",
            "net interest income after provision for loan impairment",
            "чистый процентный доход",
            "чистые процентные доходы",
            "чистые процентные доходы после создания резерва",
        ],
        "net_fee_commission_income": [
            "net fee and commission income",
            "net fee commission income",
            "чистый комиссионный доход",
            "чистые комиссионные доходы",
        ],
        "net_trading_income": [
            "net trading income",
            "net gains from trading",
            "чистые доходы от операций с финансовыми инструментами",
            "чистые доходы от торговых операций",
        ],
        "operating_income": [
            "operating income",
            "total operating income",
            "операционные доходы",
            "итого операционные доходы",
            "операционный доход",
        ],
        "operating_expenses": [
            "operating expenses",
            "administrative and other operating expenses",
            "staff costs and administrative expenses",
            "personnel and administrative expenses",
            "операционные расходы",
            "административные и прочие операционные расходы",
            "расходы на содержание персонала и административные расходы",
        ],
        "impairment_charge": [
            "credit loss allowance",
            "provision for loan impairment",
            "impairment losses",
            "расходы по кредитным убыткам",
            "резерв под ожидаемые кредитные убытки",
        ],
        "profit_before_tax": [
            "profit before tax",
            "profit before income tax",
            "прибыль до налогообложения",
        ],
    },
    "balance_sheet": {
        "total_assets": ["total assets", "итого активов"],
        "total_liabilities": ["total liabilities", "итого обязательств"],
        "total_equity": ["total equity", "total shareholders equity", "итого собственных средств", "итого капитала"],
        "current_assets": ["total current assets", "current assets"],
        "current_liabilities": ["total current liabilities", "current liabilities"],
        "cash_and_equivalents": ["cash and cash equivalents", "денежные средства и их эквиваленты"],
        "loans_to_customers": ["loans and advances to customers", "loans to customers", "кредиты и авансы клиентам"],
        "retail_customer_accounts": ["amounts due to individuals", "retail customer accounts", "средства физических лиц"],
        "corporate_customer_accounts": [
            "amounts due to corporate customers",
            "corporate customer accounts",
            "средства корпоративных клиентов",
        ],
        "customer_accounts": ["amounts due to customers", "customer accounts", "due to customers", "средства клиентов"],
    },
    "cash_flow": {
        "operating_cash_flow": [
            "net cash provided by operating activities",
            "net cash generated from operating activities",
            "net cash from operating activities",
            "чистые денежные средства полученные от операционной деятельности",
            "денежные средства полученные от операционной деятельности",
        ],
        "capex": ["capital expenditures", "purchase of property plant and equipment"],
    },
}

PRIMARY_MARKERS = {
    "balance_sheet": [
        "statement of financial position",
        "balance sheet",
        "обобщенный консолидированный отчет о финансовом положении",
        "консолидированный отчет о финансовом положении",
        "отчет о финансовом положении",
    ],
    "income_statement": [
        "statement of profit or loss",
        "statement of comprehensive income",
        "statement of income",
        "consolidated statement of profit or loss",
        "обобщенный консолидированный отчет о прибылях и убытках",
        "консолидированный отчет о прибылях и убытках",
        "отчет о прибылях и убытках",
        "обобщенный консолидированный отчет о совокупном доходе",
        "консолидированный отчет о совокупном доходе",
    ],
    "cash_flow": [
        "statement of cash flows",
        "обобщенный консолидированный отчет о движении денежных средств",
        "консолидированный отчет о движении денежных средств",
        "отчет о движении денежных средств",
    ],
}

NOTES_MARKERS = ["notes to"]

BLOCKED_METRIC_MARKERS = [
    "ebitda",
    "dividend",
    "total debt",
    "borrowings",
    "loans and borrowings",
    "interest income",
    "fee and commission income",
    "процентные доходы",
    "комиссионные доходы",
]


def _table_rejected(reason: str) -> TextFallbackGateResult:
    return TextFallbackGateResult(
        accepted_rows=[],
        rejected_rows=[TextFallbackRejectedRow(None, None, reason)],
        warnings=[],
        confidence_score=0.0,
    )


def _traceability_ok(table: dict[str, Any]) -> bool:
    return bool(table.get("source_location") and table.get("table_index") is not None and table.get("page_number"))


def _is_notes_like(table: dict[str, Any]) -> bool:
    title = normalize_label(table.get("table_title") or "")
    first_line = normalize_label(_line_text((table.get("rows") or [{}])[0]))
    return any(marker in title for marker in NOTES_MARKERS) or first_line.startswith("note ")


def _has_primary_statement_marker(table: dict[str, Any], statement_type: str) -> bool:
    title = normalize_label(table.get("table_title") or "")
    first_lines = " ".join(_line_text(row) for row in (table.get("rows") or [])[:8])
    text = normalize_label(f"{title} {first_lines}")
    return any(marker in text for marker in PRIMARY_MARKERS.get(statement_type, []))


def _period_order(table: dict[str, Any], period: str, statement_type: str) -> str:
    header_text = normalize_label(" ".join(_line_text(row) for row in (table.get("rows") or [])[:8]))
    if statement_type == "income_statement" and (
        (period.endswith("Q2") and "for the six" in header_text)
        or (period.endswith("Q3") and "for the nine" in header_text)
    ):
        return "current_ytd_third"
    current_markers = _current_period_markers(period)
    current_positions = [header_text.find(marker) for marker in current_markers if marker in header_text]
    prior_markers = _prior_period_markers(period)
    prior_positions = [header_text.find(marker) for marker in prior_markers if marker in header_text]
    if not current_positions:
        return "unknown"
    if not prior_positions:
        return "single_current"
    current_pos = min(current_positions)
    prior_pos = min(prior_positions)
    return "current_first" if current_pos < prior_pos else "current_second"


def _current_period_markers(period: str) -> list[str]:
    year = _period_year(period)
    if period.endswith("Q1"):
        return [f"31 march {year}", f"march {year}", str(year)]
    if period.endswith("Q2"):
        return [f"30 june {year}", f"june {year}", str(year)]
    if period.endswith("Q3"):
        return [f"30 september {year}", f"september {year}", str(year)]
    if period.endswith("Q4") or (str(period).isdigit() and len(str(period)) == 4):
        return [f"31 december {year}", str(year)]
    return [str(year)]


def _prior_period_markers(period: str) -> list[str]:
    year = _period_year(period) - 1
    return [f"31 december {year}", str(year)]


def _period_year(period: str) -> int:
    match = re.match(r"^(\d{4})", str(period or ""))
    return int(match.group(1)) if match else 2021


def _parse_line(line: str) -> tuple[str, list[_NumberToken]] | None:
    matches = _trailing_number_matches(line)
    numbers: list[_NumberToken] = []
    raw_numbers = _group_spaced_number_tokens([match.group(0).strip() for match in matches])
    for raw in raw_numbers:
        value = parse_number(raw)
        if value is None:
            continue
        numbers.append(_NumberToken(raw=raw, value=value, is_note_ref=_is_note_reference(raw)))
    if not numbers:
        return None
    label = line
    if matches:
        label = line[: matches[0].start()]
    label = " ".join(label.split(" -:"))
    label = re.sub(r"\s+", " ", label).strip()
    if not label:
        return None
    return label, numbers


def _candidate_lines(table: dict[str, Any]) -> list[tuple[list[str], str]]:
    lines = [str((row or {}).get("line") or "").strip() for row in table.get("rows", []) or []]
    candidates: list[tuple[list[str], str]] = []
    for index, line in enumerate(lines):
        if not line:
            continue
        candidates.append(([line], line))
        if _is_header_or_title_line(line):
            continue
        parsed = _parse_line(line)
        metric_code, _confidence = _match_metric(parsed[0], table.get("statement_type")) if parsed else (None, 0.0)
        if parsed and metric_code:
            continue
        for width in [2, 3]:
            source_lines = lines[index : index + width]
            if len(source_lines) != width or not all(source_lines):
                continue
            combined = " ".join(source_lines)
            candidates.append((source_lines, combined))
    return candidates


def _trailing_number_matches(line: str) -> list[re.Match[str]]:
    token_pattern = r"\(?-?\d[\d,]*(?:\.\d+)?\)?"
    matches = list(re.finditer(token_pattern, line))
    if not matches:
        return []
    trailing: list[re.Match[str]] = []
    cursor = len(line)
    for match in reversed(matches):
        between = line[match.end() : cursor]
        if between.strip():
            break
        trailing.append(match)
        cursor = match.start()
    return list(reversed(trailing))


def _group_spaced_number_tokens(tokens: list[str]) -> list[str]:
    if len(tokens) <= 2:
        return tokens
    start = 1 if _is_note_reference(tokens[0]) and len(tokens) % 2 == 1 else 0
    value_tokens = tokens[start:]
    if len(value_tokens) <= 2 or len(value_tokens) % 2:
        return tokens
    pair_grouped = _group_decimal_pairs(value_tokens)
    if pair_grouped:
        return ([tokens[0]] if start else []) + pair_grouped
    if any("," in token for token in tokens):
        return tokens
    half = len(value_tokens) // 2
    first = value_tokens[:half]
    second = value_tokens[half:]
    if _valid_spaced_group(first) and _valid_spaced_group(second):
        grouped = [" ".join(first), " ".join(second)]
        return ([tokens[0]] if start else []) + grouped
    return tokens


def _group_decimal_pairs(tokens: list[str]) -> list[str] | None:
    if len(tokens) % 2:
        return None
    grouped: list[str] = []
    for index in range(0, len(tokens), 2):
        first = tokens[index].strip("()").lstrip("-")
        second = tokens[index + 1].strip("()").lstrip("-")
        if len(first) > 3 or not re.match(r"^\d{3}(?:[,.]\d+)$", second):
            return None
        left_paren = "(" if tokens[index].startswith("(") else ""
        right_paren = ")" if tokens[index + 1].endswith(")") else ""
        grouped.append(f"{left_paren}{tokens[index].strip('()')} {tokens[index + 1].strip('()')}{right_paren}")
    return grouped


def _valid_spaced_group(tokens: list[str]) -> bool:
    if len(tokens) < 2:
        return False
    cleaned = [token.strip("()").lstrip("-") for token in tokens]
    return bool(cleaned[0]) and len(cleaned[0]) <= 3 and all(len(token) == 3 for token in cleaned[1:])


def _is_header_or_title_line(line: str) -> bool:
    normalized = normalize_label(line)
    primary_markers = (
        PRIMARY_MARKERS["income_statement"]
        + PRIMARY_MARKERS["balance_sheet"]
        + PRIMARY_MARKERS["cash_flow"]
    )
    if any(marker in normalized for marker in primary_markers):
        return True
    if normalized.startswith("note"):
        return True
    if "million" in normalized or "russian rubles" in normalized:
        return True
    return False


def _is_note_reference(raw: str) -> bool:
    cleaned = raw.strip("()").replace(" ", "").replace(",", "")
    return cleaned.isdigit() and len(cleaned) <= 2


def _match_metric(raw_label: str, statement_type: str) -> tuple[str | None, float]:
    normalized = normalize_label(raw_label)
    for metric_code, labels in LABELS.get(statement_type, {}).items():
        for label in labels:
            if normalized == normalize_label(label):
                return metric_code, 0.94
    best: tuple[str | None, float, int] = (None, 0.0, 0)
    for metric_code, labels in LABELS.get(statement_type, {}).items():
        for label in labels:
            candidate = normalize_label(label)
            if candidate in normalized:
                best = max(best, (metric_code, 0.86, len(candidate)), key=lambda item: (item[1], item[2]))
    return best[0], best[1]


def _select_current_number(numbers: list[_NumberToken], period_order: str) -> _SelectedNumber:
    value_numbers = numbers
    if len(value_numbers) >= 3 and value_numbers[0].is_note_ref:
        value_numbers = value_numbers[1:]
    if len(value_numbers) == 4 and period_order == "current_ytd_third":
        item = value_numbers[2]
        return _SelectedNumber(raw=item.raw, value=item.value, column_name=period_order)
    if len(value_numbers) == 1:
        item = value_numbers[0]
        return _SelectedNumber(raw=item.raw, value=item.value, column_name="single_current_period_value")
    if len(value_numbers) == 2 and period_order in {"current_first", "current_second", "current_first_inferred"}:
        item = value_numbers[0] if period_order == "current_first" else value_numbers[1]
        if period_order == "current_first_inferred":
            item = value_numbers[0]
        return _SelectedNumber(raw=item.raw, value=item.value, column_name=period_order)
    return _SelectedNumber(reason="ambiguous_period_column")


def _select_statement_numbers(
    numbers: list[_NumberToken],
    period_order: str,
    period: str,
    statement_type: str,
    *,
    include_comparative_columns: bool = False,
) -> list[_SelectedNumber]:
    value_numbers = numbers
    if len(value_numbers) >= 3 and value_numbers[0].is_note_ref:
        value_numbers = value_numbers[1:]
    current_period = _current_fact_period(period)
    comparative_period = _comparative_fact_period(period)
    if not include_comparative_columns:
        selected = _select_current_number(numbers, period_order)
        if selected.reason:
            return []
        selected.period = current_period
        return [selected]
    if len(value_numbers) == 2 and period_order in {"current_first", "current_first_inferred"}:
        return [
            _SelectedNumber(
                raw=value_numbers[1].raw,
                value=value_numbers[1].value,
                column_name="comparative_second",
                period=comparative_period,
            ),
            _SelectedNumber(
                raw=value_numbers[0].raw,
                value=value_numbers[0].value,
                column_name="current_first",
                period=current_period,
            ),
        ]
    if len(value_numbers) == 2 and period_order == "current_second":
        return [
            _SelectedNumber(
                raw=value_numbers[0].raw,
                value=value_numbers[0].value,
                column_name="comparative_first",
                period=comparative_period,
            ),
            _SelectedNumber(
                raw=value_numbers[1].raw,
                value=value_numbers[1].value,
                column_name="current_second",
                period=current_period,
            ),
        ]
    if len(value_numbers) == 1:
        return [
            _SelectedNumber(
                raw=value_numbers[0].raw,
                value=value_numbers[0].value,
                column_name="single_current_period_value",
                period=current_period,
            )
        ]
    if len(value_numbers) == 4 and period_order == "current_ytd_third":
        return [
            _SelectedNumber(
                raw=value_numbers[2].raw,
                value=value_numbers[2].value,
                column_name=period_order,
                period=current_period,
            )
        ]
    return []


def _current_fact_period(period: str) -> str:
    raw = str(period or "").upper()
    if raw.isdigit() and len(raw) == 4:
        return f"{raw}Q4"
    return raw


def _comparative_fact_period(period: str) -> str:
    year = _period_year(period) - 1
    return f"{year}Q4"


def _unit_basis_ok(table: dict[str, Any]) -> bool:
    return bool(table.get("unit") or table.get("currency"))


def _looks_like_blocked_metric(raw_label: str) -> bool:
    normalized = normalize_label(raw_label)
    return any(marker in normalized for marker in BLOCKED_METRIC_MARKERS)


def _excluded_label(raw_label: str) -> str | None:
    normalized = normalize_label(raw_label)
    if "per share" in normalized:
        return "per_share_row_not_statement_fact"
    if "непрофиль" in normalized:
        return "non_core_revenue_not_statement_revenue"
    if "страхов" in normalized and ("выручк" in normalized or "revenue" in normalized):
        return "insurance_revenue_not_total_revenue"
    return None


def _line_text(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("line") or "")
    return str(row or "")


def normalize_label(value: Any) -> str:
    normalized = re.sub(r"[^\w\s]", " ", str(value or "").casefold())
    return " ".join(normalized.split())


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("\u00a0", " ")
    cleaned = re.sub(r"[^0-9,.\-\s]", "", cleaned).strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        parts = cleaned.split(",")
        cleaned = "".join(parts) if all(len(part) == 3 for part in parts[1:]) else cleaned.replace(",", ".")
    cleaned = cleaned.replace(" ", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def unit_multiplier_for(unit: str | None) -> float:
    normalized = normalize_label(unit or "")
    if "billion" in normalized or "миллиард" in normalized:
        return 1_000_000_000.0
    if "million" in normalized or "млн" in normalized or "миллион" in normalized:
        return 1_000_000.0
    if "thousand" in normalized or "тыс" in normalized:
        return 1_000.0
    return 1.0


def table_unit_multiplier(table: dict[str, Any]) -> float:
    raw = table.get("unit_multiplier")
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return unit_multiplier_for(table.get("unit"))


def period_type_for(statement_type: str, period: str) -> str:
    if statement_type == "balance_sheet":
        return "balance_sheet_snapshot"
    if period.endswith("Q4"):
        return "annual"
    return "ytd"
