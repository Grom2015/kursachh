import argparse
from pathlib import Path
from typing import Any

from app.services.market.market_audit import run_market_technical_report, save_market_technical_report


def build_market_technical_report(
    ticker: str,
    period_from: str,
    period_to: str,
    provider: str = "moex-iss",
    board: str = "TQBR",
    mode: str = "replay-cache",
) -> tuple[dict[str, Any], Path]:
    report = run_market_technical_report(
        ticker=ticker,
        period_from=period_from,
        period_to=period_to,
        provider=provider,
        board=board,
        mode=mode,
    )
    path = save_market_technical_report(report)
    return report, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build MOEX ISS market technical report from daily candles.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--provider", default="moex-iss", choices=["moex-iss"])
    parser.add_argument("--board", default="TQBR")
    parser.add_argument("--mode", default="replay-cache", choices=["live", "replay-cache", "fixture"])
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args(argv)
    report, path = build_market_technical_report(
        ticker=args.ticker,
        period_from=args.period_from,
        period_to=args.period_to,
        provider=args.provider,
        board=args.board,
        mode=args.mode,
    )
    if args.json_only:
        print(path)
    else:
        summary = report["summary"]
        print(f"company: {report['company']}")
        print(f"period: {report['period_from']}..{report['period_to']}")
        print(f"date_range: {report['requested_date_range']['from']}..{report['requested_date_range']['to']}")
        print(f"provider: {report['provider']}")
        print(f"mode: {report['market_data_mode']}")
        print(f"status: {report['status']}")
        print(f"candles_count: {summary['candles_count']}")
        print(f"latest_close: {report['latest_summary'].get('close')}")
        print(f"report_path: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
