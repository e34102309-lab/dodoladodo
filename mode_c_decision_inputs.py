from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


CALIBRATION_INPUT_CONTRACT_VERSION = "2026-08-pit-calibration-input-v2"
PORTFOLIO_FIT_CONTRACT_VERSION = "2026-08-portfolio-fit-input-v2"

CALIBRATION_REQUIRED_COLUMNS = frozenset(
    {
        "Ticker",
        "Industry_Model_Key",
        "Decision_Timestamp",
        "Available_To_Model_At",
        "Universe_Membership_AsOf",
        "Raw_Model_Score",
        "Forward_Return_End",
        "Forward_Return_Horizon_Days",
        "Forward_Return_pct",
        "Benchmark_Id",
        "Benchmark_Return_pct",
        "Forward_Excess_Return_pct",
        "Delisted_Securities_Included",
        "Transaction_Costs_Included",
    }
)

PORTFOLIO_REQUIRED_COLUMNS = frozenset(
    {
        "Ticker",
        "AsOf",
        "Current_Position_Weight_pct_Total",
        "ETF_Lookthrough_Weight_pct_Total",
        "Active_Sleeve_Weight_pct_Total",
        "Active_Sector_Weight_pct_Total",
        "Economic_Risk_Bucket",
        "Economic_Risk_Bucket_Weight_pct_Total",
        "ETF_Top10_Overlap",
        "Correlation_Stress_Status",
    }
)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value) == 1.0
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def _timestamp(value: Any) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    return pd.Timestamp(parsed).tz_convert(None)


