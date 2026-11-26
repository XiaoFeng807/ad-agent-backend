"""Product evidence retrieval and source verification for the listing catalog."""

import hashlib
import json
import re
from contextlib import closing
from functools import lru_cache

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from backend.database import listing_operations, product_documents as store
from backend.rag.chinese_embedding import BGEEmbedding, BGE_REVISION
from backend.rag.product_attribute_queries import attribute_queries
from backend.rag.product_fact_rules import questions_for, source_failure, validate_facts
from backend.rag.product_query_expansion import EXPANSIONS, ExpansionPolicy, catalog_queries, merge_channels, query_channels
from backend.rag.rag_knowledge import _keyword_tokens

FIXED_LENGTH = 320
FIXED_OVERLAP = 40
RECALL_K = 20
RRF_K = 60
BGE_ID = f"BAAI/bge-small-zh-v1.5@{BGE_REVISION}"
STRATEGIES = ("fixed", "structured")
MODES = ("vector", "hybrid")
R1_EXPANSION_POLICY = ExpansionPolicy(original_slots=5, english_part_terms=True)
DEFAULT_EXPANSION_POLICY = ExpansionPolicy(original_slots=5, english_part_terms=True, catalog_intents=True)
AUDIT_FIELDS = ("channels", "pool", "complete_sources", "queries", "encoding_calls", "full_vector_orders")
_HEADING = re.compile(r"^## ([^|\r\n]+) \| ([^\r\n]+)", re.M)
_LEGACY_SEPARATOR = re.compile(r"[，,；;。？?\n]+|\s*(?:以及|并且|另外)\s*")

class ReleaseConflict(ValueError):
    pass


def source_regions(kind, original):
    if kind == "specs":
        rows = []
        offset = 0
        for number, line in enumerate(original.splitlines(keepends=True), 1):
            content = line.rstrip("\r\n")
            columns = content.split("\t")
            if number > 1 and content:
                if len(columns) != 5 or any(not col.strip() for col in columns):
                    raise ValueError(f"incomplete specification row {number}")
                rows.append(_region(f"row:{number}", columns[0], offset, offset + len(content)))
            offset += len(line)
        regions = rows
    elif kind in ("manual", "faq"):
        headings = list(_HEADING.finditer(original))
        regions = []
        for index, heading in enumerate(headings):
            label, title = heading.groups()
            if kind == "manual" and not label.isdecimal():
                raise ValueError("manual section must identify its page")
            end = headings[index + 1].start() if index + 1 < len(headings) else len(original)
            body = original[heading.end():end]
            if not body.strip() or (kind == "faq" and not re.search(r"^Q: .+\r?\nA: \S", body, re.M)):
                raise ValueError(f"incomplete {kind} section {label}")
            location = f"page:{int(label)}" if kind == "manual" else f"faq:{label}"
            regions.append(_region(location, title, heading.start(), end - (len(body) - len(body.rstrip()))))
    else:
        raise ValueError(f"unsupported document kind {kind}")
    if not regions or len({region["location"] for region in regions}) != len(regions):
        raise ValueError("document needs unique source locations")
    return regions


def _region(location, section, start, end):
    return {"location": location, "section": section, "start_offset": start, "end_offset": end}


def _ranges(original, regions, strategy):
    if strategy == "structured":
        yield from ((region["start_offset"], region["end_offset"]) for region in regions)
        return
    for start in range(0, len(original), FIXED_LENGTH - FIXED_OVERLAP):
        end = min(len(original), start + FIXED_LENGTH)
        yield start, end
        if end == len(original):
            break


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _existing(conn, scope, version, fingerprint):
    row = conn.execute(
        """SELECT id, fingerprint, evidence_rules FROM product_document_releases
           WHERE shop_id=? AND marketplace_id=? AND sku=? AND version=?""", (*scope, version),
    ).fetchone()
    if row and row["fingerprint"] != fingerprint:
        raise ReleaseConflict("release version already exists with different contents or index settings")
    return row


def _rule_sets(encoded):
    rules = json.loads(encoded)
    return (rules, []) if isinstance(rules, list) else (rules["legacy"], rules["facts"])


