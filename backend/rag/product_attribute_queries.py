"""Attribute aliases and question projections for product chat retrieval."""

import re


_CLAUSE_BREAK = re.compile(r"[，,；;。！？?!\n]+|\s*(?:以及|并且|另外)\s*|\s+and\s+", re.I)
_BATTERY = re.compile(r"电芯|(?:内置|机内|灯内)电池|\bbuilt[ -]in battery\b|\binternal battery\b|\b(?:battery )?cell\b", re.I)
_CAPACITY = re.compile(r"容量|额定|毫安时|\b(?:capacity|mAh)\b", re.I)
_CHARGER = re.compile(r"充电器|充电线|适配器|\b(?:charger|adapter|charging cable)\b", re.I)
_FOLD = re.compile(r"收拢|折起|折叠|合拢|收起|闭合|\bfolded\b|\bstand.{0,15}closed\b", re.I)
_THICKNESS = re.compile(r"厚|\b(?:thick(?:ness)?|depth)\b", re.I)
_RESERVOIR = re.compile(r"额定(?:容积|容量)|(?:接|灌|加)满.{0,8}(?:几|多少)升|(?:水箱|储水罐).{0,14}(?:容积|容量|升)"
                        r"|\b(?:reservoir|water tank)\b.{0,32}\b(?:capacity|volume|lit[er]+s?)\b", re.I)
_DUTY = re.compile(r"(?:连续|一直|不松).{0,16}(?:喷|冲洗|运行).{0,12}(?:分钟|多久|时长)"
                   r"|\b(?:continuous|uninterrupted)\b.{0,24}\b(?:spray\w*|run\w*)\b.{0,30}\b(?:time|duration|minutes|duty cycle)\b", re.I)
_INLET_TEMP = re.compile(r"水温.{0,10}(?:上限|最高|限值)|进水温度|最多.{0,5}接多热的?水|接.{0,6}热水.{0,12}(?:上限|最高|几度)"
                         r"|\b(?:maximum|max|limit)\b.{0,24}\b(?:inlet|intake|feed)\b.{0,20}\btemperature\b"
                         r"|\b(?:inlet|intake|feed)\b.{0,20}\btemperature\b.{0,16}\b(?:limit|maximum)\b", re.I)
_HOSE = re.compile(r"软管|\bhoses?\b", re.I)
_HOSE_ASSEMBLY_ONLY = re.compile(r"软管头|软管弯头|软管接头|\bhose (?:elbow|connector|fitting|coupler)\b", re.I)
_REPLACEMENT = re.compile(r"替换|更换|备用|补买|订货|\b(?:replacement|spare|order(?:ing)?)\b", re.I)
_ORDER_CODE = re.compile(r"订货号|配件号|编号|料号|零件号|\b(?:part|order|service|ordering) (?:number|code|id)\b", re.I)
_OPERATING_ATTRIBUTES = (
    ("reservoir", _RESERVOIR, "rated reservoir capacity reservoir rated volume L"),
    ("spray_time", _DUTY, "continuous spray duty cycle uninterrupted spray time minutes"),
    ("inlet_temperature", _INLET_TEMP, "maximum working water temperature pump inlet °C"),
)


def attribute_queries(query, expansion):
    """Project explicitly requested attributes without using the bound catalog."""
    clauses = [clause.strip() for clause in _CLAUSE_BREAK.split(query) if clause.strip()]
    facts = {}
    for clause in clauses:
        if _BATTERY.search(clause) and _CAPACITY.search(clause) and not _CHARGER.search(clause):
            facts.setdefault("cell_capacity", (clause, "internal battery capacity mAh"))
        if _FOLD.search(clause) and _THICKNESS.search(clause):
            facts.setdefault("folded_thickness", (clause, "folded case thickness stand fully closed mm"))
        for name, pattern, terms in _OPERATING_ATTRIBUTES:
            if pattern.search(clause):
                facts.setdefault(name, (clause, terms))
        if (_HOSE.search(clause) and not _HOSE_ASSEMBLY_ONLY.search(clause)
                and _REPLACEMENT.search(clause) and _ORDER_CODE.search(clause)):
            facts.setdefault("hose_code", (clause, "replacement hose order code service hose assembly"))
    split = len({clause for clause, _ in facts.values()}) >= 2
    if expansion == "parts":
        if not split:
            return {}
        return {f"attribute_{name}": clause for name, (clause, _) in list(facts.items())[:2]}
    if expansion == "aliases" and facts:
        return {"attribute_alias": " ".join(terms for _, terms in list(facts.values())[:2]) + " " + query}
    return {f"attribute_{name}": f"{terms} {clause}" for name, (clause, terms) in list(facts.items())[:2]}
