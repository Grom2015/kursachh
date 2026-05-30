import pandas as pd

from app.db.models import ReportDocument, StatementFact
from app.services.parsing.normalizer import normalize_metric_code, normalize_quality_flag


class CSVParser:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def can_parse(self, document: ReportDocument) -> bool:
        return bool(document.storage_path and document.storage_path.lower().endswith(".csv"))

    def parse(self, document: ReportDocument) -> list[StatementFact]:
        df = pd.read_csv(document.storage_path)
        return self.parse_dataframe(document, df)

    def parse_dataframe(self, document: ReportDocument, df: pd.DataFrame) -> list[StatementFact]:
        if "period" in df.columns:
            df = df[df["period"].astype(str) == document.report_period]
        facts: list[StatementFact] = []
        for _, row in df.iterrows():
            source_location = {
                "raw": str(row.get("source_location", "fixture")),
                "source_type": document.source_type,
                "document_id": document.id,
                "source_url": document.source_url,
            }
            facts.append(
                StatementFact(
                    company_id=document.company_id,
                    report_document_id=document.id,
                    period=str(row.get("period", document.report_period)),
                    reporting_standard=str(row.get("reporting_standard", document.reporting_standard)),
                    statement_type=str(row.get("statement_type", "other")),
                    metric_code=normalize_metric_code(str(row.get("metric_code", ""))),
                    metric_name_original=str(row.get("metric_name_original", "")),
                    value=float(row["value"]) if pd.notna(row.get("value")) else None,
                    currency=str(row.get("currency", "RUB")),
                    unit_multiplier=float(row.get("unit_multiplier", 1.0)),
                    period_type=str(row.get("period_type", "quarter")),
                    source_location=source_location,
                    quality_flag=normalize_quality_flag(row.get("quality_flag")),
                    confidence_score=float(row.get("confidence_score", 1.0)),
                )
            )
        return facts
