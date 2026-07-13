from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional


EVIDENCE_COLUMNS = [
    "evidence_id",
    "record_type",
    "source_system",
    "ticker",
    "cik",
    "concept",
    "original_tag",
    "normalized_metric",
    "value",
    "unit",
    "period_start",
    "period_end",
    "period_duration_days",
    "period_anomaly_flag",
    "filed_at",
    "accepted_at",
    "accession_number",
    "form",
    "fy",
    "fp",
    "amendment_flag",
    "source_priority",
    "availability_source",
    "available_to_model_at",
    "decision_timestamp",
    "is_available_at_decision",
    "selected_for_model",
    "selection_roles",
    "formula",
    "source_evidence_ids",
    "source_confidence_score",
]


def _clean_scalar(value: Any) -> Any:
    if value is None:
        return ""
    try:
        if bool(value != value):
            return ""
    except Exception:
        pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def _evidence_id(parts: Iterable[Any]) -> str:
    raw = "|".join(str(_clean_scalar(part)) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _as_date(value: Any) -> Optional[date]:
    cleaned = _clean_scalar(value)
    if cleaned == "":
        return None
    if isinstance(cleaned, datetime):
        return cleaned.date()
    if isinstance(cleaned, date):
        return cleaned
    text = str(cleaned).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _period_duration_days(period_start: Any, period_end: Any) -> Optional[int]:
    start = _as_date(period_start)
    end = _as_date(period_end)
    if start is None or end is None:
        return None
    return (end - start).days


class PointInTimeEvidenceLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, dict] = {}

    def reset(self) -> None:
        with self._lock:
            self._records = {}

    def register_source_fact(
        self,
        *,
        ticker: str,
        cik: str,
        source_system: str,
        concept: str,
        normalized_metric: str,
        value: Any,
        unit: str,
        period_start: Any,
        period_end: Any,
        filed_at: Any,
        accepted_at: Any,
        accession_number: str,
        form: str,
        fy: Any,
        fp: str,
        source_priority: int,
        availability_source: str,
        available_to_model_at: Any,
        decision_timestamp: Any,
        is_available_at_decision: bool,
    ) -> str:
        evidence_id = _evidence_id(
            [
                "source",
                source_system,
                ticker,
                cik,
                normalized_metric,
                concept,
                accession_number,
                period_start,
                period_end,
                filed_at,
                value,
                unit,
            ]
        )
        duration_days = _period_duration_days(period_start, period_end)
        period_anomaly = bool(
            duration_days is not None
            and str(form).upper()
            in {
                "10-Q",
                "10-Q/A",
                "10-K",
                "10-K/A",
                "20-F",
                "20-F/A",
                "40-F",
                "40-F/A",
            }
            and not 45 <= duration_days <= 400
        )
        confidence = {
            "accepted_at": 100.0,
            "observed_at_run": 85.0,
            "filed_date_fallback": 75.0,
        }.get(availability_source, 0.0)
        confidence -= min(max(int(source_priority), 0) * 2.0, 10.0)
        if period_anomaly:
            confidence -= 25.0
        confidence = max(0.0, confidence)
        record = {
            "evidence_id": evidence_id,
            "record_type": "source_fact",
            "source_system": source_system,
            "ticker": ticker,
            "cik": cik,
            "concept": concept,
            "original_tag": concept,
            "normalized_metric": normalized_metric,
            "value": _clean_scalar(value),
            "unit": unit,
            "period_start": _clean_scalar(period_start),
            "period_end": _clean_scalar(period_end),
            "period_duration_days": duration_days if duration_days is not None else "",
            "period_anomaly_flag": period_anomaly,
            "filed_at": _clean_scalar(filed_at),
            "accepted_at": _clean_scalar(accepted_at),
            "accession_number": accession_number,
            "form": form,
            "fy": _clean_scalar(fy),
            "fp": fp,
            "amendment_flag": str(form).upper().endswith("/A"),
            "source_priority": int(source_priority),
            "availability_source": availability_source,
            "available_to_model_at": _clean_scalar(available_to_model_at),
            "decision_timestamp": _clean_scalar(decision_timestamp),
            "is_available_at_decision": bool(is_available_at_decision),
            "selected_for_model": False,
            "selection_roles": set(),
            "formula": "",
            "source_evidence_ids": [],
            "source_confidence_score": confidence,
        }
        with self._lock:
            existing = self._records.get(evidence_id)
            if existing:
                record["selected_for_model"] = existing["selected_for_model"]
                record["selection_roles"] = set(existing["selection_roles"])
            self._records[evidence_id] = record
        return evidence_id

    def mark_used(self, evidence_ids: Iterable[str], role: str) -> None:
        ids = [str(item) for item in evidence_ids if item]
        if not ids:
            return
        with self._lock:
            for evidence_id in ids:
                record = self._records.get(evidence_id)
                if not record or not bool(record["is_available_at_decision"]):
                    continue
                record["selected_for_model"] = True
                record["selection_roles"].add(role)

    def register_derived_metric(
        self,
        *,
        ticker: str,
        cik: str,
        normalized_metric: str,
        value: Any,
        unit: str,
        formula: str,
        source_evidence_ids: Iterable[str],
        decision_timestamp: Any,
        role: str,
    ) -> str:
        source_ids = sorted(set(str(item) for item in source_evidence_ids if item))
        with self._lock:
            source_records = [self._records[item] for item in source_ids if item in self._records]
        has_complete_lineage = (
            bool(source_ids)
            and len(source_records) == len(source_ids)
            and all(bool(record["is_available_at_decision"]) for record in source_records)
        )
        available_values = [record["available_to_model_at"] for record in source_records if record["available_to_model_at"]]
        available_to_model_at = max(available_values) if available_values else ""
        source_confidence = min(
            [float(record["source_confidence_score"]) for record in source_records] or [0.0]
        )
        evidence_id = _evidence_id(
            ["derived", ticker, cik, normalized_metric, value, formula, json.dumps(source_ids), decision_timestamp]
        )
        record = {
            "evidence_id": evidence_id,
            "record_type": "derived_metric",
            "source_system": "MODEL",
            "ticker": ticker,
            "cik": cik,
            "concept": "",
            "original_tag": "",
            "normalized_metric": normalized_metric,
            "value": _clean_scalar(value),
            "unit": unit,
            "period_start": "",
            "period_end": "",
            "period_duration_days": "",
            "period_anomaly_flag": False,
            "filed_at": "",
            "accepted_at": "",
            "accession_number": "",
            "form": "",
            "fy": "",
            "fp": "",
            "amendment_flag": False,
            "source_priority": -1,
            "availability_source": "derived_from_sources",
            "available_to_model_at": available_to_model_at,
            "decision_timestamp": _clean_scalar(decision_timestamp),
            "is_available_at_decision": has_complete_lineage,
            "selected_for_model": has_complete_lineage,
            "selection_roles": {role} if has_complete_lineage else set(),
            "formula": formula,
            "source_evidence_ids": source_ids,
            "source_confidence_score": source_confidence,
        }
        with self._lock:
            self._records[evidence_id] = record
        if has_complete_lineage:
            self.mark_used(source_ids, role)
        return evidence_id

    def selected_source_stats(self, ticker: str) -> dict:
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record["ticker"] == ticker
                and record["record_type"] == "source_fact"
                and record["source_system"] == "SEC"
                and record["selected_for_model"]
            ]
        accepted = sum(record["availability_source"] == "accepted_at" for record in records)
        fallback = sum(record["availability_source"] != "accepted_at" for record in records)
        fallback_tags = sum(int(record["source_priority"]) > 0 for record in records)
        period_anomalies = sum(bool(record["period_anomaly_flag"]) for record in records)
        confidence_values = [float(record["source_confidence_score"]) for record in records]
        return {
            "selected_source_count": len(records),
            "accepted_at_count": accepted,
            "fallback_count": fallback,
            "accepted_at_ratio": accepted / len(records) if records else 0.0,
            "fallback_tag_count": fallback_tags,
            "fallback_tag_ratio": fallback_tags / len(records) if records else 0.0,
            "period_anomaly_count": period_anomalies,
            "average_source_confidence": (
                sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
            ),
        }

    def selected_evidence_ids(self, ticker: str, normalized_metric: str) -> List[str]:
        with self._lock:
            return sorted(
                record["evidence_id"]
                for record in self._records.values()
                if record["ticker"] == ticker
                and record["normalized_metric"] == normalized_metric
                and record["selected_for_model"]
            )

    def selected_source_evidence_ids(self, ticker: str, source_system: str = "") -> List[str]:
        source_filter = str(source_system or "").upper()
        with self._lock:
            return sorted(
                record["evidence_id"]
                for record in self._records.values()
                if record["ticker"] == ticker
                and record["record_type"] == "source_fact"
                and record["selected_for_model"]
                and (
                    not source_filter
                    or str(record["source_system"]).upper() == source_filter
                )
            )

    def source_original_tags(self, evidence_id: str) -> List[str]:
        """Return the transitive source tags behind one evidence record."""
        with self._lock:
            records = dict(self._records)
        tags = set()
        pending = [str(evidence_id)]
        visited = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            record = records.get(current)
            if not record:
                continue
            if record.get("record_type") == "source_fact":
                tag = str(record.get("original_tag") or "")
                if tag:
                    tags.add(tag)
            else:
                pending.extend(
                    str(item) for item in record.get("source_evidence_ids", [])
                )
        return sorted(tags)

    def rows(self, selected_only: bool = False) -> List[dict]:
        with self._lock:
            records = list(self._records.values())
        if selected_only:
            records = [record for record in records if record["selected_for_model"]]
        output = []
        for record in sorted(
            records,
            key=lambda item: (
                item["ticker"],
                item["normalized_metric"],
                str(item["period_end"]),
                item["record_type"],
                item["evidence_id"],
            ),
        ):
            row = {column: record.get(column, "") for column in EVIDENCE_COLUMNS}
            row["selection_roles"] = " | ".join(sorted(record["selection_roles"]))
            row["source_evidence_ids"] = json.dumps(record["source_evidence_ids"], ensure_ascii=True)
            output.append(row)
        return output


GLOBAL_EVIDENCE_LEDGER = PointInTimeEvidenceLedger()
