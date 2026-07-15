from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from build_mode_c_dashboard import build_dashboard
from enhance_dashboard_ui import enhance_dashboard
from mode_c_evidence import EVIDENCE_COLUMNS
from mode_c_metric_contract import annotate_dataframe
from validate_mode_c_outputs import (
    build_zero_classification_report,
    validate_outputs,
)


DECISION_TIMESTAMP = "2026-07-16T00:00:00+00:00"


def _base_row(ticker: str) -> dict[str, Any]:
    return {
        "Ticker": ticker,
        "Status": "Pass",
        "Scoring_Framework": "GENERAL_CORPORATE_V13",
        "Input_Security_Class": "COMMON_OR_EQUIVALENT",
        "Security_Class_Confidence": "HIGH",
        "Security_Class_Evidence_Source": "fixture security master",
        "Initial_Industry_Model_Key": "GENERAL_CORPORATE",
        "Model_Route_Refined": False,
        "Model_Route_Reason": "fixture route",
        "Model_Route": "GENERAL_CORPORATE",
        "Model_Supported": True,
        "Industry_Model_Key": "GENERAL_CORPORATE",
        "Industry_Model_Decision": "",
        "Industry_Model_Score": None,
        "Industry_Model_Coverage": None,
        "Industry_Model_Metrics_JSON": "{}",
        "Industry_Model_Components_JSON": "{}",
        "Industry_Model_Required_Missing": "",
        "Industry_Model_Optional_Missing": "",
        "Decision_State": "PASS",
        "Decision_Timestamp": DECISION_TIMESTAMP,
        "Data_Confidence_Score": 90.0,
        "Data_Confidence_Reasons": "fixture with complete point-in-time inputs",
        "Long_Term_Score": 78.0,
        "Long_Term_Eligible": False,
        "Stress_Survival_30": True,
        "Persistent_Dilution": False,
        "Share_Count_Change_pct": 0.5,
        "Share_Count_Change_3Y_pct": 1.5,
        "Share_Basis_Discontinuity": False,
        "Industry_Stress_Extension_Status": "IMPLEMENTED",
        "Industry_Stress_Extension_Reason": "fixture general stress",
    }


def _fixture_rows() -> list[dict[str, Any]]:
    glw = {
        **_base_row("GLW"),
        "Sector": "Technology",
        "Industry": "Electronic Components",
        "Price": 30.0,
        "MarketCap_B": 30.0,
        "EV_B": 32.0,
        "Total_Debt_B": 4.0,
        "Cash_B": 2.0,
        "Net_Debt_B": 2.0,
        "Debt_Source_Method": "SEC fixture composition",
        "ICR": 10.0,
        "ICR_Method": "reported_interest_expense",
        "TTM_OCF_B": 3.0,
        "Dynamic_CapEx_B": 1.0,
        "Maintenance_CapEx_B": 0.7,
        "Maintenance_CapEx_Low_B": 0.6,
        "Maintenance_CapEx_High_B": 0.8,
        "Growth_CapEx_B": 0.3,
        "Maintenance_CapEx_Confidence": "HIGH",
        "CapEx_Reinvestment_Method": "D&A anchored fixture estimate",
        "TTM_SBC_B": 0.2,
        "SBC_Economic_Cost_B": 0.2,
        "Maintenance_Real_FCF_B": 2.1,
        "Conservative_Real_FCF_B": 1.8,
        "Real_FCF_Yield_pct": 7.0,
        "Conservative_Real_FCF_Yield_pct": 6.0,
        "Maintenance_Real_FCF_to_MarketCap_Yield_pct": 7.0,
        "Conservative_Real_FCF_to_MarketCap_Yield_pct": 6.0,
        "Maintenance_Real_FCF_to_EV_Yield_pct": 6.56,
        "Conservative_Real_FCF_to_EV_Yield_pct": 5.62,
        "ROIC_pct": 15.0,
        "EV_EBITDA_x": 8.0,
        "Historical_Valuation_Coverage": 0.8,
        "Historical_Valuation_Status": "VALID",
    }
    pgr = {
        **_base_row("PGR"),
        "Scoring_Framework": "INDUSTRY_SPECIALIZED_INSURANCE_P_AND_C_V1",
        "Initial_Industry_Model_Key": "INSURANCE_P_AND_C",
        "Industry_Model_Key": "INSURANCE_P_AND_C",
        "Model_Route": "INSURANCE_P_AND_C",
        "Model_Route_Reason": "property and casualty insurer fixture",
        "Industry_Model_Decision": "PASS",
        "Industry_Model_Score": 82.0,
        "Industry_Model_Coverage": 88.0,
        "Industry_Model_Metrics_JSON": json.dumps(
            {
                "combined_ratio_pct": 92.0,
                "premium_growth_pct": 11.0,
                "net_investment_income_ttm_b": 4.2,
                "investment_book_yield_pct": 4.5,
            },
            sort_keys=True,
        ),
        "Industry_Model_Components_JSON": json.dumps(
            {"underwriting": 85.0, "capital_strength": 80.0}, sort_keys=True
        ),
        "Sector": "Financial Services",
        "Industry": "Insurance - Property & Casualty",
        "Price": 250.0,
        "MarketCap_B": 145.0,
        "Long_Term_Score": 82.0,
        "Industry_Stress_Extension_Status": "NOT_IMPLEMENTED",
        "Industry_Stress_Extension_Reason": "insurance catastrophe extension required",
    }
    ifnny = {
        **_base_row("IFNNY"),
        "Status": "Abstain: foreign currency / annual filing chain incomplete",
        "Input_Security_Class": "COMMON_ADS_INFERRED",
        "Security_Class_Confidence": "MEDIUM",
        "Security_Class_Evidence_Source": "fixture ADS classification",
        "Decision_State": "ABSTAIN",
        "Data_Confidence_Score": 0.0,
        "Data_Confidence_Reasons": "Missing point-in-time FX rate and verified ADR ratio",
        "Long_Term_Score": None,
        "Sector": "Technology",
        "Industry": "Semiconductors",
        "Point_in_Time_FX_Rate": None,
        "ADR_Ratio": None,
    }
    return [glw, pgr, ifnny]


