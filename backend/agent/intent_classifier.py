"""意图识别模块：先分类再处理，减少无效LLM调用"""

# ==================== 意图分类 ====================
INTENT_CATEGORIES = {
    "query_dashboard": "查看仪表盘/总览数据",
    "query_account": "查看账户详情/余额",
    "query_plan": "查看广告计划",
    "query_alert": "查看告警/异常",
    "query_trend": "查看趋势/搜索热度",
    "query_report": "查看报表/对比数据",
    "query_product": "查看热销产品/选品",
    "query_knowledge": "查询广告知识/行业标准",
    "sensitive_operation": "充值/修改预算/切换计划",
    "optimize_suggestion": "获取优化建议",
    "greeting": "打招呼/闲聊",
    "unknown": "其他"
}

# ==================== 关键词规则 ====================
# 每个意图的关键词列表，命中任意一个即匹配
INTENT_KEYWORDS = {
    "query_dashboard": [
        "仪表盘", "总览", "今天数据", "昨天数据", "整体表现", "总花费",
        "总销售额", "roas", "投产比", "概况", "主页"
    ],
    "query_account": [
        "账户", "余额", "预算", "账户详情", "账号", "还剩", "多少钱",
        "google ads", "meta ads", "tiktok"
    ],
    "query_plan": [
        "计划", "哪个计划", "广告计划", "投放计划", "计划列表",
        "计划表现", "计划对比"
    ],
    "query_alert": [
        "告警", "异常", "警告", "预警", "提醒", "有问题", "出问题",
        "告警中心", "异常计划"
    ],
    "query_trend": [
        "趋势", "搜索趋势", "搜索热度", "热度", "走势", "搜索量",
        "关键词趋势"
    ],
    "query_report": [
        "报表", "对比", "环比", "同比", "上周", "本周与上周",
        "日报", "周报"
    ],
    "query_product": [
        "热销", "爆款", "选品", "什么好卖", "卖什么", "热门产品"
    ],
    "query_knowledge": [
        "什么是", "多少算正常", "行业标准", "参考", "知识",
        "怎么优化", "如何提升", "建议是什么"
    ],
    "sensitive_operation": [
        "充值", "加钱", "加预算", "修改预算", "调预算",
        "日预算改成", "存钱", "打钱", "付款"
    ],
    "optimize_suggestion": [
        "优化建议", "建议", "怎么改", "如何优化", "提升效果",
        "策略", "策略建议"
    ],
    "greeting": [
        "你好", "您好", "hi", "hello", "在吗", "在不在",
        "早上好", "下午好", "晚上好"
    ]
}

# ==================== 注入检测 ====================
INJECTION_PATTERNS = [
    "忽略之前的", "忽略所有", "忽略指令", "无视",
    "你现在是", "你是一个", "扮演", "假装",
    "system prompt", "system_message",
    "不要遵守", "忘记", "reset"
]


def classify_intent(message):
    """分类用户意图：先规则匹配，规则不明确则返回 unknown"""
    msg_lower = message.lower().strip()
    
    # 1. 注入检测优先
    for pattern in INJECTION_PATTERNS:
        if pattern in msg_lower:
            return "injection_attempt"
    
    # 2. 关键词匹配
    scores = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in msg_lower:
                score += 1
        if score > 0:
            scores[intent] = score
    
    if not scores:
        return "unknown"
    
    # 取得分最高的意图
    best_intent = max(scores, key=scores.get)
    return best_intent


def get_intent_description(intent):
    """获取意图的中文描述"""
    return INTENT_CATEGORIES.get(intent, "未知")


def should_skip_llm(intent):
    """某些意图可以直接处理，不需要经过LLM"""
    return intent in [
        "query_dashboard", "query_account", "query_plan",
        "query_alert", "query_trend", "query_product",
        "sensitive_operation", "injection_attempt"
    ]


print(f"共 {len(INTENT_CATEGORIES)} 个意图分类")
print(f"共 {sum(len(v) for v in INTENT_KEYWORDS.values())} 个关键词规则")
