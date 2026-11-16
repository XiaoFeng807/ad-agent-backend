"""对话记忆向量库 v2 — 实体+状态提取 + LLM元认知 + 遗忘机制"""
import os, json, logging, hashlib, re
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import chromadb
from chromadb.config import Settings
from backend.rag.chinese_embedding import BGEEmbedding
from backend.memory.importance import score_turn, importance_label
from backend.database import chat_groups, chat_storage, mysql_diagnosis
from backend.memory.memory_manager import memory_root

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "conv_memory")
CONV_TTL_DAYS = 30  # 对话记忆保留30天
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def _today():
    return datetime.now(SHANGHAI).date()


def _ttl_valid(value, *, today=None):
    """A Shanghai expiry date is exclusive; malformed/missing dates fail closed."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        return False
    try:
        expires = date.fromisoformat(value)
    except ValueError:
        return False
    return expires > (today if today is not None else _today())


class MemoryIsolationError(RuntimeError):
    """Only a fixed, non-sensitive code may leave the retrieval boundary."""

    code = "conversation_memory_filter_failed"


def _message_id(value):
    return type(value) is int and value > 0


def _document(user_msg, ai_reply):
    return f"用户: {user_msg.strip()[:300]}\nAI: {ai_reply.strip()[:500]}"


def _committed_messages(conn, user_id, user_message_id, assistant_message_id):
    if not (_message_id(user_message_id) and _message_id(assistant_message_id)
            and user_message_id < assistant_message_id):
        return None
    return chat_storage.committed_turn(conn, user_id, user_message_id, assistant_message_id)


def _committed_document(user_id, user_message_id, assistant_message_id):
    with closing(chat_storage.connect(read_only=True)) as conn:
        messages = _committed_messages(conn, user_id, user_message_id, assistant_message_id)
    return _document(*messages) if messages else None


def _content_digest(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()


def _get_client(root=None):
    root = root if root is not None else memory_root()
    path = str(root / "conv_memory") if root is not None else DB_PATH
    return chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))


def _get_collection(client):
    return client.get_or_create_collection(
        name="conversations",
        embedding_function=BGEEmbedding()
    )


# ===== 实体+状态提取（规则法，不额外调LLM） =====

_AD_PLANS = [
    "Google搜索-品牌词", "Google搜索-通用词",
    "Meta-再营销", "Meta-新客拓展",
    "TikTok-爆款视频", "TikTok-达人合作",
    "部门A-搜索广告", "部门B-社媒广告",
    "测试计划A", "测试计划B", "个人推广-搜索",
]

_METRICS = ["ROAS", "CPC", "CPM", "CTR", "CVR", "CPA", "ROI"]

_ACTIONS = ["暂停", "开启", "关闭", "调整", "增加", "减少", "优化", "提升", "降低", "加预算", "减预算"]


def _extract_entities(text):
    """从对话文本中提取实体 + 状态"""
    entities = []
    metrics = {}
    decisions = []

    for plan in _AD_PLANS:
        if plan in text:
            # 判断该计划的状态
            status = "提及"
            for action in _ACTIONS:
                if action in text:
                    status = action
                    break
            entities.append(f"{plan}({status})")

    for m in _METRICS:
        # 找 "ROAS 2.47" 或 "ROAS=2.47" 或 "ROAS下降"
        pattern = re.compile(rf"{m}\s*[=:：]?\s*([\d.]+%?)", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            metrics[m] = match.group(1)
        elif m in text:
            # 可能提到"ROAS下降"但没有具体数字
            for direction in ["上升", "下降", "增长", "减少", "波动", "异常"]:
                if direction in text:
                    metrics[m] = direction
                    break

    for action in _ACTIONS:
        if action in text:
            # 提取: "建议暂停Meta-新客拓展" → {"action": "暂停", "target": "Meta-新客拓展"}
            for plan in _AD_PLANS:
                if plan in text:
                    decisions.append({"action": action, "target": plan, "time": datetime.now().strftime("%m-%d")})

    return entities, metrics, decisions


# ===== 主存储逻辑 =====

def store_conversation(user_id, user_msg, ai_reply, *, user_message_id, assistant_message_id,
                       conversation_id=None):
    """Index a committed turn. Its user message ID is the idempotency key."""
    if not user_msg or not user_msg.strip():
        return
    if conversation_id is not None and not (_message_id(user_message_id) and _message_id(assistant_message_id)
                                            and user_message_id < assistant_message_id):
        raise ValueError("conversation_memory_uncommitted_turn")

    text = _document(user_msg, ai_reply)
    root = None
    source = {}
    mysql = mysql_diagnosis.chat_enabled()
    if conversation_id is not None or mysql:
        with closing(chat_storage.connect(read_only=True)) as conn:
            if mysql:
                root = mysql_diagnosis.chat_memory_root(conn)
            if conversation_id is None:
                original = _committed_messages(conn, user_id, user_message_id, assistant_message_id)
            else:
                original = chat_groups.committed_turn(conn, user_id, conversation_id, user_message_id,
                                                      assistant_message_id)
        if original != (user_msg, ai_reply):
            raise ValueError("conversation_memory_uncommitted_turn")
        source = {"storage_namespace": root.name if root is not None else "sqlite",
                  "content_sha256": _content_digest(original)}
        if conversation_id is not None:
            source["conversation_id"] = conversation_id
    else:
        with closing(chat_storage.connect(read_only=True)) as conn:
            original = _committed_messages(conn, user_id, user_message_id, assistant_message_id)
        if user_id is None or original != (user_msg, ai_reply):
            raise ValueError("conversation_memory_uncommitted_turn")

    client = _get_client(root) if root is not None else _get_client()
    collection = _get_collection(client)
    now = datetime.now(SHANGHAI)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    namespace = root.name if root is not None else "sqlite"
    scope = f":{conversation_id}" if conversation_id is not None else ""
    did = hashlib.sha256(f"{namespace}-turn:{user_id}{scope}:{user_message_id}".encode()).hexdigest()

    # 提取实体 + 状态
    entities, metrics, decisions = _extract_entities(text)

    imp_score = score_turn(user_msg, ai_reply)
    meta = {
        **source,
        "importance": imp_score,
        "importance_label": importance_label(imp_score),
        "user_id": str(user_id),
        "schema_version": 3 if conversation_id is not None else 2,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "timestamp": ts,
        "type": "conversation",
        "user_msg": user_msg.strip()[:80],
        "entities": ";".join(entities) if entities else "",
        "metrics": json.dumps(metrics, ensure_ascii=False) if metrics else "",
        "has_decision": "1" if decisions else "0",
        "decisions": json.dumps(decisions, ensure_ascii=False) if decisions else "",
        "ttl": (now.date() + timedelta(days=CONV_TTL_DAYS)).isoformat(),
    }

    existing = collection.get(ids=[did])
    if did in existing.get("ids", []):
        previous = (existing.get("metadatas") or [{}])[0] or {}
        if ((existing.get("documents") or [None])[0] != text
                or previous.get("user_id") != str(user_id)
                or previous.get("user_message_id") != user_message_id
                or previous.get("assistant_message_id") != assistant_message_id
                or any(previous.get(key) != value for key, value in source.items())):
            raise ValueError("conversation_memory_turn_conflict")
        return

    collection.add(documents=[text], metadatas=[meta], ids=[did])
    if entities or metrics:
        logger.info("conversation_memory_stored")


# ===== 遗忘机制 =====

def _purge_expired(*, root=None):
    """删除超过TTL的旧对话"""
    client = _get_client(root) if root is not None else _get_client()
    collection = _get_collection(client)
    count = collection.count()
    if count == 0:
        return 0

    today = _today()
    try:
        all_data = collection.get()
        ids_to_delete = []
        for i, meta in enumerate(all_data.get("metadatas", [])):
            if not isinstance(meta, dict) or not _ttl_valid(meta.get("ttl"), today=today):
                ids_to_delete.append(all_data["ids"][i])
        if ids_to_delete:
            collection.delete(ids=ids_to_delete)
            return len(ids_to_delete)
    except:
        pass
    return 0


# ===== LLM元认知检索 =====

def search_conversation(query, user_id, top_k=3, *, through_message_id=None, snapshot=None, conversation_id=None):
    """Use the accepted message boundary and recheck every vector hit against SQL."""
    if (user_id is None or not _message_id(through_message_id)
            or (mysql_diagnosis.chat_enabled() and snapshot is None and conversation_id is None)):
        logger.warning("conversation_memory_boundary_missing")
        return []
    try:
        root = snapshot.memory_root if snapshot is not None else None
        if conversation_id is not None and mysql_diagnosis.chat_enabled():
            with closing(chat_storage.connect(read_only=True)) as conn:
                root = mysql_diagnosis.chat_memory_root(conn)
        version = 3 if conversation_id is not None else 2
        namespace = root.name if root is not None else "sqlite"
        client = _get_client(root) if root is not None else _get_client()
        collection = _get_collection(client)
        count = collection.count()
        if count == 0:
            return []
        n = min(top_k * 3, count)
        filters = [
            {"user_id": str(user_id)}, {"schema_version": version},
            {"user_message_id": {"$lt": through_message_id}},
            {"assistant_message_id": {"$lt": through_message_id}},
        ]
        if root is not None or conversation_id is not None:
            filters.append({"storage_namespace": namespace})
        if conversation_id is not None:
            filters.append({"conversation_id": conversation_id})
        results = collection.query(query_texts=[query], n_results=n, where={"$and": filters})
    except Exception:
        logger.warning(MemoryIsolationError.code)
        raise MemoryIsolationError(MemoryIsolationError.code) from None

    docs = results.get("documents", [[]])[0] or []
    metas = results.get("metadatas", [[]])[0] or []
    dists = results.get("distances", [[]])[0] or []

    out = []
    for i in range(len(docs)):
        meta = metas[i] if i < len(metas) else {}
        # Do not trust a backend to have applied where. Recheck before formatting,
        # then verify the document against the owner's committed SQL rows.
        if not isinstance(meta, dict) or meta.get("user_id") != str(user_id):
            logger.warning("conversation_memory_owner_rejected")
            continue
        question_id, answer_id = meta.get("user_message_id"), meta.get("assistant_message_id")
        if (meta.get("schema_version") != version or not _message_id(question_id) or not _message_id(answer_id)
                or not question_id < answer_id < through_message_id
                or not _ttl_valid(meta.get("ttl"))):
            logger.warning("conversation_memory_boundary_rejected")
            continue
        if ((root is not None or conversation_id is not None) and meta.get("storage_namespace") != namespace
                or conversation_id is not None and meta.get("conversation_id") != conversation_id):
            logger.warning("conversation_memory_namespace_rejected")
            continue
        try:
            if conversation_id is not None:
                with closing(chat_storage.connect(read_only=True)) as conn:
                    original = chat_groups.committed_turn(conn, user_id, conversation_id, question_id, answer_id)
                verified = (_document(*original)
                            if original and meta.get("content_sha256") == _content_digest(original) else None)
            elif snapshot is None:
                verified = _committed_document(user_id, question_id, answer_id)
            else:
                original = _committed_messages(snapshot.connection, user_id, question_id, answer_id)
                verified = (_document(*original)
                            if original and meta.get("content_sha256") == _content_digest(original) else None)
        except Exception:
            logger.warning("conversation_memory_source_unavailable")
            continue
        if verified is None or docs[i] != verified:
            logger.warning("conversation_memory_receipt_rejected")
            continue
        # Recompute derived fields from verified text; unverified metadata must
        # never become prompt content even when its owner field is forged.
        entities, metrics, decisions = _extract_entities(verified)
        extra = "\n提及实体: " + ";".join(entities) if entities else ""
        if metrics:
            extra += "\n指标: " + ", ".join(f"{key}={value}" for key, value in metrics.items())
        if decisions:
            extra += "\n【包含决策】"

        out.append({
            "content": docs[i] + extra,
            "time": "",  # Unverified free-text metadata is not model input.
            "relevance": f"{1 - dists[i]:.2f}" if i < len(dists) else "?",
            "entities": ";".join(entities),
        })
        if len(out) >= top_k:
            break
    return out


def format_search_results(results):
    """格式化检索结果"""
    if not results:
        return "未找到相关历史对话。你可以根据当前已有信息直接回答。"
    parts = ["【历史对话检索结果】"]
    for i, r in enumerate(results):
        parts.append(f"---\n记录{i+1} ({r['time']}, 相关度: {r['relevance']}):\n{r['content']}")
    parts.append("\n注意：如果检索结果与用户问题无关，请忽略并直接回答。")
    return "\n".join(parts)


def search_conversation_tool(query, user_id, *, through_message_id=None, snapshot=None, conversation_id=None):
    """LLM可调用的工具"""
    results = search_conversation(query, user_id, top_k=3, through_message_id=through_message_id,
                                  snapshot=snapshot, **({"conversation_id": conversation_id} if conversation_id else {}))
    return format_search_results(results)


# ===== Tool定义（含元认知提示） =====

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "search_conversation_memory",
        "description": (
            "检索历史对话记录。当遇到以下情况时应该调用此工具：\n"
            "1. 用户问「之前/上次/刚才/昨天/上次聊到」的事情\n"
            "2. 用户提到的某个计划、指标你不确定当前状态\n"
            "3. 你对用户的问题没有足够信息，需要查看历史记录\n"
            "4. 用户说「还记得吗」「我之前说过」「像上次一样」\n\n"
            "不要假设你知道历史记录的内容。不确定时就调用此工具。\n"
            "参数query要包含关键实体名（计划名/指标名）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要检索的关键信息，如：「ROAS下降 Meta-再营销」「暂停的计划」「预算调整」"
                }
            },
            "required": ["query"]
        }
    }
}
