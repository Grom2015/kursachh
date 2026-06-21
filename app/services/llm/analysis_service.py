"""LLM Analysis Service — turns structured data into analytical reports."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.services.llm.client import LLMClient, LLMResponse
from app.services.llm.prompts import (
    detailed_memo_prompt,
    extend_analysis_prompt,
    fundamental_analysis_prompt,
    overall_summary_prompt,
    peer_analysis_prompt,
    site_summary_json_prompt,
    technical_analysis_prompt,
)

logger = logging.getLogger(__name__)


@dataclass
class LLMAnalysisReport:
    """Container for all LLM-generated report sections."""

    fundamental_note: str | None = None
    technical_note: str | None = None
    peer_note: str | None = None
    overall_summary: str | None = None
    recommendation: str | None = None
    full_markdown: str | None = None
    structured_summary: dict[str, Any] = field(default_factory=dict)
    memo_markdown: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    llm_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fundamental_note": self.fundamental_note,
            "technical_note": self.technical_note,
            "peer_note": self.peer_note,
            "overall_summary": self.overall_summary,
            "recommendation": self.recommendation,
            "full_markdown": self.full_markdown,
            "structured_summary": self.structured_summary,
            "memo_markdown": self.memo_markdown,
            "token_usage": self.token_usage,
            "warnings": self.warnings,
            "llm_model": self.llm_model,
        }


@dataclass
class LLMExtendReport:
    """Container for the extended-analysis LLM output."""

    extended_note: str | None = None
    recommendation: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    llm_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "extended_note": self.extended_note,
            "recommendation": self.recommendation,
            "token_usage": self.token_usage,
            "warnings": self.warnings,
            "llm_model": self.llm_model,
        }


class LLMAnalysisService:
    """Orchestrates LLM calls for the analysis pipeline."""

    def __init__(self, client: LLMClient):
        self.client = client
        self._total_input = 0
        self._total_output = 0

    def generate_full_report(
        self,
        *,
        company: dict,
        period: dict,
        financial_metrics: list[dict],
        structured_facts: list[dict] | None = None,
        derived_safe_facts: list[dict] | None = None,
        analysis_readiness_summary: dict[str, Any] | None = None,
        top_blockers: list[str] | None = None,
        unresolved_evidence_summary: dict[str, Any] | None = None,
        parser_risk_summary: dict[str, Any] | None = None,
        market_analysis: dict,
        peer_analysis: dict,
        source_documents: Any,
        data_quality: dict,
        warnings: list[str],
    ) -> LLMAnalysisReport:
        """Run all LLM stages and return the assembled report."""

        report = LLMAnalysisReport()

        if not self.client.available:
            report.warnings.append(
                "ANTHROPIC_API_KEY not configured - LLM analysis skipped"
            )
            logger.info("LLM analysis skipped: API key not available")
            return report

        self._total_input = 0
        self._total_output = 0

        try:
            report.fundamental_note = self._fundamental(
                company=company,
                period=period,
                metrics=financial_metrics,
                structured_facts=structured_facts or [],
                derived_safe_facts=derived_safe_facts or [],
                analysis_readiness_summary=analysis_readiness_summary or {},
                top_blockers=top_blockers or [],
                unresolved_evidence_summary=unresolved_evidence_summary or {},
                parser_risk_summary=parser_risk_summary or {},
                docs=source_documents,
                quality=data_quality,
                warns=warnings,
            )
        except Exception as exc:
            msg = f"LLM fundamental analysis failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        try:
            report.technical_note = self._technical(
                company=company,
                period=period,
                market=market_analysis,
                warns=warnings,
            )
        except Exception as exc:
            msg = f"LLM technical analysis failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        peer_table = peer_analysis.get("peer_table", [])
        if peer_table:
            try:
                report.peer_note = self._peer(
                    company=company,
                    period=period,
                    peer=peer_analysis,
                    warns=warnings,
                )
            except Exception as exc:
                msg = f"LLM peer analysis failed: {exc}"
                logger.warning(msg)
                report.warnings.append(msg)
        else:
            report.peer_note = None
            report.warnings.append("Peer data unavailable - peer LLM analysis skipped")

        try:
            report.overall_summary = self._overall(
                company=company,
                period=period,
                fund_note=report.fundamental_note or "(фундаментальный анализ недоступен)",
                tech_note=report.technical_note or "(технический анализ недоступен)",
                peer_note=report.peer_note or "(peer-анализ недоступен)",
                docs=source_documents,
                quality=data_quality,
                warns=warnings,
            )
        except Exception as exc:
            msg = f"LLM overall summary failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        try:
            report.structured_summary = self._site_summary(
                company=company,
                period=period,
                fund_note=report.fundamental_note or "",
                tech_note=report.technical_note or "",
                peer_note=report.peer_note or "",
                quality=data_quality,
                warns=warnings,
            )
        except Exception as exc:
            msg = f"LLM site summary failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        try:
            report.memo_markdown = self._detailed_memo(
                company=company,
                period=period,
                metrics=financial_metrics,
                structured_facts=structured_facts or [],
                derived_safe_facts=derived_safe_facts or [],
                analysis_readiness_summary=analysis_readiness_summary or {},
                top_blockers=top_blockers or [],
                unresolved_evidence_summary=unresolved_evidence_summary or {},
                parser_risk_summary=parser_risk_summary or {},
                market_analysis=market_analysis,
                docs=source_documents,
                quality=data_quality,
                warns=warnings,
            )
        except Exception as exc:
            msg = f"LLM detailed memo failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        report.recommendation = self._extract_recommendation(
            report.overall_summary or report.technical_note or ""
        )
        report.full_markdown = self._assemble_markdown(report, company, period)
        report.token_usage = {
            "total_input_tokens": self._total_input,
            "total_output_tokens": self._total_output,
            "total_tokens": self._total_input + self._total_output,
        }
        report.llm_model = self.client._model
        return report

    def generate_extend_report(
        self,
        *,
        company: dict,
        period: dict,
        previous_summary: str,
        delta_summary: dict,
        new_metrics: list[dict],
        new_market: dict,
        warnings: list[str],
    ) -> LLMExtendReport:
        """Generate an extended analysis comparing old and new data."""

        report = LLMExtendReport()

        if not self.client.available:
            report.warnings.append(
                "ANTHROPIC_API_KEY not configured - LLM extend analysis skipped"
            )
            return report

        self._total_input = 0
        self._total_output = 0

        try:
            system, user = extend_analysis_prompt(
                company=company,
                period=period,
                previous_summary=previous_summary,
                delta_summary=delta_summary,
                new_metrics=new_metrics,
                new_market=new_market,
                warnings=warnings,
            )
            resp = self.client.generate(system=system, user_message=user)
            self._track(resp)
            report.extended_note = resp.text
            report.recommendation = self._extract_recommendation(resp.text)
        except Exception as exc:
            msg = f"LLM extend analysis failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        report.token_usage = {
            "total_input_tokens": self._total_input,
            "total_output_tokens": self._total_output,
            "total_tokens": self._total_input + self._total_output,
        }
        report.llm_model = self.client._model
        return report

    def _fundamental(
        self,
        *,
        company: dict,
        period: dict,
        metrics: list[dict],
        structured_facts: list[dict],
        derived_safe_facts: list[dict],
        analysis_readiness_summary: dict[str, Any],
        top_blockers: list[str],
        unresolved_evidence_summary: dict[str, Any],
        parser_risk_summary: dict[str, Any],
        docs: Any,
        quality: dict,
        warns: list[str],
    ) -> str:
        system, user = fundamental_analysis_prompt(
            company=company,
            period=period,
            financial_metrics=metrics,
            structured_facts=structured_facts,
            derived_safe_facts=derived_safe_facts,
            analysis_readiness_summary=analysis_readiness_summary,
            top_blockers=top_blockers,
            unresolved_evidence_summary=unresolved_evidence_summary,
            parser_risk_summary=parser_risk_summary,
            source_documents=docs,
            data_quality=quality,
            warnings=warns,
        )
        resp = self.client.generate(
            system=system,
            user_message=user,
            pdf_attachments=self._pdf_attachments_from_source_documents(docs),
            max_tokens=2200,
        )
        self._track(resp)
        return resp.text

    def _technical(self, *, company: dict, period: dict, market: dict, warns: list[str]) -> str:
        system, user = technical_analysis_prompt(
            company=company,
            period=period,
            market_analysis=market,
            warnings=warns,
        )
        resp = self.client.generate(system=system, user_message=user, max_tokens=1200)
        self._track(resp)
        return resp.text

    def _peer(self, *, company: dict, period: dict, peer: dict, warns: list[str]) -> str:
        system, user = peer_analysis_prompt(
            company=company,
            period=period,
            peer_analysis=peer,
            warnings=warns,
        )
        resp = self.client.generate(system=system, user_message=user, max_tokens=1200)
        self._track(resp)
        return resp.text

    def _overall(
        self,
        *,
        company: dict,
        period: dict,
        fund_note: str,
        tech_note: str,
        peer_note: str,
        docs: Any,
        quality: dict,
        warns: list[str],
    ) -> str:
        system, user = overall_summary_prompt(
            company=company,
            period=period,
            fundamental_note=fund_note,
            technical_note=tech_note,
            peer_note=peer_note,
            source_documents=docs,
            data_quality=quality,
            warnings=warns,
        )
        resp = self.client.generate(
            system=system,
            user_message=user,
            pdf_attachments=self._pdf_attachments_from_source_documents(docs),
            max_tokens=1600,
        )
        self._track(resp)
        return resp.text

    def _site_summary(
        self,
        *,
        company: dict,
        period: dict,
        fund_note: str,
        tech_note: str,
        peer_note: str,
        quality: dict,
        warns: list[str],
    ) -> dict[str, Any]:
        system, user = site_summary_json_prompt(
            company=company,
            period=period,
            fundamental_note=fund_note,
            technical_note=tech_note,
            peer_note=peer_note,
            data_quality=quality,
            warnings=warns,
        )
        resp = self.client.generate(system=system, user_message=user, max_tokens=1400)
        self._track(resp)
        parsed = resp.extract_json() or {}
        return self._normalize_structured_summary(parsed)

    def _detailed_memo(
        self,
        *,
        company: dict,
        period: dict,
        metrics: list[dict],
        structured_facts: list[dict],
        derived_safe_facts: list[dict],
        analysis_readiness_summary: dict[str, Any],
        top_blockers: list[str],
        unresolved_evidence_summary: dict[str, Any],
        parser_risk_summary: dict[str, Any],
        market_analysis: dict[str, Any],
        docs: Any,
        quality: dict,
        warns: list[str],
    ) -> str:
        system, user = detailed_memo_prompt(
            company=company,
            period=period,
            financial_metrics=metrics,
            structured_facts=structured_facts,
            derived_safe_facts=derived_safe_facts,
            analysis_readiness_summary=analysis_readiness_summary,
            top_blockers=top_blockers,
            unresolved_evidence_summary=unresolved_evidence_summary,
            parser_risk_summary=parser_risk_summary,
            market_analysis=market_analysis,
            source_documents=docs,
            data_quality=quality,
            warnings=warns,
        )
        resp = self.client.generate(
            system=system,
            user_message=user,
            pdf_attachments=self._pdf_attachments_from_source_documents(docs),
            max_tokens=5000,
        )
        self._track(resp)
        return resp.text

    def _track(self, resp: LLMResponse) -> None:
        self._total_input += resp.input_tokens
        self._total_output += resp.output_tokens
        logger.info(
            "LLM call: model=%s, in=%d, out=%d",
            resp.model,
            resp.input_tokens,
            resp.output_tokens,
        )

    @staticmethod
    def _normalize_structured_summary(data: dict[str, Any]) -> dict[str, Any]:
        def _string_list(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            out: list[str] = []
            for item in value:
                text = str(item or "").strip()
                if text:
                    out.append(text)
            return out[:6]

        metrics: list[dict[str, str]] = []
        raw_metrics = data.get("important_metrics")
        if isinstance(raw_metrics, list):
            for item in raw_metrics[:8]:
                if not isinstance(item, dict):
                    continue
                metrics.append(
                    {
                        "label": str(item.get("label") or "").strip(),
                        "value": str(item.get("value") or "").strip(),
                        "comment": str(item.get("comment") or "").strip(),
                    }
                )

        recommendation = str(data.get("recommendation") or "N/A").upper().strip()
        if recommendation not in {"BUY", "HOLD", "SELL", "N/A"}:
            recommendation = "N/A"
        status = str(data.get("status") or "mixed").lower().strip()
        if status not in {"strong", "mixed", "weak"}:
            status = "mixed"

        return {
            "status": status,
            "recommendation": recommendation,
            "investment_signal": str(data.get("investment_signal") or "").strip(),
            "executive_summary": _string_list(data.get("executive_summary")),
            "key_strengths": _string_list(data.get("key_strengths")),
            "key_risks": _string_list(data.get("key_risks")),
            "watch_items": _string_list(data.get("watch_items")),
            "important_metrics": metrics,
            "data_quality_note": str(data.get("data_quality_note") or "").strip(),
        }

    @staticmethod
    def _pdf_attachments_from_source_documents(source_documents: Any) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        if isinstance(source_documents, dict):
            candidates = source_documents.get("source_pdf_attachments") or []
            if isinstance(candidates, list):
                attachments.extend(item for item in candidates if isinstance(item, dict))
            manual_pdf = (source_documents.get("manual_upload") or {}).get("source_pdf")
            if isinstance(manual_pdf, dict):
                attachments.append(manual_pdf)
            return LLMAnalysisService._dedupe_pdf_attachments(attachments)

        if isinstance(source_documents, list):
            for document in source_documents:
                if not isinstance(document, dict):
                    continue
                path = (
                    document.get("storage_path")
                    or document.get("stored_document_path")
                    or document.get("path")
                )
                if not path:
                    continue
                attachments.append(
                    {
                        "path": path,
                        "source_document_id": document.get("id")
                        or document.get("report_document_id"),
                        "file_name": document.get("file_name"),
                    }
                )
        return LLMAnalysisService._dedupe_pdf_attachments(attachments)

    @staticmethod
    def _dedupe_pdf_attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any, Any]] = set()
        for item in items:
            key = (item.get("path"), item.get("source_document_id"), item.get("file_name"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _extract_recommendation(text: str) -> str:
        """Try to extract BUY/HOLD/SELL from an LLM response."""

        upper = text.upper()
        for keyword in ("BUY", "SELL", "HOLD"):
            patterns = [
                f"РЕКОМЕНДАЦИЯ: {keyword}",
                f"РЕКОМЕНДАЦИЯ — {keyword}",
                f"РЕКОМЕНДАЦИЯ: **{keyword}**",
                f"RECOMMENDATION: {keyword}",
                f"**{keyword}**",
            ]
            for pattern in patterns:
                if pattern in upper:
                    return keyword

        for keyword in ("BUY", "SELL"):
            if keyword in upper:
                return keyword
        if "HOLD" in upper:
            return "HOLD"
        return "N/A"

    @staticmethod
    def _assemble_markdown(
        report: LLMAnalysisReport, company: dict, period: dict
    ) -> str:
        """Concatenate all sections into one Markdown document."""

        parts: list[str] = []
        ticker = company.get("ticker", "N/A")
        name = company.get("short_name") or company.get("name") or ticker
        pfrom = period.get("from", "")
        pto = period.get("to", "")

        parts.append(
            f"# Аналитический отчёт: {name} ({ticker})\n"
            f"**Период:** {pfrom} — {pto}\n"
        )

        if report.fundamental_note:
            parts.append(f"---\n## Фундаментальный анализ\n\n{report.fundamental_note}")
        if report.technical_note:
            parts.append(f"---\n## Технический анализ\n\n{report.technical_note}")
        if report.peer_note:
            parts.append(f"---\n## Сравнительный анализ (Peer Analysis)\n\n{report.peer_note}")
        if report.overall_summary:
            parts.append(f"---\n## Консолидированный вывод\n\n{report.overall_summary}")
        if report.recommendation and report.recommendation != "N/A":
            parts.append(
                f"---\n### Итоговая рекомендация: **{report.recommendation}**\n"
            )

        parts.append(
            "---\n*Материал носит информационно-аналитический характер и не является "
            "индивидуальной инвестиционной рекомендацией.*\n"
        )
        return "\n\n".join(parts)
