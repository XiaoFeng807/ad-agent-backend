"""多轮任务追踪：跨对话轮次跟踪用户意图"""

from datetime import datetime

# ==================== 任务类型定义 ====================
TASK_TYPES = {
    "viewing_dashboard": "查看仪表盘/总览数据",
    "viewing_account": "查看账户详情/余额",
    "viewing_plan": "查看/对比广告计划",
    "viewing_alert": "查看告警/异常",
    "viewing_trend": "查看趋势/搜索热度",
    "viewing_product": "查看热销产品/选品",
    "getting_advice": "获取优化建议/策略",
    "learning_knowledge": "学习广告知识/行业标准",
    "sensitive_operation": "执行敏感操作",
    "other": "其他"
}

# ==================== 意图→任务映射 ====================
INTENT_TO_TASK = {
    "query_dashboard": "viewing_dashboard",
    "query_account": "viewing_account",
    "query_plan": "viewing_plan",
    "query_alert": "viewing_alert",
    "query_trend": "viewing_trend",
    "query_report": "viewing_dashboard",  # 报表是仪表盘的延伸
    "query_product": "viewing_product",
    "query_knowledge": "learning_knowledge",
    "sensitive_operation": "sensitive_operation",
    "optimize_suggestion": "getting_advice",
    "greeting": "other",
    "unknown": "other"
}

# ==================== 状态判断关键词 ====================
SWITCH_KEYWORDS = ["换个", "转而", "改为", "回到", "看看", "查查"]
CONTINUE_KEYWORDS = ["再", "还", "也", "另外", "继续", "然后"]
COMPLETE_KEYWORDS = ["好了", "结束", "没了", "就这些", "谢谢", "可以了"]
COMPARE_KEYWORDS = ["对比", "比较", "vs", "区别", "不同"]

# ==================== 当前任务状态（内存中） ====================
class TaskTracker:
    """跨轮对话任务追踪器（每个会话独立）"""
    
    def __init__(self):
        self.current_task = None          # 当前任务类型
        self.task_status = "none"         # none/active/completed
        self.turn_count = 0               # 当前任务轮数
        self.query_history = []           # 当前任务的查询记录
        self.last_intent = None           # 上一轮的意图
        self.last_update = None           # 最后更新时间
        self.total_tasks = 0              # 本次会话累计任务数
    
    def update(self, intent, message):
        """根据当前消息的意图更新任务状态"""
        now = datetime.now().strftime("%H:%M")
        task_type = INTENT_TO_TASK.get(intent, "other")
        
        # 判断任务状态
        # 1. 完成信号
        for kw in COMPLETE_KEYWORDS:
            if kw in message.lower():
                old_task = self.current_task
                self.current_task = None
                self.task_status = "completed"
                self.query_history = []
                self.turn_count = 0
                return "completed", old_task, None
        
        # 2. 同一任务的延续
        if self.current_task == task_type and self.task_status == "active":
            self.turn_count += 1
            self.query_history.append(message[:50])
            self.task_status = "continued"
            self.last_update = now
            return "continued", self.current_task, self.turn_count
        
        # 3. 新任务（或任务切换）
        old_task = self.current_task
        self.current_task = task_type
        self.task_status = "active"
        self.turn_count = 1
        self.query_history = [message[:50]]
        self.last_intent = intent
        self.last_update = now
        self.total_tasks += 1
        
        if old_task and old_task != task_type:
            return "switched", task_type, old_task
        else:
            return "started", task_type, None
    
    def get_context(self):
        """获取当前任务上下文摘要（注入到提示词用）"""
        if not self.current_task or self.task_status == "completed":
            return ""
        
        task_name = TASK_TYPES.get(self.current_task, "其他")
        context = f"【当前任务】用户正在{task_name}"
        if self.turn_count > 1:
            context += f"，已连续问了{self.turn_count}轮"
        if self.query_history:
            last_q = self.query_history[-1] if len(self.query_history) <= 3 else "..."
            context += f"，上一句相关: {last_q}"
        return context
    
    def reset(self):
        """重置追踪器"""
        self.current_task = None
        self.task_status = "none"
        self.turn_count = 0
        self.query_history = []
        self.last_intent = None
        self.last_update = None

# 全局任务追踪实例（每个用户独立）
_task_trackers = {}

def get_tracker():
    """获取当前会话的任务追踪器"""
    import threading
    tid = threading.get_ident()
    if tid not in _task_trackers:
        _task_trackers[tid] = TaskTracker()
    return _task_trackers[tid]

def track_message(intent, message):
    """处理一条消息，更新任务追踪"""
    tracker = get_tracker()
    return tracker.update(intent, message)

def get_task_context():
    """获取任务上下文（供提示词注入）"""
    tracker = get_tracker()
    return tracker.get_context()


