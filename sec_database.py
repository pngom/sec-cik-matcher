from __future__ import annotations

import argparse
import contextlib
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from config import (
    ALIAS_FILE,
    DATA_DIR,
    SEC_CACHE_FALLBACK_FILE,
    SEC_CACHE_FILE,
    SEC_CIK_LOOKUP_URL,
    SEC_COMPANY_TICKERS_URL,
    SEC_SQLITE_FILE,
    SEC_USER_AGENT_ENV,
)
from utils import logger, normalize_company_name

REQUIRED_COLUMNS = ["cik", "name", "ticker", "source", "normalized_name", "updated_at"]

FALLBACK_COMPANIES = [
    {"cik": "0000320193", "name": "Apple Inc.", "ticker": "AAPL", "source": "fallback"},
    {"cik": "0000789019", "name": "Microsoft Corp", "ticker": "MSFT", "source": "fallback"},
    {"cik": "0001652044", "name": "Alphabet Inc.", "ticker": "GOOGL", "source": "fallback"},
    {"cik": "0001326801", "name": "Meta Platforms, Inc.", "ticker": "META", "source": "fallback"},
    {"cik": "0001018724", "name": "Amazon.com, Inc.", "ticker": "AMZN", "source": "fallback"},
    {"cik": "0001318605", "name": "Tesla, Inc.", "ticker": "TSLA", "source": "fallback"},
    {"cik": "0001045810", "name": "NVIDIA Corp", "ticker": "NVDA", "source": "fallback"},
]


def _current_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sec_headers() -> dict[str, str]:
    user_agent = os.getenv(SEC_USER_AGENT_ENV, "sec-cik-matcher/0.1 local-development afro_cat@yahoo.com")
    return {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }


def parse_company_tickers(payload: dict[str, Any]) -> pd.DataFrame:
    records = []
    for item in payload.values():
        cik = str(item.get("cik_str", "")).zfill(10)
        name = str(item.get("title", "")).strip()
        ticker = str(item.get("ticker", "")).strip().upper()
        if cik and name:
            records.append({"cik": cik, "name": name, "ticker": ticker, "source": "sec_company_tickers"})

    return prepare_sec_dataframe(pd.DataFrame.from_records(records))


def parse_cik_lookup_text(text: str) -> pd.DataFrame:
    records = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue

        match = re.match(r"^(.+):(\d{1,10}):?$", line)
        if not match:
            logger.debug("Skipping unparseable CIK lookup line: %s", line)
            continue

        name = match.group(1).strip()
        cik = match.group(2).zfill(10)
        if name and cik:
            records.append({"cik": cik, "name": name, "ticker": "", "source": "sec_cik_lookup"})

    return prepare_sec_dataframe(pd.DataFrame.from_records(records))


def enrich_with_tickers(cik_df: pd.DataFrame, ticker_df: pd.DataFrame) -> pd.DataFrame:
    if ticker_df.empty:
        return cik_df

    enriched = cik_df.copy()
    ticker_by_cik = (
        ticker_df[ticker_df["ticker"].astype(str).str.strip().ne("")]
        .drop_duplicates(subset=["cik"])
        .set_index("cik")["ticker"]
    )
    enriched["ticker"] = enriched["cik"].map(ticker_by_cik).fillna(enriched["ticker"]).fillna("")
    enriched.loc[enriched["ticker"].astype(str).str.strip().ne(""), "source"] = "sec_cik_lookup+tickers"

    existing_pairs = set(zip(enriched["cik"], enriched["normalized_name"]))
    missing_ticker_rows = ticker_df[
        ~ticker_df.apply(lambda row: (row["cik"], row["normalized_name"]) in existing_pairs, axis=1)
    ]
    if not missing_ticker_rows.empty:
        enriched = pd.concat([enriched, missing_ticker_rows], ignore_index=True)

    return prepare_sec_dataframe(enriched)


