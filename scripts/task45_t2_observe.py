"""Capture the actual model input, receipt chain, and phase timestamps."""

import inspect
import json
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from time import perf_counter_ns
from types import SimpleNamespace
from uuid import uuid4

from scripts.accept_task45_t1 import CURRENT, FACTS, SALES, Replay


def phase(body):
    system = body["messages"][0]["content"]
    if "temporary worker" in system:
        return "worker"
    if system.startswith("Produce only a JSON object"):
        return "draft"
    return "main"


def calls(message):
    for call in message.get("tool_calls") or ():
        function = call.get("function") or {}
        yield call.get("id"), function.get("name"), function.get("arguments")


class FixedModel(Replay):
    def __init__(self, observation):
        self.observation = observation
        super().__init__()

    def create(self, **payload):
        recorded = {**self.observation.model.get(), "start_ns": perf_counter_ns(),
                    "role": phase(payload), "body": deepcopy(payload)}
        self.observation.requests.append(recorded)
        reply = self.reply(**payload)
        recorded.update(end_ns=perf_counter_ns(), response=vars(reply.choices[0].message))
        return reply

    def reply(self, **payload):
        messages = payload["messages"]
        role = phase(payload)
        if role == "worker" and not any(row["role"] == "tool" for row in messages):
            context = json.loads(messages[1]["content"])
            task = context["task"]
            if task["task_id"] == "listing":
                # This fixed Worker derives its business read from the handed-off receipt.
                receipt, = context["inputs"]["facts"]
                if receipt["source"] != FACTS:
                    raise ValueError("Expected a fact receipt")
                sku = receipt["result"]["target"]["sku"]
                response = {"content": None, "tool_calls": [{"id": uuid4().hex, "type": "function",
                    "function": {"name": CURRENT, "arguments": json.dumps({"sku": sku})}}]}
                with self.lock:
                    self.requests.append({"start_ns": perf_counter_ns(), "role": role,
                                          "body": deepcopy(payload), "response": response})
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(**response))])
        return super().create(**payload)