def _reuse_release(conn, row, facts, encoded, previous_facts):
    if facts is None:
        return False
    existing = json.loads(row["evidence_rules"])
    if isinstance(existing, list) or (existing["facts"] != facts and existing["facts"] == previous_facts):
        # Update the fact configuration without changing the release's original text or chunks.
        conn.execute("UPDATE product_document_releases SET evidence_rules=? WHERE id=?", (encoded, row["id"]))
        return True
    if existing["facts"] != facts:
        raise ReleaseConflict("release version already has a different fact configuration")
    return False


def import_release(conn, manifest, source_dir, *, embedding=None, model_id=BGE_ID, previous_facts=None):
    """Import a release or update its fact rules with an optional expected previous configuration."""
    scope = _scope(manifest["target"])
    version = manifest["version"]
    if not isinstance(version, str) or not version.strip() or len(version) > 128:
        raise ValueError("invalid version")
    source_dir = source_dir.resolve()
    documents, locations = {}, {}
    for spec in manifest["documents"]:
        document_id, kind, filename = spec["id"], spec["kind"], spec["file"]
        path = (source_dir / filename).resolve()
        if not document_id or document_id in documents or not path.is_relative_to(source_dir):
            raise ValueError("invalid source document")
        original = path.read_bytes().decode("utf-8")
        regions = source_regions(kind, original)
        documents[document_id] = (kind, filename, original, regions)
        locations.update({
            (document_id, region["location"]): original[region["start_offset"]:region["end_offset"]]
            for region in regions
        })
    if not documents:
        raise ValueError("release needs source documents")
    rules = manifest.get("evidence_rules", [])
    for rule in rules:
        required = [tuple(ref) for ref in rule["requires"]]
        if (re.compile(rule["pattern"], re.I).fullmatch("") or not required
                or len(required) != len(set(required)) or any(ref not in locations for ref in required)):
            raise ValueError("evidence rules must reference complete source regions")
    facts = manifest.get("fact_rules")
    if "fact_rules" in manifest:
        validate_facts(facts, locations)
    encoded = _json(rules if facts is None else {"legacy": rules, "facts": facts})
    # The R3 fingerprint determines chunk IDs and tie order. Fact rules only affect admission.
    fingerprint = _digest([documents, rules, model_id, FIXED_LENGTH, FIXED_OVERLAP, 1])
    if _existing(conn, scope, version, fingerprint):
        if facts is None:
            return False
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return _reuse_release(conn, _existing(conn, scope, version, fingerprint), facts, encoded, previous_facts)

    chunks = []
    for document_id, (_, _, original, regions) in documents.items():
        for strategy in STRATEGIES:
            for start, end in _ranges(original, regions, strategy):
                chunk_id = _digest([scope, version, document_id, strategy, start, end, fingerprint])
                chunks.append((document_id, chunk_id, strategy, start, end, original[start:end]))
    encoder = BGEEmbedding(device="cpu") if embedding is None else embedding
    vectors = np.asarray(encoder([chunk[-1] for chunk in chunks]), dtype="<f4")
    if (vectors.ndim != 2 or vectors.shape[0] != len(chunks) or not vectors.shape[1]
            or not np.isfinite(vectors).all()):
        raise ValueError("invalid document embeddings")

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if row := _existing(conn, scope, version, fingerprint):
            return _reuse_release(conn, row, facts, encoded, previous_facts)
        if conn.execute(
            "SELECT 1 FROM listing_test_products WHERE shop_id=? AND marketplace_id=? AND sku=?", scope,
        ).fetchone() is None:
            raise listing_operations.ListingNotFound("product must exist in the listing catalog")
        release_id = conn.execute(
            """INSERT INTO product_document_releases
               (shop_id, marketplace_id, sku, version, fingerprint, embedding_model, evidence_rules)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (*scope, version, fingerprint, model_id, encoded),
        ).lastrowid
        source_ids = {}
        for document_id, (kind, filename, original, regions) in documents.items():
            source_ids[document_id] = conn.execute(
                """INSERT INTO product_documents (release_id, document_id, kind, filename, original, regions)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (release_id, document_id, kind, filename, original, _json(regions)),
            ).lastrowid
        conn.executemany(
            """INSERT INTO product_document_chunks
               (source_id, chunk_id, strategy, start_offset, end_offset, content, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(source_ids[chunk[0]], *chunk[1:], vector.tobytes()) for chunk, vector in zip(chunks, vectors)],
        )
    return True


def _scope(target):
    if set(target) != set(store.SCOPE_KEYS):
        raise ValueError("shop_id, marketplace_id and sku are required")
    scope = tuple(target[key] for key in store.SCOPE_KEYS)
    if any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in scope):
        raise ValueError("invalid product scope")
    return scope


def activate(conn, target, version):
    scope = _scope(target)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT id FROM product_document_releases
               WHERE shop_id=? AND marketplace_id=? AND sku=? AND version=?""", (*scope, version),
        ).fetchone()
        if row is None:
            raise LookupError("unknown release version")
        conn.execute(
            """UPDATE product_document_releases SET active=0
               WHERE shop_id=? AND marketplace_id=? AND sku=? AND active=1 AND id<>?""",
            (*scope, row["id"]),
        )
        conn.execute("UPDATE product_document_releases SET active=1 WHERE id=?", (row["id"],))


