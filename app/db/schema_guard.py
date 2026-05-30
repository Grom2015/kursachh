from sqlalchemy import text
from sqlalchemy.engine import Engine


def ensure_sqlite_compat_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        analysis_job_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(analysis_jobs)")).fetchall()
        }
        if analysis_job_columns and "data_mode" not in analysis_job_columns:
            conn.execute(text("ALTER TABLE analysis_jobs ADD COLUMN data_mode VARCHAR(16) DEFAULT 'fixture'"))
        report_document_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(report_documents)")).fetchall()
        }
        if report_document_columns and "source_role" not in report_document_columns:
            conn.execute(text("ALTER TABLE report_documents ADD COLUMN source_role VARCHAR(64) DEFAULT 'other'"))
        if report_document_columns and "rejection_reason" not in report_document_columns:
            conn.execute(text("ALTER TABLE report_documents ADD COLUMN rejection_reason TEXT"))
        if report_document_columns and "validation_warnings_json" not in report_document_columns:
            conn.execute(text("ALTER TABLE report_documents ADD COLUMN validation_warnings_json JSON DEFAULT '[]'"))
        if report_document_columns and "trust_level" not in report_document_columns:
            conn.execute(text("ALTER TABLE report_documents ADD COLUMN trust_level VARCHAR(64) DEFAULT 'unknown'"))
        if report_document_columns and "manual_upload_reason" not in report_document_columns:
            conn.execute(text("ALTER TABLE report_documents ADD COLUMN manual_upload_reason VARCHAR(64)"))
        if report_document_columns and "source_trust_bucket" not in report_document_columns:
            conn.execute(text("ALTER TABLE report_documents ADD COLUMN source_trust_bucket VARCHAR(64) DEFAULT 'unknown'"))
        if report_document_columns and "official_source_verified" not in report_document_columns:
            conn.execute(text("ALTER TABLE report_documents ADD COLUMN official_source_verified BOOLEAN DEFAULT 0"))
        if report_document_columns and "source_package_ready_contribution" not in report_document_columns:
            conn.execute(text("ALTER TABLE report_documents ADD COLUMN source_package_ready_contribution BOOLEAN DEFAULT 0"))
        company_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(companies)")).fetchall()}
        if company_columns and "identity_status" not in company_columns:
            conn.execute(text("ALTER TABLE companies ADD COLUMN identity_status VARCHAR(64) DEFAULT 'resolved_registry'"))
        if company_columns and "verification_scope" not in company_columns:
            conn.execute(text("ALTER TABLE companies ADD COLUMN verification_scope VARCHAR(64) DEFAULT 'registry'"))
