from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from build_mode_c_dashboard import build_dashboard
from enhance_dashboard_ui import enhance_dashboard
from mode_c_decision_inputs import PORTFOLIO_FIT_CONTRACT_VERSION
from mode_c_evidence import EVIDENCE_COLUMNS
from mode_c_industry_models import evaluate_industry_model
from mode_c_metric_contract import annotate_dataframe
from mode_c_research_priority import (
    annotate_research_priorities,
    global_research_queue,
    shortlist_by_model,
)
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
        "Decision_Reason_Code": "PASS_MODEL_GATE",
        "Decision_Timestamp": DECISION_TIMESTAMP,
        "Price_Data_Date": "2026-07-15",
        "Latest_SEC_Availability_Date": "2026-07-14T20:00:00+00:00",
        "Universe_Version": "fixture-universe-v2",
        "Git_Commit": "fixture-commit",
        "Data_Confidence_Score": 90.0,
        "Data_Confidence_Reasons": "fixture with complete point-in-time inputs",
        "Long_Term_Score": 78.0,
        "Long_Term_Eligible": False,
        "Factor_Coverage": 1.0,
        "Applicable_Factor_Weight": 95.0,
        "Available_Factor_Weight": 95.0,
        "Weight_Renormalized": True,
        "DSI_Status": "NOT_APPLICABLE",
        "DSI_Score": None,
        "Working_Capital_Quality_Status": "MISSING",
        "Working_Capital_Quality_State": "MISSING",
        "Working_Capital_Quality_Coverage": 0.0,
        "Working_Capital_Risk_Penalty": 0.0,
        "Working_Capital_Quality_Reasons": "fixture has no AR/AP history",
        "AR_vs_Revenue_Growth_Gap_pp": None,
        "AP_vs_COGS_Growth_Gap_pp": None,
        "Dilution_Double_Count_Check": "PASS",
        "Ownership_Dilution_Penalty": 0.0,
        "Capital_Allocation_Penalty": 0.0,
        "Total_Dilution_Score_Impact": 0.0,
        "Stress_Survival_30": True,
        "Persistent_Dilution": False,
        "Persistent_Dilution_Hard_Gate": False,
        "TTM_Gross_Buyback_B": None,
        "TTM_Stock_Issuance_B": None,
        "Real_Buyback_B": None,
        "Acquisition_Stock_Consideration_B": None,
        "Acquisition_Issuance_Attribution_Status": "MISSING",
        "Acquisition_Issuance_Reconciliation_Status": "NOT_APPLICABLE",
        "Acquisition_Related_Issuance_Flag": False,
        "Acquisition_Accretion_Review_Required": False,
        "Dilution_Total_Score_Impact": 0.0,
        "Share_Count_Change_pct": 0.5,
        "Share_Count_Change_3Y_pct": 1.5,
        "Share_Basis_Discontinuity": False,
        "Growth_CapEx_Risk_State": "CLEAR",
        "Growth_CapEx_Risk_Corroboration_Count": 0,
        "Growth_CapEx_Risk_Reasons": "",
        "Specialized_Stress_Status": "NOT_APPLICABLE",
        "Specialized_Stress_Scenario": "",
        "Specialized_Stress_Reason": "",
        "Specialized_Stress_Survival": False,
        "Specialized_Stress_Missing_Inputs": "",
        "Portfolio_Fit_Status": "NOT_APPLICABLE",
        "Portfolio_Fit_Reason": "",
        "Portfolio_Fit_AsOf": "",
        "Portfolio_Fit_Input_Age_Days": None,
        "Portfolio_Current_Position_Weight_pct_Total": None,
        "Portfolio_PreTrade_Active_Sleeve_Weight_pct_Total": None,
        "Portfolio_PreTrade_Sector_Weight_pct_Total": None,
        "Portfolio_PreTrade_Economic_Risk_Weight_pct_Total": None,
        "Portfolio_PostTrade_Position_Weight_pct_Total": None,
        "Portfolio_PostTrade_Active_Sleeve_Weight_pct_Total": None,
        "Portfolio_PostTrade_Sector_Weight_pct_Total": None,
        "Portfolio_PostTrade_Economic_Risk_Weight_pct_Total": None,
        "ETF_Lookthrough_Weight_pct_Total": None,
        "Portfolio_ETF_Top10_Overlap": False,
        "Portfolio_Correlation_Stress_Status": "",
        "Economic_Risk_Bucket": "",
        "Portfolio_Fit_Contract_Version": PORTFOLIO_FIT_CONTRACT_VERSION,
        "Suggested_Starter_Weight_pct_Total": 0.0,
        "Industry_Stress_Extension_Status": "IMPLEMENTED",
        "Industry_Stress_Extension_Reason": "fixture general stress",
    }