def _hit(row):
    start, end = row["start_offset"], row["end_offset"]
    return {
        **{key: row[key] for key in (*store.SCOPE_KEYS, "version", "document_id", "kind", "filename",
                                      "strategy", "chunk_id", "content", "start_offset", "end_offset")},
        "active": True,
        "locations": [{**region, "complete": start <= region["start_offset"] and end >= region["end_offset"]}
                      for region in json.loads(row["regions"])
                      if start < region["end_offset"] and end > region["start_offset"]],
    }


def read_chunk(conn, user_id, chunk_id):
    row = store.scoped_chunk(conn, user_id, chunk_id)
    return {**_hit(row), "original": row["original"]} if row is not None else None


@lru_cache(maxsize=32)
def _keyword_index(contents):
    if not any(next(_keyword_tokens(content), None) for content in contents):
        return None
    vectorizer = TfidfVectorizer(analyzer=_keyword_tokens)
    return vectorizer, vectorizer.fit_transform(contents)


def _keyword_order(rows, query):
    index = _keyword_index(tuple(row["content"] for row in rows))
    if index is None:
        return []
    vectorizer, matrix = index
    scores = (matrix @ vectorizer.transform([query]).T).toarray().ravel()
    return sorted((i for i, score in enumerate(scores) if score > 0), key=lambda i: (-scores[i], i))[:RECALL_K]


def _matching_rules(rules, query):
    return [(tuple(map(tuple, rule["requires"])), match.groupdict().get("part_code"))
            for rule in rules if (match := re.fullmatch(rule["pattern"], query, re.I))]


def _complete_references(candidates):
    refs = {}
    for hit in candidates:
        start, end = hit["start_offset"], hit["end_offset"]
        if not 0 <= start < end or end - start != len(hit["content"]):
            continue
        for loc in hit["locations"]:
            if loc["complete"] and start <= loc["start_offset"] < loc["end_offset"] <= end:
                refs.setdefault((hit["document_id"], loc["location"]), (hit, loc))
    return refs


def _citation(refs, ref):
    hit, loc = refs[ref]
    offset = hit["start_offset"]
    return {
        **{key: hit[key] for key in (*store.SCOPE_KEYS, "version", "document_id", "filename", "chunk_id")},
        **loc, "quote": hit["content"][loc["start_offset"] - offset:loc["end_offset"] - offset],
    }


def _verify_rule(refs, required, part_code):
    missing = [ref for ref in required if ref not in refs]
    if missing:
        return [], "incomplete_source_coverage", missing
    evidence = [_citation(refs, ref) for ref in required]
    if part_code and not any(re.search(r"(?<![\w-])" + re.escape(part_code) + r"(?![\w-])", item["quote"], re.I)
                             for item in evidence):
        return [], "part_code_unverified", []
    return evidence, None, []


def _evidence(candidates, rules, query, *, report_all_missing=False):
    matches = _matching_rules(rules, query)
    if len(matches) != 1:
        return [], "unsupported_question" if not matches else "ambiguous_question", []
    required, part_code = matches[0]
    evidence, reason, missing = _verify_rule(_complete_references(candidates), required, part_code)
    return evidence, reason, missing if report_all_missing or len(missing) < len(required) else []


def _fact_evidence(refs, fact):
    source = fact["source"]
    if source is None:
        return [], "source_not_configured"
    ref = source["document_id"], source["location"]
    if ref not in refs:
        return [], "incomplete_source_coverage"
    citation = _citation(refs, ref)
    reason = source_failure(source, citation["quote"])
    return ([] if reason else [citation]), reason


