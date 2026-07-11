# -*- coding: utf-8 -*-
"""意图检测模块 — 识别用户意图 + 检测意图漂移"""

import re
from datetime import datetime
from backend.config.config import settings


# ==================== 广告领域的意图分类 ====================

INTENT_CATEGORIES = {
    # 数据查询
    "query_data": {
        "keywords": ["数据", "多少", "趋势", "roas", "cpc", "ctr", "花费", "营收", 
                     "销售额", "点击", "转化", "展示", "曝光", "报表", "报告",
                     "今天", "昨天", "本周", "上周", "本月"],
        "desc": "查询广告投放原始数据"
    },
    # 数据分析
    "analyze": {
        "keywords": ["为什么", "原因", "分析", "对比", "比较", "vs", "差异",
                     "下降", "上升", "上涨", "变化", "异常", "影响"],
        "desc": "分析数据背后的原因"
    },
    # 知识查询
    "query_knowledge": {
        "keywords": ["什么", "怎么", "如何", "多少算", "标准", "定义",
                     "意思", "含义", "请教", "了解", "知道", "介绍"],
        "desc": "查询广告行业知识/标准"
    },
    # 操作建议
    "suggest": {
        "keywords": ["建议", "优化", "提升", "改进", "改善", "推荐",
                     "怎么办", "怎么做", "方案", "策略"],
        "desc": "获取优化建议或方案"
    },
    # 敏感操作
    "sensitive_op": {
        "keywords": ["充值", "加钱", "预算", "暂停", "删除", "关闭", "修改"],
        "desc": "涉及资金或账户状态的敏感操作"
    },
    # 闲聊/非广告
    "chit_chat": {
        "keywords": ["你好", "谢谢", "再见", "你是谁", "你能做什么", "功能"],
        "desc": "问候、感谢、询问功能等非业务对话"
    },
    # 持续跟进（前一个话题的延续）
    "follow_up": {
        "keywords": [],
        "desc": "对前一个话题的继续追问或深入"
    },
}

# 表示"继续上一个话题"的关键词（没有新意图）
FOLLOW_UP_INDICATORS = [
    "那", "然后", "还有", "另外", "除此之外",
    "具体", "详细", "进一步", "更", "再",
    "它", "他们", "这个", "那个", "这些",
    r"^那\w", r"^还有\w", r"^另外",
]

# 表示"开启新话题"的关键词
TOPIC_SHIFT_INDICATORS = [
    "对了", "换个", "换一个", "不说这个", 
    "之前说的", "回到", "刚刚",
    r"^那什么", r"^不过",
]


# ==================== 核心检测函数 ====================

def detect_intent(message: str) -> str:
    """
    基于关键词检测用户意图。
    返回: intent_name (如 "query_data", "analyze", ...)
    """
    msg_lower = message.lower().strip()
    
    # 先检查敏感操作（优先级最高）
    if any(kw in msg_lower for kw in INTENT_CATEGORIES["sensitive_op"]["keywords"]):
        # 但如果是问"ROAS下降对预算的影响"这种，不是真的要操作
        if not any(kw in msg_lower for kw in ["删除", "暂停", "关闭", "充值", "修改预算"]):
            pass  # 继续往下判断
        else:
            return "sensitive_op"
    
    # 按规则匹配
    scores = {}
    for intent_name, config in INTENT_CATEGORIES.items():
        if intent_name == "sensitive_op":
            continue
        score = sum(1 for kw in config["keywords"] if kw in msg_lower)
        if score > 0:
            scores[intent_name] = score
    
    if not scores:
        return "chit_chat"
    
    # 取得分最高的
    best = max(scores, key=scores.get)
    return best


def is_follow_up(message: str) -> bool:
    """检测是否是延续上一个话题"""
    msg = message.strip()
    
    # 太短的消息一般是跟进
    if len(msg) <= 4:
        return True
    
    # 包含跟进指示词
    for indicator in FOLLOW_UP_INDICATORS:
        if re.search(indicator, msg):
            return True
    
    # 没有新意图词 → 大概率是跟进
    intent = detect_intent(message)
    if intent == "chit_chat":
        # 检查是否有话题转移词
        for indicator in TOPIC_SHIFT_INDICATORS:
            if re.search(indicator, msg):
                return False
        # 不是新话题也不是明确转移 → 可能是跟进
        return True
    
    return False


def has_topic_shift(message: str) -> bool:
    """检测用户是否明确切换了话题"""
    msg = message.strip()
    for indicator in TOPIC_SHIFT_INDICATORS:
        if re.search(indicator, msg):
            return True
    return False


# ==================== 意图管理器（带状态） ====================

