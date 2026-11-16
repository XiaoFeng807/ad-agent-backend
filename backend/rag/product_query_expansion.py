"""Query-only expansion for catalog attributes and coordinated product parts."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExpansionPolicy:
    original_slots: int
    english_part_terms: bool
    catalog_intents: bool = False


EXPANSIONS = ("none", "aliases", "parts", "both")


ATTRIBUTE_ALIASES = (
    (r"高度|多高", "height"),
    (r"空(?:杯|瓶)(?:的)?(?:重量|多重|有多重)", "empty weight"),
    (r"直径|横向尺寸|多宽", "diameter"),
    (r"容量", "capacity"),
    (r"扭矩", "torque"),
    (r"(?:配件|零件)号", "service code"),
)
PARTS = {
    "杯身": "bottle body", "瓶身": "bottle body", "bottle body": "bottle body", "body": "bottle body",
    "杯盖": "cap", "盖子": "cap", "cap": "cap", "lid": "cap",
    "密封圈": "gasket", "gasket": "gasket", "seal": "gasket",
}
# ASCII boundaries keep identifiers such as cap-123 intact while allowing adjacent Chinese text.
_PART = r"(?<![A-Za-z0-9_-])(?:" + "|".join(map(re.escape, PARTS)) + r")(?![A-Za-z0-9_-])"
_COORDINATED = re.compile(rf"(?P<first>{_PART})(?:\s+and\s+|和|与|及|、)(?P<second>{_PART})", re.I)
_WASH = re.compile(r"洗碗机|机洗|手洗|清洗|dishwasher|\bwash(?:ing)?\b", re.I)
_DISHWASHER = re.compile(r"洗碗机|机洗|dishwasher", re.I)
_REPLACEMENT_CAP = re.compile(
    r"(?:替换|更换|备用|换个|换新)(?:的|一只|一个)?(?:杯盖|瓶盖|盖子|盖)"
    r"|\b(?:replacement|spare)\s+(?:bottle\s+)?(?:caps?|lids?)\b", re.I,
)
_COMPATIBILITY = re.compile(
    r"通用|兼容|互换|适配|外观|外形|长得|看起来|一样|相似"
    r"|\b(?:fit|fits|compatible|compatibility|interchange\w*|lookalikes?|similar|appearance)\b", re.I,
)
_MICROWAVE = re.compile(r"微波炉|\bmicrowav\w*\b", re.I)
_DRINKWARE = re.compile(r"杯|瓶|饮料|\b(?:bottles?|mugs?|tumblers?|drinks?|tea|coffee)\b", re.I)
_HOLDER = re.compile(r"杯架|杯座|杯托|\b(?:cup|bottle)[ -]?holders?\b|\b(?:my|the) holder\b", re.I)
_FIT = re.compile(r"适配|能放|放到|尺寸|高度|总高|直径|宽度|取出|比|\b(?:fit|width|height|room|clearance|compare)\b", re.I)


def catalog_queries(query, expansion="both"):
    """Build focused queries from the question's intent inside the authorized product."""
    aliases = expansion in ("aliases", "both")
    parts = expansion in ("parts", "both")
    result = {}
    if _REPLACEMENT_CAP.search(query) and _COMPATIBILITY.search(query):
        if aliases:
            result["cap_compatibility"] = "替换盖 外观相似 是否通用 replacement cap compatibility similar appearance exact SKU"
        if parts:
            result["cap_service_part"] = "replacement screw cap service part number service code cap thread bottle body"
    if aliases and _MICROWAVE.search(query) and _DRINKWARE.search(query):
        result["microwave"] = "微波炉加热 水杯使用限制 microwave heating bottle warming instructions"
    if parts and _HOLDER.search(query) and _FIT.search(query):
        result["holder_dimensions"] = "杯身直径 闭盖高度 bottle body diameter overall height with the cap closed"
        result["holder_clearance"] = "杯架适配与取出空间 cup holder fit opening width clearance to lift bottle out"
    return result


def alias_query(query):
    terms = [term for pattern, term in ATTRIBUTE_ALIASES if re.search(pattern, query)]
    return f"{query} {' '.join(terms)}" if terms else None


def part_queries(query, *, english_terms):
    if not _WASH.search(query):
        return []
    pairs = list(_COORDINATED.finditer(query))
    if len(pairs) != 1:
        return []
    pair = pairs[0]
    first, second = pair.group("first", "second")
    if PARTS[first.lower()] == PARTS[second.lower()]:
        return []
    # Keep the whole clause around the coordinated phrase, including restrictions and identifiers.
    # For longer lists, leave the original query to handle the ambiguous scope.
    if re.search(rf"^\s*(?:and\s+|和|与|及|、){_PART}", query[pair.end():], re.I):
        return []
    result = []
    for part in (first, second):
        projected = query[:pair.start()] + part + query[pair.end():]
        if english_terms:
            method = "dishwasher wash" if _DISHWASHER.search(query) else "wash"
            projected += f" {PARTS[part.lower()]} {method}"
        result.append(projected)
    return result


def query_channels(query, expansion, policy):
    channels = {"vector": query}
    if expansion in ("aliases", "both") and (alias := alias_query(query)):
        channels["alias"] = alias
    if expansion in ("parts", "both"):
        for i, projected in enumerate(part_queries(query, english_terms=policy.english_part_terms), 1):
            channels[f"part_{i}"] = projected
    if policy.catalog_intents:
        channels.update(catalog_queries(query, expansion))
    return channels


def merge_channels(orders, original_slots):
    """Keep a prefix of the original ranks, then interleave unique hits by channel."""
    original, *expanded = orders
    merged = dict.fromkeys(original[:original_slots])
    tails = [iter(order) for order in (*expanded, original[original_slots:])]
    while tails:
        remaining = []
        for tail in tails:
            for candidate in tail:
                if candidate not in merged:
                    merged[candidate] = None
                    remaining.append(tail)
                    break
        tails = remaining
    return list(merged)
