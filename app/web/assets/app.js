"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const STATEMENT_LABELS = {
  income_statement: "Отчёт о прибылях",
  balance_sheet: "Баланс",
  cash_flow: "Движение денежных средств",
  changes_in_equity: "Изменения в капитале",
};

let lastAnalysisId = null;
let sourceLookupTimer = null;
let pendingAnalyzeFiles = [];
let stageUploadInFlight = false;

function fileFingerprint(file) {
  return [file.stage_id || "", file.file_name || file.name || "", file.size_bytes || file.size || 0].join("::");
}

function appendAnalyzeFiles(files) {
  const existing = new Set(pendingAnalyzeFiles.map(fileFingerprint));
  for (const file of files) {
    const key = fileFingerprint(file);
    if (existing.has(key)) continue;
    existing.add(key);
    pendingAnalyzeFiles.push(file);
  }
}

async function removeAnalyzeFileAt(index) {
  if (index < 0 || index >= pendingAnalyzeFiles.length) return;
  const [removed] = pendingAnalyzeFiles.splice(index, 1);
  if (removed?.stage_id) {
    try {
      await apiDeleteStageFile(removed.stage_id);
    } catch (_err) {}
  }
  renderSelectedAnalyzeFiles();
}

function submitStageUploadInput() {
  const form = $("#stage-upload-form");
  const input = $("#file-input");
  if (!form || !input || !input.files || !input.files.length) return;
  stageUploadInFlight = true;
  setBusy("dropzone", true);
  form.submit();
}

/* ---------------- toast ---------------- */
let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("err", isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

/* ---------------- tabs ---------------- */
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});
function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.tab === name));
  $$(".panel-view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === name));
  if (name === "history") loadHistory();
  if (name === "augment") populateAugmentSelect();
}

/* ---------------- formatting ---------------- */
function fmtNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return num.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function reportUrl(path) {
  if (!path) return "";
  const normalized = String(path).replaceAll("\\", "/").replace(/^\/+/, "");
  return `/reports/artifact/${encodeURI(normalized)}`;
}

