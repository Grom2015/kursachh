import argparse
from pathlib import Path
from typing import Any

from app.services.llm.llm_analysis_payload_builder import LLMAnalysisPayloadBuilder, LLMAnalysisPayloadRequest


def build_llm_analysis_payload(
    company_ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    include_provider_strategy: bool = False,
) -> tuple[dict[str, Any], Path]:
    builder = LLMAnalysisPayloadBuilder()
    payload = builder.build(
        LLMAnalysisPayloadRequest(
            company_ticker=company_ticker,
            period_from=period_from,
            period_to=period_to,
            reporting_standard=reporting_standard,
            include_provider_strategy=include_provider_strategy,
        )
    )
    path = builder.save_payload(payload)
    return payload.to_dict(), path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a safe report-only JSON payload for future LLM analysis.")
    parser.add_argument("company_ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS")
    parser.add_argument("--include-provider-strategy", action="store_true")
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args(argv)

    payload, path = build_llm_analysis_payload(
        args.company_ticker,
        args.period_from,
        args.period_to,
        reporting_standard=args.reporting_standard,
        include_provider_strategy=args.include_provider_strategy,
    )
    if args.json_only:
        print(path)
    else:
        ratios = payload["financial_ratios"]
        unavailable = len(ratios["missing"]) + len(ratios["unsupported"]) + len(ratios["blocked"])
        print(f"company: {payload['company']['ticker']}")
        print(f"period: {payload['period']['from']}..{payload['period']['to']}")
        print(f"calculated_ratios: {len(ratios['calculated'])}")
        print(f"unavailable_metrics: {unavailable}")
        print(f"blockers: {', '.join(payload['blockers']) if payload['blockers'] else 'none'}")
        print(f"report_path: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
