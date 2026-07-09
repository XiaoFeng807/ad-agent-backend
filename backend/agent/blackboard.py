"""共享工作区 Blackboard v1 — 多 Agent 协作的核心"""

import json
from datetime import datetime


class Blackboard:
    """共享工作区，所有 Agent 共享的"黑板" """

    def __init__(self):
        self._data = {}
        self._log = []

    def set(self, key, value, agent_name='system'):
        """写入数据到工作区"""
        self._data[key] = {
            "value": value,
            "agent": agent_name,
            "timestamp": datetime.now().isoformat()
        }
        self._log.append({
            "agent": agent_name,
            "action": "写入 " + key,
            "detail": str(value)[:100],
            "timestamp": datetime.now().isoformat()
        })
        return True

    def get(self, key, default=None):
        """从工作区读取数据"""
        entry = self._data.get(key)
        return entry['value'] if entry else default

    def has(self, key):
        return key in self._data

    def keys(self):
        return list(self._data.keys())

    def summary(self):
        """生成给 LLM 看的上下文摘要"""
        if not self._data:
            return ""
        parts = []
        for key, entry in self._data.items():
            val = entry['value']
            agent = entry['agent']
            if isinstance(val, (dict, list)):
                val_str = json.dumps(val, ensure_ascii=False)[:200]
            else:
                val_str = str(val)[:200]
            parts.append(f"[{agent}]{key}: {val_str}")
        return "\n".join(parts)

    def summary_for_prompt(self):
        """生成可直接注入 prompt 的上下文"""
        s = self.summary()
        if s:
            return "[共享工作区当前状态]\n" + s + "\n[/共享工作区]"
        return ""

    def get_trace(self):
        """获取完整的决策追溯"""
        if not self._log:
            return "暂无操作记录"
        parts = []
        for entry in self._log:
            ts = entry["timestamp"][:19]
            parts.append(f"[{ts}] {entry["agent"]}: {entry["action"]}")
        return "\n".join(parts[-20:])

    def clear(self):
        """清空工作区（新任务时调用）"""
        self._data.clear()
        self._log = []

    def merge(self, other):
        """合并另一个工作区"""
        for key, entry in other._data.items():
            if key not in self._data:
                self._data[key] = entry
        self._log.extend(other._log)

    def to_dict(self):
        """导出为字典"""
        return {
            "data": {k: v["value"] for k, v in self._data.items()},
            "log": self._log[-20:]
        }