"""Request-local task graph and receipt admission for delegation."""

from copy import deepcopy
from threading import Lock
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo, model_validator


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: Identifier
    artifact_id: Identifier


class TaskRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: Identifier
    arguments: dict


class TeamTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: Identifier
    objective: Description
    input_refs: tuple[Identifier, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    direct_refs: tuple[Identifier, ...] = Field(default=(), max_length=12)
    capabilities: tuple[Identifier, ...]
    read_scope: tuple[TaskRead, ...] = Field(default=(), max_length=12,
        description="Parameters for known reads. Supply only when the exact tool arguments are known.")
    dependencies: tuple[Identifier, ...] = ()
    expected_output: Description
    stop_condition: Description


class TeamPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Description
    tasks: tuple[TeamTask, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def graph_is_valid(self, info: ValidationInfo):
        tasks = {task.task_id: task for task in self.tasks}
        if len(tasks) != len(self.tasks):
            raise ValueError("duplicate_task_id")
        previous = (info.context or {}).get("previous_states", {})
        artifacts = (info.context or {}).get("previous_artifacts", set())
        direct_artifacts = (info.context or {}).get("direct_artifacts", set())
        if tasks.keys() & previous.keys():
            raise ValueError("duplicate_task_id")
        available = tasks.keys() | {task_id for task_id, state in previous.items() if state == "succeeded"}
        for task in self.tasks:
            dependencies = set(task.dependencies)
            exact_refs = {(ref.task_id, ref.artifact_id) for ref in task.artifact_refs}
            if (len(dependencies) != len(task.dependencies)
                    or len(set(task.input_refs)) != len(task.input_refs)
                    or len(exact_refs) != len(task.artifact_refs)
                    or len(set(task.direct_refs)) != len(task.direct_refs)
                    or len(set(task.capabilities)) != len(task.capabilities)):
                raise ValueError("duplicate_task_field")
            exact_tasks = {task_id for task_id, _ in exact_refs}
            if (not dependencies <= available or not (set(task.input_refs) | exact_tasks) <= dependencies
                    or set(task.input_refs) & exact_tasks or not exact_refs <= artifacts
                    or not set(task.direct_refs) <= direct_artifacts):
                raise ValueError("invalid_input_reference")
        visiting, visited = set(), set()

        def visit(task_id):
            if task_id in visiting:
                raise ValueError("cyclic_dependency")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in tasks[task_id].dependencies:
                if dependency in tasks:
                    visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)
        return self

    def authorize(self, allowed_capabilities):
        """Validate against a server-owned allowlist, never a model-owned list."""
        allowed = frozenset(allowed_capabilities)
        if any(not set(task.capabilities) <= allowed for task in self.tasks):
            raise ValueError("unauthorized_capability")
        from backend.agent.team_tools import tool_arguments

        for task in self.tasks:
            for read in task.read_scope:
                if read.tool not in task.capabilities:
                    raise ValueError("unauthorized_capability")
                tool_arguments(read.tool, read.arguments)


class ReceiptBoard:
    """Own one run's receipts; reject stale attempts and all post-cancel writes."""

    def __init__(self, plan, *, run_id=None):
        self.run_id = run_id or uuid4().hex
        self._tasks = {task.task_id: task for task in plan.tasks} if plan else {}
        self._attempts = {task_id: 0 for task_id in self._tasks}
        self._states = {task_id: "pending" for task_id in self._tasks}
        self._receipts = {}
        self._direct = {}
        self._cancelled = False
        self._lock = Lock()

    def append(self, payload, *, allowed_capabilities, task_limit, exact_prior_refs_for=None):
        with self._lock:
            if self._cancelled:
                raise ValueError("run_cancelled")
            artifacts = {(receipt["task_id"], receipt["artifact_id"]) for receipt in self._accepted_artifacts()}
            if isinstance(payload, TeamPlan):
                payload = payload.model_dump()
            plan = TeamPlan.model_validate(payload, context={"previous_states": self._states,
                                                              "previous_artifacts": artifacts,
                                                              "direct_artifacts": {
                                                                  key for key, row in self._direct.items()
                                                                  if row["status"] == "succeeded"
                                                              }})
            plan.authorize(allowed_capabilities)
            if exact_prior_refs_for and any(
                exact_prior_refs_for in task.capabilities and self._tasks.keys() & set(task.input_refs)
                for task in plan.tasks
            ):
                raise ValueError("invalid_input_reference")
            if len(self._tasks) + len(plan.tasks) > task_limit:
                raise ValueError("task_budget_exhausted")
            for task in plan.tasks:
                self._tasks[task.task_id] = task
                self._attempts[task.task_id] = 0
                self._states[task.task_id] = "pending"
            return plan

    def accept_direct(self, receipt):
        with self._lock:
            if self._cancelled:
                return False
            self._direct[receipt["artifact_id"]] = deepcopy({**receipt, "run_id": self.run_id})
            return True

    def _accepted_artifacts(self):
        return (receipt for receipt in self._receipts.values()
                if receipt["status"] == "succeeded" and self._states[receipt["task_id"]] == "succeeded")

    def get_artifact(self, artifact_id):
        with self._lock:
            if artifact_id in self._direct and self._direct[artifact_id]["status"] == "succeeded":
                return deepcopy(self._direct[artifact_id])
            matches = [receipt for receipt in self._accepted_artifacts() if receipt["artifact_id"] == artifact_id]
            if matches and all(
                (row["source"], row["tool_call_id"], row.get("arguments"), row["result"]) ==
                (matches[0]["source"], matches[0]["tool_call_id"], matches[0].get("arguments"), matches[0]["result"])
                for row in matches[1:]
            ):
                return deepcopy(matches[0])
            raise KeyError(artifact_id)

    def tool_receipts(self, *sources):
        """Read admitted tool results, including failures, independently of worker summaries."""
        with self._lock:
            return deepcopy([row for row in (*self._direct.values(), *self._receipts.values())
                             if row["source"] in sources and row["status"] in {"succeeded", "failed"}])

    def get_rule_artifact(self, artifact_id):
        """Read admitted tool evidence independently of the worker's model summary."""
        with self._lock:
            if self._cancelled:
                return None
            direct = self._direct.get(artifact_id)
            if direct and direct["source"] == "search_knowledge" and direct["status"] == "succeeded":
                return deepcopy(direct)
            for receipt in self._receipts.values():
                if (receipt["artifact_id"] == artifact_id and receipt["source"] == "search_knowledge"
                        and receipt["status"] == "succeeded"):
                    return deepcopy(receipt)
            return None

    def start(self, task_id):
        with self._lock:
            if self._cancelled or task_id not in self._tasks:
                raise ValueError("run_cancelled_or_unknown_task")
            task = self._tasks[task_id]
            if self._states[task_id] not in ("pending", "temporary_failure"):
                raise ValueError("task_not_startable")
            if self._attempts[task_id] >= 2:
                raise ValueError("retry_limit")
            if any(self._states[dep] != "succeeded" for dep in task.dependencies):
                raise ValueError("dependency_not_accepted")
            self._attempts[task_id] += 1
            self._states[task_id] = "running"
            return self._attempts[task_id], deepcopy(self._task_inputs(task))

    def _task_inputs(self, task):
        exact_refs = {(ref.task_id, ref.artifact_id) for ref in task.artifact_refs}
        inputs = {ref: [] for ref in task.input_refs}
        for receipt in self._accepted_artifacts():
            upstream = receipt["task_id"]
            if upstream in task.input_refs or (upstream, receipt["artifact_id"]) in exact_refs:
                inputs.setdefault(upstream, []).append(receipt)
        return inputs

    def input_artifact_ids(self, task):
        with self._lock:
            return frozenset((*task.direct_refs, *(row["artifact_id"] for rows in self._task_inputs(task).values()
                                                   for row in rows if row["source"] != "worker_model")))

    def input_receipts(self, task):
        with self._lock:
            def inputs(current):
                return [*(self._direct[ref] for ref in current.direct_refs),
                        *(row for rows in self._task_inputs(current).values() for row in rows)]

            rows = inputs(task)
            checked_producers = set()
            for row in rows:
                if row["source"] != "worker_model" or row["task_id"] in checked_producers:
                    continue
                # A prose summary can contain the producer's inputs and tool results.
                producer = row["task_id"]
                checked_producers.add(producer)
                rows.extend(inputs(self._tasks[producer]))
                rows.extend(receipt for receipt in self._accepted_artifacts()
                            if receipt["task_id"] == producer and receipt["source"] != "worker_model")
            return deepcopy(rows)

    def accept(self, *, run_id, task_id, attempt, artifact_id, status, source,
               tool_call_id, result=None, error_code=None, final=True, retry_of=None, arguments=None, worker_id=None):
        """Accept only the active attempt; never let a failed result feed a dependency."""
        with self._lock:
            if (self._cancelled or run_id != self.run_id or task_id not in self._tasks
                    or self._states[task_id] != "running"
                    or self._attempts[task_id] != attempt):
                return False
            if (not isinstance(artifact_id, str) or not artifact_id
                    or not isinstance(source, str) or not source
                    or not isinstance(tool_call_id, str) or not tool_call_id
                    or status not in ("succeeded", "temporary_failure", "failed")):
                raise ValueError("invalid_receipt")
            key = (task_id, attempt, artifact_id)
            if key in self._receipts:
                raise ValueError("duplicate_artifact")
            receipt = {"run_id": run_id, "task_id": task_id, "attempt": attempt,
                       "artifact_id": artifact_id, "status": status, "source": source,
                       "tool_call_id": tool_call_id, "error_code": error_code,
                       "retry_of": retry_of, "result": deepcopy(result)}
            if arguments is not None:
                receipt["arguments"] = deepcopy(arguments)
            if worker_id is not None:
                receipt["worker_id"] = worker_id
            self._receipts[key] = receipt
            if final:
                self._states[task_id] = status
            return True

    def cancel(self):
        with self._lock:
            self._cancelled = True
            for task_id, state in self._states.items():
                if state in ("pending", "running", "temporary_failure"):
                    self._states[task_id] = "cancelled"

    def block(self, task_id, dependencies, *, duplicate_of=None, unavailable_input=None):
        with self._lock:
            if self._cancelled or self._states[task_id] != "pending":
                return False
            artifact_id = uuid4().hex
            receipt = {
                "run_id": self.run_id, "task_id": task_id, "attempt": 0, "artifact_id": artifact_id,
                "status": "failed", "source": "scheduler", "tool_call_id": None, "retry_of": None,
                "error_code": "duplicate_dispatch" if duplicate_of else "dependency_failed",
                "result": {"duplicate_of": duplicate_of} if duplicate_of else {"dependencies": list(dependencies)},
            }
            if unavailable_input:
                receipt.update(unavailable_input)
            self._receipts[(task_id, 0, artifact_id)] = receipt
            self._states[task_id] = "blocked"
            return True

    def states(self):
        with self._lock:
            return dict(self._states)

    def snapshot(self):
        with self._lock:
            return deepcopy({"run_id": self.run_id, "states": self._states,
                             "attempts": self._attempts, "receipts": list(self._receipts.values())})
