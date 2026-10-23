"""The fixed research universe, validated before any market-data request."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path


FIXED_WATCHLIST_PATH = Path(__file__).resolve().with_name("research_watchlist.csv")
FIXED_WATCHLIST_SIZE = 100


def load_fixed_watchlist(path: str | Path = FIXED_WATCHLIST_PATH) -> tuple[list[dict[str, str]], str]:
    """Return exactly 100 unique ticker/CIK pairs and the input's version hash.

    A missing or invalid file never falls back to the legacy market-wide universe.
    The optional path is for offline checks; production always uses the checked-in file.
    """
    path = Path(path)
    payload = path.read_bytes()
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    fields = reader.fieldnames or []
    if not {"Ticker", "CIK"}.issubset(fields) or len(set(fields)) != len(fields):
        raise ValueError(f"{path.name} 必須包含唯一的 Ticker、CIK 欄位。")
    rows = list(reader)
    if len(rows) != FIXED_WATCHLIST_SIZE:
        raise ValueError(
            f"固定研究名單必須恰好 {FIXED_WATCHLIST_SIZE} 家，實際 {len(rows)} 家；"
            "停止執行，不補入全市場股票。"
        )
    tickers: set[str] = set()
    ciks: set[str] = set()
    for index, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"{path.name} 第 {index} 列 CSV 欄位數不符。")
        ticker = row["Ticker"].strip().upper()
        cik = row["CIK"].strip()
        if not re.fullmatch(r"[A-Z]{1,6}(?:[-.][A-Z])?", ticker):
            raise ValueError(f"{path.name} 第 {index} 列 Ticker 無效：{ticker!r}")
        if not re.fullmatch(r"[0-9]{1,10}", cik) or int(cik) <= 0:
            raise ValueError(f"{path.name} 第 {index} 列 CIK 無效：{cik!r}")
        cik = cik.zfill(10)
        if ticker in tickers or cik in ciks:
            raise ValueError(f"{path.name} 第 {index} 列 Ticker 或 CIK 重複：{ticker}")
        tickers.add(ticker)
        ciks.add(cik)
        row["Ticker"], row["CIK"] = ticker, cik
    return rows, hashlib.sha256(payload).hexdigest()[:16]


def main() -> int:
    rows, version = load_fixed_watchlist()
    print(f"固定研究名單已驗證：{len(rows)} 家，版本 {version}；未連網。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
