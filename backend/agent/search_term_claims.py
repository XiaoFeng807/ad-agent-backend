"""Verify search term demands against complete, scoped product specification rows."""

import re
from decimal import Decimal

from backend.database import product_documents
from backend.rag.product_evidence import read_chunk


TYPE = r"[a-z]+(?:[ -][a-z]+){0,7}"
NUMBER = r"[0-9]{1,5}(?:\.[0-9]{1,4})?"
UNIT = r'inches|inch|in|millimeters?|mm|centimeters?|cm|"'
DIMENSION = rf"(?P<measure>{NUMBER})\s*-?\s*(?P<unit>{UNIT})(?:\s+(?P<axis>wide|long|high|tall|deep|diameter))?"
AXES = {"wide": "width", "long": "length", "high": "height", "tall": "height",
        "deep": "depth", "diameter": "diameter", None: "size"}
DISPLAY_AXIS = {**dict(zip(AXES.values(), AXES)), "height": "high"}
VESAS = r"[0-9]{2,4}\s*x\s*[0-9]{2,4}"
MODEL = r"(?=[a-z0-9 -]*[0-9])[a-z0-9]+(?:[ -][a-z0-9]+){0,7}"
PART = r"body|lid|cap|base|basket|tray|insert|handle|seal|whole"
CLEANING = rf"(?:(?P<part>{PART})\s+)?(?:(?P<restriction>top rack(?: only)?)\s+)?(?P<method>dishwasher safe|hand wash only)"
PATTERNS = (
    ("size", rf"{DIMENSION}\s+(?P<product>{TYPE})"),
    ("size", rf"(?P<product>{TYPE})\s+{DIMENSION}"),
    ("vesa", rf"vesa\s+(?P<value>{VESAS})(?:\s*mm)?\s+(?P<product>{TYPE})"),
    ("vesa", rf"(?P<product>{TYPE})\s+vesa\s+(?P<value>{VESAS})(?:\s*mm)?"),
    ("model", rf"(?P<product>{TYPE})\s+(?:compatible with|fits|for)\s+(?P<value>{MODEL})"),
    ("cleaning", rf"(?P<product>{TYPE}?)\s+{CLEANING}"),
    ("cleaning", rf"{CLEANING}\s+(?P<product>{TYPE})"),
)
SIGNALS = {
    **dict.fromkeys(AXES.values(), rf"\b{NUMBER}\s*-?\s*(?:{UNIT})(?![a-z])"),
    "vesa": r"\bvesa\b", "model": r"\b(?:fits|for|compatible with)\b",
    "cleaning": r"\b(?:dishwasher|hand wash|top rack)\b",
}
SOURCE_FIELDS = set(AXES.values()) | {"product type", "vesa", "compatible models", "cleaning"}


def canonical(text):
    return " ".join(text.casefold().replace("×", "x").replace("‐", "-").replace("–", "-").split())


def measure_mm(value, unit):
    unit = canonical(unit)
    multiplier = Decimal("25.4") if unit in {"in", "inch", "inches", '"'} else (
        Decimal(10) if unit in {"cm", "centimeter", "centimeters"} else Decimal(1)
    )
    return format((Decimal(value) * multiplier).normalize(), "f")


def normalize_reported_demand(demand):
    if not isinstance(demand, dict):
        return demand
    result = {**demand}
    for key in ("product_type", "attribute", "value"):
        if isinstance(result.get(key), str):
            result[key] = canonical(result[key])
    value, unit = result.get("value"), result.get("unit")
    if result.get("attribute") == "vesa" and isinstance(value, str) and re.fullmatch(VESAS, value):
        result["value"] = value.replace(" ", "")
    if type(value) in (int, float):
        value = str(value)
    if (result.get("attribute") in AXES.values() and isinstance(value, str) and isinstance(unit, str)
            and re.fullmatch(NUMBER, value) and re.fullmatch(UNIT, canonical(unit))):
        result.update(value=measure_mm(value, unit), unit="mm")
    return result


def parse_demand(term):
    term = canonical(term).replace("top-rack", "top rack").replace("dishwasher-safe", "dishwasher safe")
    for kind, pattern in PATTERNS:
        if not (match := re.fullmatch(pattern, term)):
            continue
        fields = match.groupdict()
        product_type = fields["product"]
        if kind == "size":
            return {"product_type": product_type, "attribute": AXES[fields["axis"]],
                    "value": measure_mm(fields["measure"], fields["unit"]), "unit": "mm", "conditions": {}}
        if kind == "vesa":
            return {"product_type": product_type, "attribute": "vesa",
                    "value": fields["value"].replace(" ", ""), "unit": "mm", "conditions": {}}
        if kind == "model":
            return {"product_type": product_type, "attribute": "model", "value": fields["value"],
                    "unit": "", "conditions": {}}
        if fields["restriction"] and fields["method"] != "dishwasher safe":
            return None
        return {"product_type": product_type, "attribute": "cleaning", "value": fields["method"], "unit": "",
                "conditions": {"part": fields["part"] or "whole",
                               "restriction": "top rack only" if fields["restriction"] else ""}}
    return None


def source_fields(chunk):
    if chunk["kind"] != "specs":
        return None
    columns = tuple(canonical(cell) for cell in chunk["content"].split("\t"))
    return columns if len(columns) == 5 and columns[0] in SOURCE_FIELDS else None


