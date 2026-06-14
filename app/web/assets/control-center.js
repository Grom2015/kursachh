const safeRunForm = document.querySelector("#safe-run-form");
const uploadForm = document.querySelector("#manual-upload-form");
const pickPdfButton = document.querySelector("#pick-pdf-button");
const refreshButton = document.querySelector("#refresh-status");
const overallStatus = document.querySelector("#overall-status");
const demoSummaryTitle = document.querySelector("#demo-summary-title");
const demoSummaryCopy = document.querySelector("#demo-summary-copy");
const demoSummaryBadges = document.querySelector("#demo-summary-badges");
const stageList = document.querySelector("#stage-list");
const eventList = document.querySelector("#event-list");
const whatHappened = document.querySelector("#what-happened");
const nextActions = document.querySelector("#next-actions");
const defenseNotes = document.querySelector("#defense-notes");
const sourceLinks = document.querySelector("#source-links");
const reportList = document.querySelector("#report-list");
const analysisResult = document.querySelector("#analysis-result");
const manualResult = document.querySelector("#manual-result");
const backendBuildBadge = document.querySelector("#backend-build-badge");
let activeJobTimer = null;
let autoDraftRunning = false;
const STORAGE_KEY = "moexPipelineControlCenterState.v2";
const META_APP_VERSION = document.querySelector('meta[name="app-version"]')?.content || "unknown";
const META_BUILD_ID = document.querySelector('meta[name="app-build-id"]')?.content || "unknown";

const STATUS_LABELS = {
  PASS: "УСПЕХ",
  PARTIAL: "ЧАСТИЧНО",
  BLOCKED: "ЗАБЛОКИРОВАНО",
  FAIL: "ОШИБКА",
  FAILED: "ОШИБКА",
  Running: "Выполняется",
  running: "Выполняется",
  completed: "Завершено",
  failed: "Ошибка",
  recommended: "Рекомендация",
};

const STAGE_LABELS = {
  identity_resolution: "Определение компании",
  source_discovery: "Поиск источников",
  report_discovery: "Поиск отчетов",
  download_documents: "Скачивание документов",
  document_validation: "Проверка документа",
  statement_table_extraction: "Извлечение таблиц",
  dataframe_fact_parse: "Поиск фактов",
  financial_ratios: "Финансовые коэффициенты",
  fact_candidate_review: "Проверка фактов",
  fact_candidate_promotion: "Подтверждение фактов",
  market_technical: "Рыночный теханализ",
  peer_analysis: "Сравнение с аналогами",
  llm_payload: "Payload для LLM",
  demo_report: "Демо-отчет",
};

const TEXT_LABELS = {
  generated: "Сформирован отчет",
  "read from replay-cache": "Прочитано из replay-cache",
  "generated from existing ratios": "Сформировано из существующих ratios-отчетов",
  "generated from DataFrame candidates": "Сформировано из DataFrame-кандидатов загруженного отчета",
  "built, LLM not invoked": "Payload собран, LLM не вызывался",
  "checked local registry and source candidates": "Проверен локальный реестр и кандидаты источников",
  "bootstrapped identity from MOEX ISS exact ticker match": "Компания добавлена из точного совпадения тикера MOEX ISS",
  "discovered source candidates": "Найдены кандидаты источников",
  "discovered report candidates": "Найдены кандидаты отчетов",
  "downloaded official report candidates": "Скачаны кандидаты официальных отчетов",
  "not executed": "Не запускалось",
  cache_missing: "Кэш отсутствует",
  run_market_live_explicitly: "Запустите live-загрузку рынка отдельно",
  peer_comparison_not_ready: "Сравнение с аналогами пока не готово",
  company_not_in_registry: "Компания не найдена в локальном реестре",
  add_company_to_registry_or_use_manual_upload: "Добавьте компанию в реестр или используйте ручную загрузку",
  missing_report_candidates: "Не найдены кандидаты отчетов",
  use_manual_upload_or_enable_live_discovery: "Используйте ручную загрузку или включите живой поиск",
  no_documents_downloaded: "Документы не скачаны",
  manual_upload_or_provider_access_check: "Используйте ручную загрузку или проверьте доступ к провайдеру",
  no_cached_or_downloaded_documents: "Нет кэшированных или скачанных документов",
  download_documents_or_manual_upload: "Скачайте документы или используйте ручную загрузку",
  no_documents: "Нет документов",
  official_report_not_found: "Официальный отчет не найден",
  ocr_required: "Нужен OCR или извлечение из изображений",
  manual_upload_endpoint_exception: "Ошибка endpoint ручной загрузки",
  no_calculated_ratios_from_candidates: "Из кандидатов пока не удалось рассчитать коэффициенты",
  upload_better_statement_or_review_candidate_facts: "Загрузите более полный отчет или проверьте найденные кандидаты фактов",
  run_full_pipeline_or_upload_report_with_fact_parser: "Запустите полный пайплайн или загрузите отчет с DataFrame-парсером",
};

const REPORT_LABELS = {
  financial_ratios: "Финансовые коэффициенты",
  market_technical: "Рыночный теханализ",
  peer_analysis: "Сравнение с аналогами",
  llm_payload: "Payload для LLM",
  demo_report: "Демо-отчет",
  machine_report: "Machine report",
};
const LINK_LABELS = {
  "E-Disclosure company card": "Карточка компании",
  "Consolidated financial statements": "МСФО / консолидированная отчетность",
  "E-Disclosure consolidated financial statements": "E-Disclosure: консолидированная отчетность",
  "Annual reports": "Годовая отчетность",
  "E-Disclosure annual reports": "E-Disclosure: годовая отчетность",
  "Accounting financial statements": "РСБУ / бухгалтерская отчетность",
  "E-Disclosure accounting financial statements": "E-Disclosure: бухгалтерская отчетность",
  "Official investor relations": "Официальная страница для инвесторов",
  "E-Disclosure company search": "Поиск компании в E-Disclosure",
  "E-Disclosure portal": "Портал E-Disclosure",
};

