from pathlib import Path

APP_NAME = "SEC CIK Matcher"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

DEMO_FILE = DATA_DIR / "demo_companies.csv"
ALIAS_FILE = DATA_DIR / "aliases.csv"
SEC_CACHE_FILE = DATA_DIR / "sec_companies.parquet"
SEC_CACHE_FALLBACK_FILE = DATA_DIR / "sec_companies.csv"

SEC_CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_USER_AGENT_ENV = "SEC_USER_AGENT"

DEFAULT_MATCH_LIMIT = 3
DEFAULT_SCORE_THRESHOLD = 86
