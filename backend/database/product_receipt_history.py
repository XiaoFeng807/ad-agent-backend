"""Project saved product source receipts through the current catalog access rules."""

import json
import re

from backend.database import listing_operations, mysql_diagnosis, product_documents


UNAVAILABLE = "部分或全部商品资料引用已失效，无法回查。"
SCOPE_FORMAT = "请按「商品资料：店铺 站点 SKU 问题」指定商品。"
_CHOICE_LEAD = "请选择一个 SKU，并用「商品资料：店铺 站点 SKU 问题」重新提交。"
_CHOICE_RECEIPT = "product-evidence-sku-choices-v1\n"


def render_choices(choices):
    if not choices:
        return SCOPE_FORMAT
    options = "\n".join(f"- {' '.join(item['title'].split())} · {item['sku']}" for item in choices)
    return f"{_CHOICE_LEAD}\n\n{options}"


def choice_receipt(scope, choices):
    return _CHOICE_RECEIPT + json.dumps([scope, choices], ensure_ascii=False)


def filter_choices(conn, user_id, messages, *, role_key="role"):
    for message in messages:
        if message[role_key] != "assistant" or not message["content"]:
            continue
        content = message["content"]
        if content.startswith(_CHOICE_RECEIPT):
            try:
                scope, saved = json.loads(content.removeprefix(_CHOICE_RECEIPT))
                if (not isinstance(scope, list) or len(scope) != 2 or not all(isinstance(value, str) for value in scope)
                        or not isinstance(saved, list)):
                    raise ValueError("Invalid stored product choice receipt")
            except (TypeError, ValueError):
                message["content"] = SCOPE_FORMAT
            else:
                current = listing_operations.product_choices(conn, user_id, *scope)
                message["content"] = render_choices([item for item in saved if item in current])
        elif content.startswith(_CHOICE_LEAD + "\n\n- "):
            # Legacy option receipts have no persisted scope or selection basis.
            message["content"] = SCOPE_FORMAT

# The stored template has a SKU/version heading and one or more two-line
# source blocks. The standalone link line is also the frontend's button marker.
_LINK = "\n原文回查（需登录） /api/product-evidence/chunks/"
_SOURCE_PREFIX = r"[^\n]+ · (?:page:(?:[0-9]+|M[0-9]+)|row:[0-9]+|faq:[^\n]+) · [^\n]+" + re.escape(_LINK)
_CHUNK_ID = r"[a-f0-9]{64}"
_BLOCK_END = r"(?=\n\n|\Z)"
_SOURCE = _SOURCE_PREFIX + _CHUNK_ID + _BLOCK_END
_SOURCE_BLOCK = re.compile(_SOURCE_PREFIX + rf"(?P<chunk_id>{_CHUNK_ID})" + _BLOCK_END)
# A missing-evidence paragraph can also contain the SKU/version separator.
# Keep the match from crossing the start of the actual heading.
_HEADING_START = r"\S+ · "
_HEADING = rf"{_HEADING_START}(?:(?!\n\n{_HEADING_START})[\s\S])+?"
_RECEIPT = re.compile(
    rf"(?:^|(?<=\n\n))(?P<header>{_HEADING})\n\n(?P<sources>{_SOURCE}(?:\n\n{_SOURCE})*)"
)


def _stored_heading(content, match, sources, stored):
    # A legal version can contain another heading-shaped paragraph. Match the
    # full catalog heading at the end of the text preceding the first source.
    headers = {stored.get(source["chunk_id"], "") for source in sources} - {""}
    end = match.end("header")
    for header in sorted(headers, key=len, reverse=True):
        start = end - len(header)
        if start >= 0 and (start == 0 or content[start - 2:start] == "\n\n") and content.startswith(header, start, end):
            return start, header
    return match.start(), match["header"]


def _visible_receipt(header, kept, visible):
    blocks = []
    if any(header == visible[source["chunk_id"]] for source in kept):
        blocks.append(header)
    blocks.extend(source[0] for source in kept)
    blocks.append(UNAVAILABLE)
    return "\n\n".join(blocks)


def filter_receipts(conn, user_id, messages, *, role_key="role"):
    """Filter assistant receipts in place on response copies from one read snapshot."""
    if mysql_diagnosis.is_mysql(conn):
        return
    filter_choices(conn, user_id, messages, role_key=role_key)
    receipts = []
    for message in messages:
        if message[role_key] != "assistant" or not message["content"]:
            continue
        content = message["content"]
        if _LINK in content:
            matches = [(match, list(_SOURCE_BLOCK.finditer(match["sources"]))) for match in _RECEIPT.finditer(content)]
            if matches:
                receipts.append((message, matches))
    if not receipts:
        return

    chunk_ids = {source["chunk_id"] for _, matches in receipts for _, sources in matches for source in sources}
    visible = product_documents.visible_receipt_headers(conn, user_id, chunk_ids)
    stale = chunk_ids - visible.keys()
    if not stale:
        return
    stored = {**visible, **product_documents.stored_receipt_headers(conn, stale)}
    for message, matches in receipts:
        content = message["content"]
        for match, sources in reversed(matches):
            kept = [source for source in sources if source["chunk_id"] in visible]
            if len(kept) == len(sources):
                continue
            start, header = _stored_heading(content, match, sources, stored)
            content = content[:start] + _visible_receipt(header, kept, visible) + content[match.end():]
        message["content"] = content
