import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Company
from app.services.peers.peer_registry import PeerRegistry


class PeerAnalysisService:
    def __init__(self, db: Session):
        self.db = db

    def build(self, company: Company, period: str) -> dict:
        peers = PeerRegistry(self.db).peers_for(company.id)
        path = get_settings().root_dir / "data" / "fixtures" / "peers_financial_facts.csv"
        fixture = pd.read_csv(path) if path.exists() else pd.DataFrame()
        rows: list[dict] = []
        warnings: list[str] = []
        for peer in peers:
            match = fixture[(fixture["ticker"] == peer.ticker) & (fixture["period"] == period)]
            if match.empty:
                warnings.append(f"Peer data missing for {peer.ticker} {period}")
                rows.append(self._missing_row(peer))
            else:
                data = match.iloc[0].to_dict()
                rows.append(
                    {
                        "ticker": peer.ticker,
                        "company_name": peer.full_name,
                        "sector": peer.sector,
                        "roe": data.get("roe"),
                        "ebitda_margin": data.get("ebitda_margin"),
                        "debt_to_equity": data.get("debt_to_equity"),
                        "current_ratio": data.get("current_ratio"),
                        "fcf_margin": data.get("fcf_margin"),
                        "pe_ratio": None if pd.isna(data.get("pe_ratio")) else data.get("pe_ratio"),
                        "ev_to_ebitda": None
                        if pd.isna(data.get("ev_to_ebitda"))
                        else data.get("ev_to_ebitda"),
                        "dividend_yield": data.get("dividend_yield"),
                        "data_quality_flags": [data.get("quality_flag", "fixture")],
                        "warnings": ["fixture peer metrics; not sourced from live filings"],
                    }
                )
        return {
            "peer_table": rows,
            "peer_summary": {
                "target_company": company.ticker,
                "peer_count": len(peers),
                "available_peer_count": sum(1 for row in rows if "missing" not in row["data_quality_flags"]),
                "sector": company.sector,
                "warnings": warnings,
            },
            "warnings": warnings,
        }

    def _missing_row(self, peer: Company) -> dict:
        return {
            "ticker": peer.ticker,
            "company_name": peer.full_name,
            "sector": peer.sector,
            "roe": None,
            "ebitda_margin": None,
            "debt_to_equity": None,
            "current_ratio": None,
            "fcf_margin": None,
            "pe_ratio": None,
            "ev_to_ebitda": None,
            "dividend_yield": None,
            "data_quality_flags": ["missing"],
            "warnings": ["No peer fixture facts available"],
        }