def _p_and_c_row(
    ticker: str,
    *,
    company_ratio: float | None,
    sec_proxy_ratio: float,
    premium_growth: float,
    policy_growth: float,
    market_cap: float,
    price_to_book: float,
    monthly_ratio: float | None = None,
    quarterly_ratio: float | None = None,
    reserve_development_pct: float = 0.0,
    catastrophe_loss_ratio_pct: float = 3.0,
) -> dict[str, Any]:
    premiums = 60.0 if ticker == "PGR" else 12.0
    equity = market_cap / price_to_book
    assets = equity / 0.25
    invested_assets = assets * 0.75
    raw_metrics = {
        "company_reported_combined_ratio_pct": company_ratio,
        "monthly_combined_ratio_pct": monthly_ratio,
        "quarterly_combined_ratio_pct": quarterly_ratio,
        "premium_growth_pct": premium_growth,
        "policy_count_growth_pct": policy_growth,
        "premiums_earned_ttm_b": premiums,
        "claims_incurred_ttm_b": premiums * 0.65,
        "underwriting_expense_ttm_b": premiums * max(sec_proxy_ratio / 100.0 - 0.65, 0.0),
        "combined_expense_ttm_b": premiums * sec_proxy_ratio / 100.0,
        "reserve_development_to_premium_pct": reserve_development_pct,
        "catastrophe_loss_ratio_pct": catastrophe_loss_ratio_pct,
        "net_investment_income_ttm_b": invested_assets * 0.045,
        "invested_assets_b": invested_assets,
        "investment_yield_pct": 4.5,
        "pretax_income_ttm_b": equity * 0.18,
        "net_income_ttm_b": equity * 0.18 * 0.79,
        "average_equity_b": equity * 0.95,
        "equity_b": equity,
        "assets_b": assets,
        "market_cap_b": market_cap,
    }
    evaluation = evaluate_industry_model("INSURANCE_P_AND_C", raw_metrics)
    metrics = evaluation.metrics
    source_status = str(metrics["combined_ratio_source_status"])
    model_ratio = float(metrics["combined_ratio_for_model_pct"])
    model_score = round(float(evaluation.score), 2)
    review_required = source_status not in {
        "COMPANY_REPORTED",
        "SEC_PROXY_RECONCILED",
    }
    stress_status = str(metrics.get("p_and_c_stress_status") or "ABSTAIN")
    decision_state = (
        "PASS" if stress_status == "PASS" else "FAIL" if stress_status == "FAIL" else "ABSTAIN"
    )
    status = (
        "Pass"
        if decision_state == "PASS"
        else "Fail: INSURANCE_P_AND_C specialized stress survival failed"
        if decision_state == "FAIL"
        else "Abstain: INSURANCE_P_AND_C specialized stress evidence incomplete"
    )
    return {
        **_base_row(ticker),
        "Status": status,
        "Decision_State": decision_state,
        "Decision_Reason_Code": (
            "PASS_MODEL_GATE"
            if decision_state == "PASS"
            else "FINANCIAL_HARD_GATE"
            if decision_state == "FAIL"
            else "SPECIALIZED_STRESS_NOT_AVAILABLE"
        ),
        "Scoring_Framework": "INDUSTRY_SPECIALIZED_INSURANCE_P_AND_C_V2",
        "Initial_Industry_Model_Key": "INSURANCE_P_AND_C",
        "Industry_Model_Key": "INSURANCE_P_AND_C",
        "Model_Route": "INSURANCE_P_AND_C",
        "Model_Route_Reason": "property and casualty insurer fixture",
        "Industry_Model_Decision": "PASS",
        "Industry_Model_Score": model_score,
        "Industry_Model_Coverage": round(evaluation.score_coverage * 100.0, 1),
        "Industry_Model_Metrics_JSON": json.dumps(metrics, sort_keys=True),
        "Industry_Model_Components_JSON": json.dumps(evaluation.components, sort_keys=True),
        "Industry_Model_Warnings": "; ".join(evaluation.warnings),
        "Sector": "Financial Services",
        "Industry": "Insurance - Property & Casualty",
        "Price": 250.0,
        "MarketCap_B": market_cap,
        "Long_Term_Score": model_score,
        "Long_Term_Eligible": stress_status == "PASS",
        "Company_Reported_Combined_Ratio": metrics["company_reported_combined_ratio_pct"],
        "SEC_Combined_Ratio_Proxy": metrics["sec_combined_ratio_proxy_pct"],
        "Combined_Ratio_Reconciliation_Difference_pp": metrics["combined_ratio_reconciliation_difference_pp"],
        "Combined_Ratio_Source_Status": source_status,
        "Human_KPI_Review_Required": review_required,
        "Premium_Growth_pct": premium_growth,
        "Policy_Count_Growth_pct": policy_growth,
        "Prior_Year_Reserve_Development_pct": reserve_development_pct,
        "Catastrophe_Loss_Ratio_pct": catastrophe_loss_ratio_pct,
        "Price_to_Book_x": price_to_book,
        "P_and_C_Stress_CR_Mild": metrics["p_and_c_stress_cr_mild"],
        "P_and_C_Stress_CR_Moderate": metrics["p_and_c_stress_cr_moderate"],
        "P_and_C_Stress_CR_Severe": metrics["p_and_c_stress_cr_severe"],
        "P_and_C_Stress_Underwriting_Income_Mild_B": metrics["p_and_c_stress_underwriting_income_mild_b"],
        "P_and_C_Stress_Underwriting_Income_Moderate_B": metrics["p_and_c_stress_underwriting_income_moderate_b"],
        "P_and_C_Stress_Underwriting_Income_Severe_B": metrics["p_and_c_stress_underwriting_income_severe_b"],
        "P_and_C_Stress_PreTax_Income_Moderate_B": metrics["p_and_c_stress_pretax_income_moderate_b"],
        "P_and_C_Stress_ROE_Moderate_pct": metrics["p_and_c_stress_roe_moderate_pct"],
        "P_and_C_Stress_Equity_to_Assets_Moderate_pct": metrics["p_and_c_stress_equity_to_assets_moderate_pct"],
        "P_and_C_Stress_Survival_Moderate": metrics["p_and_c_stress_survival_moderate"],
        "P_and_C_Stress_Status": stress_status,
        "Specialized_Stress_Status": stress_status,
        "Specialized_Stress_Scenario": metrics["specialized_stress_scenario"],
        "Specialized_Stress_Reason": metrics["specialized_stress_reason"],
        "Specialized_Stress_Survival": metrics["specialized_stress_survival"],
        "Specialized_Stress_Missing_Inputs": "; ".join(
            metrics["specialized_stress_missing_inputs"]
        ),
        "Industry_Stress_Extension_Status": "IMPLEMENTED_PASS" if stress_status == "PASS" else "IMPLEMENTED_FAIL",
        "Industry_Stress_Extension_Reason": "deterministic P&C stress fixture",
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
        "Maintenance_CapEx_Base_B": 0.7,
        "Maintenance_CapEx_Low_B": 0.6,
        "Maintenance_CapEx_High_B": 0.8,
        "Growth_CapEx_B": 0.3,
        "Maintenance_CapEx_Confidence": "HIGH",
        "CapEx_Reinvestment_Method": "D&A anchored fixture estimate",
        "TTM_SBC_B": 0.2,
        "SBC_Economic_Cost_B": 0.2,
        "Maintenance_Real_FCF_B": 2.1,
        "Maintenance_FCF_B": 2.1,
        "Conservative_Real_FCF_B": 1.8,
        "Conservative_FCF_B": 1.8,
        "Real_FCF_Yield_pct": 7.0,
        "Maintenance_FCF_Yield_pct": 7.0,
        "Conservative_Real_FCF_Yield_pct": 6.0,
        "Conservative_FCF_Yield_pct": 6.0,
        "Maintenance_FCF_Yield_Low_pct": 6.67,
        "Maintenance_FCF_Yield_High_pct": 7.33,
        "Maintenance_FCF_Yield_Range_Text": "6.67%-7.33%",
        "Maintenance_Real_FCF_to_MarketCap_Yield_pct": 7.0,
        "Conservative_Real_FCF_to_MarketCap_Yield_pct": 6.0,
        "Maintenance_Real_FCF_to_EV_Yield_pct": 6.56,
        "Conservative_Real_FCF_to_EV_Yield_pct": 5.62,
        "ROIC_pct": 15.0,
        "ROIC_Average_Capital_pct": 15.0,
        "ROIC_Ending_Capital_pct": 14.2,
        "ROIC_Including_Goodwill_pct": 15.0,
        "ROIC_Excluding_Goodwill_pct": 18.1,
        "ROIC_Capital_Method": "BEGIN_END_AVERAGE",
        "EV_EBITDA_x": 8.0,
        "DSI_Status": "VALID",
        "DSI_Score": 72.0,
        "DSI_Latest_Days": 51.0,
        "DSI_QoQ_Change_pct": -6.0,
        "DSI_YoY_Change_pct": -9.0,
        "Inventory_to_Revenue_pct": 8.0,
        "Inventory_to_Assets_pct": 5.0,
        "Long_Term_Eligible": True,
        "Historical_Valuation_Valid_Years": 8,
        "Historical_Valuation_Quantile_Used": 20.0,
        "Historical_Valuation_Coverage": 0.8,
        "Historical_Valuation_Status": "VALID",
        "Exit_Multiple_Company_Component_x": 8.5,
        "Exit_Multiple_Peer_Component_x": 8.0,
        "Exit_Multiple_Rate_Component_x": 7.5,
        "Exit_Multiple_Blend_x": 8.1,
        "Valuation_Status": "VALID",
    }
    pgr = _p_and_c_row(
        "PGR",
        company_ratio=92.0,
        sec_proxy_ratio=92.8,
        premium_growth=11.0,
        policy_growth=7.0,
        market_cap=145.0,
        price_to_book=4.21,
        monthly_ratio=84.0,
        quarterly_ratio=92.0,
        reserve_development_pct=-0.5,
    )
    acgl = _p_and_c_row(
        "ACGL",
        company_ratio=86.0,
        sec_proxy_ratio=85.2,
        premium_growth=16.1,
        policy_growth=9.0,
        market_cap=38.0,
        price_to_book=1.46,
        reserve_development_pct=-1.5,
        catastrophe_loss_ratio_pct=3.2,
    )
    knsl = _p_and_c_row(
        "KNSL",
        company_ratio=78.0,
        sec_proxy_ratio=77.82,
        premium_growth=3.0,
        policy_growth=1.0,
        market_cap=12.0,
        price_to_book=3.99,
        reserve_development_pct=0.5,
    )
    ifnny = {
        **_base_row("IFNNY"),
        "Status": "Abstain: foreign currency / annual filing chain incomplete",
        "Input_Security_Class": "COMMON_ADS_INFERRED",
        "Security_Class_Confidence": "MEDIUM",
        "Security_Class_Evidence_Source": "fixture ADS classification",
        "Decision_State": "ABSTAIN",
        "Decision_Reason_Code": "FX_CHAIN_UNAVAILABLE",
        "Data_Confidence_Score": 0.0,
        "Data_Confidence_Reasons": "Missing point-in-time FX rate and verified ADR ratio",
        "Long_Term_Score": None,
        "Sector": "Technology",
        "Industry": "Semiconductors",
        "Point_in_Time_FX_Rate": None,
        "ADR_Ratio": None,
    }
    return [glw, pgr, acgl, knsl, ifnny]


