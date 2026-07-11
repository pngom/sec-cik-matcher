from __future__ import annotations

import pandas as pd
import streamlit as st

from config import (
    ALIAS_FILE,
    APP_NAME,
    DEFAULT_SCORE_THRESHOLD,
    DEMO_FILE,
    SEC_CACHE_FALLBACK_FILE,
    SEC_CACHE_FILE,
)
from matcher import MatchConfig, SECMatcher
import sec_database
from sec_database import build_sec_database, load_aliases, read_sec_database
from utils import (
    dataframe_to_csv_bytes,
    dataframe_to_excel_bytes,
    describe_file,
    detect_company_column,
    logger,
    read_uploaded_table,
)


st.set_page_config(page_title=APP_NAME, page_icon=":mag:", layout="wide")

st.markdown(
    """
    <style>
    .block-container { padding-top: 1.7rem; max-width: 1180px; }
    div[data-testid="stMetric"] {
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        padding: 0.8rem 0.9rem;
        background: #ffffff;
    }
    .app-subtitle { color: #4b5563; font-size: 1.02rem; margin-top: -0.5rem; }
    .status-pill {
        display: inline-block;
        border: 1px solid #d1d5db;
        border-radius: 999px;
        padding: 0.15rem 0.55rem;
        color: #374151;
        background: #f9fafb;
        font-size: 0.82rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def load_demo() -> pd.DataFrame:
    return pd.read_csv(DEMO_FILE)


@st.cache_data(show_spinner=False)
def load_cached_sec_data(cache_signature: tuple[str, int, int]) -> pd.DataFrame:
    return read_sec_database()


@st.cache_resource(show_spinner=False)
def get_matcher(score_threshold: int, cache_signature: tuple[str, int, int]) -> SECMatcher:
    return SECMatcher(
        sec_df=load_cached_sec_data(cache_signature),
        aliases=load_aliases(),
        config=MatchConfig(score_threshold=score_threshold),
    )


def sec_cache_signature() -> tuple[str, int, int]:
    for path in (SEC_CACHE_FILE, SEC_CACHE_FALLBACK_FILE):
        if path.exists():
            stat = path.stat()
            return (path.name, stat.st_size, int(stat.st_mtime))
    return ("fallback", 0, 0)


def get_sec_database_summary() -> dict[str, object]:
    if hasattr(sec_database, "sec_database_summary"):
        return sec_database.sec_database_summary()
    if SEC_CACHE_FILE.exists():
        size_mb = SEC_CACHE_FILE.stat().st_size / (1024 * 1024)
        return {"status": f"{size_mb:.1f} MB", "is_fallback": SEC_CACHE_FILE.stat().st_size < 100_000}
    if SEC_CACHE_FALLBACK_FILE.exists():
        size_kb = SEC_CACHE_FALLBACK_FILE.stat().st_size / 1024
        return {"status": f"{size_kb:.1f} KB CSV", "is_fallback": True}
    return {"status": "fallback", "is_fallback": True}


def reset_caches() -> None:
    load_cached_sec_data.clear()
    get_matcher.clear()


def source_dataframe(uploaded_file) -> tuple[pd.DataFrame, str]:
    if uploaded_file is None:
        return load_demo(), "Demo file"
    return read_uploaded_table(uploaded_file), uploaded_file.name


def render_sidebar() -> int:
    with st.sidebar:
        st.header("Data")
        cache_summary = get_sec_database_summary()
        st.metric("SEC cache", cache_summary["status"])
        if cache_summary["is_fallback"]:
            st.warning("SEC cache is using the small fallback dataset. Refresh with network access for full matching.")
        st.caption("Persistent cache")
        st.write(f"SEC Parquet: `{describe_file(SEC_CACHE_FILE)}`")
        st.write(f"SEC CSV: `{describe_file(SEC_CACHE_FALLBACK_FILE)}`")
        st.write(f"Aliases: `{describe_file(ALIAS_FILE)}`")

        if st.button("Refresh SEC cache", use_container_width=True):
            with st.spinner("Downloading SEC company data..."):
                build_sec_database(force=True)
                reset_caches()
            st.success("SEC cache refreshed.")

        st.divider()
        st.header("Matching")
        threshold = st.slider(
            "Review threshold",
            min_value=60,
            max_value=100,
            value=DEFAULT_SCORE_THRESHOLD,
            help="Rows below this fuzzy score are flagged for review.",
        )
        st.caption("Set `SEC_USER_AGENT` in Streamlit secrets for production SEC requests.")
        return threshold


def main() -> None:
    threshold = render_sidebar()

    st.image(
        "https://pngom.github.io/PNgom%20Profile%20Picture%20resized.jpg",
        caption="Pappe Ngom",
        width=140,
    )
    st.title(APP_NAME)
    st.markdown(
        '<p class="app-subtitle">Upload company names, match them to SEC identifiers, review low-confidence rows, and export a clean file.</p>',
        unsafe_allow_html=True,
    )

    uploaded_file = st.file_uploader("Upload CSV or Excel", type=["csv", "xlsx", "xls"])

    try:
        df, source_name = source_dataframe(uploaded_file)
    except Exception as exc:
        logger.exception("Failed to read uploaded file: %s", exc)
        st.error(f"Could not read that file: {exc}")
        return

    if df.empty:
        st.warning("The selected file has no rows.")
        return

    auto_column = detect_company_column(df)
    top_cols = st.columns([1.4, 1, 1])
    with top_cols[0]:
        company_column = st.selectbox(
            "Company name column",
            options=list(df.columns),
            index=list(df.columns).index(auto_column),
        )
    with top_cols[1]:
        st.metric("Input rows", f"{len(df):,}")
    with top_cols[2]:
        st.metric("Input columns", f"{len(df.columns):,}")

    st.markdown(f'<span class="status-pill">Source: {source_name}</span>', unsafe_allow_html=True)

    with st.expander("Preview input", expanded=uploaded_file is None):
        st.dataframe(df.head(100), use_container_width=True, hide_index=True)

    run_match = st.button("Match companies", type="primary", use_container_width=True)

    if run_match:
        with st.spinner("Matching against SEC cache..."):
            matcher = get_matcher(threshold, sec_cache_signature())
            matched_df, summary = matcher.match_dataframe(df, company_column)
            st.session_state["matched_df"] = matched_df
            st.session_state["summary"] = summary

    matched_df = st.session_state.get("matched_df")
    summary = st.session_state.get("summary")
    if matched_df is None:
        st.info("Use the demo data or upload your own file, then run the match.")
        return

    st.subheader("Results")
    exact_or_matched = int(matched_df["match_status"].isin(["exact", "matched"]).sum())
    review = int(matched_df["match_status"].eq("review").sum())
    unmatched = int(matched_df["match_status"].isin(["unmatched", "blank"]).sum())

    metric_cols = st.columns(4)
    metric_cols[0].metric("Matched", f"{exact_or_matched:,}")
    metric_cols[1].metric("Needs review", f"{review:,}")
    metric_cols[2].metric("Unmatched", f"{unmatched:,}")
    metric_cols[3].metric("Best avg score", f"{matched_df['match_score'].mean():.1f}")

    tabs = st.tabs(["Matched File", "Review Queue", "Summary", "Aliases"])
    with tabs[0]:
        st.dataframe(matched_df, use_container_width=True, hide_index=True)
    with tabs[1]:
        review_df = matched_df[matched_df["match_status"].isin(["review", "unmatched", "blank"])]
        st.dataframe(review_df, use_container_width=True, hide_index=True)
    with tabs[2]:
        st.dataframe(summary, use_container_width=True, hide_index=True)
    with tabs[3]:
        aliases = load_aliases()
        st.dataframe(aliases[["alias", "canonical_name", "cik"]], use_container_width=True, hide_index=True)

    download_cols = st.columns([1, 1, 2])
    with download_cols[0]:
        st.download_button(
            "Download CSV",
            data=dataframe_to_csv_bytes(matched_df),
            file_name="sec_cik_matches.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with download_cols[1]:
        st.download_button(
            "Download Excel",
            data=dataframe_to_excel_bytes(matched_df),
            file_name="sec_cik_matches.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
