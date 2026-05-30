from app.db.models import ReportDocument, StatementFact


class PDFParser:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def can_parse(self, document: ReportDocument) -> bool:
        return bool(document.storage_path and document.storage_path.lower().endswith(".pdf"))

    def parse(self, document: ReportDocument) -> list[StatementFact]:
        try:
            import pdfplumber  # type: ignore
        except ImportError:
            self.warnings.append(
                f"PDF parser unavailable for document {document.id}: pdfplumber is not installed"
            )
            return []
        try:
            with pdfplumber.open(document.storage_path) as pdf:
                _ = [page.extract_tables() for page in pdf.pages[:3]]
        except Exception as exc:
            self.warnings.append(f"PDF parsing failed for document {document.id}: {exc}")
        self.warnings.append(
            f"PDF document {document.id} requires manual mapping; no financial facts invented"
        )
        return []

