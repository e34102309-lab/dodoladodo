"""Bounded SEC original-filing snapshots for the approved 61-company review."""
import concurrent.futures
import json
from pathlib import Path
import argparse

import pandas as pd

from AQR_ModeC_Agent_V12 import SECDataDistiller, require_sec_contact_email, _GLOBAL_SEC_SESSION
from mode_c_sec_filings import collect_filing, select_recent_filings
from mode_c_watchlist import load_fixed_watchlist

PLAN = Path(__file__).with_name("sec_filing_review.json")


def recent_filings(payload, decision):
    return select_recent_filings(payload, decision)


def collect_company(target, email, decision, output):
    ticker, cik = target["ticker"], target["cik"]
    sec = SECDataDistiller(email, ticker=ticker, cik=cik, decision_timestamp=decision)
    response = sec.session.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=sec.headers)
    if response is None:
        result = {**target, "filings": [], "errors": ["SUBMISSIONS_UNAVAILABLE"]}
    else:
        submission = response.json()
        (output / f"{ticker}_submissions.json").write_text(json.dumps(submission), encoding="utf-8")
        # Source snapshot for offline replay, never an authorization to change the universe.
        facts = sec._fetch_companyfacts(cik)
        (output / f"{ticker}_companyfacts.json").write_text(json.dumps({"facts": facts}), encoding="utf-8")
        filings = [collect_filing(sec.session, sec.headers, cik, row) for row in recent_filings(submission, decision)]
        result = {**target, "filings": filings, "errors": []}
    result["decision_timestamp"] = decision.isoformat()
    (output / f"{ticker}_filings.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return {"ticker": ticker, "filings": len(result["filings"]),
            "facts": sum(len(f.get("facts", [])) for f in result["filings"]),
            "errors": result["errors"] + [e for f in result["filings"] for e in f.get("errors", [])]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="sec_filing_snapshot")
    args = parser.parse_args()
    watchlist, version = load_fixed_watchlist()
    approved = {r["Ticker"]: r["CIK"] for r in watchlist}
    targets = json.loads(PLAN.read_text(encoding="utf-8"))["targets"]
    if len(targets) != 61 or len({r["ticker"] for r in targets}) != 61 or any(approved.get(r["ticker"]) != r["cik"] for r in targets):
        raise ValueError("Review targets must be the approved 61 companies within the fixed 100")
    email = require_sec_contact_email()
    decision = pd.Timestamp.now(tz="UTC")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        pending = [pool.submit(collect_company, t, email, decision, output) for t in targets]
        for index, future in enumerate(concurrent.futures.as_completed(pending), 1):
            rows.append(future.result())
            print(json.dumps({"progress": index, "total": len(targets), **rows[-1]}), flush=True)
    summary = {"watchlist_version": version, "decision_timestamp": decision.isoformat(), "companies": rows}
    (output / "snapshot_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
