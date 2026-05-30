from app.services.parsing.ifrs.derived_fact_rules import FactInput, derive_total_debt


def test_total_debt_uses_explicit_when_available():
    result = derive_total_debt({"total_debt": FactInput("total_debt", 100.0)})

    assert result.value == 100.0
    assert result.quality_flag == "exact"


def test_total_debt_avoids_double_counting_current_portion():
    result = derive_total_debt(
        {
            "short_term_debt_including_current_portion": FactInput("short_term_debt_including_current_portion", 9714),
            "gross_long_term_debt": FactInput("gross_long_term_debt", 25682),
            "long_term_debt_net_of_current_portion": FactInput("long_term_debt_net_of_current_portion", 22926),
        }
    )

    assert result.value == 32640
    assert result.formula == "short_term_debt_including_current_portion + long_term_debt_net_of_current_portion"
    assert "gross_long_term_debt" not in result.inputs_json


def test_forbidden_total_debt_combination_returns_none():
    result = derive_total_debt(
        {
            "short_term_debt_including_current_portion": FactInput("short_term_debt_including_current_portion", 9714),
            "gross_long_term_debt": FactInput("gross_long_term_debt", 25682),
        }
    )

    assert result is None
