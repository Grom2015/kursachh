import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company, PeerGroup

STATEMENT_METRICS = [
    "roe",
    "roa",
    "operating_margin",
    "net_margin",
    "current_ratio",
    "fcf_margin",
    "debt_to_equity",
    "revenue_growth",
]
VALUATION_METRICS = ["pe_ratio", "ev_to_ebitda"]
COMPARISON_METRICS = STATEMENT_METRICS + VALUATION_METRICS
LOW_COVERAGE_THRESHOLD = 2


@dataclass
class PeerAnalysisRequest:
    ticker: str
    period_from: str
    period_to: str
    reporting_standard: str = "IFRS"
    max_peers: int = 5


@dataclass
class PeerAnalysisReport:
    generated_at: str
    target_ticker: str
    period_from: str
    period_to: str
    reporting_standard: str
    sector: str | None
    subsector: str | None
    sector_source: str
    peer_selection_status: str
    comparison_data_status: str
    valuation_status: str
    comparison_readiness: str
    selected_peers: list[dict[str, Any]]
    ratio_report_availability: list[dict[str, Any]]
    comparison_table: list[dict[str, Any]]
    blockers: list[str]
    recommended_next_actions: list[dict[str, Any]]
    warnings: list[str]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrictPeerAnalysisService:
    def __init__(self, db: Session, root: Path | None = None):
        self.db = db
        self.root = (root or get_settings().root_dir).resolve()

    def build(self, request: PeerAnalysisRequest) -> PeerAnalysisReport:
        request = PeerAnalysisRequest(
            ticker=request.ticker.upper(),
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard.upper(),
            max_peers=request.max_peers,
        )
        target = self.db.scalar(select(Company).where(Company.ticker == request.ticker))
        warnings: list[str] = []
        if target is None:
            return self._not_ready_report(request, "target_company_not_found")
        selected_peers = self._select_peers(target, request.max_peers)
        peer_selection_status = self._peer_selection_status(target, selected_peers)
        if not target.sector:
            warnings.append("target_sector_missing")
        availability = [self._ratio_availability(target.ticker, "target", request)]
        availability.extend(self._ratio_availability(peer.ticker, "peer", request) for peer in selected_peers)
        table = [self._comparison_row(target, "target", availability[0], request)]
        table.extend(
            self._comparison_row(peer, "peer", item, request)
            for peer, item in zip(selected_peers, availability[1:], strict=False)
        )
        blockers = self._blockers(peer_selection_status, availability, table)
        comparison_data_status = self._comparison_data_status(availability)
        readiness = self._comparison_readiness(peer_selection_status, comparison_data_status, availability, table)
        recommended = self._recommended_next_actions(availability)
        return PeerAnalysisReport(
            generated_at=datetime.now(UTC).isoformat(),
            target_ticker=target.ticker,
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard,
            sector=target.sector,
            subsector=target.subsector,
            sector_source="registry" if target.sector else "missing",
            peer_selection_status=peer_selection_status,
            comparison_data_status=comparison_data_status,
            valuation_status="unavailable",
            comparison_readiness=readiness,
            selected_peers=[self._peer_payload(peer) for peer in selected_peers],
            ratio_report_availability=availability,
            comparison_table=table,
            blockers=blockers,
            recommended_next_actions=recommended,
            warnings=sorted(set(warnings)),
            summary={
                "peer_count": len(selected_peers),
                "available_peer_count": sum(1 for item in availability if item["role"] == "peer" and item["exists"]),
                "metrics_compared_count": self._metrics_compared_count(table),
                "valuation_metrics_available": False,
            },
        )

    def save_report(self, report: PeerAnalysisReport) -> Path:
        root = self.root / "data" / "validation" / "PEERS"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{report.target_ticker}_{report.period_from}_{report.period_to}_peer_analysis_report.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _select_peers(self, target: Company, max_peers: int) -> list[Company]:
        rows = self.db.scalars(
            select(PeerGroup).where(PeerGroup.company_id == target.id).order_by(PeerGroup.peer_rank)
        ).all()
        peers = [row.peer_company for row in rows if row.peer_company and row.peer_company.is_active]
        if peers:
            return peers[:max_peers]
        if not target.sector:
            return []
        candidates = self.db.scalars(
            select(Company).where(
                Company.id != target.id,
                Company.is_active.is_(True),
                Company.sector == target.sector,
            )
        ).all()
        candidates = sorted(
            candidates,
            key=lambda company: (0 if company.subsector == target.subsector else 1, company.ticker),
        )
        return candidates[:max_peers]

    def _ratio_availability(self, ticker: str, role: str, request: PeerAnalysisRequest) -> dict[str, Any]:
        path = self._ratio_path(ticker, request)
        command = (
            f"python -m app.tools.calculate_financial_ratios {ticker} {request.period_from} {request.period_to} "
            f"--reporting-standard {request.reporting_standard}"
        )
        if not path.exists():
            return {
                "ticker": ticker,
                "role": role,
                "ratios_report_path": str(path),
                "exists": False,
                "calculated_count": 0,
                "missing_count": 0,
                "unsupported_count": 0,
                "blocked_count": 0,
                "status": "missing_report",
                "recommended_command": command,
            }
        payload = self._load_json(path)
        summary = payload.get("summary") or {}
        calculated = int(summary.get("calculated_count") or 0)
        status = "available" if calculated >= LOW_COVERAGE_THRESHOLD else "available_but_low_coverage"
        return {
            "ticker": ticker,
            "role": role,
            "ratios_report_path": str(path),
            "exists": True,
            "calculated_count": calculated,
            "missing_count": int(summary.get("missing_count") or 0),
            "unsupported_count": int(summary.get("unsupported_count") or 0),
            "blocked_count": int(summary.get("blocked_count") or 0),
            "status": status,
            "recommended_command": None,
        }

    def _comparison_row(
        self,
        company: Company,
        role: str,
        availability: dict[str, Any],
        request: PeerAnalysisRequest,
    ) -> dict[str, Any]:
        if not availability["exists"]:
            return {
                "ticker": company.ticker,
                "company_name": company.full_name,
                "role": role,
                "sector": company.sector,
                "subsector": company.subsector,
                "row_status": "metrics_unavailable",
                "metrics": {code: self._report_missing_cell(code, request.period_to) for code in STATEMENT_METRICS}
                | self._valuation_cells(request.period_to),
                "blockers": ["peer_ratios_report_missing" if role == "peer" else "target_ratios_report_missing"],
            }
        payload = self._load_json(Path(availability["ratios_report_path"]))
        by_code = {
            item.get("metric_code"): item
            for item in payload.get("metrics", [])
            if item.get("period") == request.period_to
        }
        metrics = {code: self._metric_cell(code, by_code.get(code), request.period_to) for code in STATEMENT_METRICS}
        metrics.update(self._valuation_cells(request.period_to))
        row_status = "low_coverage" if availability["status"] == "available_but_low_coverage" else "available"
        return {
            "ticker": company.ticker,
            "company_name": company.full_name,
            "role": role,
            "sector": company.sector,
            "subsector": company.subsector,
            "row_status": row_status,
            "metrics": metrics,
            "blockers": [] if row_status == "available" else ["low_ratio_coverage"],
        }

    def _metric_cell(self, code: str, metric: dict[str, Any] | None, period: str) -> dict[str, Any]:
        if not metric:
            return {"value": None, "display_value": None, "status": "missing", "reason": "metric_missing", "period": period}
        return {
            "value": metric.get("value"),
            "display_value": metric.get("display_value"),
            "status": metric.get("status"),
            "reason": metric.get("reason"),
            "period": metric.get("period") or period,
        }

    def _valuation_cells(self, period: str) -> dict[str, dict[str, Any]]:
        return {
            "pe_ratio": {
                "value": None,
                "display_value": None,
                "status": "unavailable",
                "reason": "market_cap_or_price_inputs_missing",
                "period": period,
            },
            "ev_to_ebitda": {
                "value": None,
                "display_value": None,
                "status": "unavailable",
                "reason": "enterprise_value_or_explicit_ebitda_missing",
                "period": period,
            },
        }

    def _report_missing_cell(self, code: str, period: str) -> dict[str, Any]:
        return {
            "value": None,
            "display_value": None,
            "status": "report_missing",
            "reason": "ratios_report_missing",
            "period": period,
        }

    def _peer_selection_status(self, target: Company, peers: list[Company]) -> str:
        if not target.sector or not peers:
            return "not_ready"
        if len(peers) < 3:
            return "partial"
        return "ready"

    def _comparison_data_status(self, availability: list[dict[str, Any]]) -> str:
        target = next((item for item in availability if item["role"] == "target"), {})
        if target.get("status") == "missing_report":
            return "not_ready"
        peers = [item for item in availability if item["role"] == "peer"]
        usable = [item for item in peers if item["status"] == "available"]
        if len(usable) >= max(2, (len(peers) // 2) + 1):
            return "ready"
        if peers:
            return "partial"
        return "not_ready"

    def _comparison_readiness(
        self,
        peer_selection_status: str,
        comparison_data_status: str,
        availability: list[dict[str, Any]],
        table: list[dict[str, Any]],
    ) -> str:
        if peer_selection_status == "not_ready" or comparison_data_status == "not_ready":
            return "NOT_READY"
        if self._almost_empty(table):
            return "NOT_READY"
        peer_usable = sum(1 for item in availability if item["role"] == "peer" and item["status"] == "available")
        if peer_usable >= 2:
            return "READY_WITH_LIMITATIONS"
        return "PARTIAL"

    def _almost_empty(self, table: list[dict[str, Any]]) -> bool:
        calculated = 0
        for row in table:
            for code in STATEMENT_METRICS:
                if row["metrics"].get(code, {}).get("status") == "calculated":
                    calculated += 1
        return calculated == 0

    def _blockers(self, peer_selection_status: str, availability: list[dict[str, Any]], table: list[dict[str, Any]]) -> list[str]:
        blockers = set()
        if peer_selection_status == "not_ready":
            blockers.add("peer_selection_not_ready")
        for item in availability:
            if item["status"] == "missing_report":
                blockers.add("target_ratios_report_missing" if item["role"] == "target" else "peer_ratios_report_missing")
            if item["status"] == "available_but_low_coverage":
                blockers.add("peer_ratios_low_coverage" if item["role"] == "peer" else "target_ratios_low_coverage")
        if self._almost_empty(table):
            blockers.add("comparison_table_metrics_unavailable")
        blockers.add("valuation_inputs_unavailable")
        return sorted(blockers)

    def _recommended_next_actions(self, availability: list[dict[str, Any]]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        missing_peers = [item["ticker"] for item in availability if item["role"] == "peer" and item["status"] == "missing_report"]
        if missing_peers:
            actions.append(
                {
                    "action": "calculate_missing_peer_ratios",
                    "tickers": missing_peers,
                    "reason": "peer ratios reports are missing",
                }
            )
        target_missing = [
            item["ticker"]
            for item in availability
            if item["role"] == "target" and item["status"] == "missing_report"
        ]
        if target_missing:
            actions.append(
                {
                    "action": "calculate_target_ratios",
                    "tickers": target_missing,
                    "reason": "target ratios report is missing",
                }
            )
        actions.append(
            {
                "action": "build_valuation_input_module",
                "reason": "P/E and EV/EBITDA unavailable without market cap, EV and explicit EBITDA",
            }
        )
        return actions

    def _metrics_compared_count(self, table: list[dict[str, Any]]) -> int:
        return sum(
            1
            for row in table
            for code in STATEMENT_METRICS
            if row.get("role") == "peer" and row["metrics"].get(code, {}).get("status") == "calculated"
        )

    def _not_ready_report(self, request: PeerAnalysisRequest, blocker: str) -> PeerAnalysisReport:
        return PeerAnalysisReport(
            generated_at=datetime.now(UTC).isoformat(),
            target_ticker=request.ticker,
            period_from=request.period_from,
            period_to=request.period_to,
            reporting_standard=request.reporting_standard,
            sector=None,
            subsector=None,
            sector_source="missing",
            peer_selection_status="not_ready",
            comparison_data_status="not_ready",
            valuation_status="unavailable",
            comparison_readiness="NOT_READY",
            selected_peers=[],
            ratio_report_availability=[],
            comparison_table=[],
            blockers=[blocker],
            recommended_next_actions=[],
            warnings=[blocker],
            summary={
                "peer_count": 0,
                "available_peer_count": 0,
                "metrics_compared_count": 0,
                "valuation_metrics_available": False,
            },
        )

    def _peer_payload(self, peer: Company) -> dict[str, Any]:
        return {
            "ticker": peer.ticker,
            "company_name": peer.full_name,
            "sector": peer.sector,
            "subsector": peer.subsector,
        }

    def _ratio_path(self, ticker: str, request: PeerAnalysisRequest) -> Path:
        filename = f"{request.period_from}_{request.period_to}_financial_ratios.json"
        return self.root / "data" / "validation" / ticker.upper() / filename

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
