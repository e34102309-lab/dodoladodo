"""Read consolidated facts from original SEC XBRL instances, without guessing mappings."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import io
import math
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
MAX_XML_BYTES = 20_000_000


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
    facts, duplicates = [], {}
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
        if key in duplicates:
            if duplicates[key] != number:
                duplicates[key] = None
                skipped["conflicting_duplicate"] += 1
            continue
        duplicates[key] = number
        facts.append({"qname": qname, "namespace": namespace, "unit": unit,
                      "val": value, "context_id": node.get("contextRef"), **context})
    facts = [fact for fact in facts if duplicates[(fact["qname"], fact["unit"], fact["start"], fact["end"])] is not None]
    return {"facts": facts, "skipped": dict(skipped), "sha256": hashlib.sha256(payload).hexdigest()}


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