def _admit_facts(result, questions, legacy_rules=()):
    refs = _complete_references(result["candidates"])
    checks, missing, ambiguous, adopted = [], [], [], {}
    for clause, facts in questions:
        check = {"question": clause, "facts": [fact["key"] for fact in facts], "chunk_ids": []}
        checks.append(check)
        alternatives = set(_matching_rules(legacy_rules, clause)) if not facts else set()
        if len(facts) > 1 or len(alternatives) > 1:
            check.update(status="ambiguous", reason="ambiguous_question")
            ambiguous.append(clause)
            continue
        label = clause
        if facts:
            fact, = facts
            source = fact["source"]
            check.update(component=fact["component"], conditions=fact["conditions"], required_sources=[
                {key: source[key] for key in ("document_id", "location")}
            ] if source else [])
            evidence, reason = _fact_evidence(refs, fact)
            label = fact["missing"]
        elif alternatives:
            (required, part_code), = alternatives
            check["required_sources"] = [{"document_id": doc, "location": loc} for doc, loc in required]
            evidence, reason, _ = _verify_rule(refs, required, part_code)
        else:
            evidence, reason = [], "unsupported_question"
        if reason:
            check.update(status="missing", reason=reason, missing=label)
            missing.append(label)
            continue
        for citation in evidence:
            adopted.setdefault((citation["document_id"], citation["location"]), citation)
        check.update(status="verified", reason=None, chunk_ids=[citation["chunk_id"] for citation in evidence])
    evidence = list(adopted.values())
    status = ("partial_support" if evidence else "insufficient_evidence") if missing or ambiguous else "supported"
    reason = "ambiguous_question" if ambiguous else "incomplete_source_coverage" if missing else None
    return {**result, "status": status, "reason": reason, "ambiguous_questions": ambiguous,
            "fact_checks": checks, "missing_sections": list(dict.fromkeys(missing)), "evidence": evidence}


def admit_evidence(result, rows, query, *, report_all_missing=False, multi_intent=False):
    """Apply the active release's citation rules to the supplied candidate prefix."""
    if not rows:
        return result
    if multi_intent:
        scope = {**result["target"], "version": result["version"]}
        authorized = {row["chunk_id"]: row for row in rows if all(row[key] == value for key, value in scope.items())}
        if not authorized:
            return {**result, "candidates": []}
        # Resolve IDs only within this round's authorized snapshot. Read the
        # offsets and regions from that snapshot, including for reranked hits.
        result = {**result, "candidates": [
            _hit(authorized[hit["chunk_id"]]) for hit in result["candidates"][:RECALL_K]
            if hit["chunk_id"] in authorized and all(hit[key] == value for key, value in scope.items())
        ]}
        rows = list(authorized.values())
    rules, facts = _rule_sets(rows[0]["evidence_rules"])
    if multi_intent and not (rules or facts):
        result = {**result, "unconfigured_evidence": True}
    if questions := questions_for(query, facts):
        return _admit_facts(result, questions)
    if multi_intent:
        clauses = [clause.strip() for clause in _LEGACY_SEPARATOR.split(query) if clause.strip()]
        if len(clauses) > 1 and all(_matching_rules(rules, clause) for clause in clauses):
            return _admit_facts(result, [(clause, []) for clause in clauses], rules)
    evidence, reason, missing = _evidence(result["candidates"], rules, query,
                                         report_all_missing=report_all_missing)
    if missing:
        sections = {(row["document_id"], loc["location"]): loc["section"] for row in rows
                    for loc in json.loads(row["regions"])}
        result = {**result, "missing_sections": [sections[ref] for ref in missing]}
    return {**result, "evidence": evidence, "status": "supported" if evidence else "insufficient_evidence",
            "reason": reason}


