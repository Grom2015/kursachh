# Инструкция по запуску полного парсера

Этот документ объясняет, как запустить полный parser на чистом компьютере.

Рекомендуемый вариант запуска — **PDF-heavy runtime**. Это режим, в котором включается максимальный каскад извлечения данных, реально используемый текущим кодом:

- FastAPI-приложение;
- ручная загрузка PDF;
- canonical `machine_report.json`;
- native PDF extraction через `pdfplumber`;
- вторичное извлечение таблиц через `Camelot`;
- создание OCR-слоя через `OCRmyPDF`;
- растеризация страниц через `pypdfium2` или Poppler `pdftoppm`;
- OCR text/table recovery через `PaddleOCR` / `PaddleX`;
- fallback OCR через `Tesseract`;
- fallback по layout документа через `Docling`;
- котировки MOEX ISS;
- Claude/Anthropic анализ по PDF + JSON, если настроен `ANTHROPIC_API_KEY`.

Предпочтительный способ запуска:

```bash
docker build -f Dockerfile.pdf-heavy -t kursachh-parser:pdf-heavy .
docker run --rm -p 8000:8000 --env-file .env -v "%cd%/data:/app/data" kursachh-parser:pdf-heavy
```

На Linux/macOS:

```bash
docker run --rm -p 8000:8000 --env-file .env -v "$PWD/data:/app/data" kursachh-parser:pdf-heavy
```

После запуска открыть:

```text
http://127.0.0.1:8000/app
```

## Вариант A: рекомендуемый запуск через Docker

### 1. Установить базовые программы

Нужно установить:

- Docker Desktop;
- Git.

### 2. Склонировать репозиторий

```bash
git clone https://github.com/Grom2015/kursachh.git
cd kursachh
```

### 3. Создать `.env`

Скопировать пример:

```bash
copy .env.example .env
```

Linux/macOS:

```bash
cp .env.example .env
```

Рекомендуемые значения:

```env
APP_ENV=local
DATABASE_URL=sqlite:///./local.db
MOEX_BASE_URL=https://iss.moex.com/iss
MAX_REPORT_DOWNLOAD_MB=50
LLM_ENABLED=true
ANTHROPIC_API_KEY=your_claude_api_key_here
LLM_MODEL=claude-sonnet-4-6
```

Если `ANTHROPIC_API_KEY` не задан, парсер все равно работает, но Claude-анализ будет пропущен.

### 4. Собрать image полного парсера

```bash
docker build -f Dockerfile.pdf-heavy -t kursachh-parser:pdf-heavy .
```

Этот image устанавливает только те системные инструменты, которые реально используются текущим кодом:

- `ghostscript`;
- `qpdf`;
- `poppler-utils`;
- `tesseract-ocr`;
- `tesseract-ocr-rus`;
- `tesseract-ocr-eng`;
- image/runtime libraries, нужные для OCR/rendering.

Также устанавливаются Python-зависимости:

- базовые зависимости приложения из `pyproject.toml`;
- dev-зависимости для тестов/smoke checks;
- зависимости `pdf-heavy`.

### 5. Запустить приложение

Windows PowerShell:

```powershell
docker run --rm -p 8000:8000 --env-file .env -v "${PWD}\data:/app/data" kursachh-parser:pdf-heavy
```

Linux/macOS:

```bash
docker run --rm -p 8000:8000 --env-file .env -v "$PWD/data:/app/data" kursachh-parser:pdf-heavy
```

Открыть:

```text
http://127.0.0.1:8000/app
```

### 6. Что происходит после загрузки PDF

Когда пользователь загружает PDF через сайт:

1. PDF сохраняется в controlled storage.
2. Компания распознается или создается через bootstrap.
3. Документ проходит validation/classification.
4. Parser запускает доступные table/text/OCR extraction engines.
5. Создается `machine_report.json`.
6. Создаются JSON-артефакты с statement facts/evidence.
7. Если доступны ticker/board, скачиваются котировки MOEX ISS.
8. Создается `market_technical_report.json`.
9. LLM payload включает:
   - факты parser-а;
   - unresolved evidence;
   - market technical analysis;
   - metadata исходного PDF для attachment.
10. Если запускается Claude-анализ и задан `ANTHROPIC_API_KEY`, исходный PDF может быть приложен к Claude request.

## Вариант B: ручная установка на Windows

Использовать только если Docker недоступен.

### 1. Установить Python

Подходит Python 3.11, 3.12 или 3.13.

Проверка:

```powershell
python --version
```

### 2. Склонировать репозиторий

```powershell
git clone https://github.com/Grom2015/kursachh.git
cd kursachh
```

### 3. Создать virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

### 4. Установить Python-пакеты

Для полного parser runtime:

```powershell
pip install -e ".[dev,pdf-heavy]"
```

Эта команда устанавливает Python-библиотеки, которые использует код:

- `fastapi`;
- `uvicorn`;
- `sqlalchemy`;
- `pandas`;
- `httpx`;
- `openpyxl`;
- `pdfplumber`;
- `anthropic`;
- `camelot-py`;
- `pypdfium2`;
- `pytesseract`;
- `ocrmypdf`;
- `docling`;
- `paddleocr`;
- `paddlepaddle`;
- `paddlex[ocr]`.

