from app.services.metrics.formula_registry import FORMULAS, FormulaDefinition


def safe_divide(numerator: float | None, denominator: float | None) -> tuple[float | None, str | None]:
    if numerator is None or denominator is None:
        return None, "missing input"
    if denominator == 0:
        return None, "division by zero"
    return numerator / denominator, None


def display_value(value: float | None, output_format: str) -> str | None:
    if value is None:
        return None
    if output_format == "percent":
        return f"{value * 100:.1f}%"
    if output_format == "currency":
        return f"{value:,.0f} RUB"
    return f"{value:.2f}x"


def get_formula(code: str) -> FormulaDefinition:
    return FORMULAS[code]

