import asyncio

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AnalysisJob, AnalysisResult, Company, ReportDocument, StatementFact
from app.services.analysis.llm_payload_builder import LLMPayloadBuilder
from app.services.analysis.result_builder import ResultBuilder, document_to_dict, metric_to_dict
from app.services.company_registry import CompanyRegistry
from app.services.market.market_service import MarketService
from app.services.metrics.metric_engine import MetricEngine
from app.services.parsing.csv_parser import CSVParser
from app.services.parsing.excel_parser import ExcelParser
from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser
from app.services.parsing.pdf_parser import PDFParser
from app.services.parsing.reconciliation import SourceReconciler
from app.services.parsing.tatn_ifrs_pdf_parser import TATNIFRSPDFParser
from app.services.parsing.zip_parser import ZIPParser
from app.services.peers.peer_analysis import PeerAnalysisService
from app.services.periods import ensure_period_range, periods_between
from app.services.reports.adapters.lkoh_ir_adapter import LKOHIRAdapter
from app.services.reports.downloader import ReportDownloader
from app.services.reports.manifest import ReportManifest


class AnalysisOrchestrator:
    def __init__(self, db: Session):
        self.db = db

    def run(self, job_id: str, parent_result_id: str | None = None, version: int = 1) -> AnalysisResult | None:
        job = self.db.get(AnalysisJob, job_id)
        if not job:
            return None
        warnings: list[str] = list(job.warnings_json or [])
        try:
            self._stage(job, "resolving_company", 0.05)
            ensure_period_range(job.period_from, job.period_to)
            company = CompanyRegistry(self.db).resolve_one(job.company_query)
            if not company:
                raise ValueError(f"Company not found or ambiguous: {job.company_query}")
            job.company_id = company.id
            self.db.commit()

            self._stage(job, "fetching_reports", 0.15)
            documents, actual_data_mode = self._load_reports(company, job, warnings)
            if any(document.source_type == "fixture" for document in documents):
                warnings.append("Fixture/demo report data is used; numbers are synthetic demo values")

            self._stage(job, "parsing_reports", 0.3)
            facts = self._parse_documents(documents, warnings)
            reconciler = SourceReconciler()
            facts = reconciler.reconcile(facts)
            warnings.extend(reconciler.warnings)
            if not facts:
                warnings.append("No financial facts parsed; metrics will be missing")
            self.db.add_all(facts)
            for doc in documents:
                doc.status = "parsed"
            self.db.commit()

            self._stage(job, "calculating_metrics", 0.45)
            periods = periods_between(job.period_from, job.period_to)
            metrics = MetricEngine(self.db).calculate(
                company.id,
                periods,
                report_document_ids=[document.id for document in documents],
                exclude_fixture=actual_data_mode == "real",
            )
            self.db.commit()

            market_analysis: dict = {
                "candles_summary": {"source_type": "unavailable"},
                "technical_indicators": {},
                "liquidity_metrics": {},
                "valuation_inputs": {},
                "warnings": ["Market data disabled"],
            }
            if job.include_market_data:
                self._stage(job, "loading_market_data", 0.58)
                if actual_data_mode == "real":
                    market_analysis = {
                        "market_data_source": "unavailable",
                        "candles_summary": {"source_type": "unavailable"},
                        "technical_indicators": {},
                        "liquidity_metrics": {
                            "bid_ask_spread": {
                                "status": "missing",
                                "missing_reason": "requires_orderbook_or_bid_ask_data",
                                "warnings": ["Daily candles do not contain bid/ask spread."],
                            }
                        },
                        "valuation_inputs": {
                            "market_cap": {
                                "status": "missing",
                                "missing_reason": "shares_outstanding_or_market_cap_unavailable",
                            },
                            "enterprise_value": {
                                "status": "missing",
                                "missing_reason": "market_cap_and_net_debt_required",
                            },
                            "dividends": {"status": "missing", "missing_reason": "dividend_data_unavailable"},
                        },
                        "warnings": ["Real market data adapter is not enabled; fixture market fallback not used in real mode"],
                    }
                    warnings.extend(market_analysis["warnings"])
                else:
                    market_analysis = MarketService(self.db).analyze_fixture(company)
                    warnings.extend(market_analysis.get("warnings", []))
                    if market_analysis.get("candles_summary", {}).get("source_type") == "fixture":
                        warnings.append("Fixture/demo market candles are used")

            self._stage(job, "calculating_technical_indicators", 0.68)

            peer_analysis = {"peer_table": [], "peer_summary": {}, "warnings": ["Peer analysis disabled"]}
            if job.include_peers and actual_data_mode != "real":
                self._stage(job, "building_peer_analysis", 0.78)
                peer_analysis = PeerAnalysisService(self.db).build(company, job.period_to)
                warnings.extend(peer_analysis.get("warnings", []))
                if any("fixture" in row.get("data_quality_flags", []) for row in peer_analysis.get("peer_table", [])):
                    warnings.append("Fixture/demo peer metrics are used")
            elif job.include_peers:
                peer_analysis = {
                    "peer_table": [],
                    "peer_summary": {"target_company": company.ticker, "peer_count": 0, "available_peer_count": 0},
                    "warnings": ["Fixture peer fallback not used in real mode"],
                }
                warnings.extend(peer_analysis["warnings"])

            self._stage(job, "building_llm_payload", 0.88)
            result_json = ResultBuilder().build(
                company,
                job.period_from,
                job.period_to,
                job.reporting_standard,
                facts,
                metrics,
                documents,
                market_analysis,
                peer_analysis,
                warnings,
                data_mode=actual_data_mode,
            )
            source_documents = [document_to_dict(document) for document in documents]
            metric_items = [metric_to_dict(metric) for metric in metrics]
            llm_payload = LLMPayloadBuilder().build(
                company,
                job.period_from,
                job.period_to,
                job.reporting_standard,
                metric_items,
                market_analysis,
                peer_analysis,
                source_documents,
                warnings,
                data_mode=actual_data_mode,
                data_quality=result_json["data_quality"],
            )

            self._stage(job, "saving_result", 0.95)
            result = AnalysisResult(
                job_id=job.id,
                parent_result_id=parent_result_id,
                version=version,
                user_id=job.user_id,
                company_id=company.id,
                period_from=job.period_from,
                period_to=job.period_to,
                reporting_standard=job.reporting_standard,
                data_snapshot_json={
                    "data_mode": actual_data_mode,
                    "requested_data_mode": job.data_mode,
                    "document_count": len(documents),
                    "real_data_used": any(document.source_type != "fixture" for document in documents),
                    "fixture_data_used": any(document.source_type == "fixture" for document in documents),
                },
                result_json=result_json,
                llm_payload_json=llm_payload,
                warnings_json=warnings,
                disclaimer=get_settings().disclaimer,
            )
            self.db.add(result)
            self.db.flush()
            job.result_id = result.id
            job.warnings_json = warnings
            job.status = "succeeded"
            self._stage(job, "completed", 1.0, commit=False)
            self.db.commit()
            return result
        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            job.warnings_json = warnings
            self.db.commit()
            return None

    def _stage(self, job: AnalysisJob, stage: str, progress: float, commit: bool = True) -> None:
        job.status = "running" if stage != "completed" else "succeeded"
        job.stage = stage
        job.progress = progress
        if commit:
            self.db.commit()

    def _load_reports(self, company: Company, job: AnalysisJob, warnings: list[str]) -> tuple[list[ReportDocument], str]:
        mode = job.data_mode or "fixture"
        if mode in {"real", "auto"}:
            real_documents = self._load_real_reports(company, job, warnings)
            if real_documents:
                self.db.commit()
                return real_documents, "real"
            if mode == "real":
                warnings.append("Real data unavailable; no fixture fallback used.")
                raise ValueError("Real report documents unavailable for requested period")
            warnings.append("Real data unavailable; fixture fallback used.")
        manifest_items = ReportManifest().load_for_company(company.ticker, job.period_from, job.period_to)
        if not manifest_items:
            warnings.append(f"No report manifest entries for {company.ticker}")
        downloader = ReportDownloader(self.db)
        documents = [downloader.register(company, item) for item in manifest_items]
        self.db.commit()
        return documents, "fixture"

    def _load_real_reports(self, company: Company, job: AnalysisJob, warnings: list[str]) -> list[ReportDocument]:
        adapters = [LKOHIRAdapter()]
        discovered = []
        for adapter in adapters:
            if not adapter.supports(company, job.reporting_standard):
                continue
            reports = asyncio.run(
                adapter.discover_reports(company, job.period_from, job.period_to, job.reporting_standard)
            )
            discovered.extend(reports)
            warnings.extend(adapter.warnings)
        if not discovered:
            return []
        downloader = ReportDownloader(self.db)
        documents: list[ReportDocument] = []
        for report in discovered:
            try:
                documents.append(downloader.download(company, report.to_manifest_item()))
            except Exception as exc:
                warnings.append(str(exc))
        return documents

    def _parse_documents(
        self, documents: list[ReportDocument], warnings: list[str]
    ) -> list[StatementFact]:
        parsers = [CSVParser(), ExcelParser(), LKOHIFRSPDFParser(), TATNIFRSPDFParser(), ZIPParser(), PDFParser()]
        facts: list[StatementFact] = []
        for document in documents:
            parser = next((candidate for candidate in parsers if candidate.can_parse(document)), None)
            if not parser:
                warnings.append(f"No parser for document {document.id}")
                continue
            facts.extend(parser.parse(document))
            warnings.extend(getattr(parser, "warnings", []))
        return facts


def run_analysis_job(job_id: str, parent_result_id: str | None = None, version: int = 1) -> None:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        AnalysisOrchestrator(db).run(job_id, parent_result_id=parent_result_id, version=version)
