import pandas as pd
import pytest

from config import ALIAS_FILE
import sec_database
from sec_database import (
    enrich_with_tickers,
    fallback_database,
    load_aliases,
    parse_cik_lookup_text,
    parse_company_tickers,
)


def test_parse_company_tickers_normalizes_cik_and_ticker():
    payload = {
        "0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    }

    df = parse_company_tickers(payload)

    assert list(df["cik"]) == ["0000320193", "0000789019"]
    assert list(df["ticker"]) == ["AAPL", "MSFT"]
    assert "normalized_name" in df.columns


def test_parse_cik_lookup_text_includes_private_companies_without_tickers():
    text = "PRIVATE COMPANY LLC:123456:\nWALMART INC:104169:\n"

    df = parse_cik_lookup_text(text)

    assert list(df["cik"]) == ["0000123456", "0000104169"]
    assert list(df["ticker"]) == ["", ""]
    assert "PRIVATE" in set(df["normalized_name"])


def test_enrich_with_tickers_maps_ticker_by_cik():
    cik_df = parse_cik_lookup_text("PRIVATE COMPANY LLC:123456:\nWALMART INC:104169:\n")
    ticker_df = parse_company_tickers(
        {"0": {"cik_str": 104169, "ticker": "WMT", "title": "Walmart Inc."}}
    )

    enriched = enrich_with_tickers(cik_df, ticker_df)

    assert enriched.loc[enriched["cik"].eq("0000123456"), "ticker"].iloc[0] == ""
    assert enriched.loc[enriched["cik"].eq("0000104169"), "ticker"].iloc[0] == "WMT"


def test_fallback_database_contains_demo_alias_targets():
    df = fallback_database()

    assert {"0001652044", "0001326801"}.issubset(set(df["cik"]))


def test_alias_file_loads_quoted_company_names():
    aliases = load_aliases(ALIAS_FILE)

    assert "Meta Platforms, Inc." in set(aliases["canonical_name"])
    assert aliases.loc[aliases["alias"].eq("Meta"), "cik"].iloc[0] == "0001326801"


def test_failed_forced_refresh_keeps_existing_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "sec_companies.parquet"
    fallback_path = tmp_path / "sec_companies.csv"
    alias_path = tmp_path / "aliases.csv"
    existing = sec_database.prepare_sec_dataframe(
        pd.DataFrame(
            [
                {
                    "cik": "0000104169",
                    "name": "Walmart Inc.",
                    "ticker": "WMT",
                    "source": "test",
                }
            ]
        )
    )
    existing.to_parquet(cache_path, index=False)

    monkeypatch.setattr(sec_database, "SEC_CACHE_FILE", cache_path)
    monkeypatch.setattr(sec_database, "SEC_CACHE_FALLBACK_FILE", fallback_path)
    monkeypatch.setattr(sec_database, "ALIAS_FILE", alias_path)
    monkeypatch.setattr(
        sec_database,
        "download_sec_database",
        lambda: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )

    refreshed = sec_database.build_sec_database(force=True)

    assert len(refreshed) == 1
    assert refreshed["name"].iloc[0] == "Walmart Inc."
    assert len(sec_database.read_sec_database(cache_path)) == 1
