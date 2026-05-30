import argparse

from app.db.session import SessionLocal
from app.services.company_source_discovery import CompanySourceDiscoveryService


def discover_company_sources(
    query: str,
    ticker: str | None = None,
    market: str = "MOEX",
    limit: int = 10,
    live: bool = False,
) -> tuple[dict, str]:
    with SessionLocal() as db:
        service = CompanySourceDiscoveryService(db)
        report = service.discover(query=query, ticker=ticker, market=market, limit=limit, live=live)
        path = service.save_report(report)
    return report.to_dict(), str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover official company website/disclosure source-page candidates.")
    parser.add_argument("query", help="Company name or ticker.")
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--market", default="MOEX")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Check URL metadata availability only; does not verify financial statements.",
    )
    args = parser.parse_args(argv)

    report, path = discover_company_sources(
        query=args.query,
        ticker=args.ticker,
        market=args.market,
        limit=args.limit,
        live=args.live,
    )
    print(
        "\n".join(
            [
                f"query: {report['query']}",
                f"recommended_candidates: {len(report['recommended_candidates'])}",
                f"auto_select_allowed: {report['auto_select_allowed']}",
                f"disclaimer: {report['disclaimer']}",
                f"report_path: {path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