function reportLink(path, label) {
  const url = reportUrl(path);
  if (!url) return "";
  return `<a class="btn-ghost" href="${url}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
}

/* ---------------- dropzones ---------------- */
function wireDropzone(zoneId, inputId, onFiles) {
  const zone = $("#" + zoneId);
  const input = $("#" + inputId);
  zone.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    const files = Array.from(input.files || []);
    if (files.length) onFiles(files);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.add("is-drag");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.remove("is-drag");
    })
  );
  zone.addEventListener("drop", (e) => {
    const files = Array.from(e.dataTransfer.files || []);
    if (files.length) onFiles(files);
  });
}

function setBusy(zoneId, busy) {
  $("#" + zoneId).classList.toggle("is-busy", busy);
}

function loadingHTML(text) {
  return `<div class="empty-state"><div class="spinner"></div><h3>${esc(text)}</h3><p>Извлекаем таблицы и считаем коэффициенты…</p></div>`;
}

function formatFileSize(bytes) {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(2)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function renderSelectedAnalyzeFiles() {
  const card = $("#selected-files-card");
  const list = $("#selected-files-list");
  const button = $("#start-analysis-button");
  const clearButton = $("#clear-analysis-files-button");
  if (!card || !list || !button) return;
  if (!pendingAnalyzeFiles.length) {
    card.classList.add("hidden");
    list.innerHTML = "";
    button.disabled = true;
    if (clearButton) clearButton.disabled = true;
    return;
  }
  card.classList.remove("hidden");
  list.innerHTML = pendingAnalyzeFiles
    .map(
      (file, index) => `
        <div class="selected-file-row">
          <div class="selected-file-meta">
            <span>${esc(file.file_name || file.name)}</span>
            <small>${esc(formatFileSize(file.size_bytes || file.size))}</small>
          </div>
          <button class="selected-file-remove" type="button" data-remove-index="${index}">Удалить</button>
        </div>
      `
    )
    .join("");
  button.disabled = false;
  if (clearButton) clearButton.disabled = false;
  list.querySelectorAll("[data-remove-index]").forEach((element) => {
    element.addEventListener("click", async () => {
      await removeAnalyzeFileAt(Number(element.dataset.removeIndex));
    });
  });
}

async function submitAnalyzeFiles(files) {
  const ticker = ($("#analyze-ticker").value || "").trim();
  if (!ticker) return toast("Укажите тикер компании", true);
  setBusy("dropzone", true);
  $("#start-analysis-button").disabled = true;
  const leadName = files[0]?.file_name || files[0]?.name || "upload";
  $("#analyze-result").innerHTML = loadingHTML(
    files.length > 1
      ? `Анализируем ${files.length} файлов (${leadName} и другие)`
      : `Анализируем «${leadName}»`
  );
  try {
    const payload = await apiUploadCreateStagedBatch(files, ticker, "");
    lastAnalysisId = payload.id;
    renderAnalysis($("#analyze-result"), payload);
    toast("Анализ готов");
    pendingAnalyzeFiles = [];
    renderSelectedAnalyzeFiles();
  } catch (err) {
    $("#analyze-result").innerHTML = `<div class="notice err">Не удалось обработать файл: ${esc(err.message)}</div>`;
    toast(err.message, true);
  } finally {
    setBusy("dropzone", false);
    $("#start-analysis-button").disabled = pendingAnalyzeFiles.length === 0;
  }
}

/* ---------------- API ---------------- */
async function apiUploadCreate(file, ticker, period) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("company_ticker", ticker);
  if (period) fd.append("period", period);
  const res = await fetch("/pdf-analysis", { method: "POST", body: fd });
  return handleJson(res);
}
async function apiUploadCreateBatch(files, ticker, period) {
  const fd = new FormData();
  files.forEach((file) => fd.append("files", file));
  fd.append("company_ticker", ticker);
  if (period) fd.append("period", period);
  const res = await fetch("/pdf-analysis/batch", { method: "POST", body: fd });
  return handleJson(res);
}
async function apiDeleteStageFile(stageId) {
  const res = await fetch(`/pdf-analysis/staged-file/${encodeURIComponent(stageId)}`, { method: "DELETE" });
  return handleJson(res);
}
async function apiUploadCreateStagedBatch(files, ticker, period) {
  const res = await fetch("/pdf-analysis/staged-batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      stage_ids: files.map((file) => file.stage_id),
      company_ticker: ticker,
      period: period || null,
      reporting_standard: "IFRS",
    }),
  });
  return handleJson(res);
}
async function apiUploadAugment(resultId, file, period) {
  const fd = new FormData();
  fd.append("file", file);
  if (period) fd.append("period", period);
  const res = await fetch(`/pdf-analysis/${resultId}/augment`, { method: "POST", body: fd });
  return handleJson(res);
}
async function apiHistory() {
  const res = await fetch("/pdf-analysis");
  return handleJson(res);
}
async function apiGet(resultId) {
  const res = await fetch(`/pdf-analysis/${resultId}`);
  return handleJson(res);
}
async function handleJson(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Ошибка ${res.status}`);
  return data;
}

/* ---------------- analyze flow ---------------- */
wireDropzone("dropzone", "file-input", async (files) => {
  const input = $("#file-input");
  if (input && files && files.length && input.files !== files) {
    try {
      const dt = new DataTransfer();
      files.forEach((file) => dt.items.add(file));
      input.files = dt.files;
    } catch (_err) {
      toast("Не удалось подготовить файлы для загрузки", true);
      return;
    }
  }
  submitStageUploadInput();
});

/* ---------------- augment flow ---------------- */
wireDropzone("augment-dropzone", "augment-file-input", async (files) => {
  const file = files[0];
  const resultId = $("#augment-select").value;
  if (!resultId) return toast("Сначала выберите базовый анализ", true);
  setBusy("augment-dropzone", true);
  $("#augment-result").innerHTML = loadingHTML(`Добавляем «${file.name}»`);
  try {
    const payload = await apiUploadAugment(resultId, file, "");
    lastAnalysisId = payload.id;
    renderAnalysis($("#augment-result"), payload);
    toast("Анализ дополнен — создана новая версия");
  } catch (err) {
    $("#augment-result").innerHTML = `<div class="notice err">Не удалось дополнить анализ: ${esc(err.message)}</div>`;
    toast(err.message, true);
  } finally {
    setBusy("augment-dropzone", false);
  }
});

