# SEC CIK Matcher

A Streamlit app for matching uploaded company names to SEC company metadata and exporting enriched CSV or Excel files.

## Features

- Polished Streamlit upload, demo, review, and export workflow
- Dedicated SEC cache builder using the broad SEC CIK lookup feed plus ticker enrichment
- Persistent local cache in `data/sec_companies.parquet` with CSV fallback
- High-performance fuzzy matching with RapidFuzz
- Alias management through `data/aliases.csv`
- Automatic company-name column detection
- Demo mode when no file is uploaded
- CSV and Excel download buttons
- Application logging to `logs/app.log`
- Pytest coverage and GitHub Actions CI
- Streamlit Community Cloud-ready project layout

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set a descriptive SEC user agent before refreshing live SEC data:

```powershell
$env:SEC_USER_AGENT="your-name your-email@example.com"
python sec_database.py --force
```

Run the app locally:

```powershell
streamlit run app.py
```

Run tests:

```powershell
pytest -q
```

## Deployment On Streamlit Community Cloud

1. Push this folder to a GitHub repository.
2. In Streamlit Community Cloud, create a new app from that repository.
3. Set the main file path to `app.py`.
4. Add a secret or environment variable named `SEC_USER_AGENT` with your contact details.
5. Deploy. The app can run with fallback demo data immediately and can refresh the SEC cache from the sidebar.

## Input Format

Upload a `.csv`, `.xlsx`, or `.xls` file with one column containing company names. The app guesses the company column, but you can override it before matching.

## Alias Management

Edit `data/aliases.csv` to add known short names, historical names, or common misspellings.

Required columns:

- `alias`
- `canonical_name`
- `cik`

## SEC Data Notes

The app first downloads `https://www.sec.gov/Archives/edgar/cik-lookup-data.txt` to match company names and retrieve CIK numbers. It then downloads `https://www.sec.gov/files/company_tickers.json` and enriches rows with ticker symbols by CIK when a public ticker exists. SEC access policies expect automated tools to identify themselves with a descriptive `User-Agent`; use `SEC_USER_AGENT` for that value.
