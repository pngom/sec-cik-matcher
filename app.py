from __future__ import annotations

import pandas as pd
import streamlit as st

from config import (
    APP_NAME,
    DEFAULT_SCORE_THRESHOLD,
    DEMO_FILE,
)
from matcher import MatchConfig, SQLiteSECMatcher
from sec_database import load_aliases
from utils import (
    dataframe_to_csv_bytes,
    dataframe_to_excel_bytes,
    detect_company_column,
    logger,
    read_uploaded_table,
)


st.set_page_config(page_title=APP_NAME, page_icon=":mag:", layout="wide")

st.markdown(
    """
    <style>
    .stApp {
        background: #fbfaf7;
        color: #20272b;
    }
    .block-container {
        max-width: 1080px;
        padding-top: 4.2rem;
        padding-bottom: 4rem;
    }
    h1, h2, h3, p, label, [data-testid="stMarkdownContainer"] {
        font-family: "Source Sans Pro", "Segoe UI", sans-serif;
    }
    h1 {
        font-weight: 400;
        letter-spacing: -0.035em;
        margin-bottom: 0.45rem;
    }
    h2, h3 {
        font-weight: 400;
        letter-spacing: -0.02em;
    }
    [data-testid="stFileUploader"] {
        background: #f1f0eb;
        border: 1px solid #e2e0d8;
        border-radius: 10px;
        padding: 0.45rem 0.8rem;
    }
    [data-testid="stFileUploaderDropzone"] {
        border: 0;
        background: transparent;
    }
    [data-testid="stExpander"] {
        border: 1px solid #dedbd3;
        border-radius: 8px;
        background: transparent;
    }
    [data-testid="stDataFrame"] {
        border: 1px solid #dedbd3;
        border-radius: 8px;
    }
    .eyebrow {
        color: #6d716e;
        font-size: 0.78rem;
        font-weight: 600;
        letter-spacing: 0.075em;
        margin-bottom: 1rem;
    }
    .app-subtitle {
        color: #5f6561;
        font-size: 1.08rem;
        max-width: 760px;
        margin-bottom: 2.2rem;
    }
    .results-summary {
        color: #5f6561;
        margin: -0.35rem 0 1.25rem;
    }
    [data-testid="stButton"] button[kind="primary"] {
        background: #356b5c;
        border-color: #356b5c;
        color: #ffffff;
    }
    [data-testid="stButton"] button[kind="primary"]:hover {
        background: #2a584c;
        border-color: #2a584c;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def load_demo() -> pd.DataFrame:
    return pd.read_csv(DEMO_FILE)


@st.cache_resource(show_spinner=False)
def get_matcher(score_threshold: int, cache_signature: tuple[str, int, int]) -> SQLiteSECMatcher:
    return SQLiteSECMatcher(
        aliases=load_aliases(),
        config=MatchConfig(score_threshold=score_threshold),
    )


def sec_cache_signature() -> tuple[str, int, int]:
    from config import SEC_CACHE_FALLBACK_FILE, SEC_CACHE_FILE, SEC_SQLITE_FILE

    for path in (SEC_SQLITE_FILE, SEC_CACHE_FILE, SEC_CACHE_FALLBACK_FILE):
        if path.exists():
            stat = path.stat()
            return (path.name, stat.st_size, int(stat.st_mtime))
    return ("fallback", 0, 0)


def source_dataframe(uploaded_file) -> tuple[pd.DataFrame, str]:
    if uploaded_file is None:
        return load_demo(), "Demo file"
    return read_uploaded_table(uploaded_file), uploaded_file.name


def main() -> None:
    threshold = DEFAULT_SCORE_THRESHOLD
    st.markdown('<div class="eyebrow">COMPANY MATCHING</div>', unsafe_allow_html=True)
    st.title("Match company names")
    st.markdown(
        '<p class="app-subtitle">Upload a list of company names and get their official SEC company ID (CIK), ticker symbol, and best match. You can download the completed list when you are done.</p>',
        unsafe_allow_html=True,
    )

    uploaded_file = st.file_uploader("Upload your company list", type=["csv", "xlsx", "xls"])

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
    company_column = st.selectbox(
        "Which column contains the company names?",
        options=list(df.columns),
        index=list(df.columns).index(auto_column),
    )
    st.caption(f"Using {source_name.lower()} with {len(df):,} companies.")

    with st.expander("Preview your list", expanded=uploaded_file is None):
        st.dataframe(df.head(100), width="stretch", hide_index=True)

    with st.expander("Matching options"):
        threshold = st.slider(
            "How certain should a match be before it is marked ready?",
            min_value=60,
            max_value=100,
            value=DEFAULT_SCORE_THRESHOLD,
            help="Lower settings match more names automatically. Higher settings send more names to review.",
        )

    run_match = st.button("Find matches", type="primary", width="stretch")

    if run_match:
        with st.spinner("Looking for company matches..."):
            matcher = get_matcher(threshold, sec_cache_signature())
            matched_df, summary = matcher.match_dataframe(df, company_column)
            st.session_state["matched_df"] = matched_df
            st.session_state["summary"] = summary

    matched_df = st.session_state.get("matched_df")
    summary = st.session_state.get("summary")
    if matched_df is None:
        st.info("Upload a company list, then choose Find matches. You can also try the included example.")
        return

    st.subheader("Your matches")
    exact_or_matched = int(matched_df["match_status"].isin(["exact", "matched"]).sum())
    review = int(matched_df["match_status"].eq("review").sum())
    unmatched = int(matched_df["match_status"].isin(["unmatched", "blank"]).sum())

    st.markdown(
        f'<p class="results-summary"><strong>{exact_or_matched:,}</strong> matched &nbsp;·&nbsp; '
        f'<strong>{review:,}</strong> to review &nbsp;·&nbsp; <strong>{unmatched:,}</strong> not found</p>',
        unsafe_allow_html=True,
    )

    tabs = st.tabs(["All matches", "Review these", "Match summary"])
    with tabs[0]:
        st.dataframe(matched_df, width="stretch", hide_index=True)
    with tabs[1]:
        review_df = matched_df[matched_df["match_status"].isin(["review", "unmatched", "blank"])]
        st.dataframe(review_df, width="stretch", hide_index=True)
    with tabs[2]:
        st.dataframe(summary, width="stretch", hide_index=True)

    download_cols = st.columns([1, 1, 2])
    with download_cols[0]:
        st.download_button(
            "Download CSV",
            data=dataframe_to_csv_bytes(matched_df),
            file_name="sec_cik_matches.csv",
            mime="text/csv",
            width="stretch",
        )
    with download_cols[1]:
        st.download_button(
            "Download Excel",
            data=dataframe_to_excel_bytes(matched_df),
            file_name="sec_cik_matches.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )


if __name__ == "__main__":
    main()
