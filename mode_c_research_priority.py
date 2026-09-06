from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


RESEARCH_PRIORITY_VERSION = "2026-09-coverage-first-round-robin-v3"
CROSS_MODEL_CALIBRATION_STATUS = "UNCALIBRATED"
RESEARCH_PRIORITY_METHOD = "COVERAGE_FIRST_MODEL_ROUND_ROBIN_V2"
SHRINKAGE_PRIOR_COUNT = 20.0
GLOBAL_RESEARCH_QUEUE_SIZE = 12
PER_MODEL_RESEARCH_QUEUE_SIZE = 3


def _get(row: Any, field: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(field, default)
    return getattr(row, field, default)


def _set(row: Any, field: str, value: Any) -> None:
    if isinstance(row, MutableMapping):
        row[field] = value
    else:
        setattr(row, field, value)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _boolean(value: Any) -> bool | None:
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "y"}:
            return True
        if token in {"0", "false", "no", "n"}:
            return False
        return None
    if _finite(value) and float(value) in (0.0, 1.0):
        return float(value) == 1.0
    return None


def _truthy(value: Any) -> bool:
    return _boolean(value) is True


def _confidence_score(row: Any) -> float:
    value = _get(row, "Data_Confidence_Score", 0.0)
    return float(value) if _finite(value) else 0.0


def model_key(row: Any) -> str:
    return str(_get(row, "Industry_Model_Key", "GENERAL_CORPORATE") or "GENERAL_CORPORATE").upper()


def raw_model_score(row: Any) -> float:
    key = model_key(row)
    source = (
        _get(row, "Long_Term_Score")
        if key == "GENERAL_CORPORATE"
        else _get(row, "Industry_Model_Score")
    )
    return float(source) if _finite(source) else math.nan


def shrunk_percentile(raw_percentile: float, peer_count: int) -> float:
    if not _finite(raw_percentile) or peer_count <= 0:
        return math.nan
    weight = float(peer_count) / (float(peer_count) + SHRINKAGE_PRIOR_COUNT)
    return 50.0 + weight * (float(raw_percentile) - 50.0)


def _average_rank_percentiles(rows: Sequence[Any]) -> dict[int, float]:
    """Return ascending average-rank percentiles on a 0-100 scale."""
    ordered = sorted(
        ((index, raw_model_score(row)) for index, row in enumerate(rows)),
        key=lambda item: (item[1], str(_get(rows[item[0]], "Ticker", ""))),
    )
    count = len(ordered)
    output: dict[int, float] = {}
    cursor = 0
    while cursor < count:
        end = cursor + 1
        while end < count and math.isclose(ordered[end][1], ordered[cursor][1]):
            end += 1
        average_one_based_rank = ((cursor + 1) + end) / 2.0
        percentile = average_one_based_rank / count * 100.0
        for position in range(cursor, end):
            output[ordered[position][0]] = percentile
        cursor = end
    return output


