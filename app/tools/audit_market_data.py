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
                f"market_period_aligned: {report.get('market_period_aligned')}",
                f"candles_count: {summary['candles_count']}",
                f"first_trade_date: {summary['first_trade_date']}",
                f"last_trade_date: {summary['last_trade_date']}",
                f"technical_indicators_valid_count: {summary['technical_indicators_valid_count']}",
                f"technical_indicators_missing_count: {summary['technical_indicators_missing_count']}",
                f"liquidity_metrics_valid_count: {summary['liquidity_metrics_valid_count']}",
                f"liquidity_metrics_missing_count: {summary['liquidity_metrics_missing_count']}",
                f"valuation_inputs_available: {summary['valuation_inputs_available']}",
                f"report_path: {save_market_audit_report(report)}",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit market data and liquidity for a ticker.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--mode", choices=["fixture", "live", "replay-cache"], default="fixture")
    parser.add_argument("--board", default="TQBR")
    args = parser.parse_args(argv)
    report = run_market_audit(args.period_from, args.period_to, ticker=args.ticker, board=args.board, mode=args.mode)
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
