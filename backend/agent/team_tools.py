"""Server-owned read-only tools and shared delegation call budget."""

import json
import sqlite3
from contextlib import nullcontext
from copy import deepcopy
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from backend.agent.diagnosis import DateWindow, valid_plan_metrics
from backend.observability import add_trace_step, trace_tool_call
from backend.rag.rule_reference import canonical_rule_query, rule_query_scope


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
PlanId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class PlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_id: PlanId


class WindowArgs(DateWindow):
    model_config = ConfigDict(extra="forbid", strict=True)


class MetricsArgs(WindowArgs):
    plan_id: PlanId


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: Text


class ListingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sku: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    shop_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    marketplace_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None


class ListingSalesArgs(ListingArgs):
    baseline_start: str | None = None
    baseline_end: str | None = None
    current_start: str | None = None
    current_end: str | None = None


class OlistSalesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


SCHEMAS = {"get_plan_detail": PlanArgs, "get_daily_report_by_date": WindowArgs,
           "get_plan_metrics_by_date": MetricsArgs, "search_knowledge": SearchArgs}
LISTING_READS = {
    "get_listing_sales": "Read the server-scoped sales window or comparison windows for a test SKU, including daily coverage and units.",
    "get_listing_current": "Read the current Listing version and five bullet points for this test SKU.",
    "get_listing_facts": "Read versioned product facts and approved English wording options for this test SKU.",
}
DELEGATION_SCHEMAS = {**SCHEMAS, **dict.fromkeys(LISTING_READS, ListingArgs),
                      "get_listing_sales": ListingSalesArgs, "get_olist_product_sales": OlistSalesArgs}
TOOL_ERRORS = {"temporary_tool_failure", "plan_not_found", "plan_history_not_mapped",
               "plan_history_not_configured", "source_version_mismatch", "plan_history_unavailable", "olist_product_not_found"}
PERMANENT_READ_GAPS = {"plan_not_found", "plan_history_not_mapped", "plan_history_not_configured",
                       "source_version_mismatch", "plan_history_missing_dates", "daily_reports_missing"}


def readonly_functions():
    from backend.tools import tools

    return {name: getattr(tools, name) for name in SCHEMAS}


def plan_scope_error(plan_id, user_id):
    from backend.tools.tools import owns_plan

    try:
        return None if owns_plan(plan_id, user_id) else "read_scope_changed"
    except sqlite3.Error:
        return "read_scope_unverified"


def tool_arguments(name, arguments):
    return DELEGATION_SCHEMAS[name].model_validate(arguments).model_dump(exclude_unset=name in LISTING_READS)


def read_key(name, args):
    if name == "search_knowledge":
        args = {"query": canonical_rule_query(args["query"])}
    return name, tuple(sorted(args.items()))


def receipt_error(name, result, args, user_id):
    if not isinstance(result, dict):
        return "invalid_tool_receipt"
    if result.get("success") is False:
        code = result.get("error_code")
        if isinstance(code, str) and code in TOOL_ERRORS:
            return code
        return "plan_not_found" if name == "get_plan_detail" else "tool_reported_failure"
    if name == "get_plan_detail":
        if (type(result.get("id")) is not int or result["id"] != args["plan_id"]
                or type(result.get("user_id")) is not int or result["user_id"] != user_id):
            return "invalid_plan_receipt"
    elif name == "get_plan_metrics_by_date":
        if not valid_plan_metrics(result, args):
            return "invalid_metrics_receipt"
        if not result["coverage"]["complete"]:
            return "plan_history_missing_dates"
    elif name == "get_daily_report_by_date":
        rows = result.get("reports")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or type(row.get("user_id")) is not int or row["user_id"] != user_id
            or not isinstance(row.get("report_date"), str)
            or not args["start_date"] <= row["report_date"] <= args["end_date"] for row in rows
        ):
            return "invalid_daily_report_receipt"
        if not rows:
            return "daily_reports_missing"
    elif name == "search_knowledge":
        if (not isinstance(result.get("context"), str) or not isinstance(result.get("sources"), list)
                or not isinstance(result.get("evidence"), list)):
            return "invalid_knowledge_receipt"
        if any(not isinstance(hit, dict) or not all(isinstance(hit.get(key), str) and hit[key]
                   for key in ("chunk_id", "content")) for hit in result["evidence"]):
            return "invalid_knowledge_receipt"
        if not result["evidence"]:
            return "knowledge_missing"
    return None


