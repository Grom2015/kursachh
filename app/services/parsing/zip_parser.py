from pathlib import Path
from zipfile import ZipFile

from app.db.models import ReportDocument, StatementFact
from app.services.parsing.audit import write_parse_audit


class ZIPParser:
    def __init__(self, nested_parsers: list | None = None) -> None:
        from app.services.parsing.excel_parser import ExcelParser
        from app.services.parsing.lkoh_ifrs_pdf_parser import LKOHIFRSPDFParser

        self.nested_parsers = nested_parsers or [ExcelParser(), LKOHIFRSPDFParser()]
        self.warnings: list[str] = []

    def can_parse(self, document: ReportDocument) -> bool:
        return bool(document.storage_path and document.storage_path.lower().endswith(".zip"))

    def parse(self, document: ReportDocument) -> list[StatementFact]:
        if not document.storage_path:
            return []
        facts: list[StatementFact] = []
        extract_dir = Path(document.storage_path).with_suffix("")
        try:
            with ZipFile(document.storage_path) as archive:
                for member in archive.infolist():
                    name = Path(member.filename)
                    if name.is_absolute() or ".." in name.parts:
                        self.warnings.append(f"Skipped unsafe zip member: {member.filename}")
                        continue
                    if not member.filename.lower().endswith((".pdf", ".xlsx", ".xls")):
                        continue
                    archive.extract(member, extract_dir)
                    nested = self._proxy_document(document, str(extract_dir / member.filename))
                    parser = next((candidate for candidate in self.nested_parsers if candidate.can_parse(nested)), None)
                    if parser:
                        facts.extend(parser.parse(nested))
                        self.warnings.extend(getattr(parser, "warnings", []))
        except Exception as exc:
            self.warnings.append(f"ZIP parsing failed for document {document.id}: {exc}")
        write_parse_audit(document, self.__class__.__name__, 0, facts, self.warnings)
        return facts

    def _proxy_document(self, document: ReportDocument, storage_path: str) -> ReportDocument:
        document.storage_path = storage_path
        return document