class Observation:
    def __init__(self):
        self.link = ContextVar("task45_t2_read", default={})
        self.model = ContextVar("task45_t2_model", default={})
        self.reset()

    def reset(self):
        self.trace = []
        self.reads = []
        self.board = []
        self.plans = []
        self.budgets = []
        self.requests = []

    def trace_step(self, original):
        @wraps(original)
        def observe(trace_id, stage, action, *args, **kwargs):
            if stage == "model_call":
                self.model.set({"trace_id": trace_id, "model_call_id": kwargs.get("runtime_call_id"),
                                "worker_id": kwargs.get("parent_call_id"), "step": action})
            self.trace.append({"ns": perf_counter_ns(), "trace_id": trace_id, "stage": stage,
                               "action": action, "detail": kwargs.get("detail", args[0] if args else ""),
                               **deepcopy(kwargs)})
            return original(trace_id, stage, action, *args, **kwargs)
        return observe

    def trace_tool(self, original):
        @contextmanager
        def observe(trace_id, role, name, call_id=None, **kwargs):
            link = self.link.set({"trace_id": trace_id, "role": role, "tool_call_id": call_id, **kwargs})
            try:
                with original(trace_id, role, name, call_id, **kwargs) as step:
                    yield step
            finally:
                self.link.reset(link)
        return observe

    def reader(self, name, original):
        signature = inspect.signature(original)

        @wraps(original)
        def observe(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            arguments = {key: value for key, value in bound.arguments.items() if key not in {"conn", "user_id"}}
            link = self.link.get()
            stage = "initial" if link.get("tool_call_id") else "other"
            artifact_id = None
            trigger = None
            if stage == "other":
                frame = sys._getframe(1)
                while frame is not None:
                    caller = frame.f_code.co_name
                    if caller == "recheck":
                        artifact_id = frame.f_locals["row"]["artifact_id"]
                    if caller == "verify" and frame.f_globals.get("__name__") == "backend.database.listing_operations":
                        artifact_id = frame.f_locals["artifact_id"]
                    if caller in {"basis", "_block_unavailable_inputs", "create_group_proposal", "approve",
                                  "read_action", "draft", "run_listing"}:
                        stage = {"_block_unavailable_inputs": "worker_handoff", "create_group_proposal": "proposal",
                                 "approve": "approval", "read_action": "review"}.get(caller, "stage")
                        trigger = f"{caller}:{frame.f_lineno}"
                        if caller != "read_action":
                            break
                    frame = frame.f_back
            row = {**link, "source": name, "arguments": arguments, "start_ns": perf_counter_ns(),
                   "checkpoint": stage, "trigger": trigger}
            if artifact_id:
                row["artifact_id"] = artifact_id
            self.reads.append(row)
            try:
                result = original(*args, **kwargs)
                row["result"] = deepcopy(result)
                return result
            except Exception as error:
                row["error"] = type(error).__name__
                raise
            finally:
                row["end_ns"] = perf_counter_ns()
        return observe

    def reading_inventory(self):
        """Join captured reader calls to their run and original receipt after the request."""
        inventory = []
        for read in self.reads:
            row = {key: value for key, value in read.items() if key != "result"}
            matches = [receipt for receipt in self.board if receipt["source"] == row["source"]
                       and all(row["arguments"].get(key) == value
                               for key, value in (receipt.get("arguments") or {}).items())]
            if row.get("tool_call_id"):
                matches = [receipt for receipt in matches if receipt["tool_call_id"] == row["tool_call_id"]]
            if matches:
                row.setdefault("artifact_id", matches[0]["artifact_id"])
                row.setdefault("run_id", matches[0]["run_id"])
            elif self.budgets:
                row.setdefault("run_id", self.budgets[-1]["run_id"])
            inventory.append(row)
        return inventory

    def board_accept(self, original, *, direct=False):
        @wraps(original)
        def observe(board, *args, **kwargs):
            accepted = original(board, *args, **kwargs)
            if accepted:
                receipt = (next(row for row in board.tool_receipts(args[0]["source"])
                                if row["artifact_id"] == args[0]["artifact_id"]) if direct else kwargs)
                self.board.append({"ns": perf_counter_ns(), "run_id": board.run_id, **deepcopy(receipt)})
            return accepted
        return observe

    def board_append(self, original):
        @wraps(original)
        def observe(board, *args, **kwargs):
            plan = original(board, *args, **kwargs)
            self.plans.append({"ns": perf_counter_ns(), "run_id": board.run_id, **plan.model_dump(mode="json")})
            return plan
        return observe


def public_receipt(row):
    fields = ("run_id", "artifact_id", "source", "tool_call_id", "status", "arguments", "result")
    return {key: row[key] for key in fields if key in row}


def verify_handoffs(obs, rows, trace_id, evidence):
    """Join actual first inputs to accepted task references, the board, and reader results."""
    runtime = [row for row in obs.trace if row["trace_id"] == trace_id]
    created = {row.get("runtime_call_id"): row["detail"] for row in runtime
               if (row["stage"], row["action"]) == ("worker", "created")}
    seen = set()
    result = []
    for row in rows:
        if phase(row["body"]) != "worker":
            continue
        try:
            context = json.loads(row["body"]["messages"][1]["content"])
            task = context["task"]
            task_id = task["task_id"]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        worker_id = row.get("worker_id") or next((key for key, value in created.items() if value == task_id), None)
        if worker_id in seen:
            continue
        seen.add(worker_id)
        run_id = evidence.get("run_id")
        plan_task = next((item for plan in obs.plans if plan["run_id"] == run_id
                          for item in plan["tasks"] if item["task_id"] == task_id), None)
        referenced = [(ref, receipt) for ref, receipts in context.get("inputs", {}).items() for receipt in receipts]
        supplied = [*referenced, *((None, receipt) for receipt in context.get("direct_inputs", []))]
        reads = [read for read in obs.reads if read.get("parent_call_id") == worker_id and worker_id]
        reconciled = []
        for ref, receipt in supplied:
            board = next((source for source in obs.board if source["artifact_id"] == receipt["artifact_id"]), {})
            tool = next((read for read in obs.reads if read.get("runtime_call_id") == receipt.get("tool_call_id")), {})
            ranked = receipt["artifact_id"] in {item["artifact_id"] for item in evidence.get("comparison_receipts", [])}
            accepted_refs = plan_task or {}
            reference_matches = (
                (ranked or receipt["artifact_id"] in accepted_refs.get("direct_refs", [])) if ref is None else
                ref == board.get("task_id") and (ref in accepted_refs.get("input_refs", []) or
                    {"task_id": ref, "artifact_id": receipt["artifact_id"]} in accepted_refs.get("artifact_refs", []))
            )
            passed = bool(worker_id and plan_task == task and board.get("run_id") == run_id
                          and public_receipt(board) == public_receipt(receipt)
                          and reference_matches and tool.get("source") == receipt["source"]
                          and tool.get("arguments") == receipt.get("arguments")
                          and tool.get("result") == receipt.get("result")
                          and tool.get("end_ns", float("inf")) <= board["ns"] <= row.get("start_ns", 0))
            reconciled.append({"task_ref": ref, "task_reference_matches": reference_matches,
                               "reader": tool, "board": public_receipt(board),
                               "first_input_receipt": public_receipt(receipt), "passed": passed})
        reclaimed = any((step["stage"], step["action"], step.get("runtime_call_id")) ==
                        ("worker", "reclaimed", worker_id) for step in runtime)
        completed = any(item["tool_call_id"] == worker_id and item["source"] == "worker_model"
                        and item["status"] == "succeeded" for item in obs.board)
        scoped_reads = bool(reads) and all(any(
            allowed["tool"] == read["source"] and all(read["arguments"].get(key) == value
                                                      for key, value in allowed["arguments"].items())
            for allowed in task["read_scope"]) for read in reads)
        result.append({"run_id": run_id, "worker_id": worker_id, "model_call_id": row.get("model_call_id"),
                       "task": task, "board_task": plan_task, "scope": context.get("scope"), "receipts": reconciled,
                       "downstream_reads": reads, "reclaimed": reclaimed, "completed": completed,
                       "passed": bool(reconciled and all(item["passed"] for item in reconciled)
                                      and scoped_reads and completed and reclaimed)})
    return result
