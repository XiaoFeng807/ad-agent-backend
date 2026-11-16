"""Product chat retrieval, GPU admission, and citation revalidation."""

import re
from contextlib import closing
from math import isfinite
from threading import Lock
from time import perf_counter

from backend.config.config import settings
from backend.database import database, listing_operations, product_documents
from backend.observability import add_trace_step, record_late_product_rerank
from backend.rag import product_evidence as rag
from backend.rag.product_rerank_queue import RerankQueue

TOP_K = 5
_R4_OPTIONS = {"mode": "vector", "expansion": "both"}
_CPU_OPTIONS = {**_R4_OPTIONS, "attribute_expansion": "both"}
_queue = None
_queue_lock = Lock()
_IDENTIFIER = r"[A-Z0-9]+(?:[._-]+[A-Z0-9]+)*"
_TOKEN = re.compile(rf"(?<![A-Z0-9_-]){_IDENTIFIER}(?![A-Z0-9_-])", re.I | re.ASCII)
# Consume the whole SKU label before looking for its value.
_LABELED_MODEL = re.compile(
    r"(?:\bSKU\b(?:\s+(?P<qualifier>is|number)(?![A-Z0-9]|[._-]+[A-Z0-9]))?+"
    r"|(?:商品|产品)?型号|\b(?:product )?model\b)"
    rf"\s*[:：#=]?\s*[`'\"“]?\s*(?=(?P<value>{_TOKEN.pattern}))",
    re.I | re.ASCII,
)
_MODEL_FAMILY = re.compile(r"([A-Z][A-Z0-9._-]*[._-][A-Z]*)[0-9]+[A-Z]*", re.ASCII)


def get_queue():
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = RerankQueue(settings.PRODUCT_RERANK_MAX_CONCURRENCY, settings.PRODUCT_RERANK_QUEUE_SIZE,
                                 settings.PRODUCT_RERANK_QUEUE_TIMEOUT_MS, settings.PRODUCT_RERANK_INFERENCE_TIMEOUT_MS)
        return _queue


def chunk_ids(hits):
    return [hit["chunk_id"] for hit in hits]


def current_scope(conn, user_id, target, check_session, version, ids):
    scope = rag._scope(target)
    if not check_session(conn) or listing_operations.scope_error(conn, user_id, *scope):
        return False
    if ids:
        return product_documents.current_chunk_ids(conn, user_id, scope, version, ids) == ids
    return product_documents.active_version(conn, scope) == version


def current(user_id, target, check_session, result, hits):
    with closing(database.get_db(read_only=True)) as conn:
        conn.execute("BEGIN")
        return current_scope(conn, user_id, target, check_session, result["version"], set(chunk_ids(hits)))


def _conflicting_skus(conn, user_id, scope, query):
    shop, site, bound_sku = scope
    family = _MODEL_FAMILY.fullmatch(bound_sku.upper())
    labels = list(_LABELED_MODEL.finditer(query))
    label_words = {label.start("qualifier") for label in labels}
    labeled_models = {label.start("value") for label in labels}
    conflicts = {}
    # An unlisted model needs an explicit label or the bound SKU's complete model family.
    for match in _TOKEN.finditer(query):
        token = match[0]
        if match.start() in label_words or token == bound_sku or token.upper() in conflicts:
            continue
        same_shop_and_site = not listing_operations.scope_error(conn, user_id, shop, site, token)
        if not same_shop_and_site and token.upper() == bound_sku.upper():
            continue
        model = _MODEL_FAMILY.fullmatch(token.upper())
        same_family = family and model and family[1] == model[1]
        explicitly_labeled = token.upper() != "SKU" and match.start() in labeled_models
        if same_shop_and_site or same_family or explicitly_labeled:
            conflicts.setdefault(token.upper(), token)
    return list(conflicts.values())


def search(user_id, target, query, *, check_session, trace_id=None, cancelled=lambda: False):
    query = query.strip()
    enabled = settings.PRODUCT_RERANK_ENABLED and user_id in settings.PRODUCT_RERANK_USER_IDS
    r4 = settings.PRODUCT_R4_CPU_ENABLED
    cpu_path = "r4_cpu" if r4 else "default"
    trace = {"path": "rerank" if enabled else cpu_path, "eligible": enabled, "scored_chunks": 0}

    def cpu_search(reason=None):
        trace.update(path=cpu_path, cpu_path=cpu_path)
        if reason:
            trace["fallback_reason"] = reason
        if cancelled():
            return None
        options = _CPU_OPTIONS if r4 else rag.chat_retrieval_options(query)
        trace_key = "r4_retrieval" if r4 else "default_retrieval"
        result, rows = _retrieve(user_id, target, query, check_session, trace, trace_key, options)
        if cancelled():
            return None
        result = rag.admit_evidence(result, rows, query, report_all_missing=r4 or enabled, multi_intent=True)
        if not current(user_id, target, check_session, result, result["candidates"]):
            raise listing_operations.ListingNotFound
        return result

    try:
        if cancelled():
            trace["status"] = "cancelled"
            return None
        scope = rag._scope(target)
        if not 1 <= len(query) <= 500:
            raise ValueError("invalid query length")
        with closing(database.get_db(read_only=True)) as conn:
            conn.execute("BEGIN")
            if not check_session(conn) or listing_operations.scope_error(conn, user_id, *scope):
                raise listing_operations.ListingNotFound
            trace["selected_sku"] = scope[2]
            conflicts = _conflicting_skus(conn, user_id, scope, query)
        if conflicts:
            trace.update(status="sku_conflict", admission_reason="sku_conflict", blocked_at="before_retrieval",
                         bound_sku=scope[2], mentioned_skus=conflicts, candidate_ids=[], citation_ids=[])
            return {"status": "sku_conflict", "mentioned_skus": conflicts}
        if enabled:
            result = _gray_search(user_id, target, query, check_session, trace_id, cancelled, trace, cpu_search)
        else:
            result = cpu_search()
        if result is not None and not cancelled() and not current(
            user_id, target, check_session, result, result["evidence"],
        ):
            if "cpu_path" in trace:
                raise listing_operations.ListingNotFound
            result = cpu_search("scope_changed_before_return")
            if result is not None and not current(user_id, target, check_session, result, result["evidence"]):
                raise listing_operations.ListingNotFound
        if result is None or cancelled():
            trace["status"] = "cancelled"
            return None
        trace.update(status=result["status"], admission_reason=result.get("reason"),
                     citation_ids=chunk_ids(result["evidence"]))
        if "fact_checks" in result:
            trace["version"] = result["version"]
            trace["fact_checks"] = result["fact_checks"]
        return result
    except listing_operations.ListingNotFound:
        trace["status"] = "denied"
        raise
    finally:
        add_trace_step(trace_id, "product_evidence", "chat_retrieval", evidence=trace)