def retrieve(conn, user_id, product, items):
    """Select typed rows from the existing release index and re-read their original slices."""
    if not product or not product.get("document_version") or not any(
        item["attribution"] == "product" for item in items
    ):
        return
    scope = tuple(product["target"][key] for key in product_documents.SCOPE_KEYS)
    rows = product_documents.scoped_chunks(conn, user_id, scope, "structured")
    chunks = []
    for row in rows:
        if source_fields(row) is None:
            continue
        chunk = read_chunk(conn, user_id, row["chunk_id"])
        if (chunk and chunk["version"] == product["document_version"]
                and any(loc["complete"] for loc in chunk["locations"])
                and chunk["content"] == chunk["original"][chunk["start_offset"]:chunk["end_offset"]]):
            chunks.append({key: chunk[key] for key in (
                "chunk_id", "sku", "version", "document_id", "filename", "kind", "content", "locations",
            )})
    for item in items:
        if item["attribution"] != "product":
            continue
        demand = parse_demand(item["term"])
        field = demand["attribute"] if demand else None
        fields = {"product type", "compatible models" if field == "model" else field}
        # Omit overlarge evidence sets entirely so truncation cannot hide conflicting specifications.
        matching = [chunk for chunk in chunks if source_fields(chunk)[0] in fields]
        item["source_chunks"] = matching if len(matching) <= 16 and all(
            len(chunk["content"]) <= 500 for chunk in matching
        ) else []


def relation(fields, demand):
    """Return support, explicit conflict, or no conclusion from a complete source row."""
    attribute, value, unit, part, restriction = fields
    requested = demand["value"]
    if attribute in AXES.values():
        if (part == "whole" and restriction == "-" and re.fullmatch(NUMBER, value)
                and re.fullmatch(UNIT, unit)):
            return "support" if measure_mm(value, unit) == requested else "conflict"
    elif attribute in {"vesa", "compatible models"}:
        values = [canonical(entry) for entry in value.split("|")]
        if attribute == "vesa" and unit == "mm" and part == "mount":
            valid = all(re.fullmatch(VESAS, entry) for entry in values)
            values = [entry.replace(" ", "") for entry in values]
        else:
            valid = (attribute == "compatible models" and unit == "-" and part == "whole"
                     and all(re.fullmatch(MODEL, entry) for entry in values))
        if valid and restriction in {"-", "complete list"}:
            if requested in values:
                return "support"
            if restriction == "complete list":
                return "conflict"
    elif attribute == "cleaning" and unit == "-" and re.fullmatch(PART, part):
        conditions = demand["conditions"]
        limited = value == "dishwasher safe" and restriction == "top rack only"
        valid = (value in {"dishwasher safe", "hand wash only"} and restriction == "-") or limited
        if valid and part == conditions["part"]:
            if value == requested and (not limited or conditions["restriction"]):
                return "support"
            return "conflict"
        if valid and part == "whole" and requested == "dishwasher safe":
            if value == "hand wash only" or (limited and not conditions["restriction"]):
                return "conflict"
        if valid and requested == "dishwasher safe" and conditions["part"] == "whole":
            if value == "hand wash only" or (limited and not conditions["restriction"]):
                return "conflict"
    return None


def check_sources(chunks, demand):
    attribute = demand["attribute"]
    types, assertions = [], []
    for chunk in chunks:
        fields = source_fields(chunk)
        if fields[0] == "product type" and fields[2:] == ("-", "whole", "-"):
            types.append((fields[1], chunk))
        if fields[0] == ("compatible models" if attribute == "model" else attribute):
            if outcome := relation(fields, demand):
                assertions.append((outcome, chunk))
    if {value for value, _ in types} != {demand["product_type"]}:
        return None, [], "缺少唯一且匹配的商品类型原文。"
    outcomes = {outcome for outcome, _ in assertions}
    if len(outcomes) != 1:
        names = {"size": "完整尺寸", "width": "宽度", "length": "长度", "height": "高度", "depth": "深度",
                 "diameter": "直径", "vesa": "完整 VESA 孔距", "model": "完整兼容型号或排除范围",
                 "cleaning": f"{demand['conditions'].get('part', '')} 部件的清洗方式与适用条件"}
        return None, [], f"缺少一致、可核验的{names[attribute]}原文。"
    return outcomes.pop(), [chunk for _, chunk in types + assertions], ""


def title_coverage(title, demand):
    text = canonical(title).replace(",", " ")
    words = text.split()
    # Allow a brand before the product/claim phrase and modifiers outside it.
    for i in range(len(words)):
        for j in range(i + 2, min(i + 17, len(words)) + 1):
            if parse_demand(" ".join(words[i:j])) == demand:
                # A model followed by another identifier token might be a longer, different model.
                if demand["attribute"] != "model" or j == len(words):
                    return "covered"
    attribute = demand["attribute"]
    if re.search(SIGNALS[attribute], text) or not re.search(
        rf"\b{re.escape(demand['product_type'])}\b", text
    ):
        return "ambiguous"
    return "missing"


def title_claim(demand, term):
    attribute, value = demand["attribute"], demand["value"]
    if attribute in AXES.values():
        # Prefer the unit used in the search and compare converted units when checking coverage.
        measurement = re.search(DIMENSION, canonical(term))
        unit = measurement["unit"]
        unit = "Inch" if unit in {"in", "inch", "inches", '"'} else (
            "mm" if unit in {"mm", "millimeter", "millimeters"} else "cm"
        )
        axis = DISPLAY_AXIS[attribute]
        return f"{measurement['measure']} {unit}" + (f" {axis.title()}" if axis else "")
    if attribute == "vesa":
        return f"VESA {value}"
    if attribute == "model":
        return f"Compatible with {value.upper()}"
    part = demand["conditions"]["part"]
    restriction = demand["conditions"]["restriction"]
    return " ".join(filter(None, (part if part != "whole" else "", restriction, value))).title()
