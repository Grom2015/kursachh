from app.db.models import Company
from app.services.analysis.llm_payload_builder import LLMPayloadBuilder


def test_llm_payload_real_mode_contract():
    company = Company(id=1, ticker="LKOH", board="TQBR", short_name="LKOH", full_name="LKOH")
    payload = LLMPayloadBuilder().build(
        company,
        "2021Q1",
        "2021Q4",
        "IFRS",
        [{"metric_code": "revenue", "period": "2021Q1", "value": 1, "quality_flag": "low_confidence_parse"}],
        {},
        {},
        [{"source_type": "issuer_ir_manifest", "source_url": "https://www.lukoil.ru/report.pdf"}],
        ["low confidence parse"],
        data_mode="real",
        data_quality={"real_data_used": True, "fixture_data_used": False},
    )
    assert payload["data_mode"] == "real"
    assert payload["data_quality"]["real_data_used"] is True
    assert "low confidence parse" in payload["warnings"]
    assert not any(word in str(payload).casefold() for word in ["buy", "sell", "hold", "покупать", "продавать"])

