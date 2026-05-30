import json
from pathlib import Path

import pandas as pd

from app.core.config import get_settings


def load_statement_tables_as_dataframes(document_id: int) -> dict[str, pd.DataFrame]:
    payload = _load_payload(document_id)
    frames: dict[str, pd.DataFrame] = {}
    for item in payload.get("statement_tables", []):
        key = item.get("statement_type") or "unknown"
        if key in frames:
            key = f"{key}_{item.get('table_index')}"
        frames[key] = pd.DataFrame(item.get("dataframe_json", {}).get("data") or [])
    return frames


def load_statement_table_as_dataframe(document_id: int, statement_type: str) -> pd.DataFrame:
    frames = load_statement_tables_as_dataframes(document_id)
    if statement_type in frames:
        return frames[statement_type]
    matches = [frame for key, frame in frames.items() if key.startswith(f"{statement_type}_")]
    if matches:
        return matches[0]
    raise FileNotFoundError(f"Statement table not found for document_id={document_id}, statement_type={statement_type}")


def _load_payload(document_id: int) -> dict:
    path = _find_artifact(document_id)
    if not path:
        raise FileNotFoundError(f"Statement table artifact not found for document_id={document_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _find_artifact(document_id: int) -> Path | None:
    root = get_settings().root_dir / get_settings().report_parsed_dir
    matches = list(root.glob(f"**/{document_id}_statement_tables.json"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)
