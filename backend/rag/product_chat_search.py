"""Product chat retrieval, GPU admission, and citation revalidation."""

import re
from collections import defaultdict
from contextlib import closing
from math import isfinite
from threading import Lock
from time import perf_counter

from backend.config.config import settings
from backend.database import database, listing_operations, product_documents
from backend.observability import add_trace_step, record_late_product_rerank
from backend.rag import product_candidate_admission
from backend.rag import product_evidence as rag
from backend.rag import product_scope
from backend.rag.product_rerank_queue import RerankQueue

TOP_K = 5
_R4_OPTIONS = {"mode": "vector", "expansion": "both"}
_CPU_OPTIONS = {**_R4_OPTIONS, "attribute_expansion": "both"}
_queue = None
_queue_lock = Lock()
_TOKEN = product_scope.TOKEN
_MODEL_FAMILY = re.compile(r"([A-Z][A-Z0-9._-]*[._-][A-Z]*)[0-9]+[A-Z]*", re.ASCII)
_SCOPE_PREFIX = re.compile(
    rf"(?P<command>商品资料[：:]\s*|店铺[：:]?\s*)?(?P<shop>{_TOKEN.pattern})"
    rf"(?P<shop_sep>\s*[/／]\s*|\s*(?:店(?:铺)?的?|的)\s*|\s+)"
    rf"(?:站点[：:]?\s*)?(?P<site>{_TOKEN.pattern})"
    rf"(?P<site_sep>\s*[/／]\s*|\s*站(?:点)?的?\s*|\s+)"
    r"[`'\"“]?\s*$", re.I | re.ASCII,
)
_SITE_PREFIX = re.compile(rf"(?P<site>{_TOKEN.pattern})\s*(?P<label>站(?:点)?)?\s*的?\s*[`'\"“]?\s*$", re.I | re.ASCII)
_SCOPE_JOIN = re.compile(r"(?:以及|和|与|及|、|\b(?:and|vs)\b\.?)\s*$", re.I | re.ASCII)
_MIXED_IDENTIFIER = re.compile(r"[A-Z].*[0-9]|[0-9].*[A-Z]", re.ASCII)


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


def _scope_at_end(prefix, readable_coords, bound_coords, model_identified, *, joined):
    if match := _SCOPE_PREFIX.search(prefix):
        coords = (match["shop"].casefold(), match["site"].casefold())
        shop_sep, site_sep = match["shop_sep"].strip(), match["site_sep"].strip()
        # Bare whitespace also occurs in descriptions. Resolve it only against known coordinates.
        marked = match["command"] or (shop_sep.startswith("店") and site_sep.startswith("站"))
        slash_path = shop_sep in ("/", "／") and site_sep in ("/", "／")
        if marked or coords in readable_coords or (model_identified and (slash_path or coords == bound_coords)):
            return coords, match.start()
    if model_identified and (match := _SITE_PREFIX.search(prefix)):
        coords = (bound_coords[0], match["site"].casefold())
        if match["label"] or (coords in readable_coords and (joined or _SCOPE_JOIN.search(prefix, 0, match.start()))):
            return coords, match.start()
    return None


def _mentioned_scopes(prefix, readable_coords, bound_coords, model_identified):
    # One SKU can qualify a list such as “CA 站和 US 站的 SKU”.
    joined = False
    while mention := _scope_at_end(prefix, readable_coords, bound_coords, model_identified, joined=joined):
        coords, start = mention
        yield coords
        join = _SCOPE_JOIN.search(prefix, 0, start)
        if not join:
            break
        prefix = prefix[:join.start()] + " "
        joined = True