async function populateAugmentSelect() {
  const select = $("#augment-select");
  try {
    const { items } = await apiHistory();
    const current = select.value;
    select.innerHTML = '<option value="">— выберите из истории —</option>';
    items.forEach((it) => {
      const opt = document.createElement("option");
      opt.value = it.id;
      opt.textContent = `${it.company} · ${it.period_from}→${it.period_to} · v${it.version}`;
      select.appendChild(opt);
    });
    if (lastAnalysisId && items.some((i) => i.id === lastAnalysisId)) select.value = lastAnalysisId;
    else if (current) select.value = current;
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------------- history ---------------- */
async function loadHistory() {
  const list = $("#history-list");
  list.innerHTML = '<div class="empty-state small"><div class="spinner"></div></div>';
  try {
    const { items } = await apiHistory();
    if (!items.length) {
      list.innerHTML = '<div class="empty-state small"><p>Пока нет запросов. Загрузите первый PDF на вкладке «Анализ».</p></div>';
      return;
    }
    list.innerHTML = items.map(historyRow).join("");
    $$(".hist-item", list).forEach((row) =>
      row.addEventListener("click", () => openFromHistory(row.dataset.id))
    );
  } catch (err) {
    list.innerHTML = `<div class="notice err">${esc(err.message)}</div>`;
  }
}
function historyRow(it) {
  const name = it.company_name && it.company_name !== it.company ? `${it.company_name}` : it.company;
  return `
    <div class="hist-item" data-id="${esc(it.id)}">
      <div class="hist-main">
        <div class="hist-ticker">${esc(it.company.slice(0, 5))}</div>
        <div class="hist-info">
          <h4>${esc(name)} <span class="period-chip">v${it.version}</span></h4>
          <p>${esc(it.period_from)} → ${esc(it.period_to)} · ${it.document_count} документ(ов)</p>
        </div>
      </div>
      <div class="hist-stats">
        <div class="hist-stat"><b>${it.calculated_count}</b><span>коэфф.</span></div>
        <div class="hist-stat"><b>${it.facts_count}</b><span>фактов</span></div>
        <div class="hist-time">${fmtDate(it.created_at)}</div>
      </div>
    </div>`;
}
async function openFromHistory(id) {
  switchTab("analyze");
  $("#analyze-result").innerHTML = loadingHTML("Открываем анализ");
  try {
    const payload = await apiGet(id);
    lastAnalysisId = payload.id;
    renderAnalysis($("#analyze-result"), payload);
  } catch (err) {
    $("#analyze-result").innerHTML = `<div class="notice err">${esc(err.message)}</div>`;
  }
}
$("#history-refresh").addEventListener("click", loadHistory);

async function loadSourceCard(ticker) {
  const card = $("#source-card");
  if (!card) return;
  const cleanTicker = String(ticker || "").trim().toUpperCase();
  if (!cleanTicker) {
    card.innerHTML = "";
    return;
  }
  try {
    const res = await fetch(`/report-sources/${encodeURIComponent(cleanTicker)}`);
    const payload = await res.json();
    const data = res.ok ? payload : payload.detail || {};
    const item = data.item || {};
    const guidance = data.manual_upload_guidance || {};
    const links = (guidance.recommended_links || []).filter((link) => link && link.url).slice(0, 4);
    card.innerHTML = `
      <h4>Где взять отчеты для ${esc(cleanTicker)}</h4>
      <p>${esc(item.company_name || cleanTicker)}. Откройте e-disclosure, скачайте один или несколько отчетов и загрузите их сюда одним пакетом.</p>
      <div class="source-card-links">
        ${links.map((link) => `<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)}</a>`).join("")}
      </div>
    `;
  } catch (_err) {
    card.innerHTML = "";
  }
}

