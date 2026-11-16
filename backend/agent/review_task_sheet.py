"""Model-facing task sheets for an independent multi-window review."""

from backend.agent.conditional_detail import DETAIL, METRICS
from backend.agent.multi_window_review import RULE
from backend.agent.team_contracts import TeamPlan


class ReviewTaskSheet:
    def __init__(self, handoff, *, required_rule=None):
        self.refs = handoff.get("artifact_refs", [])
        self.required_rule = required_rule

    def options(self):
        if self.refs:
            return {"stage": "append", "reference_field": "artifact_refs", "artifact_refs": self.refs}
        return {"stage": "same_batch", "reference_field": "dependencies",
                "reference_type": "取证任务在同一 tasks 数组中声明的 task_id"}

    def tool(self):
        schema = TeamPlan.model_json_schema()
        task = schema["$defs"]["TeamTask"]["properties"]
        for field in ("direct_refs", "input_refs", "artifact_refs" if not self.refs else "dependencies"):
            del task[field]
        task["capabilities"]["items"] = {"type": "string", "enum": [METRICS, RULE, DETAIL]}
        if self.refs:
            description = "追加独立复核。复核任务只用 get_plan_detail，并在 artifact_refs 中复制全部已核验回执引用。"
            ref = schema["$defs"]["ArtifactRef"]["properties"]
            for field in ("task_id", "artifact_id"):
                ref[field] = {"type": "string", "enum": list(dict.fromkeys(row[field] for row in self.refs))}
            task["artifact_refs"].update(
                description="必填。按 independent_review.artifact_refs 原样复制全部 task_id 与 artifact_id 配对。",
                minItems=len(self.refs), maxItems=len(self.refs),
            )
            del task["artifact_refs"]["default"]
            schema["$defs"]["TeamTask"]["required"].append("artifact_refs")
        else:
            description = "分派取证，可同批安排一个独立复核任务。计划号、日期和规则写在 objective 中。"
            if self.required_rule:
                description += f"本任务单须单列仅用 search_knowledge 核验 {self.required_rule} 的取证任务。"
            del schema["$defs"]["ArtifactRef"]
            task["dependencies"]["description"] = (
                "复核任务列出本任务单中全部取证任务的 task_id，运行时等待并交付回执。取证任务通常省略此字段。"
            )
        return {"type": "function", "function": {
            "name": "delegate_tasks", "description": description, "parameters": schema,
        }}

    def prepare(self, plan):
        """Validate model references before normalizing them for the request-local task graph."""
        errors = []

        def reject(field, reason):
            errors.append({"field": field, "reason": reason})

        if not isinstance(plan, dict):
            return plan, errors
        if "comparison_goal" in plan:
            reject("comparison_goal", "本轮范围已由用户请求确定，省略此字段。")
        tasks = plan.get("tasks")
        if not isinstance(tasks, list):
            return plan, errors
        if self.required_rule and not any(isinstance(task, dict) and task.get("capabilities") == [RULE] for task in tasks):
            reject("tasks", f"缺少规则取证任务。添加一个只使用 search_knowledge 的任务，核验 {self.required_rule}。")
        batch_ids = [task["task_id"] for task in tasks if isinstance(task, dict)
                     and isinstance(task.get("task_id"), str)]
        review_indices = [index for index, task in enumerate(tasks) if isinstance(task, dict)
                          and isinstance(task.get("capabilities"), list) and DETAIL in task["capabilities"]]
        if len(review_indices) > 1:
            reject("tasks", "整个比较只安排一个独立复核任务。")
        evidence_ids = {task["task_id"] for index, task in enumerate(tasks)
                        if isinstance(task, dict) and isinstance(task.get("task_id"), str)
                        and index not in review_indices}
        available = {(row["task_id"], row["artifact_id"]) for row in self.refs}
        normalized = []
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                normalized.append(task)
                continue
            prefix = f"tasks[{index}]"
            task = task.copy()
            normalized.append(task)
            capabilities = task.get("capabilities")
            reviewing = isinstance(capabilities, list) and DETAIL in capabilities
            for field in ("direct_refs", "input_refs"):
                if task.pop(field, []) != []:
                    reject(f"{prefix}.{field}", "省略此字段，计划号和规则名写入 objective。")
            if reviewing and capabilities != [DETAIL]:
                reject(f"{prefix}.capabilities", "将取证与复核拆开，复核任务只使用 get_plan_detail。")
            if self.refs:
                if "dependencies" in task:
                    reject(f"{prefix}.dependencies", "追加时仅使用 artifact_refs，运行时会建立依赖。")
                refs = task.get("artifact_refs", [])
                if not isinstance(refs, list):
                    reject(f"{prefix}.artifact_refs", "应为当前轮核验引用的数组。")
                    continue
                pairs = []
                for ref_index, ref in enumerate(refs):
                    fields = ("task_id", "artifact_id")
                    if not isinstance(ref, dict) or set(ref) != set(fields) or any(
                        not isinstance(ref[key], str) for key in fields
                    ) or (ref["task_id"], ref["artifact_id"]) not in available:
                        reject(f"{prefix}.artifact_refs[{ref_index}]",
                               "引用必须匹配当前群当前轮已核验的 task_id 和 artifact_id 配对。")
                    else:
                        pairs.append((ref["task_id"], ref["artifact_id"]))
                if reviewing and (set(pairs) != available or len(pairs) != len(refs)):
                    reject(f"{prefix}.artifact_refs", "复核任务须复制当前可用的全部核验引用。")
                task["dependencies"] = list(dict.fromkeys(task_id for task_id, _ in pairs))
            else:
                if task.pop("artifact_refs", []) != []:
                    reject(f"{prefix}.artifact_refs", "同批回执尚未生成，省略此字段并用 dependencies 填取证任务 ID。")
                deps = task.get("dependencies", [])
                if not isinstance(deps, list):
                    reject(f"{prefix}.dependencies", "应为同批取证任务 ID 的数组。")
                    continue
                for dep_index, dep in enumerate(deps):
                    if dep not in batch_ids or dep == task.get("task_id"):
                        reject(f"{prefix}.dependencies[{dep_index}]", "填写同批其他任务的 task_id。")
                        errors[-1]["options"] = [value for value in batch_ids if value != task.get("task_id")]
                if reviewing:
                    if not deps or not all(isinstance(dep, str) for dep in deps) or set(deps) != evidence_ids:
                        reject(f"{prefix}.dependencies", "列出本任务单中全部取证任务的 task_id。")
                    task["input_refs"] = deps
        return {**plan, "tasks": normalized}, errors
