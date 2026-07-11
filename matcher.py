from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import pandas as pd
from rapidfuzz import fuzz, process

from config import DEFAULT_MATCH_LIMIT, DEFAULT_SCORE_THRESHOLD, SEC_SQLITE_FILE
from sec_database import ensure_sqlite_database, load_aliases, read_sec_database
from utils import logger, normalize_company_name


@dataclass(frozen=True)
class MatchConfig:
    score_threshold: int = DEFAULT_SCORE_THRESHOLD
    match_limit: int = DEFAULT_MATCH_LIMIT


class SECMatcher:
    def __init__(
        self,
        sec_df: pd.DataFrame | None = None,
        aliases: pd.DataFrame | None = None,
        config: MatchConfig | None = None,
    ) -> None:
        self.config = config or MatchConfig()
        self.sec_df = (sec_df if sec_df is not None else read_sec_database()).copy().fillna("").reset_index(drop=True)
        self.aliases = (aliases if aliases is not None else load_aliases()).copy().fillna("")
        self.index = self._build_index()
        self.alias_index = self._build_alias_index()
        self.ticker_index = self._build_ticker_index()
        self.choices = list(self.index.keys())
        self.prefix_index = self._build_prefix_index()
        self.fuzzy_prefix_index = self._build_fuzzy_prefix_index()
        logger.info("Matcher initialized with %s searchable names", len(self.choices))

    def _build_index(self) -> dict[str, list[int]]:
        index: dict[str, list[int]] = {}
        for row in self.sec_df.itertuples(index=True):
            normalized = str(getattr(row, "normalized_name", "") or normalize_company_name(getattr(row, "name", "")))
            if normalized:
                index.setdefault(normalized, []).append(int(row.Index))
        return index

    def _build_alias_index(self) -> dict[str, dict[str, Any]]:
        alias_index: dict[str, dict[str, Any]] = {}
        sec_by_cik: dict[str, int] = {}
        sec_by_name: dict[str, int] = {}
        for row in self.sec_df.itertuples(index=True):
            cik = str(getattr(row, "cik", "")).zfill(10)
            normalized = str(getattr(row, "normalized_name", "") or normalize_company_name(getattr(row, "name", "")))
            if cik and cik not in sec_by_cik:
                sec_by_cik[cik] = int(row.Index)
            if normalized and normalized not in sec_by_name:
                sec_by_name[normalized] = int(row.Index)

        for alias in self.aliases.to_dict("records"):
            cik = str(alias.get("cik", "")).zfill(10)
            canonical = str(alias.get("canonical_name", ""))
            target_index = sec_by_cik.get(cik)
            if target_index is None:
                target_index = sec_by_name.get(normalize_company_name(canonical))
            if target_index is None:
                continue
            normalized_alias = normalize_company_name(str(alias.get("alias", "")))
            if normalized_alias:
                alias_index[normalized_alias] = self._record_from_index(target_index, source="alias")

        return alias_index

    def _build_ticker_index(self) -> dict[str, int]:
        ticker_index: dict[str, int] = {}
        for row in self.sec_df.itertuples(index=True):
            ticker = str(getattr(row, "ticker", "")).upper().strip()
            if ticker:
                row_index = int(row.Index)
                existing = ticker_index.get(ticker)
                if existing is None or len(normalize_company_name(self.sec_df.at[row_index, "name"])) < len(
                    normalize_company_name(self.sec_df.at[existing, "name"])
                ):
                    ticker_index[ticker] = row_index
        return ticker_index

    def _build_prefix_index(self) -> dict[str, list[str]]:
        prefix_index: dict[str, list[str]] = {}
        for choice in self.index:
            first_token = choice.split(" ", 1)[0]
            if first_token:
                prefix_index.setdefault(first_token, []).append(choice)
        for matches in prefix_index.values():
            matches.sort(key=lambda choice: (len(choice), choice))
        return prefix_index

    def _build_fuzzy_prefix_index(self) -> dict[str, list[str]]:
        fuzzy_prefix_index: dict[str, list[str]] = {}
        for choice in self.index:
            if len(choice) >= 3:
                fuzzy_prefix_index.setdefault(choice[:3], []).append(choice)
        return fuzzy_prefix_index

    def match_name(self, company_name: object) -> dict[str, Any]:
        query = normalize_company_name(company_name)
        if not query:
            return self._empty_result(company_name, query)

        if query in self.ticker_index:
            record = self._record_from_index(self.ticker_index[query], source="ticker")
            return self._result(company_name, query, query, 100, record, "exact")

        if query in self.index:
            record = self._record_from_index(self.index[query][0])
            return self._result(company_name, query, query, 100, record, "exact")

        if query in self.alias_index:
            record = self.alias_index[query]
            return self._result(company_name, query, query, 100, record, "exact")

        prefix_matches = self._prefix_matches(query)
        if prefix_matches:
            matched_key = prefix_matches[0]
            record = self._record_from_index(self.index[matched_key][0])
            status = "matched" if len(prefix_matches) == 1 else "review"
            return self._result(company_name, query, matched_key, 92.0, record, status)

        candidates = self._fuzzy_candidates(query)
        if not candidates:
            return self._empty_result(company_name, query)

        match = process.extractOne(query, candidates, scorer=fuzz.token_sort_ratio)
        if not match:
            return self._empty_result(company_name, query)

        matched_key, score, _ = match
        record = self._record_from_index(self.index[matched_key][0])
        status = "matched" if score >= self.config.score_threshold else "review"
        return self._result(company_name, query, matched_key, round(float(score), 2), record, status)

    def _prefix_matches(self, query: str) -> list[str]:
        if len(query) < 4:
            return []
        prefix = f"{query} "
        first_token = query.split(" ", 1)[0]
        matches = [choice for choice in self.prefix_index.get(first_token, []) if choice.startswith(prefix)]
        return matches

    def _fuzzy_candidates(self, query: str) -> list[str]:
        if len(query) < 3:
            return []
        return self.fuzzy_prefix_index.get(query[:3], [])

    def _record_from_index(self, index: int, source: str = "sec") -> dict[str, Any]:
        row = self.sec_df.iloc[index]
        return {
            "cik": str(row.get("cik", "")).zfill(10),
            "name": row.get("name", ""),
            "ticker": row.get("ticker", ""),
            "source": source,
        }

    def match_dataframe(self, df: pd.DataFrame, company_column: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        if company_column not in df.columns:
            raise KeyError(f"Company column '{company_column}' was not found.")

        results = [self.match_name(value) for value in df[company_column]]
        result_df = pd.DataFrame(results)
        output = pd.concat([df.reset_index(drop=True), result_df], axis=1)
        summary = summarize_matches(output)
        logger.info("Matched %s rows using column '%s'", len(output), company_column)
        return output, summary

    @staticmethod
    def _empty_result(original: object, normalized: str) -> dict[str, Any]:
        return {
            "input_company": original,
            "normalized_company": normalized,
            "match_status": "blank" if not normalized else "unmatched",
            "match_name": "",
            "match_cik": "",
            "match_ticker": "",
            "match_score": 0.0,
            "match_source": "",
            "matched_on": "",
        }

    @staticmethod
    def _result(
        original: object,
        normalized: str,
        matched_on: str,
        score: float,
        record: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        return {
            "input_company": original,
            "normalized_company": normalized,
            "match_status": status,
            "match_name": record.get("name", ""),
            "match_cik": str(record.get("cik", "")).zfill(10),
            "match_ticker": record.get("ticker", ""),
            "match_score": score,
            "match_source": record.get("source", ""),
            "matched_on": matched_on,
        }


def summarize_matches(df: pd.DataFrame) -> pd.DataFrame:
    if "match_status" not in df.columns:
        return pd.DataFrame()
    summary = (
        df["match_status"]
        .value_counts(dropna=False)
        .rename_axis("status")
        .reset_index(name="rows")
        .sort_values(["status"])
        .reset_index(drop=True)
    )
    summary["share"] = (summary["rows"] / max(len(df), 1)).round(3)
    return summary


def match_dataframe(
    df: pd.DataFrame,
    company_column: str,
    score_threshold: int = DEFAULT_SCORE_THRESHOLD,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    matcher = SECMatcher(config=MatchConfig(score_threshold=score_threshold))
    return matcher.match_dataframe(df, company_column)


class SQLiteSECMatcher:
    def __init__(
        self,
        sqlite_path=SEC_SQLITE_FILE,
        aliases: pd.DataFrame | None = None,
        config: MatchConfig | None = None,
    ) -> None:
        self.config = config or MatchConfig()
        self.sqlite_path = sqlite_path if sqlite_path.exists() else ensure_sqlite_database()
        self.aliases = (aliases if aliases is not None else load_aliases()).copy().fillna("")
        self.alias_index = {
            normalize_company_name(row["alias"]): {
                "cik": str(row.get("cik", "")).zfill(10),
                "canonical_name": str(row.get("canonical_name", "")),
            }
            for row in self.aliases.to_dict("records")
            if normalize_company_name(row.get("alias", ""))
        }
        logger.info("SQLite matcher initialized using %s", self.sqlite_path)

    def match_name(self, company_name: object) -> dict[str, Any]:
        query = normalize_company_name(company_name)
        if not query:
            return SECMatcher._empty_result(company_name, query)

        with self._connect() as conn:
            ticker_record = self._query_one(
                conn,
                """
                SELECT * FROM companies
                WHERE ticker = ?
                ORDER BY length(normalized_name), normalized_name
                LIMIT 1
                """,
                (query,),
                source="ticker",
            )
            if ticker_record:
                return SECMatcher._result(company_name, query, query, 100, ticker_record, "exact")

            exact_record = self._query_one(
                conn,
                """
                SELECT * FROM companies
                WHERE normalized_name = ?
                ORDER BY length(normalized_name), normalized_name
                LIMIT 1
                """,
                (query,),
            )
            if exact_record:
                return SECMatcher._result(company_name, query, query, 100, exact_record, "exact")

            alias = self.alias_index.get(query)
            if alias:
                alias_record = self._alias_record(conn, alias)
                if alias_record:
                    return SECMatcher._result(company_name, query, query, 100, alias_record, "exact")

            prefix_matches = self._prefix_matches(conn, query)
            if prefix_matches:
                record = self._row_to_record(prefix_matches[0])
                status = "matched" if len(prefix_matches) == 1 else "review"
                return SECMatcher._result(
                    company_name,
                    query,
                    str(prefix_matches[0]["normalized_name"]),
                    92.0,
                    record,
                    status,
                )

            fuzzy_match = self._fuzzy_match(conn, query)
            if fuzzy_match is None:
                return SECMatcher._empty_result(company_name, query)

            row, score = fuzzy_match
            record = self._row_to_record(row)
            status = "matched" if score >= self.config.score_threshold else "review"
            return SECMatcher._result(
                company_name,
                query,
                str(row["normalized_name"]),
                round(float(score), 2),
                record,
                status,
            )

    def match_dataframe(self, df: pd.DataFrame, company_column: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        if company_column not in df.columns:
            raise KeyError(f"Company column '{company_column}' was not found.")

        results = [self.match_name(value) for value in df[company_column]]
        result_df = pd.DataFrame(results)
        output = pd.concat([df.reset_index(drop=True), result_df], axis=1)
        summary = summarize_matches(output)
        logger.info("Matched %s rows using SQLite cache and column '%s'", len(output), company_column)
        return output, summary

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _query_one(
        self,
        conn: sqlite3.Connection,
        sql: str,
        params: tuple[Any, ...],
        source: str = "sec",
    ) -> dict[str, Any] | None:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return self._row_to_record(row, source=source)

    def _alias_record(self, conn: sqlite3.Connection, alias: dict[str, str]) -> dict[str, Any] | None:
        if alias["cik"].strip("0"):
            record = self._query_one(
                conn,
                """
                SELECT * FROM companies
                WHERE cik = ?
                ORDER BY length(normalized_name), normalized_name
                LIMIT 1
                """,
                (alias["cik"],),
                source="alias",
            )
            if record:
                return record

        canonical = normalize_company_name(alias["canonical_name"])
        if canonical:
            return self._query_one(
                conn,
                """
                SELECT * FROM companies
                WHERE normalized_name = ?
                ORDER BY length(normalized_name), normalized_name
                LIMIT 1
                """,
                (canonical,),
                source="alias",
            )
        return None

    def _prefix_matches(self, conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
        if len(query) < 4:
            return []
        first_token = query.split(" ", 1)[0]
        return conn.execute(
            """
            SELECT * FROM companies
            WHERE first_token = ? AND normalized_name LIKE ?
            ORDER BY length(normalized_name), normalized_name
            LIMIT 25
            """,
            (first_token, f"{query} %"),
        ).fetchall()

    def _fuzzy_match(self, conn: sqlite3.Connection, query: str) -> tuple[sqlite3.Row, float] | None:
        if len(query) < 3:
            return None
        rows = conn.execute(
            """
            SELECT * FROM companies
            WHERE fuzzy_prefix = ?
            ORDER BY length(normalized_name), normalized_name
            LIMIT 5000
            """,
            (query[:3],),
        ).fetchall()
        if not rows:
            return None

        choices = [str(row["normalized_name"]) for row in rows]
        match = process.extractOne(query, choices, scorer=fuzz.token_sort_ratio)
        if not match:
            return None
        _, score, row_index = match
        return rows[row_index], float(score)

    @staticmethod
    def _row_to_record(row: sqlite3.Row, source: str = "sec") -> dict[str, Any]:
        return {
            "cik": str(row["cik"]).zfill(10),
            "name": row["name"],
            "ticker": row["ticker"],
            "source": source,
        }
