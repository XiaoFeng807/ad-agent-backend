"""Versioned single-fact configuration and question matching."""

import re

_KEY = re.compile(r"[a-z][a-z0-9_]*")
_SEPARATOR = re.compile(r"[，,；;。？?！!\n]+|\s*(?:以及|并且|另外|同时)\s*|\s+and\s+", re.I)
_JOIN = re.compile(r"\s*(?:和|还有|再问|再查|顺便)\s*")
_QUESTION_WORDS = re.compile(r"多少|多久|几分钟|几升|哪个|什么|如何|吗$|\b(?:how|what|which|when|where)\b", re.I)
SOURCE_CHECKS = {
    "component_pattern": "component_unverified",
    "value_pattern": "value_or_unit_unverified",
    "condition_pattern": "condition_unverified",
}


def source_failure(source, quote):
    return next((reason for key, reason in SOURCE_CHECKS.items() if not re.search(source[key], quote, re.I)), None)


def validate_facts(facts, originals):
    if not isinstance(facts, list):
        raise ValueError("fact_rules must be a list")
    keys = set()
    for fact in facts:
        if (not isinstance(fact, dict)
                or set(fact) != {"key", "component", "conditions", "questions", "source", "missing"}
                or any(not isinstance(fact[name], str) or not fact[name].strip()
                       for name in ("key", "component", "conditions", "missing"))):
            raise ValueError("invalid fact definition")
        if not _KEY.fullmatch(fact["key"]) or fact["key"] in keys:
            raise ValueError(f"invalid or duplicate fact key {fact['key']}")
        keys.add(fact["key"])
        questions = fact["questions"]
        if not isinstance(questions, list) or not questions:
            raise ValueError("fact needs at least one question pattern")
        for pattern in questions:
            _validate_pattern(pattern)
        source = fact["source"]
        if source is None:
            continue
        if (not isinstance(source, dict) or set(source) != {"document_id", "location", *SOURCE_CHECKS}
                or any(not isinstance(value, str) or not value.strip() for value in source.values())):
            raise ValueError("invalid fact source")
        for field in SOURCE_CHECKS:
            _validate_pattern(source[field])
        ref = source["document_id"], source["location"]
        if ref not in originals:
            raise ValueError(f"fact {fact['key']} source {ref} is outside the release")
        if reason := source_failure(source, originals[ref]):
            raise ValueError(f"fact {fact['key']} source {ref} failed: {reason}")


def _validate_pattern(pattern):
    try:
        if isinstance(pattern, str) and pattern.strip() and not re.compile(pattern, re.I).search(""):
            return
    except re.error as error:
        raise ValueError("invalid fact pattern") from error
    raise ValueError("fact patterns must match nonempty text")


def questions_for(query, facts):
    if not facts:
        return []

    def matches(text):
        return [fact for fact in facts if any(re.fullmatch(pattern, text, re.I) for pattern in fact["questions"])]

    def split_joined(text):
        # Keep conjunctions inside a component or condition unless they join two questions.
        for join in _JOIN.finditer(text):
            left, right = text[:join.start()].strip(), text[join.end():].strip()
            first, second = matches(left), matches(right)
            if (first and (second or _QUESTION_WORDS.search(right))) or (second and _QUESTION_WORDS.search(left)):
                return [(left, first), (right, second)]
        return [(text, [])]

    clauses = [part.strip() for part in _SEPARATOR.split(query) if part.strip()]
    questions = []
    i = 0
    while i < len(clauses):
        clause = clauses[i]
        matched = matches(clause)
        if not matched and i + 1 < len(clauses):
            # A version can recognize a leading condition together with the following clause.
            combined = f"{clause}，{clauses[i + 1]}"
            if matched := matches(combined):
                clause = combined
                i += 1
        questions.extend([(clause, matched)] if matched else split_joined(clause))
        i += 1
    return questions if any(matched for _, matched in questions) else []
