import json
from pathlib import Path

from app.db.models import Company, MetricValue, ReportDocument, StatementFact
from app.tools import calculate_financial_ratios as cli


class SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return None


def _seed_fact(db_session):
    company = Company(ticker="CLI", board="TQBR", short_name="CLI", full_name="CLI")
    db_session.add(company)
    db_session.commit()
    doc = ReportDocument(
        company_id=company.id,
        report_period="2021Q4",
        reporting_standard="IFRS",
        document_type="financial_statement",
        source_role="financial_statements",
        source_type="issuer_ir_manifest",
        storage_path="dummy.pdf",
        status="parsed",
    )
    db_session.add(doc)
    db_session.commit()
    for code, value in [("revenue", 100.0), ("net_income", 10.0)]:
        db_session.add(
            StatementFact(
                company_id=company.id,
                report_document_id=doc.id,
                period="2021Q4",
                reporting_standard="IFRS",
                statement_type="income_statement",
                metric_code=code,
                value=value,
                period_type="annual",
                unit_multiplier=1.0,
                source_location={"raw_label": code},
                quality_flag="exact",
            )
        )
    db_session.commit()
    return company


def test_cli_writes_json_without_db_mutation(monkeypatch, db_session):
    _seed_fact(db_session)
    before_facts = db_session.query(StatementFact).count()
    before_metrics = db_session.query(MetricValue).count()
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(db_session))
    original_init = cli.FinancialRatiosCalculator.__init__
    root = Path("tests/runtime_financial_ratios_cli")

    def patched_init(self, db=None, root=None):
        original_init(self, db=db, root=root or root_path)

    root_path = root
    monkeypatch.setattr(cli.FinancialRatiosCalculator, "__init__", patched_init)

    report, path = cli.calculate_financial_ratios("CLI", "2021Q4", "2021Q4")

    assert Path(path).exists()
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert saved["company_ticker"] == "CLI"
    assert report["summary"]["calculated_count"] >= 1
    assert db_session.query(StatementFact).count() == before_facts
    assert db_session.query(MetricValue).count() == before_metrics


def test_cli_main_json_only(monkeypatch, db_session, capsys):
    _seed_fact(db_session)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(db_session))
    original_init = cli.FinancialRatiosCalculator.__init__
    root = Path("tests/runtime_financial_ratios_cli")

    def patched_init(self, db=None, root=None):
        original_init(self, db=db, root=root or root_path)

    root_path = root
    monkeypatch.setattr(cli.FinancialRatiosCalculator, "__init__", patched_init)

    exit_code = cli.main(["CLI", "2021Q4", "2021Q4", "--json-only"])

    assert exit_code == 0
    assert "financial_ratios.json" in capsys.readouterr().out


def teardown_module():
    import shutil

    shutil.rmtree("tests/runtime_financial_ratios_cli", ignore_errors=True)