const TRUST_LABELS = {
  "regulated disclosure portal": "регулируемый портал раскрытия",
  "regulated disclosure fallback": "регулируемый disclosure-fallback",
  "official issuer": "официальный сайт эмитента",
};

const MANUAL_FIELD_LABELS = {
  status: "Статус",
  ingestion_status: "Статус intake",
  identity_status: "Статус identity",
  document_classification: "Классификация документа",
  period: "Итоговый период",
  original_period: "Исходный период",
  effective_report_period: "Период из документа",
  comparative_period: "Сравнительный период",
  period_source: "Источник периода",
  period_confidence: "Уверенность в периоде",
  period_warnings: "Предупреждения по периоду",
  final_status: "Итоговый auto-parse статус",
  ocr_status: "OCR статус",
  report_document_id: "ID документа",
  duplicate_detected: "Дубликат найден",
  document_validation_status: "Статус проверки документа",
  statement_tables_extracted: "Извлечено таблиц",
  fact_parse_status: "Статус поиска фактов",
  evidence_pack_available: "Evidence pack готов",
  machine_report_available: "Machine report готов",
  structured_facts_count: "Structured facts",
  rejected_rows_count: "Rejected rows",
  unmapped_numeric_evidence_count: "Unmapped numeric evidence",
  unmapped_table_evidence_count: "Unmapped table evidence",
  db_persisted: "Записано в БД",
  source_trust_bucket: "Категория доверия источника",
  official_source_verified: "Официальный источник подтвержден",
  source_package_ready_contribution: "Учитывается в готовности source package",
  machine_report_path: "Machine report",
  identity_report_path: "Identity report",
  recommended_next_action: "Рекомендованный следующий шаг",
  top_structural_blockers: "Top blockers",
  blockers: "Blockers",
  warnings: "Warnings",
};
const KEY_BANK_FACTS = [
  "total_assets",
  "total_liabilities",
  "total_equity",
  "cash_and_equivalents",
  "loans_to_customers",
  "retail_customer_accounts",
  "corporate_customer_accounts",
  "customer_accounts",
  "net_interest_income",
  "operating_income",
  "operating_expenses",
  "profit_before_tax",
  "net_income",
  "operating_cash_flow",
];

const DEMO_CASE_COPY = {
  SBER: {
    title: "SBER: банковский черновой анализ",
    copy:
      "Лучший сценарий для защиты: загрузите консолидированную МСФО-отчетность Сбера и запустите полный черновой анализ.",
    notes: [
      "Сбер показывает банковскую ветку: факты из primary statements, ROE/ROA/cost-to-income и ограничения по недоступным метрикам.",
      "Результат lower-trust, потому что источник — manual upload; facts_persisted=false.",
      "Это сильный пример: система не применяет промышленные коэффициенты к банку.",
    ],
  },
  LKOH: {
    title: "LKOH: готовый industrial pipeline",
    copy:
      "Показывает, что официальный statement pipeline уже умеет работать на industrial-компании и строить ratios JSON.",
    notes: [
      "Лукойл показывает готовый контур: statement facts, ratios, LLM payload и demo report.",
      "Это scoped readiness, а не обещание универсального покрытия всех эмитентов.",
      "Valuation metrics не считаются без отдельного valuation input module.",
    ],
  },
  GAZP: {
    title: "GAZP: честный blocker",
    copy:
      "Показывает безопасность продукта: image-only primary statement pages блокируются, данные не выдумываются.",
    notes: [
      "Газпром нужен для защиты архитектуры: parser не принимает TOC/notes как факты.",
      "Главный blocker — image_only_primary_statement_pages; следующий engineering step — OCR/table-image extraction.",
      "Это не ошибка UX, а честное поведение quality gate.",
    ],
  },
};

function label(value, labels) {
  const text = String(value || "");
  return labels[text] || text;
}

function statusLabel(status) {
  return label(status, STATUS_LABELS);
}

function stageLabel(stage) {
  return label(stage, STAGE_LABELS);
}

function textLabel(value) {
  return label(value, TEXT_LABELS);
}

function metricLabel(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const abs = Math.abs(number);
  if (abs >= 1_000_000_000_000) return `${(number / 1_000_000_000_000).toFixed(2)} трлн`;
  if (abs >= 1_000_000_000) return `${(number / 1_000_000_000).toFixed(2)} млрд`;
  if (abs >= 1_000_000) return `${(number / 1_000_000).toFixed(2)} млн`;
  return number.toLocaleString("ru-RU", {maximumFractionDigits: 4});
}

async function loadJsonReport(path) {
  if (!path) return null;
  const response = await fetch(reportUrl(path));
  if (!response.ok) return null;
  return response.json();
}

function sourcePage(item) {
  const location = item?.source_location || {};
  return location.page_number || location.page || location.source_location?.page || "—";
}

function reportUrl(path) {
  if (!path) return "";
  const normalized = String(path || "").replaceAll("\\", "/").replace(/^\/+/, "");
  return `/reports/artifact/${encodeURI(normalized)}`;
}

function reportLink(path, labelText = "Открыть отчет") {
  const url = reportUrl(path);
  if (!url) return "";
  return `<a class="report-link" href="${url}" target="_blank" rel="noopener noreferrer">${labelText}</a>`;
}