def _conflicting_skus(conn, user_id, scope, query):
    catalog = listing_operations.readable_product_catalog(conn, user_id)
    normalized = product_scope.normalize(query)
    conflict = False
    for pairs, sku_start in product_scope.explicit_coordinates(normalized, catalog, scope[:2]):
        if len(pairs) != 1:
            raise listing_operations.ListingNotFound
        pair, = pairs
        available = {candidate[2] for candidate in catalog if candidate[:2] == pair}
        qualified = product_scope.leading_skus(normalized[sku_start:], {candidate[2] for candidate in catalog})
        if not qualified and normalized[:sku_start].rstrip().endswith("/"):
            raise listing_operations.ListingNotFound
        if not (qualified or {scope[2]}) & available:
            raise listing_operations.ListingNotFound
        conflict |= pair != scope[:2]

    query = product_scope.normalize(query, fold_case=False)
    bound_coords, bound_sku = tuple(value.casefold() for value in scope[:2]), scope[2].upper()
    family = _MODEL_FAMILY.fullmatch(bound_sku)
    labels = list(product_scope.LABELED_MODEL.finditer(query))
    labeled_models = {label.start("value"): label.start() for label in labels}
    label_words = {offset for label in labels for offset in range(label.start(), label.start("value"))}
    tokens = [match for match in _TOKEN.finditer(query) if match.start() not in label_words]
    readable = defaultdict(set)
    for shop, site, sku in catalog:
        readable[sku.upper()].add((shop.casefold(), site.casefold(), sku))
    # An unlisted model needs an explicit label or the bound SKU's complete model family.
    for match in tokens:
        token = match[0]
        model = token.upper()
        start = match.start()
        scopes = readable[model]
        readable_coords = {candidate[:2] for candidate in scopes}
        explicitly_labeled = model != "SKU" and start in labeled_models
        coords = list(_mentioned_scopes(query[:labeled_models.get(start, start)], readable_coords, bound_coords,
                                        scopes or explicitly_labeled or _MIXED_IDENTIFIER.search(model)))
        same_spelling_variant = token != scope[2] and (*bound_coords, token) in scopes
        if coords:
            for pair in coords:
                if pair not in readable_coords:
                    resolved = product_scope.catalog_coordinates(catalog, *pair)
                    if len(resolved) != 1:
                        raise listing_operations.ListingNotFound
                    pair, = ((shop.casefold(), site.casefold()) for shop, site in resolved)
                    if pair not in readable_coords:
                        raise listing_operations.ListingNotFound
                conflict |= pair != bound_coords or model != bound_sku or same_spelling_variant
            continue
        if model == bound_sku and not same_spelling_variant:
            continue
        model_family = _MODEL_FAMILY.fullmatch(model)
        same_family = family and model_family and family[1] == model_family[1]
        if explicitly_labeled and not scopes:
            raise listing_operations.ListingNotFound
        conflict |= bool(scopes or same_family)
    return conflict or bool(product_scope.sku_mentions(
        normalized, {candidate[2] for candidate in catalog if candidate[2].upper() != bound_sku},
    ))


def search(user_id, target, query, *, check_session, trace_id=None, cancelled=lambda: False, budget=None):
    query = query.strip()
    enabled = settings.PRODUCT_RERANK_ENABLED and user_id in settings.PRODUCT_RERANK_USER_IDS
    r4 = settings.PRODUCT_R4_CPU_ENABLED
    cpu_path = "r4_cpu" if r4 else "default"
    trace = {"path": "rerank" if enabled else cpu_path, "eligible": enabled, "scored_chunks": 0}
    candidate_selection = product_candidate_admission.enabled()

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
        if candidate_selection and result.get("unconfigured_evidence") and not cancelled():
            result = product_candidate_admission.select(
                result, query, budget,
                still_current=lambda sources: current(user_id, target, check_session, result, sources),
            )
        return result

    try:
        if cancelled():
            trace["status"] = "cancelled"
            return None
        scope = rag._scope(target)
        if not 1 <= len(query) <= 500:
            raise ValueError("invalid query length")
        trace["blocked_at"] = "before_retrieval"
        with closing(database.get_db(read_only=True)) as conn:
            conn.execute("BEGIN")
            if not check_session(conn) or listing_operations.scope_error(conn, user_id, *scope):
                raise listing_operations.ListingNotFound
            trace["selected_sku"] = scope[2]
            conflicts = _conflicting_skus(conn, user_id, scope, query)
        if conflicts:
            trace.update(status="sku_conflict", admission_reason="sku_conflict",
                         candidate_ids=[], citation_ids=[])
            return {"status": "sku_conflict"}
        del trace["blocked_at"]
        if enabled:
            result = _gray_search(user_id, target, query, check_session, trace_id, cancelled, trace, cpu_search,
                                  candidate_selection=candidate_selection)
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
        if "admission" in result:
            trace["admission"] = result["admission"]
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
    retrieve_started = perf_counter()
    with closing(database.get_db(read_only=True)) as conn:
        conn.execute("BEGIN")
        if not check_session(conn) or listing_operations.scope_error(conn, user_id, *rag._scope(target)):
            trace["blocked_at"] = "before_retrieval"
            raise listing_operations.ListingNotFound
        details = trace[trace_key] = dict(options)
        trace["candidate_ids"] = []
        try:
            result, rows = rag.ProductEvidence().retrieve(conn, user_id, target, query, **options)
        finally:
            trace["retrieval_ms"] = (perf_counter() - retrieve_started) * 1000
            details["ms"] = trace["retrieval_ms"]
    details["candidate_ids"] = trace["candidate_ids"] = chunk_ids(result["candidates"])
    return result, rows


def _gray_search(user_id, target, query, check_session, trace_id, cancelled, trace, cpu_search, *, candidate_selection):
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
    if candidate_selection and top.get("unconfigured_evidence"):
        return cpu_search("unconfigured_release")
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
