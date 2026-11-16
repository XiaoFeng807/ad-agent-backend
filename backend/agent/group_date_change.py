"""Interpret work-group date changes and validate the proposed scope."""

import json
import re
from dataclasses import replace
from datetime import date

from pydantic import BaseModel, Field, model_validator

from backend.agent.diagnosis import DiagnosisWindows
from backend.agent.llm_provider import ProviderFactory, create_completion
from backend.agent.multi_window_review import _RANGE, parse_review_goal
from backend.agent.request_budget import RequestBudget
from backend.agent.team_tools import TeamBudget


class ReviewDates(DiagnosisWindows):
    @model_validator(mode="after")
    def comparable(self):
        baseline, current = self.baseline, self.current
        days = lambda window: date.fromisoformat(window.end_date) - date.fromisoformat(window.start_date)
        if baseline.end_date >= current.start_date or days(baseline) != days(current):
            raise ValueError("基准窗须早于当前窗，两窗等长且互不重叠")
        return self

    def label(self):
        return (f"基准窗 {self.baseline.start_date} 至 {self.baseline.end_date}，"
                f"当前窗 {self.current.start_date} 至 {self.current.end_date}")


class Clarification(BaseModel, extra="forbid"):
    clarification: str = Field(min_length=1, max_length=400)


class InvalidDateChange(ValueError):
    pass


class InvalidPlanChange(ValueError):
    pass


class PlanReplacement(BaseModel, extra="forbid"):
    remove_plan_id: int = Field(strict=True, gt=0, le=2**63 - 1)
    add_plan_id: int = Field(strict=True, gt=0, le=2**63 - 1)

    def apply(self, goal):
        if self.remove_plan_id not in goal.plan_ids:
            raise InvalidPlanChange("移出的计划须在本轮对比组合内")
        if self.add_plan_id in goal.plan_ids:
            raise InvalidPlanChange("请加入与本轮两条计划均不同的计划")
        return tuple(self.add_plan_id if plan == self.remove_plan_id else plan for plan in goal.plan_ids)

    def revise(self, message, goal, plan_ids):
        def substitute(match):
            return f"#{self.add_plan_id}" if int(match[1]) == self.remove_plan_id else match[0]

        message = re.sub(r"#([0-9]+)", substitute, message)
        if parse_review_goal(message)[1] != replace(goal, plan_ids=plan_ids):
            raise InvalidPlanChange("无法保留本轮复核要求，请发起新一轮")
        return message


def revised_request(message, goal, windows):
    for label, old, new in (("基准", goal.baseline, windows.baseline), ("当前", goal.current, windows.current)):
        match = re.search(rf"{label}(?:窗口|窗)\s*{_RANGE}", message)
        assert match and match.groups() == (old.start_date, old.end_date)
        message = (message[:match.start(1)] + new.start_date + message[match.end(1):match.start(2)]
                   + new.end_date + message[match.end(2):])
    return message


def understand_dates(goal, request, trace_id, *, allow_plans=False):
    instructions = (
        "你是工作群主 Agent。根据旧窗口和群主的改期要求，提出两段绝对日期，等待群主确认。"
        "仅修改两窗天数且没有指定新锚点时，各保留原结束日期，起止日均计入天数。"
        "例如 7 天改 3 天时从各自结束日期往回数 2 天。"
        '日期变更输出 {"baseline":{"start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD"},'
        '"current":{"start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD"}}。'
        '意图含糊、信息不够或要求修改不支持的内容时，输出 {"clarification":"一个具体的问题"}。'
    )
    scope = {"old_dates": {"baseline": goal.baseline.model_dump(), "current": goal.current.model_dump()}}
    if allow_plans:
        scope["old_plan_ids"] = goal.plan_ids
        instructions += (
            "也可根据群主要求提出替换一条对比计划，日期、规则、告警和复核条件均保留。"
            '替换计划时只输出 {"remove_plan_id":102,"add_plan_id":103} 形状的 JSON 对象，数字使用实际 ID。'
            "只能提议改期或换一条计划，两种修改同时出现时先澄清。"
            "移出和加入的计划必须能唯一确定。缺少编号、名称无法从当前信息确定、"
            "或只说换成某计划而未指明移出哪条时，先问清楚，禁止猜测编号。"
        )
    provider = ProviderFactory.create()
    budget = RequestBudget(trace_id, output_limit=400)
    budget.execution = TeamBudget(model_limit=1, seconds=30)
    response = create_completion(
        provider, budget, role="orchestrator", step="interpret_group_change" if allow_plans else "interpret_date_change",
        model=ProviderFactory.model_for(provider), temperature=0, response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps({**scope, "request": request}, ensure_ascii=False)},
        ],
    )
    try:
        raw = json.loads(response.choices[0].message.content)
        if isinstance(raw, dict) and "clarification" in raw:
            return Clarification.model_validate(raw)
        if allow_plans and isinstance(raw, dict) and ({"remove_plan_id", "add_plan_id"} & raw.keys()):
            try:
                return PlanReplacement.model_validate(raw)
            except ValueError as error:
                raise InvalidPlanChange("请提供合法的移出和加入计划编号") from error
        return ReviewDates.model_validate(raw)
    except InvalidPlanChange:
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise InvalidDateChange("请提供两段等长且互不重叠的合法日期（基准窗在前，包含起止日）。") from error