class TeamBudget:
    def __init__(self, *, tool_limit=12, model_limit=12, seconds=90):
        if (any(type(limit) is not int or limit < 0 for limit in (tool_limit, model_limit))
                or not isfinite(seconds) or seconds <= 0):
            raise ValueError("invalid_budget")
        self._remaining = {"tool": tool_limit, "model": model_limit}
        self._deadline = monotonic() + seconds
        self._stop_reason = None
        self._lock = Lock()

    def cancel(self, code="run_cancelled"):
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = code

    def cancel_after(self, commit, code, *, committed=bool):
        """Hold admission until the database has decided whether the run can proceed."""
        with self._lock:
            result = commit()
            if committed(result):
                self._stop_reason = code
            return result

    def reserve(self, kind, *, keep=0):
        with self._lock:
            if (self._stop_reason or monotonic() >= self._deadline
                    or kind not in self._remaining or self._remaining[kind] <= keep):
                return False
            self._remaining[kind] -= 1
            return True

    def active(self):
        with self._lock:
            return self._stop_reason is None and monotonic() < self._deadline

    def admit(self, accept):
        """Linearize work and receipt admission against cancellation and the deadline."""
        with self._lock:
            return self._stop_reason is None and monotonic() < self._deadline and accept()

    def seconds_left(self):
        return max(0, self._deadline - monotonic())

    def stop_code(self):
        with self._lock:
            return self._stop_reason or ("deadline_exceeded" if not self.seconds_left() else None)

    def remaining(self):
        with self._lock:
            return dict(self._remaining)


class RunReadCache:
    """Serialize a normalized read through retries and receipt publication."""

    def __init__(self, run_id):
        self.run_id = run_id
        self._lock = Lock()
        self._keys = {}
        self.receipts = {}

    def lock_for(self, key):
        with self._lock:
            return self._keys.setdefault(key, Lock())