### 5. Установить системные программы

Нужно установить только эти программы.

#### Tesseract OCR

Нужен для fallback OCR.

Установить Tesseract и языковые пакеты:

- English;
- Russian.

Ожидаемый путь на Windows:

```text
C:\Program Files\Tesseract-OCR\tesseract.exe
```

#### Ghostscript

Нужен для OCR/PDF tooling.

Ожидаемый executable:

```text
gswin64c.exe
```

#### QPDF

Нужен для `OCRmyPDF`.

Ожидаемый executable:

```text
qpdf.exe
```

#### Poppler

Нужен для fallback rasterization через `pdftoppm`.

После скачивания Poppler для Windows:

1. Распаковать, например сюда:

```text
C:\tools\poppler
```

2. Добавить папку `bin` в Windows `PATH`, например:

```text
C:\tools\poppler\Library\bin
```

или:

```text
C:\tools\poppler\bin
```

точный путь зависит от сборки Poppler.

3. Открыть новый PowerShell и проверить:

```powershell
pdftoppm -h
```

### 6. Создать `.env`

```powershell
copy .env.example .env
```

Указать:

```env
APP_ENV=local
DATABASE_URL=sqlite:///./local.db
MOEX_BASE_URL=https://iss.moex.com/iss
MAX_REPORT_DOWNLOAD_MB=50
LLM_ENABLED=true
ANTHROPIC_API_KEY=your_claude_api_key_here
LLM_MODEL=claude-sonnet-4-6
```

### 7. Проверить runtime

Проверить импорт приложения:

```powershell
python -c "from app.main import create_app; print(create_app().title)"
```

Проверить ключевые binaries:

```powershell
tesseract --version
qpdf --version
pdftoppm -h
```

Проверить Python engine imports:

```powershell
python -c "import pdfplumber, camelot, pypdfium2, pytesseract, ocrmypdf, docling, paddleocr, paddlex; print('pdf-heavy imports ok')"
```

Проверить котировки MOEX:

```powershell
python -c "from app.services.market.moex_client import MoexClient; df=MoexClient(retries=0).get_security_candles('SBER','TQBR','2025-01-01','2025-01-10'); print(len(df), list(df.columns))"
```

### 8. Запустить backend и сайт

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Открыть:

```text
http://127.0.0.1:8000/app
```

## Как запустить parser через UI

1. Открыть `http://127.0.0.1:8000/app`.
2. Ввести ticker, например `VTBR`.
3. Прикрепить PDF-отчет.
4. Отправить upload.
5. Дождаться завершения этапов.

Ожидаемые артефакты:

```text
data/raw/manual_uploads/{TICKER}/...
data/parsed/{TICKER}/{PERIOD}/...
data/validation/{TICKER}/{PERIOD}_manual_report_ingestion.json
data/validation/{TICKER}/{PERIOD_FROM}_{PERIOD_TO}_dataframe_statement_fact_parse.json
data/validation/{TICKER}/{PERIOD_FROM}_{PERIOD_TO}_market_technical_report.json
data/validation/{TICKER}/{PERIOD_FROM}_{PERIOD_TO}_llm_analysis_payload.json
data/validation/{TICKER}/{TICKER}_{PERIOD}_machine_report.json
```

Точные имена файлов зависят от detected effective period.

## Что должно работать для полноценного demo

После загрузки PDF проверить:

- `machine_report_available = true`;
- `structured_facts_count` не равен нулю, если строки отчетности читаемые;
- `unmapped_numeric_evidence_count` может быть больше нуля — это нормально;
- `market_candles_count` больше нуля для MOEX ticker-ов, по которым доступны свечи;
- `market_technical_report_path` присутствует;
- `source_pdf_attachments` появляется в LLM payload.

## Важные ограничения

Parser не обещает идеальное извлечение из любого произвольного PDF.

Гарантии такие:

- safe readable upload создает `machine_report.json`;
- confirmed facts отделены от unresolved evidence;
- unresolved evidence не используется для ratios;
- OCR/table engines повышают recall, но не обходят semantic validation;
- исходный PDF может быть отправлен в Claude как supporting context, если включен LLM analysis.

## Troubleshooting

### `ocr_status = OCR_UNAVAILABLE`

Установить или проверить:

- `paddleocr`;
- `paddlepaddle`;
- `paddlex[ocr]`;
- `tesseract`.

### `missing_raster_runtime:pypdfium2_or_pdftoppm`

Установить:

- Python package `pypdfium2`;
- или Poppler `pdftoppm`.

### `OCRmyPDF` падает

Проверить:

```powershell
qpdf --version
tesseract --version
```

Также проверить, что установлен Ghostscript.

### Не скачиваются котировки MOEX

Проверить интернет и выполнить:

```powershell
python -c "from app.services.market.moex_client import MoexClient; print(MoexClient(retries=0).get_security_candles('VTBR','TQBR','2025-01-01','2025-01-10').tail())"
```

### Claude analysis пропускается

Указать:

```env
ANTHROPIC_API_KEY=...
LLM_ENABLED=true
```

После этого перезапустить backend.