function escapeAttribute(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function formValue(form, name) {
  return new FormData(form).get(name);
}

function normalizePeriod(value, boundary) {
  const period = String(value || "").trim().toUpperCase();
  if (/^\d{4}$/.test(period)) {
    return `${period}${boundary === "to" ? "Q4" : "Q1"}`;
  }
  return period;
}

function currentRequest() {
  const periodFrom = normalizePeriod(formValue(safeRunForm, "period_from") || "2021Q1", "from");
  const periodTo = normalizePeriod(formValue(safeRunForm, "period_to") || "2021Q4", "to");
  if (safeRunForm.period_from.value !== periodFrom) safeRunForm.period_from.value = periodFrom;
  if (safeRunForm.period_to.value !== periodTo) safeRunForm.period_to.value = periodTo;
  return {
    ticker: String(formValue(safeRunForm, "ticker") || "LKOH").toUpperCase(),
    period_from: periodFrom,
    period_to: periodTo,
    reporting_standard: String(formValue(safeRunForm, "reporting_standard") || "IFRS"),
  };
}

function loadState() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch (_error) {
    return {};
  }
}

function saveState(patch) {
  try {
    const current = loadState();
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        ...current,
        ...patch,
        backend_build_id: current.backend_build_id || META_BUILD_ID,
        backend_app_version: current.backend_app_version || META_APP_VERSION,
        updated_at: new Date().toISOString(),
      })
    );
  } catch (_error) {
    // State persistence is a UI convenience only; pipeline safety does not depend on it.
  }
}

function clearUiStateForBackendChange(previousBuildId) {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (_error) {
    // no-op
  }
  stageList.className = "stage-list empty";
  stageList.textContent = "Старый локальный state очищен после смены backend build.";
  eventList.className = "event-list empty";
  eventList.textContent = "Старый журнал очищен после смены backend build.";
  whatHappened.className = "what-happened";
  whatHappened.textContent = `Backend обновился (${previousBuildId || "unknown"} → ${META_BUILD_ID}). Состояние страницы сброшено, чтобы не показывать устаревшие результаты.`;
  nextActions.innerHTML = "";
  manualResult.className = "kv-grid empty";
  manualResult.textContent = "После смены backend build предыдущий результат очищен.";
}

function setField(form, name, value) {
  const field = form.elements[name];
  if (!field || value === undefined || value === null) return;
  if (field.type === "checkbox") {
    field.checked = Boolean(value);
    return;
  }
  field.value = value;
}

function safeFormState() {
  return currentRequest();
}

function uploadFormState() {
  return {
    company_ticker: String(uploadForm.company_ticker.value || "").toUpperCase(),
    period: uploadForm.period.value,
    reporting_standard: uploadForm.reporting_standard.value,
    manual_upload_reason: uploadForm.manual_upload_reason.value,
    run_table_extraction: uploadForm.run_table_extraction.value,
    run_dataframe_fact_parser: uploadForm.run_dataframe_fact_parser.value,
    allow_text_fallback_semantic_gate: uploadForm.allow_text_fallback_semantic_gate.value,
  };
}

function saveFormState() {
  saveState({
    safe_form: safeFormState(),
    upload_form: uploadFormState(),
  });
}

function restoreFormState(state) {
  const safe = state.safe_form || {};
  setField(safeRunForm, "ticker", safe.ticker);
  setField(safeRunForm, "period_from", safe.period_from);
  setField(safeRunForm, "period_to", safe.period_to);
  setField(safeRunForm, "reporting_standard", safe.reporting_standard);

  const upload = state.upload_form || {};
  Object.keys(upload).forEach((key) => setField(uploadForm, key, upload[key]));
}

function restoreLastResult(state) {
  if (hasStaleInvalidPeriodResult(state)) {
    saveState({
      last_job_active: false,
      last_job_id: null,
      last_job_events: null,
      last_full_result: null,
      last_safe_result: null,
    });
    overallStatus.textContent = "Ожидание";
    overallStatus.className = "";
    stageList.className = "stage-list empty";
    stageList.textContent = "Старый результат с ошибочным форматом периода очищен. Запустите пайплайн снова.";
    eventList.className = "event-list empty";
    eventList.textContent = "Задача еще не запущена.";
    whatHappened.className = "what-happened empty";
    whatHappened.textContent = "Введите год или квартальный диапазон и запустите пайплайн.";
    nextActions.innerHTML = "";
    return;
  }
  const result = state.last_full_result || state.last_safe_result;
  if (result) {
    overallStatus.textContent = statusLabel(result.overall_status || result.status || "PARTIAL");
    overallStatus.className = statusClass(result.overall_status || result.status);
    if (result.stages) renderStages(result.stages);
    renderDemoSummary(result);
    renderDefenseNotesForTicker(result.ticker || currentRequest().ticker, result);
  }
  if (state.last_job_events) {
    renderEvents(state.last_job_events);
    renderWhatHappened(state.last_job_events, result);
  }
  if (state.last_manual_result) renderManual(state.last_manual_result);
  if (state.last_draft_result) {
    renderDraftAnalysisResult(state.last_draft_result);
    renderDemoSummary(state.last_draft_result);
    renderDefenseNotesForTicker(state.last_draft_result.ticker || currentRequest().ticker, state.last_draft_result);
  }
}

function hasStaleInvalidPeriodResult(state) {
  const snapshot = JSON.stringify({
    events: state.last_job_events || [],
    full: state.last_full_result || null,
    safe: state.last_safe_result || null,
  });
  return snapshot.includes("Invalid period format");
}

function restoreUiState() {
  const state = loadState();
  if (state.backend_build_id && state.backend_build_id !== META_BUILD_ID) {
    clearUiStateForBackendChange(state.backend_build_id);
    return;
  }
  restoreFormState(state);
  restoreLastResult(state);
  if (state.last_job_id && state.last_job_active) {
    pollJob(state.last_job_id);
  }
}

