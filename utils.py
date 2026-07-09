from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

from config import LOG_DIR

LEGAL_SUFFIXES = {
    "CO",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "INC",
    "INCORPORATED",
    "LTD",
    "LIMITED",
    "LLC",
    "LP",
    "PLC",
    "SA",
    "AG",
    "NV",
    "THE",
}

COMPANY_COLUMN_HINTS = (
    "company",
    "issuer",
    "entity",
    "name",
    "registrant",
    "counterparty",
    "borrower",
    "client",
)


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sec_cik_matcher")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        file_handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


logger = setup_logging()


def normalize_company_name(value: object, strip_suffixes: bool = True) -> str:
    text = str(value or "").upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not strip_suffixes:
        return text

    parts = [part for part in text.split(" ") if part not in LEGAL_SUFFIXES]
    return " ".join(parts).strip() or text


def detect_company_column(df: pd.DataFrame) -> str:
    if df.empty:
        return str(df.columns[0])

    best_column = str(df.columns[0])
    best_score = -1.0

    for column in df.columns:
        name = str(column).lower()
        hint_score = max((1.0 for hint in COMPANY_COLUMN_HINTS if hint in name), default=0.0)
        sample = df[column].dropna().astype(str).head(50)
        text_like_ratio = float(sample.str.contains(r"[A-Za-z]", regex=True).mean()) if len(sample) else 0.0
        avg_length = min(float(sample.str.len().mean() or 0) / 25.0, 1.0)
        score = hint_score * 3.0 + text_like_ratio + avg_length
        if score > best_score:
            best_column = str(column)
            best_score = score

    return best_column


def read_uploaded_table(uploaded_file) -> pd.DataFrame:
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded_file)
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(uploaded_file)
    raise ValueError("Unsupported file type. Upload a CSV or Excel workbook.")


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def dataframe_to_excel_bytes(df: pd.DataFrame, sheet_name: str = "matched") -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    return output.getvalue()


def ensure_columns(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    copy = df.copy()
    for column in columns:
        if column not in copy.columns:
            copy[column] = ""
    return copy


def describe_file(path: Path) -> str:
    if not path.exists():
        return "missing"
    size_kb = path.stat().st_size / 1024
    return f"{size_kb:,.1f} KB"