def audit_calibration_input(
    frame: pd.DataFrame,
    calibration_as_of: Any,
    *,
    minimum_observations: int = 500,
    minimum_observations_per_model: int = 30,
    minimum_decision_years: int = 3,
) -> dict[str, Any]:
    missing_columns = sorted(CALIBRATION_REQUIRED_COLUMNS - set(frame.columns))
    if missing_columns:
        return {
            "contract_version": CALIBRATION_INPUT_CONTRACT_VERSION,
            "status": "INVALID",
            "eligible_for_model_fitting": False,
            "reasons": ["missing columns: " + ", ".join(missing_columns)],
            "observation_count": int(len(frame)),
            "model_counts": {},
        }
    if frame.empty:
        return {
            "contract_version": CALIBRATION_INPUT_CONTRACT_VERSION,
            "status": "PENDING_INPUT",
            "eligible_for_model_fitting": False,
            "reasons": ["calibration input has no observations"],
            "observation_count": 0,
            "model_counts": {},
        }

    work = frame.copy()
    decision = pd.to_datetime(work["Decision_Timestamp"], utc=True, errors="coerce")
    available = pd.to_datetime(work["Available_To_Model_At"], utc=True, errors="coerce")
    membership = pd.to_datetime(work["Universe_Membership_AsOf"], utc=True, errors="coerce")
    forward_end = pd.to_datetime(work["Forward_Return_End"], utc=True, errors="coerce")
    as_of = pd.to_datetime(calibration_as_of, utc=True, errors="coerce")
    raw_score = pd.to_numeric(work["Raw_Model_Score"], errors="coerce")
    forward_return = pd.to_numeric(work["Forward_Return_pct"], errors="coerce")
    benchmark_return = pd.to_numeric(
        work["Benchmark_Return_pct"], errors="coerce"
    )
    excess_return = pd.to_numeric(
        work["Forward_Excess_Return_pct"], errors="coerce"
    )
    horizon_days = pd.to_numeric(
        work["Forward_Return_Horizon_Days"], errors="coerce"
    )
    benchmark_ids = (
        work["Benchmark_Id"].fillna("").astype(str).str.strip().str.upper()
    )
    model_keys = work["Industry_Model_Key"].fillna("").astype(str).str.strip().str.upper()
    tickers = work["Ticker"].fillna("").astype(str).str.strip().str.upper()

    reasons: list[str] = []
    if pd.isna(as_of):
        reasons.append("calibration_as_of is invalid")
    if decision.isna().any() or available.isna().any() or membership.isna().any():
        reasons.append("feature timestamps are missing or invalid")
    if forward_end.isna().any():
        reasons.append("forward-return end timestamps are missing or invalid")
    if (available > decision).any() or (membership > decision).any():
        reasons.append("point-in-time leakage: inputs became available after decision")
    if (forward_end <= decision).any():
        reasons.append("forward-return windows must end after each decision")
    if pd.notna(as_of) and (forward_end > as_of).any():
        reasons.append("forward-return labels extend beyond calibration_as_of")
    if (
        raw_score.isna().any()
        or forward_return.isna().any()
        or benchmark_return.isna().any()
        or excess_return.isna().any()
    ):
        reasons.append(
            "raw scores, forward returns, benchmark returns or excess returns contain non-finite values"
        )
    if benchmark_ids.eq("").any():
        reasons.append("benchmark id is blank")
    nonblank_benchmark_ids = benchmark_ids[benchmark_ids.ne("")]
    if nonblank_benchmark_ids.nunique() > 1:
        reasons.append(
            "calibration input mixes benchmark ids; excess-return targets are not cross-model comparable"
        )
    benchmark_windows = pd.DataFrame(
        {
            "benchmark_id": benchmark_ids,
            "decision_date": decision.dt.normalize(),
            "forward_end_date": forward_end.dt.normalize(),
            "benchmark_return": benchmark_return,
        }
    )
    comparable_benchmark_windows = benchmark_windows.dropna(
        subset=["decision_date", "forward_end_date", "benchmark_return"]
    )
    comparable_benchmark_windows = comparable_benchmark_windows[
        comparable_benchmark_windows["benchmark_id"].ne("")
    ]
    if not comparable_benchmark_windows.empty:
        benchmark_return_spreads = comparable_benchmark_windows.groupby(
            ["benchmark_id", "decision_date", "forward_end_date"],
            dropna=False,
        )["benchmark_return"].agg(lambda values: float(values.max() - values.min()))
        if benchmark_return_spreads.gt(0.011).any():
            reasons.append(
                "benchmark returns differ within the same benchmark and forward-return window"
            )
    actual_horizon_days = (
        forward_end.dt.normalize() - decision.dt.normalize()
    ).dt.days
    invalid_horizon = (
        horizon_days.isna()
        | horizon_days.le(0.0)
        | horizon_days.mod(1.0).abs().gt(1e-9)
        | actual_horizon_days.isna()
        | (horizon_days - actual_horizon_days).abs().gt(0.011)
    )
    if invalid_horizon.any():
        reasons.append(
            "forward-return horizon is invalid or does not match its date window"
        )
    valid_horizons = horizon_days[~invalid_horizon]
    if valid_horizons.nunique() > 1:
        reasons.append("calibration input mixes multiple forward-return horizons")
    excess_mismatch = (
        excess_return - (forward_return - benchmark_return)
    ).abs().gt(0.011)
    if excess_mismatch.any():
        reasons.append(
            "forward excess return does not equal security return minus benchmark return"
        )
    if model_keys.eq("").any() or tickers.eq("").any():
        reasons.append("ticker or industry model key is blank")
    normalized_observations = pd.DataFrame(
        {"ticker": tickers, "decision_timestamp": decision}
    )
    if normalized_observations.duplicated(
        subset=["ticker", "decision_timestamp"]
    ).any():
        reasons.append("duplicate ticker and decision timestamp observations")
    if not work["Delisted_Securities_Included"].map(_truthy).all():
        reasons.append("delisted securities are not included in every calibration cohort")
    if not work["Transaction_Costs_Included"].map(_truthy).all():
        reasons.append("transaction costs are not included in every return label")

    model_counts = model_keys.value_counts().sort_index().to_dict()
    if len(work) < minimum_observations:
        reasons.append(
            f"observation count {len(work)} is below {minimum_observations}"
        )
    undersized_models = sorted(
        key
        for key, count in model_counts.items()
        if key and int(count) < minimum_observations_per_model
    )
    if undersized_models:
        reasons.append(
            "models below minimum sample: " + ", ".join(undersized_models)
        )
    distinct_years = int(decision.dt.year.nunique()) if not decision.isna().all() else 0
    if distinct_years < minimum_decision_years:
        reasons.append(
            f"decision history spans {distinct_years} years; need {minimum_decision_years}"
        )

    return {
        "contract_version": CALIBRATION_INPUT_CONTRACT_VERSION,
        "status": "ELIGIBLE_FOR_MODEL_FITTING" if not reasons else "INVALID",
        "eligible_for_model_fitting": not reasons,
        "reasons": reasons,
        "observation_count": int(len(work)),
        "model_counts": {str(key): int(value) for key, value in model_counts.items()},
        "decision_years": distinct_years,
        "return_horizon_days": (
            int(valid_horizons.iloc[0])
            if not valid_horizons.empty and valid_horizons.nunique() == 1
            else None
        ),
        "fitting_target": "Forward_Excess_Return_pct",
    }