function statusClass(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized.includes("pass") || normalized.includes("validated_financial_statement")) return "status-pass";
  if (normalized.includes("evidence_only")) return "status-partial";
  if (normalized.includes("partial")) return "status-partial";
  if (normalized.includes("blocked") || normalized.includes("fail") || normalized.includes("invalid_upload")) {
    return "status-blocked";
  }
  return "";
}

function renderStages(stages) {
  if (!stages || stages.length === 0) {
    stageList.className = "stage-list empty";
    stageList.textContent = "Этапы не вернулись.";
    return;
  }
  stageList.className = "stage-list";
  stageList.innerHTML = stages
    .map(
      (stage) => `
        <div class="stage-row">
          <strong>${stageLabel(stage.stage)}</strong>
          <span class="${statusClass(stage.status)}">${statusLabel(stage.status)}</span>
          <div>
            <div>${textLabel(stage.action_taken || "")}</div>
            ${stage.report_path ? `<div class="path">${stage.report_path} ${reportLink(stage.report_path)}</div>` : ""}
            ${stage.reason ? `<div class="reason">${textLabel(stage.reason)}</div>` : ""}
            ${stage.recommended_action ? `<div class="reason">${textLabel(stage.recommended_action)}</div>` : ""}
            ${renderStageSummary(stage)}
          </div>
        </div>
      `
    )
    .join("");
}

function renderStageSummary(stage) {
  const summary = stage.summary || {};
  if (stage.stage === "dataframe_fact_parse") {
    const facts = summary.fact_metric_codes || [];
    const quality = summary.banking_parser_quality || {};
    const engineContribution = summary.engine_contribution_summary || {};
    if (!facts.length && !summary.canonical_fact_candidates) return "";
    return `
      <div class="stage-summary">
        <div>Фактов-кандидатов: ${summary.canonical_fact_candidates ?? "—"}</div>
        ${
          quality.coverage_grade
            ? `<div>Качество банковского парсинга: ${quality.coverage_grade}; баланс: ${quality.primary_balance_sheet_found ? "да" : "нет"}; ОПУ: ${quality.primary_income_statement_found ? "да" : "нет"}; ОДДС: ${quality.primary_cash_flow_found ? "да" : "нет"}; notes использованы: ${quality.notes_used ? "да" : "нет"}</div>`
            : ""
        }
        ${facts.length ? `<div>Ключевые факты: ${facts.filter((item) => KEY_BANK_FACTS.includes(item)).join(", ") || facts.join(", ")}</div>` : ""}
        ${
          engineContribution.merged_fact_count || engineContribution.ocr_only_fact_count || engineContribution.merged_ocr_fact_count
            ? `<div>Engine contribution: merged facts ${engineContribution.merged_fact_count ?? 0}; OCR-only facts ${engineContribution.ocr_only_fact_count ?? 0}; merged OCR facts ${engineContribution.merged_ocr_fact_count ?? 0}</div>`
            : ""
        }
        ${
          (engineContribution.engines_with_evidence_only_contribution || []).length
            ? `<div>Evidence-only engines: ${engineContribution.engines_with_evidence_only_contribution.join(", ")}</div>`
            : ""
        }
      </div>
    `;
  }
  if (stage.stage === "financial_ratios") {
    const calculated = summary.calculated_metrics || [];
    const unavailable = summary.unavailable_metrics_sample || [];
    if (!calculated.length && !unavailable.length) return "";
    return `
      <div class="stage-summary">
        ${calculated.length ? `<div>Рассчитано: ${calculated.map((item) => `${item.metric_code} ${item.display_value || item.value}`).join("; ")}</div>` : ""}
        ${unavailable.length ? `<div>Недоступно: ${unavailable.map((item) => `${item.metric_code} (${textLabel(item.reason || item.status)})`).join("; ")}</div>` : ""}
        ${summary.trust_warning ? `<div class="reason">${textLabel(summary.trust_warning)}</div>` : ""}
      </div>
    `;
  }
  if (stage.stage === "fact_candidate_review") {
    return `
      <div class="stage-summary">
        <div>Кандидатов: ${summary.candidates_count ?? "—"}; можно подтвердить: ${summary.eligible_count ?? "—"}; требует проверки: ${summary.needs_review_count ?? "—"}; заблокировано: ${summary.blocked_count ?? "—"}</div>
        <div class="reason">Факты пока не записаны в БД.</div>
      </div>
    `;
  }
  if (stage.stage === "fact_candidate_promotion") {
    return `
      <div class="stage-summary">
        <div>Подтверждено и записано: ${summary.promoted_count ?? "—"}; пропущено: ${summary.skipped_count ?? "—"}; заблокировано: ${summary.blocked_count ?? "—"}</div>
        <div class="reason">Источник остается manual upload; official_source_verified=false.</div>
      </div>
    `;
  }
  if (stage.stage === "demo_report") {
    return `
      <div class="stage-summary">
        ${summary.markdown_report_path ? `<div>Markdown: ${summary.markdown_report_path} ${reportLink(summary.markdown_report_path)}</div>` : ""}
        ${summary.html_report_path ? `<div>HTML: ${summary.html_report_path} ${reportLink(summary.html_report_path, "Открыть HTML")}</div>` : ""}
      </div>
    `;
  }
  return "";
}