class GuardedTools:
    def __init__(self, *, user_id, allowed_tools, functions, budget, trace_id=None,
                 role="team_worker", project=None, read_cache=None, condition=None, on_read=None, on_reuse=None,
                 scope_error=None, effective_args=None):
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("authenticated_user_required")
        allowed = frozenset(allowed_tools)
        if not allowed <= DELEGATION_SCHEMAS.keys() or not allowed <= functions.keys():
            raise ValueError("unauthorized_capability")
        self._user_id = user_id
        self._allowed = allowed
        self._functions = {name: functions[name] for name in allowed}
        self._budget = budget
        self._trace_id = trace_id
        self._role = role
        self._project = project
        self._read_cache = read_cache
        self._condition = condition
        self._on_read = on_read
        self._on_reuse = on_reuse
        self._scope_error = scope_error
        self._effective_args = effective_args

    def call(self, name, arguments, *, tool_call_id, parent_call_id=None, retry_of=None, basis=None):
        args, error = None, None
        feedback = []
        if name not in self._allowed:
            error = "unauthorized_tool"
        else:
            try:
                args = tool_arguments(name, arguments)
                if self._scope_error:
                    error = self._scope_error(name, args)
                    if not error:
                        args = self._effective_args(name, args)
            except ValidationError as invalid:
                error = "invalid_tool_arguments"
                feedback = [{"field": ".".join(map(str, issue["loc"])) or "arguments", "reason": issue["msg"]}
                            for issue in invalid.errors(include_input=False, include_url=False)]
            except (TypeError, ValueError):
                error = "invalid_tool_arguments"
        receipt = self._read(name, args, error, tool_call_id, parent_call_id, retry_of, basis)
        if feedback:
            receipt["errors"] = feedback
        return receipt

    def _read(self, name, args, error, tool_call_id, parent_call_id, retry_of, basis):
        runtime_call_id = uuid4().hex
        with trace_tool_call(self._trace_id, self._role, name if name in self._allowed else "unknown_tool",
                             tool_call_id, runtime_call_id=runtime_call_id,
                             parent_call_id=parent_call_id, retry_of=retry_of) as step:
            executed = False

            def reject(code):
                step["error_code"] = code
                result = {"success": False, "error_code": code, "runtime_call_id": runtime_call_id,
                          "arguments": args, "retry_of": retry_of}
                if self._read_cache is not None:
                    result["executed"] = executed
                    if code in PERMANENT_READ_GAPS:
                        result["gap_type"] = "permanent"
                    elif code == "knowledge_missing":
                        result["gap_type"] = "current_search"
                return result

            if error:
                return reject(error)
            condition = self._condition
            decision = None
            if condition and name == "get_plan_metrics_by_date":
                if code := condition.metric_error(args, None):
                    if code == "review_metric_out_of_scope":
                        return {**reject(code), "effective_plan_ids": list(condition.goal.plan_ids), "effective_dates": {
                            key: getattr(condition.goal, key).model_dump() for key in ("baseline", "current")
                        }}
                    return reject(code)
            if condition and name == "search_knowledge":
                if code := condition.rule_error(args["query"]):
                    return reject(code)
            if condition and name == "get_plan_detail":
                decision = condition.admit_detail(
                    args["plan_id"], basis, self._budget.reserve,
                    worker_id=parent_call_id if self._role == "team_worker" else None,
                    runtime_call_id=runtime_call_id,
                )
                add_trace_step(self._trace_id, "conditional_detail", "allowed" if decision["executed"] else "blocked",
                               role=self._role, tool_call_id=tool_call_id, runtime_call_id=runtime_call_id,
                               parent_call_id=parent_call_id, error_code=decision["error_code"], evidence=decision)
                if code := decision["error_code"]:
                    return {**reject(code), "condition": decision}
            if decision is None and not self._budget.reserve("tool"):
                return reject("budget_exhausted_or_cancelled")
            if self._read_cache is not None and name == "get_plan_metrics_by_date":
                step["evidence"] = {"arguments": args}
            if name == "search_knowledge":
                query = canonical_rule_query(args["query"])
                step["evidence"] = {"retrieved": True, "chunk_ids": []}
                if rule_query_scope(query):
                    step["evidence"]["rule_target"] = query
                scoped = {"query": query}
            else:
                scoped = {**args, "user_id": self._user_id}
            if decision is not None:
                step["evidence"] = {"arguments": scoped}
            if self._on_read:
                self._on_read(name, args, runtime_call_id)
                if not self._budget.active():
                    return reject("budget_exhausted_or_cancelled")
            try:
                executed = True
                if self._read_cache is not None or self._effective_args is not None:
                    add_trace_step(self._trace_id, "business_read", name, role=self._role,
                                   tool_call_id=tool_call_id, parent_call_id=parent_call_id,
                                   runtime_call_id=runtime_call_id, retry_of=retry_of,
                                   evidence={**({"run_id": self._read_cache.run_id} if self._read_cache else {}),
                                             **({"arguments": args} if self._effective_args else {})})
                result = self._functions[name](**scoped)
                if name == "search_knowledge" and isinstance(result, str):
                    result = json.loads(result)
            except Exception as error:
                if not self._budget.active():
                    return reject("expired_receipt")
                sqlite_code = getattr(error, "sqlite_errorcode", 0)
                if isinstance(error, TimeoutError) or (
                    isinstance(error, sqlite3.OperationalError)
                    and (sqlite_code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                ):
                    return reject("temporary_tool_failure")
                return reject("tool_exception")
            if name == "search_knowledge" and isinstance(result, dict) and isinstance(result.get("evidence"), list):
                step["evidence"]["chunk_ids"] = [hit["chunk_id"] for hit in result["evidence"]
                                                 if isinstance(hit, dict) and isinstance(hit.get("chunk_id"), str)]
            if not self._budget.active():
                code = self._budget.stop_code()
                if code in {"run_superseded", "run_stopped"}:
                    key = "returned_after_replacement" if code == "run_superseded" else "returned_after_stop"
                    step["evidence"] = {**(step.get("evidence") or {}), key: True, "arguments": args}
                return reject("expired_receipt")
            if (condition and name == "get_plan_metrics_by_date" and isinstance(result, dict)
                    and result.get("success") is not False and (code := condition.metric_error(args, result))):
                return reject(code)
            if error_code := receipt_error(name, result, args, self._user_id):
                if self._project and error_code == "plan_history_missing_dates":
                    if step.get("evidence") is not None:
                        step["evidence"]["missing_dates"] = result["coverage"]["missing_dates"]
                    return {**reject(error_code), "result": self._project(name, result)}
                return reject(error_code)
            if condition and name == "search_knowledge":
                if code := condition.rule_error(query, result):
                    return reject(code)
            if name in {"get_plan_detail", "get_plan_metrics_by_date"}:
                result = {key: value for key, value in result.items() if key != "user_id"}
            elif name == "get_daily_report_by_date":
                result = {"success": True, "reports": [{key: value for key, value in row.items() if key != "user_id"}
                                                       for row in result["reports"]]}
            if self._project:
                result = self._project(name, result)
            try:
                if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 128 * 1024:
                    return reject("tool_receipt_too_large")
            except (TypeError, ValueError, RecursionError):
                return reject("invalid_tool_receipt")
            if decision is not None:
                step["evidence"]["result"] = {key: result[key] for key in ("id", "plan_name", "status", "daily_budget")
                                               if key in result}
            return {"success": True, "result": result, "runtime_call_id": runtime_call_id,
                    "arguments": args, "retry_of": retry_of, **({"executed": True} if self._read_cache else {})}

    def call_with_retry(self, name, arguments, *, tool_call_id, parent_call_id=None, basis=None, publish=None):
        """Retry a reported transient failure once, with the same tool and arguments."""
        def execute():
            outcomes = self._call_with_retry(name, arguments, tool_call_id=tool_call_id,
                                              parent_call_id=parent_call_id, basis=basis)
            executed = False
            for row in outcomes:
                executed |= row.pop("executed", False)
            return outcomes, executed

        cache = self._read_cache
        if cache is None or name not in self._allowed or (
                self._condition and name == "get_plan_detail" and not self._condition.reuse_detail_receipt):
            outcomes, _ = execute()
            return publish(outcomes) if publish else outcomes
        try:
            args = tool_arguments(name, arguments)
        except (ValidationError, TypeError, ValueError):
            outcomes, _ = execute()
            return publish(outcomes) if publish else outcomes
        if self._scope_error:
            if self._scope_error(name, args):
                outcomes, _ = execute()
                return publish(outcomes) if publish else outcomes
            args = self._effective_args(name, args)
        independent = self._condition and getattr(self._condition.goal, "independent", False)
        key = read_key(name, args), self._role if independent else None
        with cache.lock_for(key):
            if known := cache.receipts.get(key):
                result = self._reuse(name, args, known, tool_call_id, parent_call_id, basis)
                return publish([result]) if publish else [result]
            outcomes, executed = execute()
            result = publish(outcomes) if publish else outcomes[-1]
            if executed and self._budget.active():
                cache.receipts[key] = deepcopy(result)
            return result if publish else outcomes

    def _reuse(self, name, args, known, call_id, parent_call_id, basis):
        error = self._budget.stop_code()
        if not error and self._condition and name == "get_plan_metrics_by_date":
            error = self._condition.metric_error(args, None)
        if not error and self._condition and name == "search_knowledge":
            error = self._condition.rule_error(args["query"])
        if not error and self._condition and name == "get_plan_detail":
            decision = self._condition.decision()
            error = (basis or decision)["error_code"] or decision["error_code"] or self._condition.target_error(
                args["plan_id"], basis, decision)
        if not error and "plan_id" in args and (known["success"] or known.get("result")):
            error = plan_scope_error(args["plan_id"], self._user_id)
        if self._budget.stop_code():
            error = "expired_receipt"
        if not error and not (known["success"] or known.get("gap_type") == "permanent"):
            error = "duplicate_read"
        result = ({**deepcopy(known), "retry_of": None, "reused": True} if not error else
                  {"success": False, "error_code": error, "runtime_call_id": known["runtime_call_id"],
                   "arguments": args, "retry_of": None, "duplicate": True})
        trace_args = ({"query": canonical_rule_query(args["query"])} if rule_query_scope(args["query"]) else {}
                      ) if name == "search_knowledge" else args
        add_trace_step(self._trace_id, "tool_reuse" if not error else "duplicate_read", name,
                       role=self._role, tool_call_id=call_id, parent_call_id=parent_call_id,
                       runtime_call_id=known["runtime_call_id"], error_code=result.get("error_code"),
                       evidence={"arguments": trace_args, "artifact_id": result.get("artifact_id"),
                                 "run_id": self._read_cache.run_id, "executed": False,
                                 **({"gap_type": known["gap_type"]} if known.get("gap_type") else {})})
        if self._on_reuse and self._budget.active():
            if error == "read_scope_changed":
                self._on_reuse(name, {**result, "unavailable_artifact_id": known.get("artifact_id")})
            else:
                self._on_reuse(name, result)
        return result

    def _call_with_retry(self, name, arguments, *, tool_call_id, parent_call_id, basis):
        original = deepcopy(arguments)
        first = self.call(name, deepcopy(original), tool_call_id=tool_call_id,
                          parent_call_id=parent_call_id, basis=basis)
        results = [first]
        if (first.get("error_code") == "temporary_tool_failure" and self._budget.active()
                and not (self._condition and name == "get_plan_detail")):
            results.append(self.call(name, deepcopy(original), tool_call_id=tool_call_id,
                                     parent_call_id=parent_call_id,
                                     retry_of=first["runtime_call_id"], basis=basis))
        return results
