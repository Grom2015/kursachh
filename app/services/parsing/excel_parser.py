import pandas as pd

from app.db.models import ReportDocument, StatementFact
from app.services.parsing.csv_parser import CSVParser


class ExcelParser(CSVParser):
    def can_parse(self, document: ReportDocument) -> bool:
        return bool(document.storage_path and document.storage_path.lower().endswith((".xlsx", ".xls")))

    def parse(self, document: ReportDocument) -> list[StatementFact]:
        df = pd.read_excel(document.storage_path)
        # Keep MVP conservative: Excel must follow the fixture-like tabular schema.
        return self.parse_dataframe(document, df)