def _retrieve(user_id, target, query, check_session, trace, trace_key, options):
    details = trace[trace_key] = dict(options)
    trace["candidate_ids"] = []
    retrieve_started = perf_counter()
    try:
        with closing(database.get_db(read_only=True)) as conn:
            conn.execute("BEGIN")
            if not check_session(conn):
                raise listing_operations.ListingNotFound
            result, rows = rag.ProductEvidence().retrieve(conn, user_id, target, query, **options)
    finally:
        trace["retrieval_ms"] = (perf_counter() - retrieve_started) * 1000
        details["ms"] = trace["retrieval_ms"]
    details["candidate_ids"] = trace["candidate_ids"] = chunk_ids(result["candidates"])
    return result, rows


def _gray_search(user_id, target, query, check_session, trace_id, cancelled, trace, cpu_search):
    result, rows = _retrieve(user_id, target, query, check_session, trace, "rerank_retrieval", _R4_OPTIONS)
    hits = result["candidates"]
    ids = trace["candidate_ids"]
    if not hits:
        return cpu_search("no_candidates")
    if not current(user_id, target, check_session, result, hits):
        return cpu_search("scope_changed_before_queue")

    def run(load_model, active, timing):
        if not current(user_id, target, check_session, result, hits):
            return None, "scope_changed_while_queued"
        if not active() or cancelled():
            return None, "cancelled"
        load_start = perf_counter()
        model = load_model()
        timing["model_wait_ms"] = (perf_counter() - load_start) * 1000
        if metadata := getattr(model, "metadata", None):
            timing["model"] = {key: metadata[key] for key in ("revision", "weight_sha256", "gpu", "torch",
                                                               "local_load_ms", "weight_verification_ms")}
        if not active() or cancelled():
            return None, "cancelled"
        if not current(user_id, target, check_session, result, hits):
            return None, "scope_changed_before_scoring"
        if not active() or cancelled():
            return None, "cancelled"
        start = perf_counter()
        timing.update(scored_chunks=len(ids), scored_chunk_ids=ids)
        try:
            scores = model.score(query, [hit["content"] for hit in hits])
        finally:
            timing["inference_ms"] = (perf_counter() - start) * 1000
        if len(scores) != len(ids) or len(set(ids)) != len(ids) or not all(map(isfinite, scores)):
            return None, "invalid_rerank_scores"
        return [hits[i] for i in sorted(range(len(ids)), key=lambda i: -scores[i])], None

    ranked, reason, timing = get_queue().submit(
        run, cancelled, lambda reason, timing: record_late_product_rerank(trace_id, reason, timing),
    )
    trace.update(timing)
    if cancelled() or reason == "cancelled":
        return None
    if reason:
        return cpu_search(reason)
    if not current(user_id, target, check_session, result, ranked):
        return cpu_search("scope_changed_after_scoring")

    trace["ranked_ids"] = chunk_ids(ranked)
    top = rag.admit_evidence({**result, "candidates": ranked[:TOP_K]}, rows, query,
                             report_all_missing=True, multi_intent=True)
    trace["top5_status"] = top["status"]
    trace["top5_citation_ids"] = chunk_ids(top["evidence"])
    if top["reason"] not in {"incomplete_source_coverage", "part_code_unverified"}:
        trace["path"] = "rerank_top5"
        return top
    full = rag.admit_evidence({**result, "candidates": ranked}, rows, query,
                              report_all_missing=True, multi_intent=True)
    if full["status"] == "supported":
        ranks = {hit["chunk_id"]: rank for rank, hit in enumerate(ranked, 1)}
        trace.update(path="rerank_fill", fill=[{"chunk_id": cite["chunk_id"], "rank": ranks[cite["chunk_id"]],
                                              "document_id": cite["document_id"], "location": cite["location"]}
                                             for cite in full["evidence"] if ranks[cite["chunk_id"]] > TOP_K])
        return full
    return cpu_search(full["reason"])