/* ---------------- render analysis ---------------- */
function renderAnalysis(container, payload) {
  const a = payload.analytics || {};
  const company = a.company || {};
  const ratios = (a.ratios || []).filter((m) => m.status === "calculated");
  const facts = a.facts || [];
  const addedPeriods = new Set((a.delta && a.delta.added_periods) || []);

  let html = "";

  // header
  html += `
    <div class="res-head">
      <div>
        <h2 class="res-title">${esc(company.name || company.ticker || "Компания")}</h2>
        <p class="res-sub">${esc(company.ticker || "")}${company.sector ? " · " + esc(company.sector) : ""} · ${esc(a.reporting_standard || "IFRS")}</p>
        <div class="badges">
          <span class="badge accent">Период: ${esc((a.periods || []).join(", ") || a.period_to || "—")}</span>
          <span class="badge ${a.document_validation_status === "pass" ? "ok" : "warn"}">Документ: ${esc(a.document_validation_status || "—")}</span>
          <span class="badge">${a.facts_count || 0} фактов</span>
          <span class="badge">${ratios.length} коэффициентов</span>
        </div>
      </div>
      <div class="version-pill">v${payload.version || 1}<small>${payload.parent_result_id ? "дополнен" : "версия"}</small></div>
    </div>`;

  // delta
  if (a.delta && (a.delta.added_periods?.length || a.delta.added_metric_count)) {
    html += `<div class="delta-bar">
      <div>Добавлен период: <b>${esc((a.delta.added_periods || []).join(", ") || "—")}</b></div>
      <div>Новых коэффициентов: <b>${a.delta.added_metric_count || 0}</b></div>
      <div>Изменено: <b>${(a.delta.changed_metrics || []).length}</b></div>
    </div>`;
  }

  // ИИ-анализ и рекомендация
  html += `<div class="res-section" data-insight>
    <h3>ИИ-анализ и рекомендация</h3>
    <div data-insight-body>${a.llm_report ? insightHTML(a.llm_report) : insightButtonHTML()}</div>
  </div>`;

  // documents
  const docs = a.documents || [];
  if (docs.length) {
    html += `<div class="res-section"><h3>Источники (${docs.length})</h3><div class="chips">`;
    html += docs
      .map(
        (d) =>
          `<div class="doc-chip"><span class="dot"></span>${esc(d.file_name || "документ")}${d.period ? ` · ${esc(d.period)}` : ""}</div>`
      )
      .join("");
    html += `</div></div>`;
  }
  if ((a.batch_uploaded_filenames || []).length > 1) {
    html += `<div class="res-section"><h3>Пакет загруженных файлов</h3><div class="chips">`;
    html += a.batch_uploaded_filenames
      .map((name) => `<div class="doc-chip"><span class="dot"></span>${esc(name)}</div>`)
      .join("");
    html += `</div></div>`;
  }

  // ratios
  html += `<div class="res-section"><h3>Финансовые коэффициенты</h3>`;
  if (ratios.length) {
    html += `<div class="ratio-grid">`;
    html += ratios
      .map((m) => {
        const cls = addedPeriods.has(m.period) ? "period-chip added" : "period-chip";
        return `<div class="ratio">
          <div class="rk">${esc(m.metric_name || m.metric_code)}</div>
          <div class="rv">${esc(m.display_value ?? fmtNumber(m.value))}</div>
          <div class="rp"><span class="${cls}">${esc(m.period)}</span></div>
          ${m.formula ? `<div class="rf">${esc(m.formula)}</div>` : ""}
        </div>`;
      })
      .join("");
    html += `</div>`;
  } else {
    html += `<div class="notice warn">Из загруженного отчёта пока не удалось рассчитать коэффициенты. Извлечённые показатели — ниже.</div>`;
  }
  html += `</div>`;

  // facts table
  if (facts.length) {
    html += `<div class="res-section"><h3>Извлечённые показатели (${facts.length})</h3>
      <div class="table-wrap"><table class="facts">
        <thead><tr><th>Показатель</th><th>Раздел</th><th>Период</th><th style="text-align:right">Значение</th></tr></thead><tbody>`;
    html += facts
      .map(
        (f) => `<tr>
          <td>${esc(f.label || f.metric_code)}</td>
          <td>${esc(STATEMENT_LABELS[f.statement_type] || f.statement_type || "—")}</td>
          <td>${esc(f.period || "—")}</td>
          <td class="num">${fmtNumber(f.value)}${f.currency ? " " + esc(f.currency) : ""}</td>
        </tr>`
      )
      .join("");
    html += `</tbody></table></div></div>`;
  }

  // warnings
  const warnings = a.warnings || [];
  if (warnings.length) {
    html += `<div class="res-section"><details class="warnings">
      <summary>Примечания и предупреждения (${warnings.length})</summary>
      <ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
    </details></div>`;
  }

  // market / technical analysis for the company ticker (loaded asynchronously)
  const ticker = company.ticker || "";
  if (ticker) {
    html += `<div class="res-section" data-market-embed>
      <h3>Котировки и технический анализ · ${esc(ticker)}</h3>
      <div class="empty-state small"><div class="spinner"></div><p>Загружаем котировки с MOEX…</p></div>
    </div>`;
  }

  container.innerHTML = html;
  wireInsight(container, payload.id);
  if (ticker) embedMarket(container, ticker, a.period_to);
}

/* ---------------- LLM insight ---------------- */
async function apiInsight(resultId, regenerate) {
  const res = await fetch(`/pdf-analysis/${resultId}/insight${regenerate ? "?regenerate=true" : ""}`, {
    method: "POST",
  });
  return handleJson(res);
}