def _evidence_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticker in ("GLW", "PGR", "IFNNY"):
        row = {column: "" for column in EVIDENCE_COLUMNS}
        row.update(
            {
                "evidence_id": f"fixture-{ticker.lower()}-source",
                "record_type": "source_fact",
                "source_system": "FIXTURE",
                "ticker": ticker,
                "normalized_metric": "FixtureAnchor",
                "available_to_model_at": "2026-07-15T00:00:00+00:00",
                "decision_timestamp": DECISION_TIMESTAMP,
                "is_available_at_decision": True,
                "selected_for_model": True,
                "source_evidence_ids": "[]",
            }
        )
        rows.append(row)
    return rows


def build_fixture_pipeline(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_rows = _evidence_rows()
    screen = annotate_dataframe(pd.DataFrame(_fixture_rows()), evidence_rows)
    screen_path = output_dir / "mode_c_screen.csv"
    shortlist_path = output_dir / "mode_c_shortlist.csv"
    evidence_path = output_dir / "mode_c_evidence_ledger.csv"
    universe_path = output_dir / "qualified_universe.csv"
    dashboard_dir = output_dir / "public"
    zero_report_path = output_dir / "mode_c_zero_audit.json"

    screen.to_csv(screen_path, index=False, encoding="utf-8-sig")
    screen.iloc[0:0].to_csv(shortlist_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(evidence_rows, columns=EVIDENCE_COLUMNS).to_csv(
        evidence_path, index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(
        [
            {"Ticker": "GLW", "CIK": "24741"},
            {"Ticker": "PGR", "CIK": "80661"},
            {"Ticker": "IFNNY", "CIK": "0000000000"},
        ]
    ).to_csv(universe_path, index=False, encoding="utf-8-sig")

    index_path = build_dashboard(
        screen_path,
        shortlist_path,
        universe_path,
        dashboard_dir,
        history=None,
    )
    enhance_dashboard(index_path)
    zero_report = build_zero_classification_report(screen)
    zero_report_path.write_text(
        json.dumps(zero_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = validate_outputs(
        screen_path,
        shortlist_path,
        evidence_path,
        dashboard_dir / "data.json",
    )
    (output_dir / "mode_c_agent_payload.json").write_text(
        json.dumps(
            {
                "fixture": True,
                "decision_timestamp": DECISION_TIMESTAMP,
                "stocks": json.loads((dashboard_dir / "data.json").read_text(encoding="utf-8"))["stocks"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "mode_c_report.md").write_text(
        "# Mode C Fixture Pipeline\n\nGLW general corporate, PGR P&C, and IFNNY ADS/IFRS abstention.\n",
        encoding="utf-8",
    )
    return {**summary, "zero_report": zero_report["summary"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the deterministic Mode C fixture pipeline")
    parser.add_argument("--output-dir", default="fixture_output")
    args = parser.parse_args()
    summary = build_fixture_pipeline(Path(args.output_dir))
    print(json.dumps({"status": "ok", **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