def research_priority_order(rows: Sequence[Any]) -> list[Any]:
    """Interleave model-relative candidates without claiming cross-model alpha."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if _finite(_get(row, "Within_Model_Percentile")):
            grouped[model_key(row)].append(row)
    for peers in grouped.values():
        peers.sort(
            key=lambda row: (
                -float(_get(row, "Within_Model_Percentile")),
                -_confidence_score(row),
                str(_get(row, "Ticker", "")),
            )
        )

    ordered: list[Any] = []
    round_index = 0
    while True:
        round_candidates = [
            peers[round_index]
            for peers in grouped.values()
            if round_index < len(peers)
        ]
        if not round_candidates:
            break
        round_candidates.sort(
            key=lambda row: (
                -_confidence_score(row),
                -float(_get(row, "Within_Model_Percentile")),
                model_key(row),
                str(_get(row, "Ticker", "")),
            )
        )
        for row in round_candidates:
            _set(row, "Research_Priority_Round", round_index + 1)
        ordered.extend(round_candidates)
        round_index += 1
    return ordered


def _p_and_c_source_status(row: Any) -> str:
    explicit = str(_get(row, "Combined_Ratio_Source_Status", "") or "").upper()
    if explicit:
        return explicit
    raw = _get(row, "Industry_Model_Metrics_JSON", "")
    if isinstance(raw, Mapping):
        return str(raw.get("combined_ratio_source_status") or "").upper()
    return ""


def _human_review_required(row: Any) -> bool:
    key = model_key(row)
    if key == "GENERAL_CORPORATE":
        return False
    if str(_get(row, "Industry_Model_Required_Missing", "") or "").strip():
        return True
    if key == "INSURANCE_P_AND_C" and _p_and_c_source_status(row) not in {
        "COMPANY_REPORTED",
        "SEC_PROXY_RECONCILED",
    }:
        return True
    return _truthy(_get(row, "Human_KPI_Review_Required", False))


def _specialized_stress_status(row: Any) -> str:
    if model_key(row) == "GENERAL_CORPORATE":
        return "NOT_APPLICABLE"
    return str(
        _get(row, "Specialized_Stress_Status", "")
        or _get(row, "P_and_C_Stress_Status", "")
        or _get(row, "Industry_Stress_Extension_Status", "")
    ).upper()


def _specialized_stress_pending(row: Any) -> bool:
    if model_key(row) == "GENERAL_CORPORATE":
        return False
    return _specialized_stress_status(row) not in {
        "PASS",
        "IMPLEMENTED_PASS",
        "FAIL",
        "IMPLEMENTED_FAIL",
    }


def _specialized_stress_failed(row: Any) -> bool:
    if model_key(row) == "GENERAL_CORPORATE":
        return False
    return _specialized_stress_status(row) in {"FAIL", "IMPLEMENTED_FAIL"}


def starter_candidate_gate(row: Any) -> bool:
    raw = raw_model_score(row)
    specialized = model_key(row) != "GENERAL_CORPORATE"
    portfolio_status = str(
        _get(row, "Portfolio_Fit_Status", "") or ""
    ).upper()
    portfolio_cleared = portfolio_status == "PASS"
    return bool(
        _truthy(_get(row, "Model_Eligible", False))
        and _finite(raw)
        and raw >= 75.0
        and _boolean(_get(row, "Human_KPI_Review_Required")) is False
        and _boolean(_get(row, "Specialized_Stress_Pending")) is False
        and _boolean(_get(row, "Specialized_Stress_Failed")) is False
        and _boolean(_get(row, "Portfolio_Fit_Pending")) is False
        and portfolio_cleared
        and (not specialized or _truthy(_get(row, "Cross_Model_Comparable", False)))
    )


def refresh_research_status(row: Any) -> Any:
    states = ["SCREENED"]
    if _truthy(_get(row, "Model_Eligible", False)):
        states.append("MODEL_ELIGIBLE")
    if _truthy(_get(row, "Global_Research_Queue", False)):
        states.append("GLOBAL_RESEARCH_QUEUE")
    if _truthy(_get(row, "Human_KPI_Review_Required", False)):
        states.append("HUMAN_KPI_REVIEW_REQUIRED")
    if _truthy(_get(row, "Specialized_Stress_Pending", False)):
        states.append("SPECIALIZED_STRESS_PENDING")
    if _truthy(_get(row, "Specialized_Stress_Failed", False)):
        states.append("SPECIALIZED_STRESS_FAILED")
    if _truthy(_get(row, "Portfolio_Fit_Pending", False)):
        states.append("PORTFOLIO_FIT_PENDING")
    if _truthy(_get(row, "Starter_Candidate", False)):
        states.append("STARTER_CANDIDATE")
    if _truthy(_get(row, "Human_KPI_Review_Required", False)):
        action_state = "HUMAN_KPI_REVIEW_REQUIRED"
    elif _truthy(_get(row, "Specialized_Stress_Failed", False)):
        action_state = "SPECIALIZED_STRESS_FAILED"
    elif _truthy(_get(row, "Specialized_Stress_Pending", False)):
        action_state = "SPECIALIZED_STRESS_PENDING"
    elif _truthy(_get(row, "Global_Research_Queue", False)):
        action_state = "GLOBAL_RESEARCH_QUEUE"
    elif _truthy(_get(row, "Model_Eligible", False)):
        action_state = "MODEL_ELIGIBLE"
    else:
        action_state = "SCREENED"
    _set(row, "Research_Action_State", action_state)
    _set(row, "Research_Statuses", " | ".join(states))
    return row


def annotate_research_priorities(
    rows: Sequence[Any],
    queue_size: int = GLOBAL_RESEARCH_QUEUE_SIZE,
) -> list[Any]:
    """Mutate rows with within-model ranks and non-alpha research states."""
    groups: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        raw = raw_model_score(row)
        _set(row, "Raw_Model_Score", round(raw, 4) if _finite(raw) else math.nan)
        _set(row, "Model_Peer_Count", 0)
        _set(row, "Within_Model_Percentile", math.nan)
        _set(row, "Shrunk_Within_Model_Percentile", math.nan)
        _set(row, "Cross_Model_Calibration_Status", CROSS_MODEL_CALIBRATION_STATUS)
        _set(row, "Cross_Model_Comparable", False)
        _set(row, "Research_Priority_Rank", math.nan)
        _set(row, "Research_Priority_Round", math.nan)
        _set(row, "Research_Priority_Method", RESEARCH_PRIORITY_METHOD)
        _set(row, "Research_Priority_Version", RESEARCH_PRIORITY_VERSION)
        _set(row, "Screened", True)
        _set(row, "Model_Eligible", bool(
            _truthy(_get(row, "Long_Term_Eligible", False))
            and str(_get(row, "Decision_State", "")).strip().upper() == "PASS"
            and _truthy(_get(row, "Model_Supported", False))
            and _finite(raw) and 0.0 <= raw <= 100.0
        ))
        _set(row, "Global_Research_Queue", False)
        _set(row, "Human_KPI_Review_Required", _human_review_required(row))
        _set(row, "Specialized_Stress_Pending", _specialized_stress_pending(row))
        _set(row, "Specialized_Stress_Failed", _specialized_stress_failed(row))
        portfolio_pending = _truthy(_get(row, "Model_Eligible", False))
        _set(row, "Portfolio_Fit_Pending", portfolio_pending)
        _set(
            row,
            "Portfolio_Fit_Status",
            "PENDING_INPUT" if portfolio_pending else "NOT_APPLICABLE",
        )
        _set(
            row,
            "Portfolio_Fit_Reason",
            "portfolio holdings and risk inputs are required"
            if portfolio_pending
            else "model eligibility or starter-score gate not reached",
        )
        _set(row, "Starter_Candidate", False)
        if (
            _finite(raw)
            and str(_get(row, "Decision_State", "") or "").upper() != "ABSTAIN"
            and _truthy(_get(row, "Model_Supported", True))
        ):
            groups[model_key(row)].append(row)

    for peers in groups.values():
        percentiles = _average_rank_percentiles(peers)
        count = len(peers)
        for index, row in enumerate(peers):
            percentile = percentiles[index]
            _set(row, "Model_Peer_Count", count)
            _set(row, "Within_Model_Percentile", round(percentile, 4))
            _set(row, "Shrunk_Within_Model_Percentile", round(shrunk_percentile(percentile, count), 4))

    candidates = [
        row
        for row in rows
        if _truthy(_get(row, "Model_Eligible", False))
        and _finite(_get(row, "Shrunk_Within_Model_Percentile"))
    ]
    ordered_candidates = research_priority_order(candidates)
    for rank, row in enumerate(ordered_candidates, start=1):
        _set(row, "Research_Priority_Rank", rank)
        in_queue = rank <= max(0, int(queue_size))
        _set(row, "Global_Research_Queue", in_queue)
        _set(row, "Starter_Candidate", starter_candidate_gate(row))

    for row in rows:
        states = ["SCREENED"]
        if _truthy(_get(row, "Model_Eligible", False)):
            states.append("MODEL_ELIGIBLE")
        if _truthy(_get(row, "Global_Research_Queue", False)):
            states.append("GLOBAL_RESEARCH_QUEUE")
        if _truthy(_get(row, "Human_KPI_Review_Required", False)):
            states.append("HUMAN_KPI_REVIEW_REQUIRED")
        if _truthy(_get(row, "Specialized_Stress_Pending", False)):
            states.append("SPECIALIZED_STRESS_PENDING")
        if _truthy(_get(row, "Specialized_Stress_Failed", False)):
            states.append("SPECIALIZED_STRESS_FAILED")
        if _truthy(_get(row, "Portfolio_Fit_Pending", False)):
            states.append("PORTFOLIO_FIT_PENDING")
        if _truthy(_get(row, "Starter_Candidate", False)):
            states.append("STARTER_CANDIDATE")
        if _truthy(_get(row, "Human_KPI_Review_Required", False)):
            action_state = "HUMAN_KPI_REVIEW_REQUIRED"
        elif _truthy(_get(row, "Specialized_Stress_Failed", False)):
            action_state = "SPECIALIZED_STRESS_FAILED"
        elif _truthy(_get(row, "Specialized_Stress_Pending", False)):
            action_state = "SPECIALIZED_STRESS_PENDING"
        elif _truthy(_get(row, "Global_Research_Queue", False)):
            action_state = "GLOBAL_RESEARCH_QUEUE"
        elif _truthy(_get(row, "Model_Eligible", False)):
            action_state = "MODEL_ELIGIBLE"
        else:
            action_state = "SCREENED"
        _set(row, "Research_Action_State", action_state)
        _set(row, "Research_Statuses", " | ".join(states))
        refresh_research_status(row)
    return list(rows)


def global_research_queue(rows: Sequence[Any]) -> list[Any]:
    return sorted(
        [row for row in rows if _truthy(_get(row, "Global_Research_Queue", False))],
        key=lambda row: int(float(_get(row, "Research_Priority_Rank", math.inf))),
    )


def shortlist_by_model(
    rows: Sequence[Any],
    per_model: int = PER_MODEL_RESEARCH_QUEUE_SIZE,
) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in rows
        if _truthy(_get(row, "Model_Eligible", False))
        and _finite(_get(row, "Within_Model_Percentile"))
    ]
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in eligible:
        grouped[model_key(row)].append(row)
    output: list[dict[str, Any]] = []
    for key in sorted(grouped):
        peers = sorted(
            grouped[key],
            key=lambda row: (
                -float(_get(row, "Within_Model_Percentile")),
                -float(_get(row, "Data_Confidence_Score", 0.0) or 0.0),
                str(_get(row, "Ticker", "")),
            ),
        )
        for rank, row in enumerate(peers[: max(0, int(per_model))], start=1):
            if isinstance(row, Mapping):
                payload = dict(row)
            elif is_dataclass(row):
                payload = asdict(row)
            else:
                payload = dict(vars(row))
            payload["Within_Model_Rank"] = rank
            output.append(payload)
    return output
