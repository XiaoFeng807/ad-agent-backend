"""意图识别模块：先分类再处理，规则不够时LLM兜底"""

from openai import OpenAI
import os, json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# ==================== LLM兜底客户端 ====================
_llm_client = None
_cache = {}  # 简单缓存，避免重复调用

def _get_llm():
    global _llm_client
    if _llm_client is None:
        _llm_client = OpenAI(
            api_key=os.getenv("API_KEY"),
            base_url=os.getenv("BASE_URL")
        )
    return _llm_client

# LLM兜底提示词（极简，只有分类任务）
LLM_CLASSIFY_PROMPT = """判断用户输入属于以下哪个类别，只返回类别名称：
- query_dashboard: 查看仪表盘、总览数据
- query_account: 查看账户余额、预算、广告账户信息
- query_plan: 查看广告计划、计划对比
- query_alert: 查看告警、异常提醒
- query_trend: 查看搜索趋势、热度走势
- query_report: 查看报表、环比同比对比
- query_product: 查看热销产品、选品建议
- query_knowledge: 查询广告行业知识、标准
- sensitive_operation: 充值、加钱、改预算、切换计划等敏感操作
- optimize_suggestion: 获取优化建议、策略
- greeting: 打招呼、闲聊
- other: 以上都不属于

用户输入：{message}

类别："""

def classify_with_llm(message):
    """LLM兜底：规则匹配不到的用LLM判断"""
    try:
        # 检查缓存
        cache_key = message[:50]
        if cache_key in _cache:
            return _cache[cache_key]
        
        client = _get_llm()
        prompt = LLM_CLASSIFY_PROMPT.format(message=message[:200])
        
        resp = client.chat.completions.create(
            model=os.getenv("MODEL", "deepseek-chat"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=20  # 只要一个词，非常快
        )
        
        result = resp.choices[0].message.content.strip().lower()
        # 验证结果是有效意图
        valid_intents = list(INTENT_CATEGORIES.keys()) + ["injection_attempt", "other"]
        for intent in valid_intents:
            if intent in result:
                _cache[cache_key] = intent
                return intent
        return "unknown"
    except Exception:
        return "unknown"

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
        "日预算改成", "存钱", "打钱", "付款", "转钱",
        "预算改成", "改预算", "增加预算", "减少预算", "预算改到",
        "删除计划", "移除计划", "暂停计划", "关闭计划",
        "加余额", "加金额", "充钱",
        "块钱", "加元", "加金",
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


def classify_intent(message, use_llm_fallback=True):
    """分类用户意图：先规则匹配，规则不明确用LLM兜底"""
    msg_lower = message.lower().strip()
    
    # 1. 注入检测优先
    for pattern in INJECTION_PATTERNS:
        if pattern in msg_lower:
            return "injection_attempt"
    
    # 2. 敏感操作优先检测（高优先级）
    sensitive_kws = INTENT_KEYWORDS.get("sensitive_operation", [])
    for kw in sensitive_kws:
        if kw in msg_lower:
            # LLM双确认：只有明确是打招呼/无关才覆盖
            if use_llm_fallback:
                llm_result = classify_with_llm(message)
                # 仅当LLM明确说是打招呼时覆盖，unknown/other都不覆盖（宁愿误拦也不能放行敏感操作）
                if llm_result == "greeting":
                    return llm_result
            return "sensitive_operation"
    
    # 3. 其他关键词匹配
    scores = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        if intent == "sensitive_operation":
            continue  # 已处理
        score = 0
        for kw in keywords:
            if kw in msg_lower:
                score += 1
        if score > 0:
            scores[intent] = score
    
    if scores:
        return max(scores, key=scores.get)
    
    # 4. 规则没有匹配到，用LLM兜底
    if use_llm_fallback:
        return classify_with_llm(message)
    
    return "unknown"


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