async function renderDraftAnalysisResult(payload) {
  if (!analysisResult || !payload) return;
  const ratiosStage = (payload.stages || []).find((stage) => stage.stage === "financial_ratios");
  const parseStage = (payload.stages || []).find((stage) => stage.stage === "dataframe_fact_parse");
  const quality = parseStage?.summary?.banking_parser_quality || {};
  let parseReport = null;
  let ratiosReport = null;
  try {
    [parseReport, ratiosReport] = await Promise.all([
      loadJsonReport(parseStage?.report_path),
      loadJsonReport(ratiosStage?.report_path),
    ]);
  } catch (error) {
    analysisResult.className = "analysis-result";
    analysisResult.innerHTML = `<div class="notice warning">Не удалось прочитать детали отчетов: ${String(error)}</div>`;
    return;
  }

  const facts = parseReport?.facts || [];
  const keyFacts = facts
    .filter((item) => KEY_BANK_FACTS.includes(item.metric_code))
    .sort((left, right) => {
      const metricOrder = KEY_BANK_FACTS.indexOf(left.metric_code) - KEY_BANK_FACTS.indexOf(right.metric_code);
      if (metricOrder !== 0) return metricOrder;
      return String(right.period || "").localeCompare(String(left.period || ""));
    })
    .slice(0, 18);
  const calculated = (ratiosReport?.metrics || []).filter((item) => item.status === "calculated");
  const unavailable = (ratiosReport?.metrics || []).filter((item) => item.status !== "calculated").slice(0, 12);
  const summary = ratiosReport?.summary || {};

  analysisResult.className = "analysis-result";
  analysisResult.innerHTML = `
    <div class="analysis-hero">
      <div>
        <p class="eyebrow">Черновой результат</p>
        <h3>${payload.ticker || "Компания"} · ${payload.period_from || ""}–${payload.period_to || ""}</h3>
        <p>Система собрала факты из загруженной отчетности, рассчитала доступные коэффициенты и отдельно показала ограничения.</p>
      </div>
      <div class="analysis-score ${statusClass(payload.overall_status)}">
        ${statusLabel(payload.overall_status || "PARTIAL")}
      </div>
    </div>

    <div class="analysis-grid">
      <div class="analysis-metric">
        <span>Фактов найдено</span>
        <strong>${parseStage?.summary?.canonical_fact_candidates ?? facts.length ?? "—"}</strong>
      </div>
      <div class="analysis-metric">
        <span>Коэффициентов рассчитано</span>
        <strong>${summary.calculated_count ?? calculated.length ?? "—"}</strong>
      </div>
      <div class="analysis-metric">
        <span>Недоступно / заблокировано</span>
        <strong>${(summary.missing_count || 0) + (summary.unsupported_count || 0) + (summary.blocked_count || 0)}</strong>
      </div>
      <div class="analysis-metric">
        <span>Качество парсинга</span>
        <strong>${quality.coverage_grade || "—"}</strong>
      </div>
    </div>

    <div class="quality-strip">
      <span>Баланс: ${quality.primary_balance_sheet_found ? "найден" : "не найден"}</span>
      <span>ОПУ: ${quality.primary_income_statement_found ? "найден" : "не найден"}</span>
      <span>ОДДС: ${quality.primary_cash_flow_found ? "найден" : "не найден"}</span>
      <span>Notes использованы: ${quality.notes_used ? "да" : "нет"}</span>
    </div>

    <div class="notice warning">
      Источник: ${payload.safety?.analysis_trust_level || "manual_upload_candidate_based"}.
      Факты в БД не записаны: ${payload.safety?.facts_persisted ? "да" : "нет"}.
      Это черновой анализ, а не official-source результат.
    </div>

    <div class="analysis-section">
      <h3>Ключевые факты из отчетности</h3>
      ${
        keyFacts.length
          ? `<div class="compact-table">
              ${keyFacts
                .map(
                  (item) => `
                    <div class="compact-row">
                      <strong>${metricLabel(item.metric_code)}</strong>
                      <span>${item.period || "—"}</span>
                      <span>${formatValue(item.value)} ${item.currency || ""}</span>
                      <span>стр. ${sourcePage(item)}</span>
                    </div>
                  `
                )
                .join("")}
            </div>`
          : `<div class="empty">Ключевые банковские факты пока не найдены в parse report.</div>`
      }
    </div>

    <div class="analysis-section">
      <h3>Рассчитанные коэффициенты</h3>
      ${
        calculated.length
          ? `<div class="compact-table">
              ${calculated
                .slice(0, 12)
                .map(
                  (item) => `
                    <div class="compact-row">
                      <strong>${item.metric_name || metricLabel(item.metric_code)}</strong>
                      <span>${item.period || "—"}</span>
                      <span>${item.display_value || formatValue(item.value)}</span>
                      <span>${item.trust_warning ? textLabel(item.trust_warning) : "рассчитано"}</span>
                    </div>
                  `
                )
                .join("")}
            </div>`
          : `<div class="empty">Коэффициенты пока не рассчитаны.</div>`
      }
    </div>

    <div class="analysis-section">
      <h3>Что недоступно и почему</h3>
      ${
        unavailable.length
          ? `<div class="compact-table">
              ${unavailable
                .map(
                  (item) => `
                    <div class="compact-row">
                      <strong>${item.metric_name || metricLabel(item.metric_code)}</strong>
                      <span>${item.period || "—"}</span>
                      <span>${statusLabel(item.status)}</span>
                      <span>${textLabel(item.reason || (item.inputs_missing || []).join(", ") || item.status)}</span>
                    </div>
                  `
                )
                .join("")}
            </div>`
          : `<div class="empty">Нет недоступных метрик в текущем ratios report.</div>`
      }
    </div>

    <div class="analysis-section">
      <h3>Отчеты</h3>
      <div class="path">${parseStage?.report_path || "parse report unavailable"} ${reportLink(parseStage?.report_path)}</div>
      <div class="path">${ratiosStage?.report_path || "ratios report unavailable"} ${reportLink(ratiosStage?.report_path)}</div>
    </div>
  `;
}