function mdInline(s) {
  // Code spans first so underscores/asterisks inside identifiers are left alone.
  return s
    .replace(/`([^`]+)`/g, '<code class="insight-code">$1</code>')
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}
function mdToHtml(text) {
  return esc(text || "")
    .split(/\n/)
    .map((raw) => {
      const line = raw.trim();
      if (!line) return "";
      if (/^[-*_]{3,}$/.test(line)) return `<hr class="insight-hr"/>`;
      if (line.startsWith("&gt; ")) return `<p class="insight-quote">${mdInline(line.slice(5))}</p>`;
      if (line.startsWith("### ")) return `<h4 class="insight-h insight-h3">${mdInline(line.slice(4))}</h4>`;
      if (line.startsWith("## ")) return `<h4 class="insight-h">${mdInline(line.slice(3))}</h4>`;
      if (line.startsWith("# ")) return `<h3 class="insight-h insight-h1">${mdInline(line.slice(2))}</h3>`;
      if (line.startsWith("- ") || line.startsWith("* ")) return `<p class="insight-li">• ${mdInline(line.slice(2))}</p>`;
      if (line.startsWith("|") && line.endsWith("|")) {
        if (/^\|[\s:|-]+\|$/.test(line)) return "";
        const cells = line.slice(1, -1).split("|").map((c) => c.trim());
        return `<p class="insight-row">${cells.map(mdInline).join(" · ")}</p>`;
      }
      return `<p>${mdInline(line)}</p>`;
    })
    .join("");
}
function recoBadge(reco) {
  if (!reco) return "";
  const cls = reco === "BUY" ? "ok" : reco === "SELL" ? "warn" : "accent";
  const label = reco === "BUY" ? "Покупать" : reco === "SELL" ? "Продавать" : "Держать";
  return `<span class="badge ${cls}" style="font-size:13px">Рекомендация: ${esc(label)} (${esc(reco)})</span>`;
}
function summaryStatusBadge(status) {
  if (!status) return "";
  const cls = status === "strong" ? "ok" : status === "weak" ? "warn" : "accent";
  const label = status === "strong" ? "Сильный сигнал" : status === "weak" ? "Слабый сигнал" : "Смешанный сигнал";
  return `<span class="badge ${cls}" style="font-size:13px">${esc(label)}</span>`;
}
function insightListBlock(title, items) {
  if (!items || !items.length) return "";
  return `<div class="insight-mini-block"><div class="insight-mini-title">${esc(title)}</div>${items.map((item) => `<p class="insight-li">• ${esc(item)}</p>`).join("")}</div>`;
}
function insightMetricsBlock(metrics) {
  if (!metrics || !metrics.length) return "";
  return `<div class="insight-mini-block"><div class="insight-mini-title">Ключевые метрики</div><div class="ratio-grid">${metrics.map((m) => `<div class="ratio"><div class="rk">${esc(m.label || "Метрика")}</div><div class="rv">${esc(m.value || "—")}</div>${m.comment ? `<div class="rf">${esc(m.comment)}</div>` : ""}</div>`).join("")}</div></div>`;
}
function structuredSummaryHTML(summary, recommendation) {
  if (!summary || typeof summary !== "object") return "";
  return `<div class="insight-summary">
    <div class="badges" style="margin-bottom:10px">${recommendation ? recoBadge(recommendation) : ""}${summaryStatusBadge(summary.status)}</div>
    ${summary.investment_signal ? `<p><strong>${esc(summary.investment_signal)}</strong></p>` : ""}
    ${insightListBlock("Короткий итог", summary.executive_summary)}
    ${insightMetricsBlock(summary.important_metrics)}
    ${insightListBlock("Сильные стороны", summary.key_strengths)}
    ${insightListBlock("Риски", summary.key_risks)}
    ${insightListBlock("На что смотреть", summary.watch_items)}
    ${summary.data_quality_note ? `<p class="hint">${esc(summary.data_quality_note)}</p>` : ""}
  </div>`;
}
function insightHTML(report) {
  const downloads = [
    report.memo_markdown_path ? reportLink(report.memo_markdown_path, "Скачать .md") : "",
    report.summary_json_path ? reportLink(report.summary_json_path, "Скачать .json") : "",
  ]
    .filter(Boolean)
    .join("");

  return `<div class="insight-card">
    ${structuredSummaryHTML(report.structured_summary, report.recommendation)}
    ${!report.structured_summary && report.recommendation ? `<div style="margin-bottom:10px">${recoBadge(report.recommendation)}</div>` : ""}
    <div class="insight-text">${mdToHtml(report.text)}</div>
    <div class="insight-foot">
      <span class="hint">Сгенерировано ${esc(report.model || "Claude")}</span>
      ${downloads ? `<div class="chips">${downloads}</div>` : ""}
      <button class="btn-ghost" data-insight-regenerate>Перегенерировать</button>
    </div>
  </div>`;
}
function insightButtonHTML() {
  return `<button class="btn-primary" data-insight-generate style="max-width:360px">Сформировать выводы и рекомендацию</button>
    <p class="hint">Превращает данные отчёта и котировок в текстовый разбор с рекомендацией (Claude API, ~10 сек).</p>`;
}
function wireInsight(container, resultId) {
  const section = container.querySelector("[data-insight]");
  if (!section || !resultId) return;
  const body = section.querySelector("[data-insight-body]");
  async function run(regenerate) {
    body.innerHTML = `<div class="empty-state small"><div class="spinner"></div><p>Claude готовит отчёт: фундаментальный → технический → сводка.<br>Это занимает 1–2 минуты, не закрывайте вкладку.</p></div>`;
    try {
      const report = await apiInsight(resultId, regenerate);
      body.innerHTML = insightHTML(report);
      bind();
    } catch (err) {
      body.innerHTML = `<div class="notice err">Не удалось сформировать выводы: ${esc(err.message)}</div>`;
      toast(err.message, true);
    }
  }
  function bind() {
    const gen = section.querySelector("[data-insight-generate]");
    if (gen) gen.addEventListener("click", () => run(false));
    const regen = section.querySelector("[data-insight-regenerate]");
    if (regen) regen.addEventListener("click", () => run(true));
  }
  bind();
}

/* ---------------- market / technical analysis ---------------- */
const MARKET_LABELS = {
  ma_20: "Скользящая средняя MA 20",
  ma_50: "Скользящая средняя MA 50",
  ma_200: "Скользящая средняя MA 200",
  rsi_14: "RSI (14)",
  support_level: "Поддержка",
  resistance_level: "Сопротивление",
  volatility_20d: "Волатильность 20д",
  volatility_60d: "Волатильность 60д",
  daily_return: "Дневная доходность",
  max_drawdown: "Макс. просадка",
  average_daily_volume: "Средний дневной объём (ADV)",
  average_daily_turnover: "Средний оборот, ₽",
  median_daily_turnover: "Медианный оборот, ₽",
  trading_days_count: "Дней торгов",
  zero_volume_days_count: "Дней без объёма",
  bid_ask_spread: "Спред (bid/ask)",
};
const PERCENT_CODES = new Set(["volatility_20d", "volatility_60d", "daily_return", "max_drawdown"]);
const PRICE_CODES = new Set(["ma_20", "ma_50", "ma_200", "support_level", "resistance_level"]);

function fmtMarket(code, value) {
  if (value === null || value === undefined) return "—";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  if (PERCENT_CODES.has(code)) return (num * 100).toFixed(2) + "%";
  if (code === "rsi_14") return num.toFixed(1);
  if (PRICE_CODES.has(code)) return num.toFixed(2) + " ₽";
  if (Math.abs(num) >= 10000) return Math.round(num).toLocaleString("ru-RU");
  return num.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}

async function apiMarket(ticker, from, to, board) {
  const params = new URLSearchParams({ ticker });
  if (from) params.set("period_from", from);
  if (to) params.set("period_to", to);
  if (board) params.set("board", board);
  const res = await fetch(`/market/technical?${params.toString()}`);
  return handleJson(res);
}

async function apiCandles(ticker, from, to, board) {
  const params = new URLSearchParams({ ticker });
  if (from) params.set("period_from", from);
  if (to) params.set("period_to", to);
  if (board) params.set("board", board);
  const res = await fetch(`/market/candles?${params.toString()}`);
  return handleJson(res);
}

function priceChartSVG(candles) {
  const pts = (candles || []).filter((c) => c.close != null);
  if (pts.length < 2) return "";
  const W = 840, H = 260, padL = 10, padR = 62, padT = 14, padB = 26;
  const cw = W - padL - padR, ch = H - padT - padB;
  const closes = pts.map((c) => c.close);
  let min = Math.min(...closes), max = Math.max(...closes);
  if (min === max) { min -= 1; max += 1; }
  const n = pts.length;
  const bottom = padT + ch;
  const X = (i) => padL + (i / (n - 1)) * cw;
  const Y = (v) => padT + (1 - (v - min) / (max - min)) * ch;
  const fmtP = (v) => v.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
  const fmtD = (d) => String(d).slice(0, 7);

  const line = pts.map((c, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(c.close).toFixed(1)}`).join(" ");
  const area = `M${X(0).toFixed(1)} ${bottom.toFixed(1)} ` +
    pts.map((c, i) => `L${X(i).toFixed(1)} ${Y(c.close).toFixed(1)}`).join(" ") +
    ` L${X(n - 1).toFixed(1)} ${bottom.toFixed(1)} Z`;
  const last = pts[n - 1];
  const lastX = X(n - 1), lastY = Y(last.close);
  const midIdx = Math.floor((n - 1) / 2);

  return `<svg viewBox="0 0 ${W} ${H}" class="price-chart" preserveAspectRatio="none" role="img" aria-label="График цены закрытия">
    <line x1="${padL}" y1="${padT.toFixed(1)}" x2="${(W - padR).toFixed(1)}" y2="${padT.toFixed(1)}" stroke="#243044" stroke-width="1"/>
    <line x1="${padL}" y1="${bottom.toFixed(1)}" x2="${(W - padR).toFixed(1)}" y2="${bottom.toFixed(1)}" stroke="#243044" stroke-width="1"/>
    <path d="${area}" fill="rgba(76,141,255,0.12)"/>
    <path d="${line}" fill="none" stroke="#4c8dff" stroke-width="2" stroke-linejoin="round"/>
    <line x1="${lastX.toFixed(1)}" y1="${padT}" x2="${lastX.toFixed(1)}" y2="${bottom.toFixed(1)}" stroke="#4c8dff" stroke-width="1" stroke-dasharray="3 3" opacity="0.5"/>
    <circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5" fill="#4c8dff"/>
    <text x="${(W - padR + 6)}" y="${(padT + 4).toFixed(1)}" fill="#8b9bb0" font-size="12">${fmtP(max)}</text>
    <text x="${(W - padR + 6)}" y="${(bottom).toFixed(1)}" fill="#8b9bb0" font-size="12">${fmtP(min)}</text>
    <text x="${(W - padR + 6)}" y="${lastY.toFixed(1)}" fill="#4c8dff" font-size="12" font-weight="600">${fmtP(last.close)}</text>
    <text x="${padL}" y="${(H - 7)}" fill="#5f6f85" font-size="11">${fmtD(pts[0].date)}</text>
    <text x="${(padL + cw / 2).toFixed(1)}" y="${(H - 7)}" fill="#5f6f85" font-size="11" text-anchor="middle">${fmtD(pts[midIdx].date)}</text>
    <text x="${(W - padR).toFixed(1)}" y="${(H - 7)}" fill="#5f6f85" font-size="11" text-anchor="end">${fmtD(last.date)}</text>
  </svg>`;
}

