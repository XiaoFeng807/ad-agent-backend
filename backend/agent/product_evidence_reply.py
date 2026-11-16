"""SKU clarification and grounded replies for product evidence requests."""

import re
from contextlib import closing
from functools import partial

from backend.agent.request_budget import ExecutionStopped
from backend.database import database, listing_operations
from backend.database.listing_operations import ListingNotFound
from backend.observability import add_trace_step
from backend.rag.product_chat_search import chunk_ids, current_scope, search
from backend.rag.product_evidence import ReleaseConflict

_SCOPE = re.compile(r"^商品资料[：:]\s*(\S+)\s+(\S+)[^\S\r\n]*")
_FORMAT = "请按「商品资料：店铺 站点 SKU 问题」指定商品。"


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


def validate_choices(conn, *, user_id, scope, check_session, choices):
    if not check_session(conn) or listing_operations.product_choices(conn, user_id, *scope) != choices:
        raise ExecutionStopped("product_evidence_scope_changed")


def clarify_sku(user_id, scope, query, *, save_receipt, check_session, trace_id):
    if any(len(value) > 128 for value in scope) or len(query) > 500:
        raise ValueError("invalid product scope or query length")
    with closing(database.get_db(read_only=True)) as conn:
        conn.execute("BEGIN")
        if not check_session(conn):
            raise ListingNotFound
        choices = listing_operations.product_choices(conn, user_id, *scope)
    add_trace_step(trace_id, "product_evidence", "chat_retrieval", evidence={
        "status": "sku_clarification", "admission_reason": "sku_missing", "blocked_at": "before_retrieval",
        "selected_sku": None, "sku_options": choices, "candidate_ids": [], "citation_ids": [],
    })
    if not choices:
        return "当前范围没有可选商品，请检查店铺和站点。"
    options = "\n".join(f"- {' '.join(item['title'].split())} · {item['sku']}" for item in choices)
    reply = f"请选择一个 SKU，并用「商品资料：店铺 站点 SKU 问题」重新提交。\n\n{options}"
    validate = partial(validate_choices, user_id=user_id, scope=scope, check_session=check_session, choices=choices)
    save_receipt((reply, validate, []))
    return reply


def validate_citations(conn, *, user_id, target, check_session, version, ids):
    if not current_scope(conn, user_id, target, check_session, version, ids):
        raise ExecutionStopped("product_evidence_scope_changed")


def reply(message, user_id, *, save_receipt, check_session, trace_id=None, cancelled=lambda: False):
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
                        check_session=check_session, trace_id=trace_id, cancelled=cancelled)
    except ListingNotFound:
        return "商品不存在。"
    except (OSError, ReleaseConflict):
        return "商品资料索引暂不可用。"
    except ValueError:
        return "请检查商品范围和问题长度。"
    if result is None:
        return ""
    if result["status"] == "sku_conflict":
        mentioned = "、".join(result["mentioned_skus"])
        return (f"当前选中的是 {sku}，你问的是 {mentioned}。请确认要查哪个 SKU，"
                "并用「商品资料：店铺 站点 SKU 问题」重新提交。")
    lines = []
    if questions := result.get("ambiguous_questions"):
        lines.append(f"请澄清以下子问题具体指哪项规格：{'、'.join(questions)}。")
    if sections := result.get("missing_sections"):
        checks = result.get("fact_checks", [])
        missing_from_release = checks and all(check["reason"] == "source_not_configured" for check in checks)
        scope = "当前版本" if missing_from_release else "本次检索"
        lines.append(f"证据不足。{scope}缺少以下依据：{'、'.join(sections)}。")
    if not lines and result["status"] != "supported":
        lines.append("证据不足。当前有效资料未提供足以支持所问事实的完整依据。")
    if result["evidence"]:
        lines.append(f"{sku} · {result['version']}")
    receipt = list(lines)
    for citation in result["evidence"]:
        url = f"/api/product-evidence/chunks/{citation['chunk_id']}"
        source = f"{citation['filename']} · {citation['location']} · {citation['section']}"
        reference = f"原文回查（需登录） {url}"
        lines.append(f"{source}\n{citation['quote']}\n{reference}")
        receipt.append(f"{source}\n{reference}")
    if result["evidence"] or "fact_checks" in result:
        citation_ids = chunk_ids(result["evidence"])
        validate = partial(validate_citations, user_id=user_id, target=target, check_session=check_session,
                           version=result["version"], ids=set(citation_ids))
        save_receipt(("\n\n".join(receipt), validate, citation_ids))
    return "\n\n".join(lines)