def load_portfolio_fit_inputs(
    path: str | Path | None,
) -> tuple[dict[str, dict[str, Any]], str]:
    if path is None or not str(path).strip():
        return {}, "portfolio-fit input file is not configured"
    source = Path(path)
    if not source.exists():
        return {}, f"portfolio-fit input file does not exist: {source}"
    try:
        frame = pd.read_csv(source)
    except Exception as exc:
        return {}, f"portfolio-fit input cannot be read: {exc}"
    missing_columns = sorted(PORTFOLIO_REQUIRED_COLUMNS - set(frame.columns))
    if missing_columns:
        return {}, "portfolio-fit input missing columns: " + ", ".join(
            missing_columns
        )
    tickers = frame["Ticker"].fillna("").astype(str).str.strip().str.upper()
    if tickers.eq("").any():
        return {}, "portfolio-fit input contains a blank ticker"
    if tickers.duplicated().any():
        duplicates = sorted(set(tickers[tickers.duplicated(keep=False)]))
        return {}, "portfolio-fit input contains duplicate tickers: " + ", ".join(
            duplicates
        )
    rows = frame.to_dict(orient="records")
    return {
        str(row["Ticker"]).strip().upper(): row
        for row in rows
    }, ""


def evaluate_portfolio_fit(
    exposure: Mapping[str, Any] | None,
    *,
    decision_timestamp: Any,
    proposed_weight_pct_total: float,
    raw_model_score: float,
    single_name_limit_pct_total: float,
    active_sleeve_limit_pct_total: float,
    sector_limit_pct_total: float,
    economic_risk_limit_pct_total: float,
    etf_top10_min_score: float,
    maximum_input_age_days: int = 45,
) -> dict[str, Any]:
    pending = {
        "contract_version": PORTFOLIO_FIT_CONTRACT_VERSION,
        "status": "PENDING_INPUT",
        "pending": True,
        "reason": "portfolio-fit input is missing for this ticker",
    }
    if not exposure:
        return pending

    decision = _timestamp(decision_timestamp)
    as_of = _timestamp(exposure.get("AsOf"))
    if pd.isna(decision) or pd.isna(as_of):
        return {**pending, "status": "INVALID", "reason": "portfolio-fit as-of timestamp is invalid"}
    age_days = (decision.normalize() - as_of.normalize()).days
    if age_days < 0:
        return {**pending, "status": "INVALID", "reason": "portfolio-fit input is dated after the decision timestamp"}
    if age_days > maximum_input_age_days:
        return {
            **pending,
            "status": "STALE",
            "reason": f"portfolio-fit input is {age_days} days old",
            "as_of": as_of.isoformat(),
        }

    numeric_fields = {
        "current_position": "Current_Position_Weight_pct_Total",
        "etf_lookthrough": "ETF_Lookthrough_Weight_pct_Total",
        "active_sleeve": "Active_Sleeve_Weight_pct_Total",
        "active_sector": "Active_Sector_Weight_pct_Total",
        "economic_risk": "Economic_Risk_Bucket_Weight_pct_Total",
    }
    values = {name: _finite(exposure.get(column)) for name, column in numeric_fields.items()}
    proposed = _finite(proposed_weight_pct_total)
    score = _finite(raw_model_score)
    if proposed is None or proposed <= 0:
        return {**pending, "status": "INVALID", "reason": "proposed starter weight is not positive"}
    if score is None or any(value is None or value < 0 for value in values.values()):
        return {**pending, "status": "INVALID", "reason": "portfolio-fit exposure values are missing, negative or non-finite"}
    if not str(exposure.get("Economic_Risk_Bucket") or "").strip():
        return {**pending, "status": "INVALID", "reason": "economic risk bucket is blank"}

    pre_trade_issuer_exposure = (
        float(values["current_position"]) + float(values["etf_lookthrough"])
    )
    post_position = pre_trade_issuer_exposure + proposed
    post_active_sleeve = float(values["active_sleeve"]) + proposed
    post_sector = float(values["active_sector"]) + proposed
    post_economic_risk = float(values["economic_risk"]) + proposed
    failures: list[str] = []
    if post_position > single_name_limit_pct_total:
        failures.append("single-name look-through exposure exceeds limit")
    if post_active_sleeve > active_sleeve_limit_pct_total:
        failures.append("active sleeve exceeds limit")
    if post_sector > sector_limit_pct_total:
        failures.append("active sector weight exceeds limit")
    if post_economic_risk > economic_risk_limit_pct_total:
        failures.append("economic risk bucket exceeds limit")
    if _truthy(exposure.get("ETF_Top10_Overlap")) and score < etf_top10_min_score:
        failures.append("ETF top-10 overlap requires the higher score threshold")

    correlation_status = str(
        exposure.get("Correlation_Stress_Status") or ""
    ).strip().upper()
    if failures:
        status = "FAIL"
        pending_flag = False
        reason = "; ".join(failures)
    elif correlation_status != "PASS":
        status = "PENDING_REVIEW"
        pending_flag = True
        reason = "correlation and drawdown-contribution stress has not passed"
    else:
        status = "PASS"
        pending_flag = False
        reason = "position, sleeve, sector, economic-risk and correlation gates pass"

    return {
        "contract_version": PORTFOLIO_FIT_CONTRACT_VERSION,
        "status": status,
        "pending": pending_flag,
        "reason": reason,
        "as_of": as_of.isoformat(),
        "input_age_days": age_days,
        "current_position_weight_pct_total": float(values["current_position"]),
        "pre_trade_active_sleeve_weight_pct_total": float(values["active_sleeve"]),
        "pre_trade_sector_weight_pct_total": float(values["active_sector"]),
        "pre_trade_economic_risk_weight_pct_total": float(values["economic_risk"]),
        "post_trade_position_weight_pct_total": post_position,
        "post_trade_active_sleeve_weight_pct_total": post_active_sleeve,
        "post_trade_sector_weight_pct_total": post_sector,
        "post_trade_economic_risk_weight_pct_total": post_economic_risk,
        "etf_lookthrough_weight_pct_total": float(values["etf_lookthrough"]),
        "etf_top10_overlap": _truthy(exposure.get("ETF_Top10_Overlap")),
        "correlation_stress_status": correlation_status,
        "economic_risk_bucket": str(exposure.get("Economic_Risk_Bucket") or "").strip(),
    }


def portfolio_input_template() -> pd.DataFrame:
    return pd.DataFrame(columns=sorted(PORTFOLIO_REQUIRED_COLUMNS))


def calibration_input_template() -> pd.DataFrame:
    return pd.DataFrame(columns=sorted(CALIBRATION_REQUIRED_COLUMNS))