class ProductEvidence:
    def __init__(self, *, embedding=None, model_id=BGE_ID, expansion_policy=DEFAULT_EXPANSION_POLICY):
        self.embedding = BGEEmbedding(device="cpu") if embedding is None else embedding
        self.model_id = model_id
        self.expansion_policy = expansion_policy

    def search(self, conn, user_id, target, query, *, strategy="structured", mode="hybrid", top_k=RECALL_K,
               expansion="none", audit=False, report_all_missing=False):
        query = query.strip()
        result, rows = self.retrieve(conn, user_id, target, query, strategy=strategy, mode=mode, top_k=top_k,
                                     expansion=expansion, audit=audit)
        return admit_evidence(result, rows, query, report_all_missing=report_all_missing)

    def retrieve(self, conn, user_id, target, query, *, strategy="structured", mode="hybrid", top_k=RECALL_K,
                 expansion="none", attribute_expansion="none", audit=False):
        """Return ranked hits and their authorized release snapshot for citation admission."""
        scope = _scope(target)
        if (strategy not in STRATEGIES or mode not in MODES or expansion not in EXPANSIONS
                or attribute_expansion not in EXPANSIONS
                or not 1 <= top_k <= RECALL_K or not 1 <= len(query) <= 500):
            raise ValueError("invalid retrieval options")
        if (expansion != "none" or attribute_expansion != "none") and (strategy, mode) != ("structured", "vector"):
            raise ValueError("query expansion requires structured vector retrieval")
        if listing_operations.scope_error(conn, user_id, *scope):
            raise listing_operations.ListingNotFound
        rows = store.scoped_chunks(conn, user_id, scope, strategy)
        base = {"target": target, "candidates": [], "evidence": [], "version": None,
                "status": "insufficient_evidence", "reason": "no_active_sources"}
        if audit:
            base["channels"] = {"vector": [], "keyword": []}
            base["pool"] = []
            base["complete_sources"] = set()
            base["queries"] = {}
            base["encoding_calls"] = 0
            base["full_vector_orders"] = {}
        if not rows:
            return base, rows
        if rows[0]["embedding_model"] != self.model_id:
            raise ReleaseConflict("active product release needs matching embeddings")
        vectors = np.stack([np.frombuffer(row["embedding"], dtype="<f4") for row in rows])
        by_id = {row["chunk_id"]: row for row in rows}
        queries = query_channels(query, expansion, self.expansion_policy)
        if attribute_expansion != "none":
            queries.update(attribute_queries(query, attribute_expansion))
        orders = {}
        for channel, text in queries.items():
            query_vector = np.asarray(self.embedding.embed_query([text])[0], dtype="<f4")
            scores = vectors @ query_vector
            indices = sorted(range(len(rows)), key=lambda i: (-scores[i], i))
            orders[channel] = [rows[i]["chunk_id"] for i in indices[:RECALL_K]]
            if audit:
                base["full_vector_orders"][channel] = [rows[i]["chunk_id"] for i in indices]
        keyword = [rows[i]["chunk_id"] for i in _keyword_order(rows, query)] if mode == "hybrid" else []
        if keyword:
            ranks = {}
            for channel in (orders["vector"], keyword):
                for rank, chunk_id in enumerate(channel, 1):
                    ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1 / (RRF_K + rank)
            ordered = sorted(ranks, key=lambda chunk_id: -ranks[chunk_id])
        elif len(orders) > 1:
            ordered = merge_channels(orders.values(), self.expansion_policy.original_slots)
        else:
            ordered = orders["vector"]
        candidates = [_hit(by_id[chunk_id]) for chunk_id in ordered[:top_k]]
        if audit:
            base["channels"] = {name: [_hit(by_id[chunk_id]) for chunk_id in order]
                                for name, order in {**orders, "keyword": keyword}.items()}
            base["pool"] = [{key: row[key] for key in (*store.SCOPE_KEYS, "version", "chunk_id")} for row in rows]
            base["complete_sources"] = {(hit["document_id"], loc["location"])
                                        for hit in map(_hit, rows) for loc in hit["locations"] if loc["complete"]}
            base["queries"] = queries
            base["encoding_calls"] = len(orders)
        return {**base, "candidates": candidates, "version": rows[0]["version"]}, rows


def chat_retrieval_options(query):
    if catalog_queries(query):
        return {"mode": "vector", "expansion": "both"}
    return {"mode": "hybrid", "expansion": "none"}


def search_product(user_id, target, query, *, check_session=None, report_all_missing=False):
    """Shared authenticated chat entry point. Only verified quotes reach the reply."""
    from backend.database.database import get_db

    with closing(get_db(read_only=True)) as conn:
        conn.execute("BEGIN")
        if check_session is not None and not check_session(conn):
            raise listing_operations.ListingNotFound
        options = chat_retrieval_options(query)
        return ProductEvidence().search(conn, user_id, target, query, report_all_missing=report_all_missing, **options)