class IntentManager:
    """
    意图管理器：跟踪用户的意图状态，检测漂移。
    
    用法：
        manager = IntentManager()
        
        # 每来一条消息调用一次
        result = manager.update("今天数据怎么样")
        # {"current": "query_data", "shift": False, "last": None}
        
        result = manager.update("那ROAS呢")
        # {"current": "query_data", "shift": False, "last": "query_data", "type": "follow_up"}
        
        result = manager.update("为什么下降了")
        # {"current": "analyze", "shift": True, "last": "query_data", "type": "drift"}
    """
    
    def __init__(self):
        self.current_intent = None
        self.last_intent = None
        self.last_message = ""
        self.history = []
        self._conversation_topic = None  # 当前会话主题
    
    def update(self, message: str) -> dict:
        """
        更新意图状态。
        返回: {
            "current": "当前意图",
            "last": "上一个意图",
            "is_follow_up": bool,  # 是否延续话题
            "is_shift": bool,      # 是否切换话题
            "change_type": "same" | "shift" | "follow_up" | "drift",
            "message": "原始消息"
        }
        """
        self.last_intent = self.current_intent
        self.last_message = message
        
        current = detect_intent(message)
        follow_up = is_follow_up(message)
        shift = has_topic_shift(message)
        
        # 判断变化类型
        if self.last_intent is None:
            change_type = "first"
        elif shift:
            change_type = "shift"
        elif current != self.last_intent and not follow_up:
            change_type = "drift"  # 无意识漂移（最需要关注的情况）
        elif current != self.last_intent and follow_up:
            change_type = "follow_up_with_new_intent"
        else:
            change_type = "same"
        
        self.current_intent = current
        
        result = {
            "current": current,
            "last": self.last_intent,
            "is_follow_up": follow_up,
            "is_shift": shift,
            "change_type": change_type,
            "message": message[:50],
        }
        
        self.history.append(result)
        if len(self.history) > 20:
            self.history = self.history[-20:]
        
        return result
    
    def get_prompt_section(self) -> str:
        """生成可注入 prompt 的意图信息"""
        if not self.history:
            return ""
        
        last = self.history[-1]
        parts = ["【意图分析】"]
        parts.append(f"当前意图: {last['current']} ({INTENT_CATEGORIES.get(last['current'], {}).get('desc', '未知')})")
        
        if last["change_type"] == "drift":
            parts.append(f"⚠️ 注意: 用户话题从'{last['last']}'漂移到了'{last['current']}'，按新问题回答，不要被旧话题影响")
            parts.append("指令：忘记之前的上下文，聚焦当前问题")
        elif last["change_type"] == "shift":
            parts.append(f"用户明确切换了话题({last['last']} -> {last['current']})，重置上下文")
            parts.append("指令：按全新问题处理")
        elif last["change_type"] == "follow_up_with_new_intent":
            parts.append(f"用户在延续话题但角度变了({last['last']} -> {last['current']})，兼顾新旧意图")
        elif last["change_type"] == "same":
            parts.append(f"延续之前的对话({last['current']})")
        
        return "\n".join(parts)
    
    def reset(self):
        """重置意图状态"""
        self.current_intent = None
        self.last_intent = None
        self.last_message = ""
        self.history = []
        self._conversation_topic = None


# ==================== LLM 意图深度分析 ====================

def llm_analyze_intent(message, llm_client=None):
    """使用 LLM 深度分析用户意图，支持复合意图和重心识别。
    
    返回: {
        "primary": {"intent": "query_data", "focus": "ROAS下降", "confidence": 0.95},
        "secondary": [{"intent": "query_knowledge", "focus": "优化方法", "confidence": 0.6}],
        "summary": "用户想分析ROAS下降原因并了解优化方法"
    }
    """
    if llm_client is None:
        # fallback to keyword detection
        primary = detect_intent(message)
        return {
            "primary": {"intent": primary, "focus": message[:30], "confidence": 0.5},
            "secondary": [],
            "summary": message[:30]
        }
    
    prompt = """分析用户问题的意图。用户是做广告投放的。
    
    可用意图类型:
    - query_data: 查询数据(花费/ROAS/趋势/报表)
    - analyze: 分析原因(为什么/对比/异常)
    - query_knowledge: 查询知识(定义/标准/方法)
    - suggest: 获取建议(优化/提升/方案)
    - sensitive_op: 敏感操作(充值/修改预算/暂停)
    - chit_chat: 闲聊(问候/感谢/问功能)
    
    请分析用户的核心意图(primary)和次要意图(secondary如果有)。
    只返回JSON格式，不要其他文字。
    
    用户说：""" + message + """
    
    JSON格式:
    {
        "primary": {"intent": "意图类型", "focus": "用户关注的核心是什么", "confidence": 0.0-1.0},
        "secondary": [{"intent": "意图类型", "focus": "次要关注点", "confidence": 0.0-1.0}],
        "summary": "一句话总结用户想干什么"
    }"""
    
    try:
        import requests
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
        
        api_key = settings.API_KEY
        base_url = settings.BASE_URL
        model = settings.MODEL
        
        if not api_key:
            primary = detect_intent(message)
            return {"primary": {"intent": primary, "focus": message[:30], "confidence": 0.5}, "secondary": [], "summary": message[:30]}
        
        resp = requests.post(
            base_url + "/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一个意图分析专家，只输出JSON。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 300
            },
            headers={"Authorization": "Bearer " + api_key},
            timeout=10
        )
        if resp.status_code != 200:
            raise Exception("API error: " + str(resp.status_code))
        
        reply = resp.json()["choices"][0]["message"]["content"]
        # 提取JSON
        if "```json" in reply:
            reply = reply.split("```json")[1].split("```")[0]
        elif "```" in reply:
            reply = reply.split("```")[1].split("```")[0]
        
        import json as _json
        result = _json.loads(reply.strip())
        return result
    except Exception as e:
        logger = __import__("logging").getLogger(__name__)
        logger.warning(f"[Intent] LLM分析失败: {e}")
        primary = detect_intent(message)
        return {"primary": {"intent": primary, "focus": message[:30], "confidence": 0.5}, "secondary": [], "summary": message[:30]}


