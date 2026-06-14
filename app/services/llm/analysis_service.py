"""LLM Analysis Service — turns structured data into analytical reports.

This is the core of Developer 2's contribution.  It takes the structured
JSON output from Developer 1's pipeline and calls Claude to produce:

* Fundamental analysis note
* Technical analysis note
* Peer comparison note
* Consolidated summary with investment recommendation
* Extended (delta) analysis for the "extend" workflow

When the API key is missing the service returns ``None`` for every section
so the rest of the pipeline continues to work in data-only mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.services.llm.client import LLMClient, LLMResponse
from app.services.llm.prompts import (
    extend_analysis_prompt,
    fundamental_analysis_prompt,
    overall_summary_prompt,
    peer_analysis_prompt,
    technical_analysis_prompt,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result data-classes
# ---------------------------------------------------------------------------

@dataclass
class LLMAnalysisReport:
    """Container for all LLM-generated report sections."""

    fundamental_note: str | None = None
    technical_note: str | None = None
    peer_note: str | None = None
    overall_summary: str | None = None
    recommendation: str | None = None  # BUY / HOLD / SELL / N/A
    full_markdown: str | None = None
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
            "token_usage": self.token_usage,
            "warnings": self.warnings,
            "llm_model": self.llm_model,
        }


@dataclass
class LLMExtendReport:
    """Container for the "extend previous analytics" LLM output."""

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


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class LLMAnalysisService:
    """Orchestrates LLM calls for the analysis pipeline."""

    def __init__(self, client: LLMClient):
        self.client = client
        self._total_input = 0
        self._total_output = 0

    # -- public entry point -------------------------------------------------

    def generate_full_report(
        self,
        *,
        company: dict,
        period: dict,
        financial_metrics: list[dict],
        market_analysis: dict,
        peer_analysis: dict,
        source_documents: list[dict],
        data_quality: dict,
        warnings: list[str],
    ) -> LLMAnalysisReport:
        """Run all four LLM stages and return the assembled report."""

        report = LLMAnalysisReport()

        if not self.client.available:
            report.warnings.append(
                "ANTHROPIC_API_KEY not configured — LLM analysis skipped"
            )
            logger.info("LLM analysis skipped: API key not available")
            return report

        self._total_input = 0
        self._total_output = 0

        # --- Stage 1: Fundamental ---
        try:
            report.fundamental_note = self._fundamental(
                company, period, financial_metrics,
                source_documents, data_quality, warnings,
            )
        except Exception as exc:
            msg = f"LLM fundamental analysis failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        # --- Stage 2: Technical ---
        try:
            report.technical_note = self._technical(
                company, period, market_analysis, warnings,
            )
        except Exception as exc:
            msg = f"LLM technical analysis failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        # --- Stage 3: Peer ---
        peer_table = peer_analysis.get("peer_table", [])
        if peer_table:
            try:
                report.peer_note = self._peer(
                    company, period, peer_analysis, warnings,
                )
            except Exception as exc:
                msg = f"LLM peer analysis failed: {exc}"
                logger.warning(msg)
                report.warnings.append(msg)
        else:
            report.peer_note = None
            report.warnings.append("Peer data unavailable — peer LLM analysis skipped")

        # --- Stage 4: Overall summary ---
        try:
            report.overall_summary = self._overall(
                company, period,
                report.fundamental_note or "(фундаментальный анализ недоступен)",
                report.technical_note or "(технический анализ недоступен)",
                report.peer_note or "(peer-анализ недоступен)",
                data_quality, warnings,
            )
        except Exception as exc:
            msg = f"LLM overall summary failed: {exc}"
            logger.warning(msg)
            report.warnings.append(msg)

        # --- Extract recommendation ---
        report.recommendation = self._extract_recommendation(
            report.overall_summary or report.technical_note or ""
        )

        # --- Assemble full markdown ---
        report.full_markdown = self._assemble_markdown(report, company, period)

        report.token_usage = {
            "total_input_tokens": self._total_input,
            "total_output_tokens": self._total_output,
            "total_tokens": self._total_input + self._total_output,
        }
        report.llm_model = self.client._model

        return report

    # -- extend entry point -------------------------------------------------

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
                "ANTHROPIC_API_KEY not configured — LLM extend analysis skipped"
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

    # -- individual LLM stages ----------------------------------------------

    def _fundamental(
        self, company, period, metrics, docs, quality, warns,
    ) -> str:
        system, user = fundamental_analysis_prompt(
            company=company,
            period=period,
            financial_metrics=metrics,
            source_documents=docs,
            data_quality=quality,
            warnings=warns,
        )
        resp = self.client.generate(system=system, user_message=user)
        self._track(resp)
        return resp.text

    def _technical(self, company, period, market, warns) -> str:
        system, user = technical_analysis_prompt(
            company=company,
            period=period,
            market_analysis=market,
            warnings=warns,
        )
        resp = self.client.generate(system=system, user_message=user)
        self._track(resp)
        return resp.text

    def _peer(self, company, period, peer, warns) -> str:
        system, user = peer_analysis_prompt(
            company=company,
            period=period,
            peer_analysis=peer,
            warnings=warns,
        )
        resp = self.client.generate(system=system, user_message=user)
        self._track(resp)
        return resp.text

    def _overall(
        self, company, period, fund_note, tech_note, peer_note, quality, warns,
    ) -> str:
        system, user = overall_summary_prompt(
            company=company,
            period=period,
            fundamental_note=fund_note,
            technical_note=tech_note,
            peer_note=peer_note,
            data_quality=quality,
            warnings=warns,
        )
        resp = self.client.generate(system=system, user_message=user, max_tokens=10000)
        self._track(resp)
        return resp.text

    # -- helpers ------------------------------------------------------------

    def _track(self, resp: LLMResponse) -> None:
        self._total_input += resp.input_tokens
        self._total_output += resp.output_tokens
        logger.info(
            "LLM call: model=%s, in=%d, out=%d",
            resp.model, resp.input_tokens, resp.output_tokens,
        )

    @staticmethod
    def _extract_recommendation(text: str) -> str:
        """Try to extract BUY/HOLD/SELL from an LLM response."""
        upper = text.upper()
        # Look for explicit recommendation keywords
        for keyword in ("BUY", "SELL", "HOLD"):
            # Match keyword in typical patterns (preceded by ":" or "—")
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
        # Fallback: simple presence
        for keyword in ("BUY", "SELL"):
            if keyword in upper:
                return keyword
        if "HOLD" in upper:
            return "HOLD"
        return "N/A"

    @staticmethod
    def _assemble_markdown(
        report: LLMAnalysisReport, company: dict, period: dict,
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
