"""Auto-extracted from multi_agent.py"""

import json, os, re
from datetime import datetime
from backend.agent.agent_factory import AgentFactory
from concurrent.futures import ThreadPoolExecutor, as_completed

class Competition:
    """竞争机制：多个Agent分别回答，Judge评选最佳答案"""

    _SIMPLE_PATTERNS = [
        "查一下", "多少", "今天", "昨天", "是", "有", "吗",
        "查", "看", "展示", "显示", "打开", "看看",
        "hi", "hello", "你好", "在吗", "在不在",
        "数据", "看数据",
    ]

    _COMPLEX_PATTERNS = [
        "分析", "对比", "为什么", "建议", "原因", "如何", "怎样",
        "趋势", "优化", "评价", "评估", "比较", "哪个好",
        "方案", "策略", "预测", "总结", "归纳",
        "详细", "解释", "说明", "区别", "差异",
    ]

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.results_history = []

    def needs_competition(self, query):
        """根据问题复杂度判断是否启动竞争机制"""
        if not query or len(query.strip()) < 3:
            return False
        q = query.strip().lower()
        for kw in self._COMPLEX_PATTERNS:
            if kw in q:
                return True
        question_words = ["什么", "怎么", "哪", "是否", "是不是", "会不会", "能不能"]
        if len(q) > 15:
            for qw in question_words:
                if qw in q:
                    return True
        for kw in self._SIMPLE_PATTERNS:
            if kw in q:
                return False
        return len(q) > 20

    @staticmethod
    def score_answer(answer, query):
        """客观评分：数据密度20%% | 覆盖度25%% | 可操作性35%% | 长度合理20%%"""
        if not answer:
            return 0.0
        scores = {}
        data_score = 0.0
        numbers = re.findall(r"[\d,]+(?:\.\d+)?%?", answer)
        amounts = re.findall(r"[\xA5\$\u20AC]?\s*[\d,]+(?:\.\d+)?", answer)
        dates = re.findall(r"\d{1,2}[\u6708/]\d{1,2}[\u65E5\u53F7]?", answer)
        data_count = len(numbers) + len(amounts) + len(dates)
        if data_count >= 10:
            data_score = 1.0
        elif data_count >= 6:
            data_score = 0.8
        elif data_count >= 3:
            data_score = 0.5
        elif data_count >= 1:
            data_score = 0.3
        scores["data"] = data_score * 20

        coverage_score = 0.0
        query_keywords = set()
        for kw in ["roas", "\u82B1\u8D39", "\u8425\u6536", "\u9500\u552E\u989D", "\u70B9\u51FB",
                     "\u5C55\u793A", "\u8F6C\u5316", "\u8BA1\u5212", "\u8D26\u6237",
                     "\u8D44\u6599", "\u8D5B\u9053", "\u8D5B\u9053",
                     "cpc", "cpm", "ctr", "\u6210\u672C", "\u6548\u679C", "\u56DE\u62A5"]:
            if kw in query.lower():
                query_keywords.add(kw)
        if query_keywords:
            matched = sum(1 for kw in query_keywords if kw in answer.lower())
            coverage_score = matched / len(query_keywords)
        else:
            coverage_score = 0.5
        scores["coverage"] = coverage_score * 25

        operability = 0.0
        if re.search(r"(\u52A0|\u51CF|\u8C03|\u589E)[\u4E0A\u4E0B]?\s*[\d,]+", answer):
            operability += 0.3
        for verb in ["\u6682\u505C", "\u5173\u95ED", "\u5F00\u542F", "\u8C03\u6574",
                     "\u4F18\u5316", "\u589E\u52A0", "\u51CF\u5C11",
                     "\u63D0\u9AD8", "\u964D\u4F4E", "\u6539\u4E3A", "\u8BBE\u7F6E"]:
            if verb in answer:
                operability += 0.3
                break
        if re.search(r"[\d.]+%", answer):
            operability += 0.2
        for time_word in ["\u672C\u5468", "\u4E0B\u5468", "\u4ECA\u65E5", "\u660E\u5929",
                         "\u7ACB\u5373", "\u6682\u65F6", "\u957F\u671F", "\u77ED\u671F"]:
            if time_word in answer:
                operability += 0.2
                break
        if re.search(r"(?:^|\n)\s*[1-9][.\u3001)\uFF0E\u3002]", answer):
            operability += 0.2
        if re.search(r"(?:^|\n)\s*[-*]", answer):
            operability += 0.1
        operability = min(operability, 1.0)
        scores["operability"] = operability * 35

        length = len(answer)
        if 100 <= length <= 3000:
            if 200 <= length <= 1500:
                length_score = 1.0
            elif length < 300:
                length_score = 0.7
            else:
                length_score = 0.8
        elif length < 100:
            length_score = 0.3
        else:
            length_score = 0.5
        scores["length"] = length_score * 20

        total = scores["data"] + scores["coverage"] + scores["operability"] + scores["length"]
        return round(total, 2)

    def run(self, query, user_id, agent_type="analysis", peer_agents=None):
        if not self.needs_competition(query):
            default = AgentFactory.create_agent(agent_type, "conservative", peer_agents)
            result = default.chat([{"role": "user", "content": query}], user_id)
            return {"answer": result, "winner": "direct", "style": "direct",
                    "reason": "\u7B80\u5355\u95EE\u9898\uFF0C\u76F4\u63A5\u56DE\u7B54"}

        competitors = AgentFactory.create_competing_agents(agent_type, peer_agents)
        task_id = f"comp_{hash(query) % 100000}_{id(query)}"

        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            for agent in competitors:
                future = executor.submit(agent.chat, [{"role": "user", "content": query}], user_id)
                futures[future] = agent
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    result = future.result()
                    style = agent.name.split("_")[-1]
                    results.append({"agent": agent.name, "style": style, "result": result})
                except Exception as e:
                    results.append({"agent": agent.name, "style": "error", "result": str(e)})

        fused = self._fuse(query, results)
        self.results_history.append({"task_id": task_id, "query": query[:30], "winner": "fused"})
        return fused

    def _judge(self, query, results):
        """Judge\u51B3\u7B56\uFF1A\u5BA2\u89C2\u8BC4\u520670%% + LLM\u610F\u89C130%%"""
        if not results:
            return {"answer": "\u6240\u6709Agent\u90FD\u672A\u80FD\u751F\u6210\u56DE\u7B54", "winner": "none", "style": "none"}
        if len(results) == 1:
            return {"answer": results[0]["result"], "winner": results[0]["agent"],
                    "style": results[0]["style"], "reason": "only_candidate"}
        scored = []
        for r in results:
            obj_score = self.score_answer(r["result"], query)
            scored.append({**r, "objective_score": obj_score})

        candidates = "\n\n".join([
            f"\u5019\u9009\u4EBA{i+1}({r['style']}):\n{r['result'][:600]}" for i, r in enumerate(scored)
        ])

        judge_prompt = ("\u7528\u6237\u95EE\u9898: " + query + "\n\n"
            + "\u4EE5\u4E0B\u662F\u591A\u4E2AAgent\u7684\u7B54\u6848\uFF0C\u8BF7\u8BC4\u9009\u6700\u4F73\u3002\u6807\u51C6: \u51C6\u786E\u6027>\u5B8C\u6574\u6027>\u53EF\u8BFB\u6027\n\n"
            + candidates
            + "\n\n\u53EA\u8F93\u51FAJSON: {\"winner_style\": \"conservative/aggressive/detail\", \"reason\": \"\u7406\u7531\", \"confidence\": 0-1}")

        llm_choice = None
        llm_confidence = 0.5
        try:
            resp = self.orchestrator.client.chat.completions.create(
                model=self.orchestrator.model,
                messages=[{"role": "user", "content": judge_prompt}],
                temperature=0.1, max_tokens=500
            )
            reply = resp.choices[0].message.content or ""
            import json as _json
            if "`json" in reply:
                reply = reply.split("`json")[1].split("`")[0]
            elif "`" in reply:
                reply = reply.split("`")[1].split("`")[0]
            judge_result = _json.loads(reply.strip())
            llm_style = judge_result.get("winner_style", "")
            llm_confidence = judge_result.get("confidence", 0.5)
            for r in scored:
                if r["style"] == llm_style:
                    llm_choice = r
                    break
        except:
            pass

        if llm_choice:
            for r in scored:
                if r == llm_choice:
                    r["final_score"] = r["objective_score"] * 0.7 + llm_confidence * 30 * 0.3
                else:
                    r["final_score"] = r["objective_score"] * 0.7
        else:
            for r in scored:
                r["final_score"] = r["objective_score"]

        best = max(scored, key=lambda x: x["final_score"])
        return {"answer": best["result"], "winner": best["agent"],
                "style": best["style"],
                "reason": f"\u5BA2\u89C2{best['objective_score']}\u5206 | \u7EFC\u5408{best['final_score']:.1f}\u5206"}

    def _fuse(self, query, results):
        """结果融合：取保守派的数据精度 + 激进派的建议方向 + 详细派的全面展开"""
        if not results:
            return {"answer": "无法生成回答", "winner": "none", "style": "none"}

        valid = [r for r in results if r["style"] != "error"]
        if not valid:
            return {"answer": results[0]["result"], "winner": results[0]["agent"],
                    "style": "error", "reason": "all_agents_failed"}
        if len(valid) == 1:
            return {"answer": valid[0]["result"], "winner": valid[0]["agent"],
                    "style": valid[0]["style"], "reason": "only_one_valid"}

        parts = []
        style_labels = {"conservative": "保守风格", "aggressive": "激进风格", "detail": "详细风格"}
        for r in valid:
            short = r["result"][:800]
            label = style_labels.get(r["style"], r["style"])
            parts.append(f"[{label}]\n{short}")

        fusion_prompt = (
            "你是一个答案融合专家。用户的问题是：" + query + "\n\n"
            "以下是3个不同风格的Agent给出的回答，各有侧重：\n\n"
            + "\n---\n".join(parts) + "\n\n"
            + "请融合它们的优点，生成一个**更好的完整回答**。\n"
            + "规则：\n"
            + "1. 保留保守风格的**数据准确性和数字**\n"
            + "2. 保留激进风格的**结论方向和可操作建议**\n"
            + "3. 保留详细风格的**全面展开和逐条分析**\n"
            + "4. 用清晰的层级结构组织：先总览 -> 再逐项分析 -> 最后建议\n"
+ "5. 不要提到“保守/激进/详细”这些来源标签\n"
            + "6. 保持自然、专业的语气\n"
            + "\n直接输出融合后的回答，不要额外的解释。"
        )

        try:
            resp = self.orchestrator.client.chat.completions.create(
                model=self.orchestrator.model,
                messages=[{"role": "user", "content": fusion_prompt}],
                temperature=0.3, max_tokens=2000
            )
            fused = resp.choices[0].message.content or ""
            if fused.strip():
                fused_score = self.score_answer(fused, query)
                best_single = max(self.score_answer(r["result"], query) for r in valid)
                if fused_score >= best_single * 0.8:
                    return {"answer": fused, "winner": "fused", "style": "fused",
                            "reason": f"融合答案 | 客观分{fused_score}"}
        except:
            pass

        return self._judge(query, valid)


    def stats(self):
        return {"total_competitions": len(self.results_history)}
