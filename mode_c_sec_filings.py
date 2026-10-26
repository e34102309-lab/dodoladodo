"""Read consolidated facts from original SEC XBRL instances, without guessing mappings."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import io
import math
import json
import os
from pathlib import Path
import re
import threading
import xml.etree.ElementTree as ET

XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
MAX_XML_BYTES = 20_000_000
PARSER_VERSION = "2026-10-original-xbrl-v2"
_AUDIT = {}
_AUDIT_LOCK = threading.Lock()


def utc_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def filing_available(filing):
    accepted, filed = utc_time(filing.get("acceptanceDateTime")), utc_time(filing.get("filingDate"))
    return accepted + timedelta(minutes=5) if accepted else filed + timedelta(days=1) if filed else None


def select_recent_filings(payload: dict, decision, annual_limit=3, quarter_limit=3) -> list:
    decision = utc_time(decision)
    table = payload.get("filings", {}).get("recent", {})
    rows = []
    for i, accession in enumerate(table.get("accessionNumber", [])):
        row = {key: values[i] if i < len(values) else "" for key, values in table.items() if isinstance(values, list)}
        available = filing_available(row)
        if row.get("form") in {"10-K", "10-K/A", "10-Q", "10-Q/A"} and available and decision and available <= decision:
            rows.append(row)
    rows.sort(key=lambda r: (r.get("reportDate", ""), r.get("acceptanceDateTime", ""), r.get("filingDate", "")), reverse=True)
    result, grouped, counts = [], {}, Counter()
    for row in rows:
        key = (row["form"].split("/")[0], row.get("reportDate"))
        grouped.setdefault(key, []).append(row)
    for (form, _), candidates in grouped.items():
        if counts[form] >= (annual_limit if form == "10-K" else quarter_limit):
            continue
        counts[form] += 1
        original = next((row for row in candidates if not row["form"].endswith("/A")), None)
        if original:
            result.append(original)
        # Part-III-only amendments cannot replace the underlying financials.
        result.extend([row for row in candidates if row["form"].endswith("/A")][:2])
    return result


@lru_cache(maxsize=1)
def review_plan() -> dict:
    return json.loads(Path(__file__).with_name("sec_filing_review.json").read_text(encoding="utf-8"))


def original_filings(session, headers, ticker, cik, decision, submission) -> list:
    """Bounded immutable-accession cache; transport failures remain explicit."""
    plan = review_plan()
    if not any(row["ticker"] == ticker and row["cik"] == cik for row in plan["targets"]):
        return []
    replay = os.environ.get("MODE_C_SEC_SNAPSHOT_DIR")
    if replay:
        path = Path(replay) / f"{ticker}_filings.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("ticker") != ticker or payload.get("cik") != cik:
                raise ValueError("SEC snapshot identity mismatch")
            filings = payload.get("filings", [])
        else:
            filings = []
    else:
        cache = Path(".mode_c_sec_filings")
        cache.mkdir(exist_ok=True)
        filings = []
        for descriptor in select_recent_filings(submission, decision):
            base = filing_base(cik, descriptor["accessionNumber"])
            key = hashlib.sha256((PARSER_VERSION + base).encode()).hexdigest()
            path = cache / (key + ".json")
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                payload = collect_filing(session, headers, cik, descriptor)
                # A denied or partial response is not a reusable missing-data fact.
                if payload.get("facts") and not payload.get("errors"):
                    path.write_text(json.dumps(payload), encoding="utf-8")
            filings.append(payload)
    current = utc_time(decision)
    filings = [f for f in filings if filing_available(f) and filing_available(f) <= current]
    with _AUDIT_LOCK:
        _AUDIT[ticker] = {"cik": cik, "filings": [
            {k: f.get(k) for k in ("accessionNumber", "reportDate", "form", "filingDate", "instance_url", "sha256", "errors", "skipped")}
            for f in filings], "metrics": {}}
    return filings


def filing_concept_rows(filings: list, ticker: str, metric: str, standard_tags: list, units: tuple, override_mappings=None) -> list:
    mappings = (review_plan().get("mappings", {}).get(ticker, {}).get(metric, [])
                if override_mappings is None else override_mappings)
    names = {"us-gaap:" + tag: {"priority": index, "basis": "reviewed standard taxonomy"}
             for index, tag in enumerate(standard_tags)}
    names.update({row["qname"]: {"priority": 0, "basis": row["basis"]} for row in mappings})
    rows = []
    for filing in filings:
        for fact in filing.get("facts", []):
            mapping = names.get(fact.get("qname"))
            if not mapping or fact.get("unit") not in units:
                continue
            concept = fact["qname"].split(":", 1)[-1] if fact["qname"].startswith("us-gaap:") else fact["qname"]
            rows.append({**fact, "concept": concept, "concept_priority": mapping["priority"],
                         "accn": filing["accessionNumber"], "filed": filing["filingDate"], "form": filing["form"],
                         "fy": int(str(filing.get("reportDate", "0000"))[:4]),
                         "fp": "FY" if filing["form"].startswith("10-K") else "",
                         "source_document_url": filing.get("instance_url", ""),
                         "source_document_sha256": filing.get("sha256", ""),
                         "mapping_basis": "SEC_ORIGINAL_XBRL: " + mapping["basis"]})
    with _AUDIT_LOCK:
        if ticker in _AUDIT:
            _AUDIT[ticker]["metrics"][metric] = {"source_rows": len(rows),
                "reviewed_qnames": sorted(names), "period_ends": sorted({r["end"] for r in rows}),
                "used_qnames": sorted({r["qname"] for r in rows})}
    return rows


def audit_snapshot() -> dict:
    with _AUDIT_LOCK:
        return json.loads(json.dumps(_AUDIT))


def reset_audit():
    with _AUDIT_LOCK:
        _AUDIT.clear()


def xml_root(payload: bytes):
    if len(payload) > MAX_XML_BYTES or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.I):
        raise ValueError("Unsafe or oversized XBRL document")
    namespaces = dict(item for _, item in ET.iterparse(io.BytesIO(payload), events=("start-ns",)))
    return ET.fromstring(payload), namespaces


def parse_instance(payload: bytes, cik: str) -> dict:
    """Only entity-wide, dimensionless finite USD/shares facts; no inline scaling."""
    root, namespaces = xml_root(payload)
    if root.tag != f"{{{XBRLI}}}xbrl":
        raise ValueError("Expected an XBRL instance, not HTML or a denial page")
    prefixes = {uri: prefix for prefix, uri in namespaces.items()}
    contexts, units, skipped = {}, {}, Counter()
    for context in root.findall(f"{{{XBRLI}}}context"):
        identifier = context.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
        value = identifier.text.strip() if identifier is not None and identifier.text else ""
        if not value.isdigit() or int(value) != int(cik):
            skipped["other_entity_context"] += 1
            continue
        if any(node.tag.rsplit("}", 1)[-1] in {"explicitMember", "typedMember", "segment", "scenario"}
               for node in context.iter()):
            skipped["dimensional_context"] += 1
            continue
        period = context.find(f"{{{XBRLI}}}period")
        if period is None:
            continue
        start = period.findtext(f"{{{XBRLI}}}startDate", "")
        end = period.findtext(f"{{{XBRLI}}}endDate", "") or period.findtext(f"{{{XBRLI}}}instant", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) or (start and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start)):
            skipped["invalid_period"] += 1
            continue
        contexts[context.get("id")] = {"start": start, "end": end}
    for unit in root.findall(f"{{{XBRLI}}}unit"):
        measures = unit.findall(f"{{{XBRLI}}}measure")
        if len(measures) != 1 or unit.find(f"{{{XBRLI}}}divide") is not None:
            continue
        measure = str(measures[0].text or "")
        prefix, _, local = measure.partition(":")
        namespace = namespaces.get(prefix, "")
        if local == "USD" and namespace == "http://www.xbrl.org/2003/iso4217":
            units[unit.get("id")] = "USD"
        elif local == "shares" and namespace == XBRLI:
            units[unit.get("id")] = "shares"
    facts, duplicates = {}, {}
    for node in root:
        context = contexts.get(node.get("contextRef"))
        unit = units.get(node.get("unitRef"))
        if context is None or not unit:
            continue
        if node.get(f"{{{XSI}}}nil") in {"true", "1"}:
            skipped["nil"] += 1
            continue
        try:
            number = Decimal(str(node.text or "").strip())
            value = float(number)
            if not number.is_finite() or not math.isfinite(value):
                raise ValueError("nonfinite")
        except (InvalidOperation, ValueError, OverflowError):
            skipped["nonnumeric"] += 1
            continue
        namespace, local = node.tag[1:].split("}", 1)
        prefix = prefixes.get(namespace, "")
        qname = prefix + ":" + local
        key = (qname, unit, context["start"], context["end"])
        decimals = str(node.get("decimals", "INF"))
        try:
            if decimals != "INF" and (not re.fullmatch(r"-?\d{1,3}", decimals) or abs(int(decimals)) > 100):
                raise ValueError("invalid precision")
            resolution = Decimal(0) if decimals == "INF" else Decimal(10) ** -int(decimals)
        except (ValueError, InvalidOperation):
            skipped["invalid_precision"] += 1
            continue
        low, high = number - resolution / 2, number + resolution / 2
        if key in duplicates:
            previous = duplicates[key]
            if previous is None:
                continue
            low, high = max(low, previous[0]), min(high, previous[1])
            if low > high:
                duplicates[key] = None
                skipped["conflicting_duplicate"] += 1
                continue
            if resolution >= previous[2]:
                duplicates[key] = (low, high, previous[2])
                continue
        duplicates[key] = (low, high, resolution)
        facts[key] = {"qname": qname, "namespace": namespace, "unit": unit,
                      "val": value, "decimals": decimals, "context_id": node.get("contextRef"), **context}
    selected = [fact for key, fact in facts.items() if duplicates[key] is not None]
    return {"facts": selected, "skipped": dict(skipped), "sha256": hashlib.sha256(payload).hexdigest()}


def parse_labels(payload: bytes) -> dict:
    root, _ = xml_root(payload)
    result = {}
    for link in root.findall(f"{{{LINK}}}labelLink"):
        locations = {node.get(f"{{{XLINK}}}label"): node.get(f"{{{XLINK}}}href", "").split("#")[-1]
                     for node in link.findall(f"{{{LINK}}}loc")}
        labels = {node.get(f"{{{XLINK}}}label"): " ".join(node.itertext()).strip()
                  for node in link.findall(f"{{{LINK}}}label")
                  if node.get("{http://www.w3.org/XML/1998/namespace}lang", "en").startswith("en")
                  and node.get(f"{{{XLINK}}}role", "").endswith("/label")}
        for arc in link.findall(f"{{{LINK}}}labelArc"):
            source, target = arc.get(f"{{{XLINK}}}from"), arc.get(f"{{{XLINK}}}to")
            if source in locations and target in labels:
                result[locations[source].replace("_", ":", 1)] = labels[target]
    return result


def parse_calculations(payload: bytes) -> list:
    root, _ = xml_root(payload)
    result = []
    for link in root.findall(f"{{{LINK}}}calculationLink"):
        role = link.get(f"{{{XLINK}}}role", "")
        locations = {node.get(f"{{{XLINK}}}label"): node.get(f"{{{XLINK}}}href", "").split("#")[-1].replace("_", ":", 1)
                     for node in link.findall(f"{{{LINK}}}loc")}
        for arc in link.findall(f"{{{LINK}}}calculationArc"):
            if arc.get("use") == "prohibited":
                continue
            parent = locations.get(arc.get(f"{{{XLINK}}}from"))
            child = locations.get(arc.get(f"{{{XLINK}}}to"))
            try:
                weight = float(arc.get("weight", "nan"))
            except ValueError:
                continue
            if parent and child and weight in {-1., 1.}:
                result.append({"role": role, "parent": parent, "child": child, "weight": weight})
    return result


def filing_base(cik: str, accession: str) -> str:
    if not str(cik).isdigit() or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError("Invalid SEC filing identity")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def safe_filename(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name))


def collect_filing(session, headers: dict, cik: str, filing: dict) -> dict:
    base = filing_base(cik, filing["accessionNumber"])
    result = {**filing, "base_url": base, "facts": [], "labels": {}, "calculations": [], "errors": []}
    response = session.get(base + "index.json", headers=headers)
    if response is None:
        result["errors"].append("SEC_ARCHIVE_INDEX_UNAVAILABLE")
        return result
    try:
        items = response.json()["directory"]["item"]
    except (ValueError, KeyError, TypeError):
        result["errors"].append("INVALID_ARCHIVE_INDEX")
        return result
    names = [item["name"] for item in items if isinstance(item, dict) and safe_filename(str(item.get("name", "")))]
    instances = [name for name in names if name.lower().endswith("_htm.xml")]
    if len(instances) != 1:
        result["errors"].append("NO_UNAMBIGUOUS_EXTRACTED_XBRL_INSTANCE")
        return result
    instance = instances[0]
    response = session.get(base + instance, headers=headers)
    if response is None:
        result["errors"].append("SEC_INSTANCE_UNAVAILABLE")
        return result
    try:
        result.update(parse_instance(response.content, cik))
        result["instance_url"] = base + instance
    except (ValueError, ET.ParseError) as error:
        result["errors"].append(type(error).__name__ + ": " + str(error)[:120])
        return result
    for suffix, parser, field in (("_lab.xml", parse_labels, "labels"), ("_cal.xml", parse_calculations, "calculations")):
        matches = [name for name in names if name.lower().endswith(suffix)]
        if len(matches) != 1:
            result["errors"].append("NO_UNAMBIGUOUS_" + field.upper())
            continue
        response = session.get(base + matches[0], headers=headers)
        if response is None:
            result["errors"].append("SEC_" + field.upper() + "_UNAVAILABLE")
            continue
        try:
            result[field] = parser(response.content)
        except (ValueError, ET.ParseError):
            result["errors"].append("INVALID_" + field.upper())
    return result
