from __future__ import annotations

import re
import unicodedata

_PUNCT_TRANSLATION = str.maketrans(
    {
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u00a0": " ",
    }
)


def normalize_financial_text(value: str | None) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT_TRANSLATION)
    repaired = _repair_common_mojibake(text)
    if _mojibake_score(repaired) < _mojibake_score(text):
        text = repaired
    return re.sub(r"\s+", " ", text).strip()


def normalize_matching_text(value: str | None) -> str:
    normalized = normalize_financial_text(value).casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w\sа-яА-Я]", " ", normalized)
    return " ".join(normalized.split())


def contains_normalized_marker(text: str, *markers: str) -> bool:
    normalized_text = normalize_matching_text(text)
    return any(normalize_matching_text(marker) in normalized_text for marker in markers if marker)


def _repair_common_mojibake(text: str) -> str:
    if not text:
        return text
    if not any(marker in text for marker in ("Р", "С", "вЂ", "в€”", "в„–")):
        return text
    for source_encoding in ("cp1251", "latin1"):
        try:
            repaired = text.encode(source_encoding, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired:
            return repaired
    return text


def _mojibake_score(text: str) -> int:
    if not text:
        return 0
    markers = ("Р", "С", "вЂ", "в€”", "в„–", "Ѓ", "ё")
    return sum(text.count(marker) for marker in markers)
