import pandas as pd

from matcher import MatchConfig, SECMatcher, SQLiteSECMatcher
from sec_database import fallback_database
import sec_database


def test_matcher_finds_exact_and_alias_matches():
    aliases = pd.DataFrame(
        [
            {"alias": "Google", "canonical_name": "Alphabet Inc.", "cik": "0001652044"},
            {"alias": "Facebook", "canonical_name": "Meta Platforms, Inc.", "cik": "0001326801"},
        ]
    )
    matcher = SECMatcher(sec_df=fallback_database(), aliases=aliases, config=MatchConfig(score_threshold=86))

    google = matcher.match_name("Google")
    apple = matcher.match_name("Apple Incorporated")

    assert google["match_cik"] == "0001652044"
    assert google["match_source"] == "alias"
    assert apple["match_cik"] == "0000320193"
    assert apple["match_status"] in {"exact", "matched"}


def test_match_dataframe_appends_match_columns():
    matcher = SECMatcher(sec_df=fallback_database(), aliases=pd.DataFrame(), config=MatchConfig(score_threshold=86))
    df = pd.DataFrame({"company": ["Microsoft", "Unknown Private LLC"]})

    output, summary = matcher.match_dataframe(df, "company")

    assert "match_cik" in output.columns
    assert len(output) == 2
    assert int(summary["rows"].sum()) == 2


def test_tickers_match_exactly_but_are_not_fuzzy_choices():
    sec_df = pd.DataFrame(
        [
            {"cik": "0001002242", "name": "ENI SPA", "ticker": "E", "source": "test"},
            {"cik": "0000104169", "name": "Walmart Inc.", "ticker": "WMT", "source": "test"},
        ]
    )
    matcher = SECMatcher(sec_df=sec_df, aliases=pd.DataFrame(), config=MatchConfig(score_threshold=86))

    ticker = matcher.match_name("E")
    panera = matcher.match_name("PANERA")

    assert ticker["match_cik"] == "0001002242"
    assert ticker["match_status"] == "exact"
    assert panera["matched_on"] != "E"


def test_prefix_match_beats_bad_short_fuzzy_match():
    sec_df = pd.DataFrame(
        [
            {"cik": "0001427543", "name": "E CO", "ticker": "", "source": "test"},
            {"cik": "0001869083", "name": "PANERA BRANDS, INC.", "ticker": "", "source": "test"},
            {"cik": "0000724606", "name": "PANERA BREAD CO", "ticker": "", "source": "test"},
        ]
    )
    matcher = SECMatcher(sec_df=sec_df, aliases=pd.DataFrame(), config=MatchConfig(score_threshold=86))

    panera = matcher.match_name("PANERA")

    assert panera["match_cik"] == "0000724606"
    assert panera["matched_on"] == "PANERA BREAD"
    assert panera["match_status"] == "review"


def test_ticker_index_keeps_first_company_name_for_duplicate_cik_rows():
    sec_df = pd.DataFrame(
        [
            {"cik": "0000200406", "name": "JOHNSON & JOHNSON", "ticker": "JNJ", "source": "test"},
            {"cik": "0000200406", "name": "JOHNSON & JOHNSON ET AL", "ticker": "JNJ", "source": "test"},
        ]
    )
    matcher = SECMatcher(sec_df=sec_df, aliases=pd.DataFrame(), config=MatchConfig(score_threshold=86))

    jnj = matcher.match_name("JNJ")

    assert jnj["match_name"] == "JOHNSON & JOHNSON"


def test_sqlite_matcher_matches_without_in_memory_index(tmp_path):
    sqlite_path = tmp_path / "sec_companies.sqlite"
    sec_df = sec_database.prepare_sec_dataframe(
        pd.DataFrame(
            [
                {"cik": "0000104169", "name": "WALMART INC.", "ticker": "WMT", "source": "test"},
                {"cik": "0000724606", "name": "PANERA BREAD CO", "ticker": "", "source": "test"},
            ]
        )
    )
    sec_database.write_sqlite_database(sec_df, sqlite_path=sqlite_path)
    matcher = SQLiteSECMatcher(sqlite_path=sqlite_path, aliases=pd.DataFrame(), config=MatchConfig(score_threshold=86))

    walmart = matcher.match_name("Walmart")
    panera = matcher.match_name("Panera")

    assert walmart["match_cik"] == "0000104169"
    assert walmart["match_ticker"] == "WMT"
    assert panera["match_cik"] == "0000724606"