def _evidence_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticker in ("GLW", "PGR", "ACGL", "KNSL", "IFNNY"):
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
    ranked_rows = annotate_research_priorities(_fixture_rows())
    screen = annotate_dataframe(pd.DataFrame(ranked_rows), evidence_rows)
    screen_path = output_dir / "mode_c_screen.csv"
    shortlist_path = output_dir / "mode_c_shortlist.csv"
    by_model_path = output_dir / "mode_c_shortlist_by_model.csv"
    evidence_path = output_dir / "mode_c_evidence_ledger.csv"
    universe_path = output_dir / "qualified_universe.csv"
    dashboard_dir = output_dir / "public"
    zero_report_path = output_dir / "mode_c_zero_audit.json"

    screen.to_csv(screen_path, index=False, encoding="utf-8-sig")
    queue_tickers = {
        str(row.get("Ticker") or "") for row in global_research_queue(ranked_rows)
    }
    screen.loc[screen["Ticker"].isin(queue_tickers)].sort_values(
        "Research_Priority_Rank"
    ).to_csv(shortlist_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(shortlist_by_model(ranked_rows)).to_csv(
        by_model_path, index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(evidence_rows, columns=EVIDENCE_COLUMNS).to_csv(
        evidence_path, index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(
        [
            {"Ticker": "GLW", "CIK": "24741"},
            {"Ticker": "PGR", "CIK": "80661"},
            {"Ticker": "ACGL", "CIK": "947484"},
            {"Ticker": "KNSL", "CIK": "1669162"},
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
        "# Mode C Fixture Pipeline\n\nGLW general corporate; PGR/ACGL/KNSL P&C source and stress states; IFNNY ADS/IFRS abstention.\n",
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
