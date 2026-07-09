import pandas as pd

from utils import detect_company_column, normalize_company_name


def test_normalize_company_name_removes_suffixes_and_punctuation():
    assert normalize_company_name("The Apple, Inc.") == "APPLE"
    assert normalize_company_name("AT&T Corporation") == "AT AND T"


def test_detect_company_column_prefers_name_like_column():
    df = pd.DataFrame(
        {
            "amount": [10, 20],
            "issuer_name": ["Apple Inc.", "Microsoft Corp"],
            "id": [1, 2],
        }
    )

    assert detect_company_column(df) == "issuer_name"
