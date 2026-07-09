from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from rapidfuzz import fuzz, process

from config import DEFAULT_MATCH_LIMIT, DEFAULT_SCORE_THRESHOLD
from sec_database import load_aliases, read_sec_database
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
        self.sec_df = (sec_df if sec_df is not None else read_sec_database()).copy().fillna("")
        self.aliases = (aliases if aliases is not None else load_aliases()).copy().fillna("")
        self.index = self._build_index()
        self.ticker_index = self._build_ticker_index()
        self.choices = list(self.index.keys())
        logger.info("Matcher initialized with %s searchable names", len(self.choices))

    def _build_index(self) -> dict[str, list[dict[str, Any]]]:
        index: dict[str, list[dict[str, Any]]] = {}

        def add_choice(choice: str, record: dict[str, Any]) -> None:
            normalized = normalize_company_name(choice)
            if normalized:
                index.setdefault(normalized, []).append(record)

        for row in self.sec_df.to_dict("records"):
            record = {
                "cik": str(row.get("cik", "")).zfill(10),
                "name": row.get("name", ""),
                "ticker": row.get("ticker", ""),
                "source": "sec",
            }
            add_choice(record["name"], record)

        sec_by_cik = {
            str(row.get("cik", "")).zfill(10): row
            for row in self.sec_df.to_dict("records")
            if str(row.get("cik", "")).strip()
        }
        sec_by_name = {
            normalize_company_name(row.get("name", "")): row
            for row in self.sec_df.to_dict("records")
            if str(row.get("name", "")).strip()
        }

        for alias in self.aliases.to_dict("records"):
            cik = str(alias.get("cik", "")).zfill(10)
            canonical = str(alias.get("canonical_name", ""))
            target = sec_by_cik.get(cik) or sec_by_name.get(normalize_company_name(canonical))
            if not target:
                continue
            record = {
                "cik": str(target.get("cik", "")).zfill(10),
                "name": target.get("name", canonical),
                "ticker": target.get("ticker", ""),
                "source": "alias",
            }
            add_choice(str(alias.get("alias", "")), record)

        return index

    def _build_ticker_index(self) -> dict[str, dict[str, Any]]:
        ticker_index = {}
        for row in self.sec_df.to_dict("records"):
            ticker = str(row.get("ticker", "")).upper().strip()
            if ticker:
                record = {
                    "cik": str(row.get("cik", "")).zfill(10),
                    "name": row.get("name", ""),
                    "ticker": ticker,
                    "source": "ticker",
                }
                existing = ticker_index.get(ticker)
                if not existing or len(normalize_company_name(record["name"])) < len(
                    normalize_company_name(existing["name"])
                ):
                    ticker_index[ticker] = record
        return ticker_index

    def match_name(self, company_name: object) -> dict[str, Any]:
        query = normalize_company_name(company_name)
        if not query:
            return self._empty_result(company_name, query)

        if query in self.ticker_index:
            record = self.ticker_index[query]
            return self._result(company_name, query, query, 100, record, "exact")

        if query in self.index:
            record = self.index[query][0]
            return self._result(company_name, query, query, 100, record, "exact")

        prefix_matches = self._prefix_matches(query)
        if prefix_matches:
            matched_key = prefix_matches[0]
            record = self.index[matched_key][0]
            status = "matched" if len(prefix_matches) == 1 else "review"
            return self._result(company_name, query, matched_key, 92.0, record, status)

        match = process.extractOne(query, self.choices, scorer=fuzz.token_sort_ratio)
        if not match:
            return self._empty_result(company_name, query)

        matched_key, score, _ = match
        record = self.index[matched_key][0]
        status = "matched" if score >= self.config.score_threshold else "review"
        return self._result(company_name, query, matched_key, round(float(score), 2), record, status)

    def _prefix_matches(self, query: str) -> list[str]:
        if len(query) < 4:
            return []
        prefix = f"{query} "
        matches = [choice for choice in self.choices if choice.startswith(prefix)]
        matches.sort(key=lambda choice: (len(choice), choice))
        return matches

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
