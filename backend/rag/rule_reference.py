"""Versioned rule identifiers used by diagnosis requests and retrieval."""

import re

from backend.rag.review_rule_demo import DEMO_RULES


_REFERENCE = re.compile(r"([\w.-]+)-(v[0-9]+(?:\.[0-9]+)*)#([\w.-]+)", re.ASCII)
_VERSIONED_NAME = re.compile(r"(.+?)\s+([vV][0-9]+(?:\.[0-9]+)*)")
_CTR_FAMILY = re.compile(r"ctr-review-(v[0-9]+(?:\.[0-9]+)*)")
_CTR_SECTIONS = tuple(dict.fromkeys(rule_id for _, rule_id, _, _, _ in DEMO_RULES
                                   if rule_id.startswith("ctr-review#")))
_TITLES = {title.casefold(): rule_id for _, rule_id, _, title, _ in DEMO_RULES}


def parse_rule_reference(reference):
    match = _REFERENCE.fullmatch(reference)
    if match:
        family, version, section = match.groups()
        return f"{family}#{section}", version
    return None


def rule_reference(rule_id, version):
    family, _, section = rule_id.partition("#")
    return f"{family}-{version}#{section}"


def canonical_rule_query(query):
    """Recognize complete rule references and named titles with an explicit version."""
    if parse_rule_reference(query) or _CTR_FAMILY.fullmatch(query):
        return query
    match = _VERSIONED_NAME.fullmatch(query)
    if not match:
        return query
    name, version = match.groups()
    version = version.lower()
    title = " ".join(name.casefold().split())
    if title in {"ctr 复核规则", "ctr复核规则", "ctr review rule"}:
        return f"ctr-review-{version}"
    rule_id = _TITLES.get(title, name)
    reference = rule_reference(rule_id, version)
    return reference if parse_rule_reference(reference) else query


def rule_query_scope(query):
    """Return the section IDs and version for an exact retrieval query."""
    if identity := parse_rule_reference(query):
        rule_id, version = identity
        return (rule_id,), version
    if match := _CTR_FAMILY.fullmatch(query):
        return _CTR_SECTIONS, match[1]
    return None
