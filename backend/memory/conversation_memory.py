"""对话记忆向量库 v2 — 实体+状态提取 + LLM元认知 + 遗忘机制"""
import os, json, logging, hashlib, re
from datetime import datetime, timedelta
import chromadb
from chromadb.config import Settings
from backend.rag.chinese_embedding import BGEEmbedding
from backend.memory.importance import score_turn, importance_label

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "conv_memory")
CONV_TTL_DAYS = 30  # 对话记忆保留30天


def _get_client():
    return chromadb.PersistentClient(path=DB_PATH, settings=Settings(anonymized_telemetry=False))


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

def store_conversation(user_id, user_msg, ai_reply):
    """对话结束后，向量化存储 + 实体提取"""
    if not user_msg or not user_msg.strip():
        return

    client = _get_client()
    collection = _get_collection(client)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    text = f"用户: {user_msg.strip()[:300]}\nAI: {ai_reply.strip()[:500]}"
    did = hashlib.md5(f"{user_id}_{ts}".encode()).hexdigest()[:12]

    # 提取实体 + 状态
    entities, metrics, decisions = _extract_entities(text)

    imp_score = score_turn(user_msg, ai_reply)
    meta = {
        "importance": imp_score,
        "importance_label": importance_label(imp_score),
        "user_id": str(user_id),
        "timestamp": ts,
        "type": "conversation",
        "user_msg": user_msg.strip()[:80],
        "entities": ";".join(entities) if entities else "",
        "metrics": json.dumps(metrics, ensure_ascii=False) if metrics else "",
        "has_decision": "1" if decisions else "0",
        "decisions": json.dumps(decisions, ensure_ascii=False) if decisions else "",
        "ttl": (datetime.now() + timedelta(days=CONV_TTL_DAYS)).strftime("%Y-%m-%d"),
    }

    try:
        collection.get(ids=[did])
        return
    except:
        pass

    collection.add(documents=[text], metadatas=[meta], ids=[did])
    if entities or metrics:
        logger.info(f"  [ConvMem] stored: {user_msg[:30]}... entities={entities[:2]}")


# ===== 遗忘机制 =====

def _purge_expired():
    """删除超过TTL的旧对话"""
    client = _get_client()
    collection = _get_collection(client)
    count = collection.count()
    if count == 0:
        return 0

    cutoff = datetime.now().strftime("%Y-%m-%d")
    try:
        all_data = collection.get()
        ids_to_delete = []
        for i, meta in enumerate(all_data.get("metadatas", [])):
            if meta and meta.get("ttl", "") < cutoff:
                ids_to_delete.append(all_data["ids"][i])
        if ids_to_delete:
            collection.delete(ids=ids_to_delete)
            return len(ids_to_delete)
    except:
        pass
    return 0


# ===== LLM元认知检索 =====

def search_conversation(query, user_id, top_k=3):
    """LLM按需检索历史对话，附带实体信息"""
    client = _get_client()
    collection = _get_collection(client)

    # 每次检索前先清理过期数据
    purged = _purge_expired()
    if purged:
        logger.info(f"  [ConvMem] purged {purged} expired conversations")

    count = collection.count()
    if count == 0:
        return []

    try:
        n = min(top_k * 3, count)
        results = collection.query(
            query_texts=[query],
            n_results=n,
            where={"user_id": str(user_id)}
        )
    except:
        results = collection.query(
            query_texts=[query],
            n_results=min(top_k * 3, count)
        )

    docs = results.get("documents", [[]])[0] or []
    metas = results.get("metadatas", [[]])[0] or []
    dists = results.get("distances", [[]])[0] or []

    out = []
    for i in range(min(len(docs), top_k)):
        meta = metas[i] if i < len(metas) else {}
        # 把实体信息拼回结果
        extra = ""
        if meta.get("entities"):
            extra += f"\n提及实体: {meta['entities']}"
        if meta.get("metrics"):
            try:
                m = json.loads(meta["metrics"])
                extra += f"\n指标: {', '.join([f'{k}={v}' for k, v in m.items()])}"
            except:
                pass
        if meta.get("has_decision") == "1":
            extra += "\n【包含决策】"

        out.append({
            "content": docs[i] + extra,
            "time": meta.get("timestamp", ""),
            "relevance": f"{1 - dists[i]:.2f}" if i < len(dists) else "?",
            "entities": meta.get("entities", ""),
        })
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


def search_conversation_tool(query, user_id):
    """LLM可调用的工具"""
    results = search_conversation(query, user_id, top_k=3)
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
