"""Execute the frozen read-only inspection independently of HTTP subscribers."""

from contextlib import closing, contextmanager
import os
import sqlite3
from time import sleep
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.agent import listing_inspection, listing_run
from backend.agent.llm_provider import ProviderFactory
from backend.agent.request_budget import ExecutionStopped, RequestBudget
from backend.agent.team_tools import TeamBudget
from backend.database import background_inspections as storage
from backend.database import database, listing_operations as listings
from backend.observability import add_trace_step, end_trace, new_trace_id, start_trace, trace_tool_call


def make_spec(conn, user_id, message, requested_at):
    if not listing_run._inspection_requested(message) or listing_run._wants_draft(message):
        raise listings.ListingInvalid("后台运行仅支持三款商品的上新前只读巡检")
    named = listing_run.NAMED_PRODUCT_LIST.match(message)
    candidates = listing_run._inspection_targets(
        listings.find_targets(conn, user_id), message, named["names"] if named else None)
    if (len(candidates) != 3 or len({row["sku"].casefold() for row in candidates}) != 3
            or len({(row["shop_id"], row["marketplace_id"]) for row in candidates}) != 1):
        raise listings.ListingInvalid("请提供同一授权店铺、同一站点的三款商品名称或 SKU")
    today = requested_at.astimezone(ZoneInfo(listings.SALES_TIMEZONE)).date()
    windows = listing_run._windows(message, today, comparison=True)
    if windows is None:
        raise listings.ListingInvalid("请指定两个等长且不重叠的销量比较窗口")
    return {"candidates": candidates, "statistics_date": today.isoformat(), "windows": windows,
            "sales_timezone": listings.SALES_TIMEZONE}


def execute(job, provider):
    run_id, trace_id, generation, spec = (job[key] for key in ("run_id", "trace_id", "generation", "spec"))
    link = {"run_id": run_id, "generation": generation, "executor_id": job["executor_id"], "trace_id": trace_id}

    def step(name, call, *, tool=None):
        call_id = uuid4().hex
        metadata = {"tool": tool, "runtime_call_id": call_id} if tool else {}
        if saved := storage.prepare(job, name, **metadata):
            artifact_id, artifact = saved
            add_trace_step(trace_id, "background_inspection", "reused", detail=name,
                           evidence={**link, "artifact_id": artifact_id})
            return artifact_id, artifact["result"], artifact["tool_call_id"]
        if tool:
            result = call(call_id, name)
        else:
            result = call()
        artifact_id = storage.checkpoint(job, name, result, **metadata)
        add_trace_step(trace_id, "background_inspection", "checkpoint", detail=name,
                       evidence={**link, "artifact_id": artifact_id, **metadata})
        return artifact_id, result, call_id if tool else None

    try:
        with storage._read() as conn:
            storage.validate(conn, job)
        budget = RequestBudget(trace_id)
        budget.execution = TeamBudget(model_limit=2)
        products = {}
        for candidate in spec["candidates"]:
            sku = candidate["sku"]
            products[sku] = receipts = {}
            target = {key: candidate[key] for key in ("shop_id", "marketplace_id", "sku")}
            for tool, reader in ((listing_run.SALES, listings.read_sales),
                                 (listing_run.CURRENT, listings.read_listing),
                                 (listing_run.FACTS, listings.read_facts)):
                arguments = dict(target)
                if tool == listing_run.SALES:
                    arguments.update(zip(listings._WINDOW_KEYS, spec["windows"]))

                def read_tool(call_id, name):
                    if not budget.execution.reserve("tool"):
                        raise ExecutionStopped(budget.execution.stop_code() or "inspection_tool_budget")
                    with storage._read() as conn:
                        storage.validate(conn, job)
                        with trace_tool_call(trace_id, "orchestrator", tool, call_id, runtime_call_id=call_id) as attempt:
                            attempt["evidence"] = {**link, "step": name}
                            return reader(conn, job["user_id"], **arguments)

                artifact_id, result, tool_call_id = step(f"{sku}/{tool}", read_tool, tool=tool)
                if result is not None:
                    receipts[tool] = {"artifact_id": artifact_id, "arguments": arguments, "result": result,
                                      "source": tool, "status": "succeeded", "tool_call_id": tool_call_id}
        with storage._read() as conn:
            storage.validate(conn, job)
        _, review, _ = step("review", lambda: listing_inspection._review(
            products, provider, spec["model"], budget, trace_id))
        scope = {key: spec[key] for key in ("windows", "sales_timezone", "statistics_date")}
        scope.update(run_id=run_id, target=None, candidates=[
            {key: candidate[key] for key in listings._TARGET_KEYS} for candidate in spec["candidates"]])
        evidence = listing_inspection.inspection_evidence(spec["candidates"], scope, products, review)
        status, released = storage.finish(job, "completed", listing_inspection._summary(evidence), evidence)
    except storage.StaleExecution:
        end_trace(trace_id, "stale")
        return
    except storage.Terminated:
        status, released = storage.finish(job, "stopped")
    except storage.Invalidated as error:
        labels = {"product_scope": "商品授权范围", "conversation_owner": "工作群属主", "listing": "Listing",
                  "facts": "商品事实", "required_facts": "必需事实字段", "sales": "销量"}
        items = "、".join(" ".join(filter(None, (row.get("sku"), labels[row["source"]]))) for row in error.items)
        status, released = storage.finish(job, "invalidated", f"巡检依据已失效（{items}）。请重新发起巡检。",
                                          invalid=error.items)
    except Exception as error:
        add_trace_step(trace_id, "background_inspection", "failed", status="error",
                       error_code=type(error).__name__, evidence={"run_id": run_id})
        status, released = storage.finish(job, "failed", "后台巡检执行失败，请重新发起巡检。")
    if released is not None:
        add_trace_step(trace_id, "admission", "released", runtime_call_id=run_id, evidence={"released_ns": released})
    end_trace(trace_id, status)