def prepare_sec_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()
    for column in ["cik", "name", "ticker", "source"]:
        if column not in prepared.columns:
            prepared[column] = ""

    prepared["cik"] = prepared["cik"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(10)
    prepared["name"] = prepared["name"].astype(str).str.strip()
    prepared["ticker"] = prepared["ticker"].fillna("").astype(str).str.upper().str.strip()
    prepared["source"] = prepared["source"].fillna("unknown").astype(str)
    prepared["normalized_name"] = prepared["name"].map(normalize_company_name)
    prepared["updated_at"] = _current_timestamp()
    prepared = prepared[prepared["name"].ne("") & prepared["cik"].str.fullmatch(r"\d{10}")]
    prepared = prepared.drop_duplicates(subset=["cik", "normalized_name"]).reset_index(drop=True)
    return prepared[REQUIRED_COLUMNS]


def fallback_database() -> pd.DataFrame:
    return prepare_sec_dataframe(pd.DataFrame.from_records(FALLBACK_COMPANIES))


def download_sec_database(timeout: int = 45) -> pd.DataFrame:
    logger.info("Downloading SEC CIK lookup data from %s", SEC_CIK_LOOKUP_URL)
    cik_response = requests.get(SEC_CIK_LOOKUP_URL, headers=_sec_headers(), timeout=timeout)
    cik_response.raise_for_status()
    cik_df = parse_cik_lookup_text(cik_response.text)
    logger.info("Parsed %s SEC CIK lookup records", len(cik_df))

    try:
        logger.info("Downloading SEC ticker data from %s", SEC_COMPANY_TICKERS_URL)
        ticker_response = requests.get(SEC_COMPANY_TICKERS_URL, headers=_sec_headers(), timeout=timeout)
        ticker_response.raise_for_status()
        ticker_df = parse_company_tickers(ticker_response.json())
    except Exception as exc:
        logger.exception("Ticker enrichment failed. Continuing with CIK-only data. Error: %s", exc)
        ticker_df = pd.DataFrame(columns=REQUIRED_COLUMNS)

    enriched = enrich_with_tickers(cik_df, ticker_df)
    logger.info("Built combined SEC database with %s records", len(enriched))
    return enriched


def write_sec_database(df: pd.DataFrame, cache_path: Path | None = None) -> Path:
    cache_path = cache_path or SEC_CACHE_FILE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(cache_path, index=False)
        logger.info("Wrote SEC cache to %s with %s records", cache_path, len(df))
        try:
            write_sqlite_database(df)
        except Exception:
            if not SEC_SQLITE_FILE.exists():
                raise
            logger.exception("SQLite index refresh failed. Keeping existing SQLite cache.")
        return cache_path
    except Exception as exc:
        logger.warning("Parquet write failed (%s). Falling back to CSV.", exc)
        df.to_csv(SEC_CACHE_FALLBACK_FILE, index=False)
        try:
            write_sqlite_database(df)
        except Exception:
            if not SEC_SQLITE_FILE.exists():
                raise
            logger.exception("SQLite index refresh failed. Keeping existing SQLite cache.")
        logger.info("Wrote SEC cache to %s with %s records", SEC_CACHE_FALLBACK_FILE, len(df))
        return SEC_CACHE_FALLBACK_FILE


def write_sqlite_database(df: pd.DataFrame, sqlite_path: Path | None = None) -> Path:
    sqlite_path = sqlite_path or SEC_SQLITE_FILE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    indexed = df[REQUIRED_COLUMNS].copy()
    indexed["first_token"] = indexed["normalized_name"].astype(str).str.split(" ").str[0]
    indexed["fuzzy_prefix"] = indexed["normalized_name"].astype(str).str[:3]

    temp_path = sqlite_path.with_name(f"{sqlite_path.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp.sqlite")
    if temp_path.exists():
        temp_path.unlink()

    conn = sqlite3.connect(temp_path)
    try:
        indexed.to_sql("companies", conn, index=False, if_exists="replace", chunksize=50_000)
        conn.execute("CREATE INDEX idx_companies_normalized_name ON companies(normalized_name)")
        conn.execute("CREATE INDEX idx_companies_cik ON companies(cik)")
        conn.execute("CREATE INDEX idx_companies_ticker ON companies(ticker)")
        conn.execute("CREATE INDEX idx_companies_first_token ON companies(first_token)")
        conn.execute("CREATE INDEX idx_companies_fuzzy_prefix ON companies(fuzzy_prefix)")
        conn.execute("CREATE INDEX idx_companies_prefix_name ON companies(first_token, normalized_name)")
        conn.commit()
    finally:
        conn.close()

    if sqlite_path.exists():
        sqlite_path.unlink()
    temp_path.replace(sqlite_path)
    logger.info("Wrote SEC SQLite index to %s with %s records", sqlite_path, len(indexed))
    return sqlite_path


def ensure_sqlite_database() -> Path:
    if SEC_SQLITE_FILE.exists():
        return SEC_SQLITE_FILE
    with cache_build_lock():
        if SEC_SQLITE_FILE.exists():
            return SEC_SQLITE_FILE
        df = read_sec_database()
        write_sqlite_database(df)
    return SEC_SQLITE_FILE


def read_sec_database(cache_path: Path | None = None) -> pd.DataFrame:
    cache_path = cache_path or SEC_CACHE_FILE
    if cache_path.exists():
        return pd.read_parquet(cache_path)
    if SEC_CACHE_FALLBACK_FILE.exists():
        return pd.read_csv(SEC_CACHE_FALLBACK_FILE, dtype={"cik": str})
    logger.warning("No SEC cache found. Using built-in fallback records.")
    return fallback_database()


def sec_database_summary() -> dict[str, Any]:
    if SEC_SQLITE_FILE.exists():
        size_mb = SEC_SQLITE_FILE.stat().st_size / (1024 * 1024)
        return {"status": f"{size_mb:.1f} MB SQLite", "is_fallback": SEC_SQLITE_FILE.stat().st_size < 100_000}
    if SEC_CACHE_FILE.exists():
        size_mb = SEC_CACHE_FILE.stat().st_size / (1024 * 1024)
        status = f"{size_mb:.1f} MB"
        is_fallback = SEC_CACHE_FILE.stat().st_size < 100_000
        return {"status": status, "is_fallback": is_fallback}
    if SEC_CACHE_FALLBACK_FILE.exists():
        size_kb = SEC_CACHE_FALLBACK_FILE.stat().st_size / 1024
        return {"status": f"{size_kb:.1f} KB CSV", "is_fallback": True}
    return {"status": "fallback", "is_fallback": True}


@contextlib.contextmanager
def cache_build_lock(timeout: int = 360):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = DATA_DIR / ".sec_cache_build.lock"
    start = time.monotonic()
    lock_fd = None

    while True:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(lock_fd, f"{os.getpid()} {time.time()}".encode("utf-8"))
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > timeout:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue

            if time.monotonic() - start > timeout:
                raise TimeoutError("Timed out waiting for SEC cache refresh lock.")
            logger.info("Another SEC cache refresh is already running. Waiting...")
            time.sleep(5)

    try:
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def build_sec_database(force: bool = False) -> pd.DataFrame:
    if not force and (SEC_CACHE_FILE.exists() or SEC_CACHE_FALLBACK_FILE.exists()):
        df = read_sec_database()
        if not SEC_SQLITE_FILE.exists():
            with cache_build_lock():
                if not SEC_SQLITE_FILE.exists():
                    write_sqlite_database(df)
        return df

    with cache_build_lock():
        if not force and (SEC_CACHE_FILE.exists() or SEC_CACHE_FALLBACK_FILE.exists()):
            return read_sec_database()

        try:
            df = download_sec_database()
        except Exception as exc:
            if SEC_CACHE_FILE.exists() or SEC_CACHE_FALLBACK_FILE.exists():
                logger.exception("SEC download failed. Keeping existing cache. Error: %s", exc)
                return read_sec_database()
            logger.exception("SEC download failed and no cache exists. Using fallback database. Error: %s", exc)
            df = fallback_database()

        write_sec_database(df)
        ensure_alias_file()
        return df


def ensure_alias_file(path: Path = ALIAS_FILE) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        pd.DataFrame(
            [
                {"alias": "Google", "canonical_name": "Alphabet Inc.", "cik": "0001652044"},
                {"alias": "Facebook", "canonical_name": "Meta Platforms, Inc.", "cik": "0001326801"},
                {"alias": "Meta", "canonical_name": "Meta Platforms, Inc.", "cik": "0001326801"},
            ]
        ).to_csv(path, index=False)
    return path


def load_aliases(path: Path = ALIAS_FILE) -> pd.DataFrame:
    ensure_alias_file(path)
    aliases = pd.read_csv(path, dtype=str).fillna("")
    required = ["alias", "canonical_name", "cik"]
    for column in required:
        if column not in aliases.columns:
            aliases[column] = ""
    aliases["normalized_alias"] = aliases["alias"].map(normalize_company_name)
    aliases["cik"] = aliases["cik"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(10)
    return aliases[required + ["normalized_alias"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or refresh the local SEC company cache.")
    parser.add_argument("--force", action="store_true", help="Refresh even when a cache already exists.")
    args = parser.parse_args()
    df = build_sec_database(force=args.force)
    print(f"SEC database ready: {len(df):,} companies")


if __name__ == "__main__":
    main()