function renderEvents(events) {
  if (!events || events.length === 0) {
    eventList.className = "event-list empty";
    eventList.textContent = "Ожидаем события...";
    return;
  }
  eventList.className = "event-list";
  eventList.innerHTML = events
    .map((event) => {
      const time = event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : "";
      return `
        <div class="event-row">
          <span class="path">${time}</span>
          <strong>${stageLabel(event.stage)}</strong>
          <span class="${statusClass(event.status)}">${statusLabel(event.status)}</span>
          <div>
            <div>${textLabel(event.message || "")}</div>
            ${event.reason ? `<div class="reason">${textLabel(event.reason)}</div>` : ""}
            ${event.recommended_action ? `<div class="reason">${textLabel(event.recommended_action)}</div>` : ""}
          </div>
        </div>
      `;
    })
    .join("");
}

function renderWhatHappened(events, result) {
  const blockers = (events || []).filter((event) => ["BLOCKED", "recommended"].includes(event.status));
  if (blockers.length === 0) {
    whatHappened.className = "what-happened";
    whatHappened.textContent = result
      ? "Пайплайн завершился без жестких блокеров."
      : "Пайплайн выполняется.";
    nextActions.innerHTML = "";
    return;
  }
  const last = blockers[blockers.length - 1];
  whatHappened.className = "what-happened";
  whatHappened.textContent = `${stageLabel(last.stage)}: ${textLabel(last.reason || last.message)}`;
  const actions = Array.from(new Set(blockers.map((event) => event.recommended_action).filter(Boolean)));
  nextActions.innerHTML = actions.map((action) => `<button type="button">${textLabel(action)}</button>`).join("");
}

function renderDemoSummary(payload) {
  const status = payload?.overall_status || payload?.status || "PARTIAL";
  const stages = payload?.stages || [];
  const passed = stages.filter((stage) => stage.status === "PASS").length;
  const partial = stages.filter((stage) => stage.status === "PARTIAL").length;
  const blocked = stages.filter((stage) => stage.status === "BLOCKED").length;
  demoSummaryTitle.textContent = `Статус демо: ${statusLabel(status)}`;
  demoSummaryTitle.className = statusClass(status);
  demoSummaryCopy.textContent =
    blocked > 0
      ? "Часть этапов заблокирована, но это ожидаемо для честного demo: система показывает причину и следующий шаг."
      : "Pipeline отработал без жестких blocker-ов; можно показывать результат и открывать JSON/Markdown отчеты.";
  demoSummaryBadges.innerHTML = `
    <span>Успешно: ${passed}</span>
    <span>Частично: ${partial}</span>
    <span>Заблокировано: ${blocked}</span>
    <span>Факты не персистятся по умолчанию</span>
    <span>LLM не вызывается</span>
  `;
}

function renderDefenseNotesForTicker(ticker, payload = null) {
  const key = String(ticker || "").toUpperCase();
  const preset = DEMO_CASE_COPY[key];
  const reportPaths = Object.values(payload?.report_paths || {}).filter(Boolean);
  const pathLines = reportPaths.length
    ? `<p><strong>Артефакты:</strong> ${reportPaths.map((path) => `${path} ${reportLink(path, "открыть")}`).join("<br>")}</p>`
    : "";
  const notes = preset?.notes || [
    "Показываем report-only pipeline: система строит проверяемые JSON-артефакты, а не скрытый текстовый ответ.",
    "Недостающие данные остаются missing/blocked с причиной.",
    "Доверие к ручной загрузке ниже, чем к verified official source.",
  ];
  defenseNotes.innerHTML = `
    ${notes.map((note) => `<p>${note}</p>`).join("")}
    ${pathLines}
  `;
}

function renderReports(reports) {
  if (!reports || reports.length === 0) {
    reportList.className = "report-list empty";
    reportList.textContent = "Статус отчетов недоступен.";
    return;
  }
  reportList.className = "report-list";
  reportList.innerHTML = reports
    .map(
      (item) => `
        <div class="report-row">
          <strong>${label(item.name, REPORT_LABELS)}</strong>
          <span class="${item.exists ? "status-pass" : "status-blocked"}">${item.exists ? "есть" : "нет"}</span>
          <div class="path">${item.path} ${item.exists ? reportLink(item.path) : ""}</div>
        </div>
      `
    )
    .join("");
}

function renderSourceLinks(payload) {
  if (!payload) {
    sourceLinks.className = "source-links empty";
    sourceLinks.textContent =
      "Не удалось подготовить ссылку на E-Disclosure.";
    return;
  }
  const item = payload.item || {};
  const guidance = payload.manual_upload_guidance || {};
  const links = (guidance.recommended_links || []).filter((link) => link.url);
  const searchTerms = (guidance.search_terms || []).filter(Boolean);
  if (links.length === 0) {
    sourceLinks.className = "source-links empty";
    sourceLinks.textContent =
      "Для этого тикера ссылка на E-Disclosure не сформировалась. Откройте https://www.e-disclosure.ru/ и используйте поиск по компаниям.";
    return;
  }
  sourceLinks.className = "source-links";
  sourceLinks.innerHTML = `
    <div class="source-card">
      <div class="source-title">
        <strong>${item.ticker || payload.ticker || "Тикер"} · ${item.company_name || "поиск через E-Disclosure"}</strong>
        <span>${payload.found ? "найдено в каталоге" : "поиск вне каталога"}</span>
      </div>
      <div class="fineprint">
        Откройте E-Disclosure, найдите компанию, скачайте официальный PDF/XLSX/ZIP с отчетностью
        и загрузите файл через форму ручной загрузки.
      </div>
      ${
        searchTerms.length
          ? `<div class="trust-note">При клике первый поисковый запрос будет скопирован: ${searchTerms.join(", ")}</div>`
          : ""
      }
      <div class="source-link-list">
        ${links
          .map(
            (link) => `
              <a href="${link.url}" target="_blank" rel="noopener noreferrer"
                data-search-term="${escapeAttribute(searchTerms[0] || "")}">
                <span>${label(link.label, LINK_LABELS)}</span>
                <small>${label(link.trust_level, TRUST_LABELS)}</small>
              </a>
            `
          )
          .join("")}
      </div>
      <div class="trust-note">
        Ручная загрузка остается источником с более низким доверием:
        source_trust_bucket=${guidance.trust_boundary?.source_trust_bucket_after_validation || "manual_upload_validated"},
        official_source_verified=false.
      </div>
    </div>
  `;
}

