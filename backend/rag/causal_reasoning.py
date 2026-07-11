"""因果推理模块 — 当检测到因果类问题时，做因果拆解 + 多路检索 + 因果聚合"""
import re
import logging

logger = logging.getLogger(__name__)

# 因果信号词
CAUSE_PATTERNS = [
    "为什么", "原因", "由于", "因为", "导致", "使得",
    "影响", "所以", "因此", "归因于", "什么原因",
    "为何", "怎么会", "怎么导致",
]

EFFECT_PATTERNS = [
    "下降", "上升", "增长", "减少", "波动", "变化",
    "暴跌", "暴涨", "异常", "超标", "低于", "高于",
    "亏损", "盈利", "优化", "改善",
]

CAUSE_CATEGORIES = {
    "预算类": ["预算", "花费", "消耗", "出价", "日限额"],
    "竞争类": ["CPC", "竞争", "出价", "排名", "展示份额"],
    "素材类": ["素材", "文案", "图片", "视频", "点击率", "CTR"],
    "人群类": ["受众", "定向", "人群", "转化率", "CVR", "精准"],
    "环境类": ["季节", "节假日", "行业", "市场", "竞品"],
    "账户类": ["余额", "状态", "审核", "限制", "暂停"],
}


def is_causal_query(query):
    """判断是否为因果类问题"""
    q = query.strip().lower()
    for pat in CAUSE_PATTERNS:
        if pat in q:
            return True
    for pat in EFFECT_PATTERNS:
        if pat in q:
            return True
    return False


def extract_effect(query):
    """从因果问题中提取核心效果词"""
    effect = query.strip()
    # 去掉因果疑问词
    for pat in CAUSE_PATTERNS:
        effect = effect.replace(pat, " ")
    # 去掉标点
    effect = re.sub(r"[？?，。,.\s]+", " ", effect).strip()
    return effect if effect else query


def expand_causal_queries(query, effect):
    """根据因果结构展开多路查询"""
    queries = [query, effect]

    # 如果效果里有预定义的因果类别关键词，加上对应类别查询
    matched_categories = []
    for category, keywords in CAUSE_CATEGORIES.items():
        for kw in keywords:
            if kw in effect or kw in query:
                matched_categories.append(category)
                # 加一条"原因+关键词"的查询
                queries.append(f"{effect} 原因 {kw}")
                break

    if matched_categories:
        queries.append(f"{effect} 原因分析")
        queries.append(f"{effect} 优化建议")

    return list(set(queries)), matched_categories


def causal_retrieve(rag, query, recall_k=30):
    """因果检索流程：检测 → 拆解 → 多路检索 → 聚合"""
    if not is_causal_query(query):
        # 非因果问题走标准流程
        return rag.recall(query, recall_k=recall_k)

    effect = extract_effect(query)
    sub_queries, matched_cats = expand_causal_queries(query, effect)

    logger.info(f"  [Causal] query='{query[:30]}' effect='{effect[:20]}' cats={matched_cats}")

    # 多路检索
    all_results = []
    seen_content = set()

    for sq in sub_queries:
        results = rag.recall(sq, recall_k=12)
        for content, meta in results:
            if content not in seen_content:
                seen_content.add(content)
                all_results.append((content, meta))

    # 因果排序：原因类 > 数据类 > 普通类
    def causal_sort_key(item):
        content, meta = item
        score = 0
        # 提到具体数据加分
        if re.search(r"[\d.]+%", content):
            score += 3
        if re.search(r"[¥￥$€]?\s*[\d,]+", content):
            score += 2
        # 包含原因关键词加分
        for kw in ["原因", "因为", "由于", "导致"]:
            if kw in content:
                score += 3
                break
        # 包含建议关键词加分
        for kw in ["建议", "优化", "调整", "提升"]:
            if kw in content:
                score += 2
                break
        # 匹配查询中的关键词
        for kw in query:
            if kw in content:
                score += 1
        return -score

    all_results.sort(key=causal_sort_key)

    # 按因果类别分组
    grouped = {
        "现象": [],
        "原因分析": [],
        "数据支撑": [],
        "优化建议": [],
    }

    for content, meta in all_results[:recall_k]:
        has_data = bool(re.search(r"[\d.]+%", content) or re.search(r"[¥￥$€]?\s*[\d,]+", content))
        has_cause = any(kw in content for kw in ["原因", "因为", "由于", "导致", "归因"])
        has_action = any(kw in content for kw in ["建议", "优化", "调整", "提升", "加预算", "暂停"])

        if has_cause:
            grouped["原因分析"].append((content, meta))
        elif has_action:
            grouped["优化建议"].append((content, meta))
        elif has_data:
            grouped["数据支撑"].append((content, meta))
        else:
            grouped["现象"].append((content, meta))

    # 扁平化：原因 → 数据 → 建议 → 现象（优先展示高价值内容）
    final_order = []
    for cat in ["原因分析", "数据支撑", "优化建议", "现象"]:
        final_order.extend(grouped[cat])

    return final_order