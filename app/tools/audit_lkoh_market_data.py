import argparse

from app.services.market.market_audit import run_market_audit, save_market_audit_report


def print_summary(report: dict) -> None:
    summary = report["summary"]
    print(
        "\n".join(
            [
                f"company: {report['company']}",
                f"ticker: {report['ticker']}",
                f"board: {report['board']}",
                f"market_data_source: {report['market_data_source']}",
                f"market_data_mode: {report.get('market_data_mode')}",
                f"status: {report['status']}",
                f"requested_date_range: {report.get('requested_date_range')}",
                f"actual_candle_date_range: {report.get('actual_candle_date_range')}",
                f"market_period_aligned: {report.get('market_period_aligned')}",
                f"candles_count: {summary['candles_count']}",
                f"first_trade_date: {summary['first_trade_date']}",
                f"last_trade_date: {summary['last_trade_date']}",
                f"technical_indicators_valid_count: {summary['technical_indicators_valid_count']}",
                f"technical_indicators_missing_count: {summary['technical_indicators_missing_count']}",
                f"technical_indicators_not_applicable_count: {summary.get('technical_indicators_not_applicable_count')}",
                f"liquidity_metrics_valid_count: {summary['liquidity_metrics_valid_count']}",
                f"liquidity_metrics_missing_count: {summary['liquidity_metrics_missing_count']}",
                f"valuation_inputs_available: {summary['valuation_inputs_available']}",
                f"report_path: {save_market_audit_report(report)}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit LKOH market data and liquidity.")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument(
        "--mode",
        choices=["fixture", "live", "replay-cache"],
        default="fixture",
        help="Market data mode: offline fixture, live MOEX ISS, or cached MOEX candles.",
    )
    parser.add_argument("--live", action="store_true", help="Fetch live MOEX ISS candles instead of offline fixture candles.")
    args = parser.parse_args(argv)
    mode = "live" if args.live else args.mode
    report = run_market_audit(args.period_from, args.period_to, mode=mode)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