function marketCardGrid(items, getCode, getStatus, getValue) {
  const valid = items.filter((i) => getStatus(i) === "valid");
  if (!valid.length) return `<div class="notice warn">Нет рассчитанных значений (недостаточно данных или нет котировок).</div>`;
  return (
    `<div class="ratio-grid">` +
    valid
      .map((i) => {
        const code = getCode(i);
        return `<div class="ratio">
          <div class="rk">${esc(MARKET_LABELS[code] || code)}</div>
          <div class="rv">${esc(fmtMarket(code, getValue(i)))}</div>
        </div>`;
      })
      .join("") +
    `</div>`
  );
}

function marketSectionsHTML(report, chartHTML) {
  const summary = report.summary || {};
  const latest = report.latest_summary || {};
  const range = report.actual_candle_date_range || {};
  const ok = (report.status || "").toUpperCase() === "PASS" || (summary.candles_count || 0) > 0;
  const tech = report.technical_indicators || [];
  const liq = report.liquidity_metrics || [];

  if (!ok) {
    return `<div class="notice warn">Котировки не получены для ${esc(report.ticker || "")} (режим ${esc(report.board || "TQBR")}). Возможно, тикер не торгуется на MOEX или нет данных за период.</div>`;
  }

  let html = `<div class="badges" style="margin-bottom:14px">
      <span class="badge ok">${esc(report.status || "")}</span>
      <span class="badge accent">${summary.candles_count || 0} торговых дней</span>
      ${range.from ? `<span class="badge">${esc(range.from)} → ${esc(range.to || "")}</span>` : ""}
      ${latest.close != null ? `<span class="badge">Закрытие: ${fmtMarket("close", latest.close)} ₽</span>` : ""}
      <span class="badge">MOEX ISS</span>
    </div>`;

  if (chartHTML) {
    html += `<p class="muted" style="margin:0 0 8px">Динамика цены (закрытие, ₽)</p>
      <div class="price-chart-wrap">${chartHTML}</div>`;
  }

  html += `<p class="muted" style="margin:16px 0 8px">Скользящие средние, RSI, уровни</p>
    ${marketCardGrid(tech, (i) => i.indicator_code, (i) => i.status, (i) => i.value)}`;

  html += `<p class="muted" style="margin:16px 0 8px">Ликвидность и объёмы</p>
    ${marketCardGrid(liq, (i) => i.metric_code, (i) => i.status, (i) => i.value)}`;

  const spread = liq.find((i) => i.metric_code === "bid_ask_spread");
  if (spread && spread.status !== "valid") {
    html += `<div class="notice" style="margin-top:14px">Спред (bid/ask) недоступен: дневные свечи MOEX не содержат данных стакана. Для спреда нужны order-book / тиковые данные.</div>`;
  }

  const warnings = report.warnings || [];
  if (warnings.length) {
    html += `<details class="warnings" style="margin-top:14px">
      <summary>Примечания по рынку (${warnings.length})</summary>
      <ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
    </details>`;
  }
  return html;
}

