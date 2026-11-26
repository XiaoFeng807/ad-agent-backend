"""SKU clarification and grounded replies for product evidence requests."""

import re
from contextlib import closing
from functools import partial

from backend.agent.request_budget import ExecutionStopped
from backend.database import database, listing_operations, product_receipt_history
from backend.database.listing_operations import ListingNotFound
from backend.observability import add_trace_step
from backend.rag.product_scope import clarification_choices
from backend.rag.product_chat_search import chunk_ids, current_scope, search
from backend.rag.product_evidence import ReleaseConflict

_SCOPE = re.compile(r"^商品资料[：:]\s*(\S+)\s+(\S+)[^\S\r\n]*")
_FORMAT = product_receipt_history.SCOPE_FORMAT
_SCOPE_UNAVAILABLE = "当前账号无法访问该范围的商品资料，请核对店铺、站点和 SKU。"


def requested(message):
    return message.startswith(("商品资料：", "商品资料:"))


def parse_request(message):
    match = _SCOPE.match(message)
    if not match:
        return None
    shop_id, marketplace_id = match.groups()
    tail = message[match.end():]
    if not tail or tail.startswith(("\r", "\n")):
        return shop_id, marketplace_id, None, tail.strip()
    fields = tail.split(maxsplit=1)
    if len(fields) == 2:
        return shop_id, marketplace_id, *fields
    return None


def validate_choices(conn, *, user_id, scope, query, check_session, choices):
    if not check_session(conn) or clarification_choices(
        listing_operations.readable_product_catalog(conn, user_id), scope, query,
    ) != choices:
        raise ExecutionStopped("product_evidence_scope_changed")


def clarify_sku(user_id, scope, query, *, save_receipt, check_session, trace_id):
    if any(len(value) > 128 for value in scope) or len(query) > 500:
        raise ValueError("invalid product scope or query length")
    with closing(database.get_db(read_only=True)) as conn:
        conn.execute("BEGIN")
        if not check_session(conn):
            raise ListingNotFound
        catalog = listing_operations.readable_product_catalog(conn, user_id)
        scope_available = any(candidate[:2] == scope for candidate in catalog)
        choices = clarification_choices(catalog, scope, query)
    add_trace_step(trace_id, "product_evidence", "chat_retrieval", evidence={
        "status": "sku_clarification", "admission_reason": "sku_missing", "blocked_at": "before_retrieval",
        "selected_sku": None, "sku_option_count": len(choices), "candidate_ids": [], "citation_ids": [],
    })
    if not scope_available:
        return _SCOPE_UNAVAILABLE
    if choices:
        validate = partial(validate_choices, user_id=user_id, scope=scope, query=query,
                           check_session=check_session, choices=choices)
        save_receipt((product_receipt_history.choice_receipt(scope, choices), validate, []))
    return product_receipt_history.render_choices(choices)


def validate_citations(conn, *, user_id, target, check_session, version, ids):
    if not current_scope(conn, user_id, target, check_session, version, ids):
        raise ExecutionStopped("product_evidence_scope_changed")


def reply(message, user_id, *, save_receipt, check_session, trace_id=None, cancelled=lambda: False, budget=None):
    if not user_id:
        return "请登录后查询商品资料。"
    parsed = parse_request(message)
    if not parsed:
        return _FORMAT
    shop_id, marketplace_id, sku, query = parsed
    target = {"shop_id": shop_id, "marketplace_id": marketplace_id, "sku": sku}
    try:
        if sku is None:
            return clarify_sku(user_id, (shop_id, marketplace_id), query, save_receipt=save_receipt,
                               check_session=check_session, trace_id=trace_id)
        result = search(user_id, target, query,
                        check_session=check_session, trace_id=trace_id, cancelled=cancelled, budget=budget)
    except ListingNotFound:
        return _SCOPE_UNAVAILABLE
    except (OSError, ReleaseConflict):
        return "商品资料索引暂不可用。"
    except ValueError:
        return "请检查商品范围和问题长度。"
    if result is None:
        return ""
    if result["status"] == "sku_conflict":
        return "问题涉及不同商品范围。请确认一个店铺、站点和 SKU，并用「商品资料：店铺 站点 SKU 问题」重新提交。"
    lines = []
    labels = {}
    highlights = {}
    selected_excerpts = result.get("evidence_highlights", {})
    conflict_notices = {}
    for check in result.get("fact_checks", []):
        if result.get("admission") != "candidate_selection" or not check["claims"]:
            continue
        question = " ".join(check["question"].split())
        conflict = check["status"] == "conflict"
        for index, claim in enumerate(check["claims"], 1):
            source = claim["source"]
            key = source["document_id"], source["location"]
            if conflict:
                notice = f"当前资料对「{question}」存在冲突。以下原文分别记录各方说法。"
                if index == 1:
                    conflict_notices.setdefault(key, []).append(notice)
                label = f"「{question}」的冲突来源 {index}"
            else:
                label = f"「{question}」的依据"
            labels.setdefault(key, []).append(label)
            if excerpt := selected_excerpts.get((check.get("id"), *key)):
                highlights.setdefault(key, []).append(f"{label}摘录：{excerpt}")
            for condition in claim["requires"]:
                condition_key = condition["document_id"], condition["location"]
                labels.setdefault(condition_key, []).append(f"{label}所引用的适用条件")
    deferred_missing = None
    if questions := result.get("ambiguous_questions"):
        lines.append(f"请澄清以下子问题具体指哪项规格：{'、'.join(questions)}。")
    if sections := result.get("missing_sections"):
        checks = result.get("fact_checks", [])
        missing_from_release = checks and all(check["reason"] == "source_not_configured" for check in checks)
        scope = "当前版本" if missing_from_release else "本次检索"
        notice = f"证据不足。{scope}缺少以下依据：{'、'.join(sections)}。"
        if result.get("admission") == "candidate_selection" and result["evidence"]:
            deferred_missing = notice
        else:
            lines.append(notice)
    if not lines and not deferred_missing and result["status"] not in ("supported", "conflict"):
        lines.append("证据不足。当前有效资料未提供足以支持所问事实的完整依据。")
    if result["evidence"]:
        lines.append(f"{sku} · {result['version']}")
    receipt = list(lines)
    for citation in result["evidence"]:
        url = f"/api/product-evidence/chunks/{citation['chunk_id']}"
        source = f"{citation['filename']} · {citation['location']} · {citation['section']}"
        reference = f"原文回查（需登录） {url}"
        key = citation["document_id"], citation["location"]
        lines.extend(conflict_notices.get(key, []))
        attributions = list(dict.fromkeys(labels.get(key, [])))
        context = "\n".join(dict.fromkeys([*attributions, *highlights.get(key, ())]))
        lines.append("\n".join(filter(None, (context, source, citation["quote"], reference))))
        # Keep the attribution in the source block so history can project it with the link.
        receipt_source = " · ".join([source, *attributions])
        receipt.append(f"{receipt_source}\n{reference}")
    if conflict_notices:
        receipt.append("本轮商品资料显示存在冲突，已分别记录各方引用。")
    if deferred_missing:
        lines.append(deferred_missing)
        receipt.append(deferred_missing)
    if result["evidence"] or "fact_checks" in result:
        citation_ids = chunk_ids(result["evidence"])
        validate = partial(validate_citations, user_id=user_id, target=target, check_session=check_session,
                           version=result["version"], ids=set(citation_ids))
        save_receipt(("\n\n".join(receipt), validate, citation_ids))
    return "\n\n".join(lines)