async function loadSourceLinks() {
  const {ticker} = currentRequest();
  try {
    const response = await fetch(`/report-sources/${encodeURIComponent(ticker)}`);
    if (!response.ok) {
      const errorPayload = await response.json();
      renderSourceLinks(errorPayload.detail || {found: false, ticker});
      return;
    }
    renderSourceLinks(await response.json());
  } catch (error) {
    sourceLinks.className = "source-links empty";
    sourceLinks.textContent = `Не удалось загрузить ссылки на отчетность: ${error}`;
  }
}

sourceLinks.addEventListener("click", async (event) => {
  const anchor = event.target.closest("a[data-search-term]");
  if (!anchor) return;
  const term = anchor.dataset.searchTerm;
  if (!term || !navigator.clipboard) return;
  try {
    await navigator.clipboard.writeText(term);
  } catch (_error) {
    // If clipboard access is blocked, the term remains visible in the UI.
  }
});

function renderManual(payload) {
  const fields = [
    ["status", payload.status],
    ["ingestion_status", payload.ingestion_status],
    ["identity_status", payload.identity_status],
    ["document_classification", payload.document_classification],
    ["period", payload.period],
    ["original_period", payload.original_period],
    ["effective_report_period", payload.effective_report_period],
    ["comparative_period", payload.comparative_period],
    ["period_source", payload.period_source],
    ["period_confidence", payload.period_confidence],
    ["final_status", payload.final_status],
    ["ocr_status", payload.ocr_status],
    ["degraded_primary_statements_found", payload.degraded_primary_statements_found],
    ["report_document_id", payload.report_document_id],
    ["duplicate_detected", payload.duplicate_detected],
    ["document_validation_status", payload.document_validation_status],
    ["statement_tables_extracted", payload.statement_tables_extracted],
    ["fact_parse_status", payload.fact_parse_status],
    ["evidence_pack_available", payload.evidence_pack_available],
    ["machine_report_available", payload.machine_report_available],
    ["structured_facts_count", payload.structured_facts_count],
    ["rejected_rows_count", payload.rejected_rows_count],
    ["unmapped_numeric_evidence_count", payload.unmapped_numeric_evidence_count],
    ["unmapped_table_evidence_count", payload.unmapped_table_evidence_count],
    ["db_persisted", payload.db_persisted],
    ["source_trust_bucket", payload.source_trust_bucket],
    ["official_source_verified", payload.official_source_verified],
    ["source_package_ready_contribution", payload.source_package_ready_contribution],
  ];
  if (payload.identity_report_path) fields.push(["identity_report_path", payload.identity_report_path]);
  if (payload.machine_report_path) {
    fields.push(["machine_report_path", `${payload.machine_report_path} ${reportLink(payload.machine_report_path, "открыть")}`]);
  }
  if (payload.recommended_next_action) fields.push(["recommended_next_action", payload.recommended_next_action]);
  if (payload.error_report_path) fields.push(["error_report_path", payload.error_report_path]);
  if (payload.period_warnings?.length) fields.push(["period_warnings", payload.period_warnings.join(", ")]);
  if (payload.top_structural_blockers?.length) {
    fields.push(["top_structural_blockers", payload.top_structural_blockers.join(", ")]);
  }
  if (payload.blockers?.length) fields.push(["blockers", payload.blockers.map(textLabel).join(", ")]);
  if (payload.warnings?.length) fields.push(["warnings", payload.warnings.join(", ")]);
  manualResult.className = "kv-grid";
  manualResult.innerHTML = fields
    .map(([key, value]) => `<div class="kv-row"><strong>${label(key, MANUAL_FIELD_LABELS)}</strong><span>${value}</span></div>`)
    .join("");
}

async function refreshStatus() {
  saveFormState();
  const request = currentRequest();
  const [statusResponse, healthResponse] = await Promise.all([
    fetch(`/pipeline/status?${new URLSearchParams(request)}`),
    fetch("/health"),
  ]);
  const payload = await statusResponse.json();
  const healthPayload = await healthResponse.json();
  renderReports(payload.reports);
  renderBackendBuildInfo(healthPayload);
  await loadSourceLinks();
}

function renderBackendBuildInfo(healthPayload) {
  if (!backendBuildBadge || !healthPayload) return;
  const version = healthPayload.app_version || META_APP_VERSION;
  const buildId = healthPayload.build_id || META_BUILD_ID;
  const pid = healthPayload.process_id || "unknown";
  backendBuildBadge.textContent = `Backend build: ${version} / ${buildId} / PID ${pid}`;
  saveState({
    backend_build_id: buildId,
    backend_app_version: version,
  });
}

safeRunForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pickPdfButton) pickPdfButton.click();
});

async function runAutomaticDraftAnalysis(requestOverride = null) {
  const request = requestOverride || currentRequest();
  saveFormState();
  autoDraftRunning = true;
  overallStatus.textContent = "Выполняется черновой анализ";
  const response = await fetch("/pipeline/draft-run", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(request),
  });
  const payload = await response.json();
  overallStatus.textContent = statusLabel(payload.overall_status || "Неизвестно");
  overallStatus.className = statusClass(payload.overall_status);
  renderStages(payload.stages);
  renderWhatHappened([], payload);
  renderDemoSummary(payload);
  renderDefenseNotesForTicker(payload.ticker || request.ticker || currentRequest().ticker, payload);
  await renderDraftAnalysisResult(payload);
  saveState({last_draft_result: payload, last_job_active: false});
  await refreshStatus();
  autoDraftRunning = false;
}