async function embedMarket(container, ticker, periodTo) {
  const slot = container.querySelector("[data-market-embed]");
  if (!slot) return;
  // Anchor the market window's end to the report period, with a ~2-year tail
  // back so long indicators (MA200, 60d volatility) have enough data. The tail
  // is kept near 2 years on purpose: MOEX returns at most 500 daily candles per
  // request, so a longer window would return the earliest 500 and stop short of
  // the report date instead of ending on it.
  let from = "";
  let to = "";
  const m = /^(\d{4})Q[1-4]$/.exec(periodTo || "");
  if (m) {
    to = periodTo;
    from = `${parseInt(m[1], 10) - 1}Q1`;
  }
  try {
    const [report, candlesResp] = await Promise.all([
      apiMarket(ticker, from, to, "TQBR"),
      apiCandles(ticker, from, to, "TQBR").catch(() => ({ candles: [] })),
    ]);
    const chart = priceChartSVG(candlesResp.candles || []);
    slot.innerHTML = `<h3>Котировки и технический анализ · ${esc(ticker)}</h3>${marketSectionsHTML(report, chart)}`;
  } catch (err) {
    slot.innerHTML = `<h3>Котировки и технический анализ</h3><div class="notice warn">Котировки недоступны: ${esc(err.message)}</div>`;
  }
}

