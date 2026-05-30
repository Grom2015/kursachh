import argparse
from pathlib import Path
from typing import Any

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.peers.strict_peer_analysis import PeerAnalysisRequest, StrictPeerAnalysisService


def build_peer_analysis_report(
    ticker: str,
    period_from: str,
    period_to: str,
    reporting_standard: str = "IFRS",
    max_peers: int = 5,
) -> tuple[dict[str, Any], Path]:
    init_db()
    with SessionLocal() as db:
        service = StrictPeerAnalysisService(db)
        report = service.build(
            PeerAnalysisRequest(
                ticker=ticker,
                period_from=period_from,
                period_to=period_to,
                reporting_standard=reporting_standard,
                max_peers=max_peers,
            )
        )
        path = service.save_report(report)
    return report.to_dict(), path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build strict report-only MOEX peer analysis report.")
    parser.add_argument("ticker")
    parser.add_argument("period_from")
    parser.add_argument("period_to")
    parser.add_argument("--reporting-standard", default="IFRS")
    parser.add_argument("--max-peers", type=int, default=5)
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args(argv)
    report, path = build_peer_analysis_report(
        args.ticker,
        args.period_from,
        args.period_to,
        reporting_standard=args.reporting_standard,
        max_peers=args.max_peers,
    )
    if args.json_only:
        print(path)
    else:
        print(f"target_ticker: {report['target_ticker']}")
        print(f"sector: {report['sector']}")
        print(f"peer_selection_status: {report['peer_selection_status']}")
        print(f"comparison_data_status: {report['comparison_data_status']}")
        print(f"valuation_status: {report['valuation_status']}")
        print(f"comparison_readiness: {report['comparison_readiness']}")
        print(f"peer_count: {report['summary']['peer_count']}")
        print(f"report_path: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