async function pollJob(jobId) {
  if (activeJobTimer) clearInterval(activeJobTimer);
  const tick = async () => {
    const response = await fetch(`/pipeline/jobs/${jobId}/events`);
    const payload = await response.json();
    overallStatus.textContent = statusLabel(payload.overall_status || payload.status || "Выполняется");
    overallStatus.className = statusClass(payload.overall_status || payload.status);
    renderEvents(payload.events);
    renderWhatHappened(payload.events, payload.result);
    if (payload.result && payload.result.stages) {
      renderStages(payload.result.stages);
      renderDemoSummary(payload.result);
      renderDefenseNotesForTicker(payload.result.ticker || currentRequest().ticker, payload.result);
    }
    saveState({
      last_job_id: jobId,
      last_job_active: !["completed", "failed"].includes(payload.status),
      last_job_events: payload.events || [],
      last_full_result: payload.result || null,
    });
    if (["completed", "failed"].includes(payload.status)) {
      clearInterval(activeJobTimer);
      activeJobTimer = null;
      await refreshStatus();
    }
  };
  await tick();
  activeJobTimer = setInterval(tick, 1000);
}

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveFormState();
  const data = new FormData(uploadForm);
  data.set("company_ticker", String(safeRunForm.ticker.value || "").toUpperCase());
  data.set("reporting_standard", safeRunForm.reporting_standard.value || "IFRS");
  data.set("manual_upload_reason", "user_requested");
  data.set("run_table_extraction", "true");
  data.set("run_dataframe_fact_parser", "true");
  data.set("allow_text_fallback_semantic_gate", "true");
  manualResult.className = "kv-grid empty";
  manualResult.textContent = "Загружаем...";
  overallStatus.textContent = "Загружаем отчет";
  overallStatus.className = "";
  try {
    const response = await fetch("/reports/manual-upload", {
      method: "POST",
      body: data,
    });
    const payload = await response.json();
    if (!response.ok) {
      payload.status = payload.status || "UPLOAD_FAILED";
      payload.blockers = payload.blockers || [payload.detail || "manual_upload_failed"];
    }
    renderManual(payload);
    saveState({last_manual_result: payload, upload_form: uploadFormState()});
    if (payload.period) {
      const range = periodRangeFromUploadedPeriod(payload.period);
      safeRunForm.period_from.value = range.period_from;
      safeRunForm.period_to.value = range.period_to;
      uploadForm.period.value = payload.period;
    }
    await refreshStatus();
    if (
      response.ok &&
      (payload.document_validation_status === "pass" || payload.ingestion_status === "EVIDENCE_ONLY") &&
      !autoDraftRunning
    ) {
      await runAutomaticDraftAnalysis(currentRequest());
    } else if (payload.ingestion_status || payload.status) {
      overallStatus.textContent = statusLabel(payload.ingestion_status || payload.status);
      overallStatus.className =
        String(payload.ingestion_status || payload.status).includes("INVALID")
          || String(payload.ingestion_status || payload.status).includes("BLOCKED")
          || String(payload.status || "").includes("FAILED")
          ? "status-blocked"
          : "";
    }
  } catch (error) {
    const payload = {
      status: "UPLOAD_FAILED",
      document_validation_status: "not_run",
      statement_tables_extracted: 0,
      fact_parse_status: "not_invoked",
      db_persisted: false,
      source_trust_bucket: "manual_upload_unverified",
      official_source_verified: false,
      source_package_ready_contribution: false,
      warnings: [String(error)],
      blockers: ["manual_upload_network_or_server_error"],
    };
    renderManual(payload);
    saveState({last_manual_result: payload, upload_form: uploadFormState()});
    overallStatus.textContent = "Ошибка загрузки";
    overallStatus.className = "status-blocked";
  }
  uploadForm.file.value = "";
});

refreshButton.addEventListener("click", refreshStatus);
[safeRunForm, uploadForm].forEach((form) => {
  form.addEventListener("input", saveFormState);
  form.addEventListener("change", saveFormState);
});
if (pickPdfButton) {
  pickPdfButton.addEventListener("click", () => {
    uploadForm.company_ticker.value = String(safeRunForm.ticker.value || "").toUpperCase();
    uploadForm.reporting_standard.value = safeRunForm.reporting_standard.value || "IFRS";
    uploadForm.file.click();
  });
}
uploadForm.file.addEventListener("change", () => {
  if (uploadForm.file.files && uploadForm.file.files.length > 0) {
    uploadForm.requestSubmit();
  }
});
safeRunForm.ticker.addEventListener("change", async () => {
  saveFormState();
  uploadForm.company_ticker.value = String(safeRunForm.ticker.value || "").toUpperCase();
  await loadSourceLinks();
});
safeRunForm.ticker.addEventListener("blur", async () => {
  saveFormState();
  uploadForm.company_ticker.value = String(safeRunForm.ticker.value || "").toUpperCase();
  await loadSourceLinks();
});
restoreUiState();
refreshStatus();

function periodRangeFromUploadedPeriod(period) {
  const normalized = String(period || "").trim().toUpperCase();
  if (/^\d{4}$/.test(normalized)) {
    return {period_from: `${normalized}Q1`, period_to: `${normalized}Q4`};
  }
  if (/^\d{4}Q[1-4]$/.test(normalized)) {
    const year = normalized.slice(0, 4);
    const quarter = normalized.slice(-2);
    return quarter === "Q4"
      ? {period_from: `${year}Q1`, period_to: `${year}Q4`}
      : {period_from: normalized, period_to: normalized};
  }
  return {
    period_from: safeRunForm.period_from.value || "2025Q1",
    period_to: safeRunForm.period_to.value || "2025Q4",
  };
}