def _lock_directory():
    path = database.sandbox_db_path()
    if path is None or storage.configured_model() is None:
        raise RuntimeError("配置隔离 SQLite 和 STOREPILOT_BACKGROUND_MODEL 后再启动执行进程")
    if not path.is_file():
        raise RuntimeError("请先初始化后台任务所在的 SQLite")
    directory = path.with_name(path.name + ".executor-locks")
    directory.mkdir(exist_ok=True)
    return directory


@contextmanager
def executor_lock(directory, run_id):
    # One SQLite file per run provides a portable, process-scoped OS lock.
    # Keep its pathname stable while executors are online, including after commit.
    with closing(sqlite3.connect(directory / run_id, timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise
            yield False
        else:
            yield True


def _attempt_trace(run_id, executor_id, generation):
    trace_id = start_trace(source="background_inspection", trace_id=new_trace_id())
    add_trace_step(trace_id, "background_inspection", "claim_attempt", runtime_call_id=run_id,
                   evidence={"executor_id": executor_id, "pid": os.getpid(), "observed_generation": generation})
    return trace_id


def serve():
    directory = _lock_directory()
    executor_id = uuid4().hex
    provider = ProviderFactory.create()
    contended = set()
    while True:
        pending = storage.pending()
        contended.intersection_update(run_id for run_id, _ in pending)
        for run_id, generation in pending:
            trace_id = None if run_id in contended else _attempt_trace(run_id, executor_id, generation)
            with executor_lock(directory, run_id) as acquired:
                if not acquired:
                    if trace_id:
                        add_trace_step(trace_id, "background_inspection", "lock_busy", runtime_call_id=run_id)
                        end_trace(trace_id, "skipped")
                        contended.add(run_id)
                    continue
                trace_id = trace_id or _attempt_trace(run_id, executor_id, generation)
                job = storage.claim(run_id, executor_id, trace_id)
                if job is None:
                    add_trace_step(trace_id, "background_inspection", "no_longer_pending", runtime_call_id=run_id)
                    end_trace(trace_id, "skipped")
                    continue
                add_trace_step(trace_id, "background_inspection", "claimed", runtime_call_id=run_id,
                               evidence={"executor_id": executor_id, "pid": os.getpid(),
                                         "generation": job["generation"]})
                execute(job, provider)
                break
        else:
            sleep(0.2)


if __name__ == "__main__":
    serve()
