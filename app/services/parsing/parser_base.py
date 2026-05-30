from abc import ABC, abstractmethod

from app.db.models import ReportDocument, StatementFact


class ReportParser(ABC):
    warnings: list[str]

    @abstractmethod
    def can_parse(self, document: ReportDocument) -> bool:
        raise NotImplementedError

    @abstractmethod
    def parse(self, document: ReportDocument) -> list[StatementFact]:
        raise NotImplementedError