/* ---------------- init ---------------- */
populateAugmentSelect();
$("#analyze-ticker").addEventListener("input", () => {
  clearTimeout(sourceLookupTimer);
  sourceLookupTimer = setTimeout(() => loadSourceCard($("#analyze-ticker").value), 250);
});
$("#analyze-ticker").addEventListener("blur", () => loadSourceCard($("#analyze-ticker").value));
loadSourceCard($("#analyze-ticker").value);
$("#start-analysis-button").addEventListener("click", async () => {
  if (!pendingAnalyzeFiles.length) {
    toast("Сначала выберите хотя бы один файл", true);
    return;
  }
  await submitAnalyzeFiles([...pendingAnalyzeFiles]);
});
$("#clear-analysis-files-button").addEventListener("click", async () => {
  const files = [...pendingAnalyzeFiles];
  pendingAnalyzeFiles = [];
  renderSelectedAnalyzeFiles();
  for (const file of files) {
    if (file.stage_id) {
      try {
        await apiDeleteStageFile(file.stage_id);
      } catch (_err) {}
    }
  }
  toast("Список файлов очищен");
});
window.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.source !== "pdf-analysis-stage") return;
  stageUploadInFlight = false;
  setBusy("dropzone", false);
  const input = $("#file-input");
  if (input) input.value = "";
  if (!data.ok) {
    toast(data.error || "Ошибка сетевой загрузки файла", true);
    return;
  }
  const staged = Array.isArray(data.files) ? data.files : [];
  appendAnalyzeFiles(staged);
  renderSelectedAnalyzeFiles();
  if (!staged.length) {
    toast("Файлы не были добавлены", true);
    return;
  }
  toast(
    staged.length > 1
      ? `Добавлено ${staged.length} файлов. Всего: ${pendingAnalyzeFiles.length}`
      : `Добавлен файл ${staged[0].file_name}. Всего: ${pendingAnalyzeFiles.length}`
  );
});
renderSelectedAnalyzeFiles();
