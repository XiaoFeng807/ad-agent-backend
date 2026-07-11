# -*- coding: utf-8 -*-
"""元认知模块 — 自我反思 + 自我纠正 + 置信度评估"""

import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class MetaCognition:
    """
    元认知模块：让 Agent 具备"思考自己如何思考"的能力。
    核心流程:
      预检查(pre_check) -> 执行(Agent工作) -> 后检查(post_check) -> 修正(correct)
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client
        self.check_history = []

    def pre_check(self, query, available_tools):
        """回答前检查：工具和上下文是否足够"""
        issues = []
        q = query.lower()
        needs_data = any(kw in q for kw in ["数据", "多少", "趋势", "roas", "cpc", "ctr", "花费", "营收", "收入", "销售额", "点击", "转化", "效果", "对比", "分析"])
        has_data_tool = any("data" in t.lower() or "query" in t.lower() for t in available_tools)
        if needs_data and not has_data_tool:
            issues.append("缺少数据查询工具，无法获取原始数据")
        needs_compare = any(kw in q for kw in ["对比", "比较", "vs", "哪个好", "差异"])
        if needs_compare and "compare" not in str(available_tools).lower():
            issues.append("缺少对比分析工具")
        needs_multi = (needs_data and any(kw in q for kw in ["为什么", "原因", "建议", "如何"])) or needs_compare
        if needs_multi:
            issues.append("该问题需要多步协作：先查数据 -> 再分析 -> 最后给出结论")
        self.check_history.append({"type": "pre_check", "query": query[:50], "issues": issues, "timestamp": datetime.now().isoformat()})
        return issues

    def post_check(self, query, data_sources, draft_answer):
        """回答后检查：审核回答质量"""
        issues = []
        if not draft_answer or len(draft_answer.strip()) < 10:
            issues.append("回答过短，可能没有有效内容")
            return issues
        fuzzy = ["可能", "大概", "也许", "或许", "应该", "估计"]
        has_fuzzy = any(w in draft_answer for w in fuzzy)
        has_data = any(kw in draft_answer for kw in ["元", "%", "ROAS", "花费", "营收", "销售额", "点击", "转化"])
        if has_fuzzy and not has_data:
            issues.append("存在模糊表述但缺少数据支撑")
        qkw = set(re.findall(r"[\u4e00-\u9fff\w]+", query.lower()))
        akw = set(re.findall(r"[\u4e00-\u9fff\w]+", draft_answer.lower()))
        overlap = qkw & akw
        if len(overlap) < len(qkw) * 0.3:
            issues.append("回答可能偏离了用户问题")
        mentions_data = any(kw in draft_answer for kw in ["数据", "根据", "显示", "趋势", "分析"])
        has_nums = bool(re.search(r"\d+\.?\d*", draft_answer))
        if mentions_data and not has_nums:
            issues.append("提到了数据但未给出具体数值")
        if self._has_contradiction(draft_answer):
            issues.append("回答中存在前后矛盾")
        conf = self._estimate_confidence(draft_answer, data_sources)
        if conf < 0.5:
            issues.append("回答置信度偏低，建议补充更多数据")
        elif conf < 0.7:
            issues.append("回答置信度中等，可在回答中注明不确定性")
        self.check_history.append({"type": "post_check", "issues": issues, "confidence": conf, "timestamp": datetime.now().isoformat()})
        return issues

    def _estimate_confidence(self, answer, data_sources):
        """估算回答的置信度 0-1"""
        c = 0.5
        nums = re.findall(r"\d+\.?\d*", answer)
        if len(nums) >= 3:
            c += 0.15
        elif len(nums) >= 1:
            c += 0.1
        if re.search(r"[今昨明后]天|本周|上周|本月|[0-9]{1,2}月[0-9]{1,2}日", answer):
            c += 0.1
        if len(data_sources) >= 2:
            c += 0.1
        for w in ["可能", "大概", "也许", "不确定"]:
            if w in answer:
                c -= 0.1
                break
        if any(kw in answer for kw in ["建议", "推荐", "结论", "综上", "总结"]):
            c += 0.1
        return max(0.0, min(1.0, c))

    def _has_contradiction(self, text):
        """检测文本中是否存在前后矛盾"""
        patterns = [(r"上升.*?下降|下降.*?上升"), (r"增加.*?减少|减少.*?增加"), (r"好.*?差|差.*?好"), (r"高.*?低|低.*?高")]
        for p in patterns:
            m = re.search(p, text, re.DOTALL)
            if m:
                before = text[:m.start()]
                if not re.search(r"虽然|尽管|但是|不过|然而", before):
                    return True
        return False

    def reflect(self, query, data_sources, draft_answer, llm_call_fn=None):
        """完整的反思+修正循环"""
        issues = self.post_check(query, data_sources, draft_answer)
        log = {"pre_checks": self.pre_check(query, data_sources), "post_issues": issues, "correction_rounds": 0, "final_confidence": 0.5}
        if not issues:
            log["final_confidence"] = self._estimate_confidence(draft_answer, data_sources)
            return {"answer": draft_answer, "reflection": log, "confidence": log["final_confidence"]}
        current = draft_answer
        for rn in range(2):
            log["correction_rounds"] = rn + 1
            if llm_call_fn:
                prompt = "你之前的回答存在以下问题，请修正：\n"
                for issue in issues:
                    prompt += "- " + issue + "\n"
                prompt += "\n你之前的回答：\n" + current + "\n\n请基于以上反馈重新回答。"
                current = llm_call_fn(prompt)
            issues = self.post_check(query, data_sources, current)
            if not issues:
                break
        log["final_confidence"] = self._estimate_confidence(current, data_sources)
        log["post_issues"] = issues
        return {"answer": current, "reflection": log, "confidence": log["final_confidence"]}

    def get_reflection_summary(self):
        """生成反思过程的文字摘要"""
        if not self.check_history:
            return ""
        recent = self.check_history[-3:]
        lines = ["【元认知自检记录】"]
        for entry in recent:
            etype = "预检查" if entry["type"] == "pre_check" else "后检查"
            iss = entry.get("issues", [])
            if iss:
                lines.append("  [" + etype + "] 发现问题: " + "; ".join(iss))
            else:
                conf = entry.get("confidence", 1.0)
                lines.append("  [" + etype + "] 通过 (置信度: " + str(int(conf * 100)) + "%)")
        return "\n".join(lines)

    def summary(self):
        """获取元认知统计"""
        total = len(self.check_history)
        issues_count = sum(1 for e in self.check_history if e.get("issues"))
        rate = "0%"
        if total > 0:
            rate = str(int((total - issues_count) / total * 100)) + "%"
        return {"total_checks": total, "issues_found": issues_count, "pass_rate": rate}


_meta = None


def get_metacognition(llm_client=None):
    """获取全局元认知实例"""
    global _meta
    if _meta is None:
        _meta = MetaCognition(llm_client)
    return _meta