class IntentManagerV2:
    """意图管理器 V2 — 支持 LLM 深度分析和复合意图"""
    
    def __init__(self, use_llm=False, llm_client=None):
        self.use_llm = use_llm
        self.llm_client = llm_client
        self.current_intent = None
        self.last_intent = None
        self.last_message = ""
        self.history = []
        self._keyword_manager = IntentManager()
    
    def update(self, message):
        if self.use_llm:
            result = llm_analyze_intent(message, self.llm_client)
            primary_intent = result.get("primary", {}).get("intent", "chit_chat")
            secondary = result.get("secondary", [])
            summary = result.get("summary", message[:30])
            
            self.last_intent = self.current_intent
            self.current_intent = primary_intent
            self.last_message = message
            
            # 判断变化类型
            if self.last_intent is None:
                change_type = "first"
            elif primary_intent != self.last_intent:
                change_type = "drift"
            else:
                change_type = "same"
            
            result_data = {
                "current": primary_intent,
                "last": self.last_intent,
                "is_follow_up": False,
                "is_shift": False,
                "change_type": change_type,
                "message": message[:50],
                "llm_analysis": result,
                "secondary": secondary,
                "summary": summary,
            }
            self.history.append(result_data)
            return result_data
        else:
            return self._keyword_manager.update(message)
    
    def get_prompt_section(self):
        if self.use_llm and self.history:
            last = self.history[-1]
            llm = last.get("llm_analysis", {})
            primary = llm.get("primary", {})
            secondary = llm.get("secondary", [])
            summary = llm.get("summary", "")
            
            parts = ["【意图深度分析】"]
            parts.append(f"核心意图: {primary.get('intent', '?')} | 关注点: {primary.get('focus', '?')} | 置信度: {primary.get('confidence', 0):.0%}")
            
            if secondary:
                sec_text = []
                for s in secondary:
                    sec_text.append(f"{s.get('intent', '?')}({s.get('focus', '?')})")
                parts.append(f"次要意图: {', '.join(sec_text)}")
            
            parts.append(f"总结: {summary}")
            
            if last.get("change_type") == "drift":
                parts.append("指令: 用户话题已转移，聚焦当前问题")
            
            return "\n".join(parts)
        return self._keyword_manager.get_prompt_section() if hasattr(self, '_keyword_manager') else ""
    
    def reset(self):
        self.current_intent = None
        self.last_intent = None
        self.last_message = ""
        self.history = []
        self._keyword_manager.reset()


# ==================== 全局单例 ====================

_intent_manager = None
_intent_manager_v2 = None


def get_intent_manager():
    global _intent_manager
    if _intent_manager is None:
        _intent_manager = IntentManager()
    return _intent_manager


def get_intent_manager_v2(use_llm=False, llm_client=None):
    """获取 V2 意图管理器（支持 LLM 深度分析）"""
    global _intent_manager_v2
    if _intent_manager_v2 is None:
        _intent_manager_v2 = IntentManagerV2(use_llm, llm_client)
    else:
        _intent_manager_v2.use_llm = use_llm
        if llm_client:
            _intent_manager_v2.llm_client = llm_client
    return _intent_manager_v2


def get_intent_manager():
    global _intent_manager
    if _intent_manager is None:
        _intent_manager = IntentManager()
    return _intent_manager
