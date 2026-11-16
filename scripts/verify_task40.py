"""Run the frozen TASK-040 Chromium/GLM matrix against isolated SQLite."""

import argparse
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import socket
import sqlite3
import subprocess
from threading import Thread
from time import monotonic, sleep
from traceback import format_exc
from unittest.mock import patch

from dotenv import dotenv_values
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
OWNER = 17
IDS = list(range(101, 107))
DETAIL = "get_plan_detail"
ORDERED = ("从本人广告计划 #101 开始查投放状态。每次只读取当前计划的一条详情回执，"
           "从计划名称里的「下一条=编号」确定下一步的计划编号，见到「链尾」结束。"
           "下一条编号只从上一条实际回执获取。最后按实际读取顺序汇总每条计划的编号、名称和状态。")
EVIDENCE = ("先单独查询本人计划 #101 的投放状态。只有回执确认 #101 投放中，"
            "才把 #102 和 #103 分别委派给两个 Worker，查询各自的投放状态。"
            "请给两个取证任务填写准确的 read_scope，并分别引用 #101 回执。"
            "收到两条新回执后，逐项列出三条计划的编号、全名和投放状态，状态使用「投放中」或「已暂停」。")
PLAN_SQL = "SELECT id, plan_name, status FROM ad_plans WHERE user_id=? ORDER BY id"
COMMIT_SQL = """SELECT r.status, e.content FROM chat_group_runs r
                JOIN chat_group_entries e ON e.run_id=r.run_id WHERE r.run_id=? AND e.kind='assistant'"""


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def oracle():
    from backend.agent.tests.test_react_progress import ONE, SIX, THREE
    return {
        "one": {"message": ONE, "expected": [101], "tool_limit": 12},
        "six": {"message": SIX, "expected": IDS, "tool_limit": 12},
        "budget": {"message": SIX, "expected": IDS, "tool_limit": 2},
        "ordered": {"message": ORDERED, "expected": IDS, "tool_limit": 12},
        "evidence": {"message": EVIDENCE, "expected": [101, 102, 103], "tool_limit": 12},
        "loop": {"message": THREE, "expected": [101, 102, 103], "tool_limit": 12, "expect_loop": "reads"},
        "loop-dispatch": {"message": THREE, "expected": [101, 102, 103], "tool_limit": 12,
                          "expect_loop": "dispatch"},
    }


def has_plan_status(answer, plan):
    label = {"active": "投放中", "paused": "已暂停"}[plan["status"]]
    status = rf"(?:{label}|\b{plan['status']}\b)"
    # Match within one plan's row or multi-line section.
    sections = list(re.finditer(r"(?m)^[^\n]*?(?:#(?P<tag>[1-9][0-9]*)|^\s*\|?\s*(?P<bare>[1-9][0-9]*)\s*\|)", answer))
    for index, section in enumerate(sections):
        if int(section["tag"] or section["bare"]) != plan["id"]:
            continue
        end = sections[index + 1].start() if index + 1 < len(sections) else len(answer)
        if re.search(re.escape(plan["plan_name"]) + r"[\s\S]*?" + status, answer[section.start():end]):
            return True
    return False


def grade(scenario, browser, trace, reads, calls, committed):
    if failure := browser.get("failure"):
        raise AssertionError(failure)
    terminal = browser["sse"][-1]
    answer, = [event["content"] for event in browser["sse"] if event["type"] == "text"]
    assert browser["request"] == {"message": scenario["message"], "conversation_id": browser["conversation_id"]}
    assert terminal["type"] == "done" and committed == [("completed", answer)]
    assert trace["status"] == "completed"
    stop = [row for row in trace["steps"] if row["stage"] == "react_decision" and row["action"] == "stop"][-1]
    assert any(row["stage"] == "persistence" and row["action"] == "save_reply" for row in trace["steps"])
    order = [row["arguments"]["plan_id"] for row in reads]
    assert all(row["arguments"]["user_id"] == OWNER for row in reads)
    expected = {row["id"]: row for row in scenario["rows"]}
    for read in reads:
        plan = expected[read["arguments"]["plan_id"]]
        assert {key: read["result"][key] for key in plan} == plan
    assert all(row["sdk_retry"] == 0 and row["response"]["status"] == 200 for row in calls)
    assert len(calls) <= 12 and len(order) <= scenario["tool_limit"]
    decisions = [row for row in calls if row.get("role") == "orchestrator" and row["body"].get("tools")
                 and any(choice["message"].get("tool_calls") for choice in row["response"]["body"]["choices"])]
    missing = set(scenario["expected"]) - set(order)
    workers = [row for row in trace["steps"] if row["stage"] == "worker" and row["action"] == "created"]
    duplicates = [row for row in trace["steps"] if row["stage"] in {"tool_reuse", "duplicate_dispatch", "duplicate_read"}]
    main_tool_decisions = len(decisions)
    if loop := scenario.get("expect_loop"):
        dispatch_loop = loop == "dispatch"
        assert not calls
        assert (len(workers), order) == ((1, [101]) if dispatch_loop else (0, [101, 102]))
        assert stop["evidence"]["reason"] == "no_progress"
        assert stop["evidence"]["missing_plan_ids"] == ([102, 103] if dispatch_loop else [103])
        assert len(duplicates) == 2
        main_tool_decisions = sum(row["stage"] == "model_call" and row["action"] == "main" for row in trace["steps"])
        assert main_tool_decisions == (3 if dispatch_loop else 4)
        assert all(row["evidence"]["run_id"] == terminal["run_id"] for row in [*duplicates, stop])
        assert browser["initial"]["notices"] == browser["refreshed"]["notices"]
        assert "连续两轮没有新增证据" in browser["refreshed"]["notices"][-1]
        assert "#103 仍缺详情" in browser["refreshed"]["notices"][-1]
    elif scenario["tool_limit"] < len(scenario["expected"]):
        assert len(order) == scenario["tool_limit"] and len(set(order)) == len(order)
        assert stop["evidence"]["reason"] == "tool_budget_exhausted" and stop["budget"]["tool"] == 0
        assert set(stop["evidence"]["missing_plan_ids"]) == missing
        assert not any(row["step"] == "main_summary" for row in calls)
        for text in (answer, browser["initial"]["text"], browser["refreshed"]["text"]):
            assert "任务未完成" in text and "读取额度" in text
            assert all(f"缺少 #{plan}" in text for plan in missing)
    else:
        assert not missing and len(order) == len(scenario["expected"])
    for plan_id in order:
        assert has_plan_status(answer, expected[plan_id]), f"plan #{plan_id} name or status not found in its section"
    if not missing:
        if len(scenario["expected"]) == 1:
            assert len(calls) == len(decisions) == 1
            assert not any(row["stage"] == "worker" for row in trace["steps"])
        if scenario["name"] == "ordered":
            assert order == scenario["expected"] and len(decisions) > 5
        if scenario["name"] == "evidence":
            assert order[0] == 101 and len(workers) == 2
            worker_ids = {row["runtime_call_id"] for row in workers}
            assert {row.get("parent_call_id") for row in reads[1:]} == worker_ids
            actions = [action["function"] for row in decisions for choice in row["response"]["body"]["choices"]
                       for action in choice["message"].get("tool_calls") or []]
            sheet, = [json.loads(action["arguments"]) for action in actions if action["name"] == "delegate_tasks"]
            assert actions[0]["name"] == DETAIL and json.loads(actions[0]["arguments"]) == {"plan_id": 101}
            first_artifact, = [row["evidence"]["artifact_id"] for row in trace["steps"]
                               if row["stage"] == "receipt" and row.get("role") == "orchestrator"]
            assert all(task["direct_refs"] == [first_artifact] for task in sheet["tasks"])
            assert {read["arguments"]["plan_id"] for task in sheet["tasks"] for read in task["read_scope"]} == {102, 103}
            for worker_id in worker_ids:
                first = next(row for row in calls if row.get("worker_id") == worker_id)
                context, = [json.loads(row["content"]) for row in first["body"]["messages"] if row["role"] == "user"]
                assert [row["artifact_id"] for row in context["direct_inputs"]] == [first_artifact]
            accepted = {row["runtime_call_id"]: row["evidence"]["artifact_id"] for row in trace["steps"]
                        if row["stage"] == "receipt" and row["action"] == "accepted"}
            assert set(stop["evidence"]["artifact_ids"]) == {accepted[row["runtime_call_id"]] for row in reads}
    return {"order": order, "sdk_attempts": len(calls), "main_tool_decisions": main_tool_decisions,
            "crossed_five": main_tool_decisions > 5, "stop": stop, "missing": sorted(missing),
            "workers_created": len(workers), "duplicate_actions": len(duplicates),
            "sse_terminal": terminal, "committed_matches": True}


def run(output, selected, *, supplement=False, fixed_loop=False):
    output.mkdir(parents=True, exist_ok=False)
    password = token_urlsafe(24)
    values = dotenv_values(ROOT / ".env")
    os.environ.update(AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", ANONYMIZED_TELEMETRY="False",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH=str(output / "isolated.sqlite3"), PLAN_METRICS_DB_PATH="",
                      RAG_DB_PATH=str(output / "knowledge"), LLM_PROVIDER="glm", GLM_MODEL="glm-4.5-air",
                      JWT_SECRET=token_urlsafe(32))
    if values.get("GLM_API_KEY"):
        os.environ.setdefault("GLM_API_KEY", values["GLM_API_KEY"])
    from backend import observability
    from backend.agent import home_react, llm_provider, team_runtime, team_tools
    from backend.captcha import captcha
    from backend.config.config import settings
    from backend.database import database
    from backend.memory import conversation_memory, memory_manager
    from backend.rag import rag_knowledge
    from backend.tools import tools
    from scripts.eval_review_retrieval import FixedReviewEmbedding
    from scripts.run_real_diagnosis import Redactor
    from scripts.run_task27_t5 import ENDPOINT, GLMAudit, MODEL
    from scripts.run_task27_t6 import install_transport_observer
    from tests.home_diagnosis_server import demo_captcha

    config = settings.llm_config()
    if not fixed_loop:
        assert (config.provider, config.model, config.base_url) == ("glm", MODEL, ENDPOINT) and config.api_key
    redactor = Redactor()
    redactor.secrets.extend((password, config.api_key, settings.JWT_SECRET))
    audit = GLMAudit(output / "glm-sdk-ledger.sqlite3", redactor, token_urlsafe(12), 60)
    scenarios = oracle()
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        conn.execute("INSERT INTO users (id, username, password) VALUES (?, 'task40-owner', ?)",
                     (OWNER, database.hash_password(password)))
        names = ["北岸搜索", "南桥展示", "西港重定向", "东站品牌", "湖畔新品", "松岭拓量"]
        conn.executemany("INSERT INTO ad_plans (id, user_id, plan_name, status) VALUES (?, ?, ?, ?)", [
            (plan, OWNER, f"{name}（下一条={plan + 1}）" if plan != 106 else f"{name}（链尾）",
             "active" if plan % 2 else "paused") for plan, name in zip(IDS, names, strict=True)
        ])
        rows = [dict(row) for row in conn.execute(PLAN_SQL, (OWNER,))]
    for name, scenario in scenarios.items():
        scenario.update(name=name, rows=[row for row in rows if row["id"] in scenario["expected"]])
    write(output / "frozen.json", {"sql": PLAN_SQL, "params": [OWNER], "cases": scenarios})

    report = {}
    with ExitStack() as stack:
        if fixed_loop:
            from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
            from backend.agent.tests.test_home_sse_tracing import FixedModel, tool_call
            from backend.agent.tests.test_team_runtime import task

            provider = FixedModel()

            def alternate(messages, tools=None, response_format=None, **_):
                if messages[0]["content"] == "你是一个任务规划专家, 输出JSON格式计划。":
                    return reply('{"steps":[]}')
                index = sum(message["role"] == "tool" for message in messages)
                assert tools
                if response_format:
                    return worker_summary("已取证") if index else reply("", tool_call("worker", name=DETAIL, plan_id=101))
                if audit.case == "loop-dispatch":
                    assert index < 3
                    scoped = {**task(f"renamed-{index}"),
                              "read_scope": [{"tool": DETAIL, "arguments": {"plan_id": 101}}]}
                    return reply("", dispatch(f"dispatch-{index}", scoped))
                assert index < 4
                return reply("", tool_call(f"alternate-{index}", name=DETAIL, plan_id=(101, 102)[index % 2]))

            provider.create = alternate
        else:
            provider = llm_provider.ProviderFactory.create()
            stack.callback(provider._client.close)
            hooks = provider._client._client.event_hooks
            hooks["request"].append(audit.request)
            hooks["response"].append(audit.response)
            install_transport_observer(stack, provider, audit)
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
        for module in (llm_provider, team_runtime):
            stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        for module in (home_react, team_tools):
            stack.enter_context(patch.object(module, "trace_tool_call", audit.trace_tool(module.trace_tool_call)))
        stack.enter_context(patch.object(tools, "query_plan_detail", audit.wrap_read(DETAIL, tools.query_plan_detail)))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        for module in (rag_knowledge, conversation_memory):
            stack.enter_context(patch.object(module, "BGEEmbedding", FixedReviewEmbedding))
        stack.enter_context(patch.object(conversation_memory, "DB_PATH", str(output / "conversation-memory")))
        stack.enter_context(patch.object(memory_manager, "MEMORY_DIR", str(output / "memory")))
        for name in ("TRACE_LOG_FILE", "FUNC_LOG_FILE", "ERROR_LOG_FILE"):
            stack.enter_context(patch.object(observability, name, output / (name.lower() + ".jsonl")))
        import fast_server

        def budget(**kwargs):
            return team_tools.TeamBudget(tool_limit=scenarios[audit.case]["tool_limit"], **kwargs)

        stack.enter_context(patch.object(fast_server, "TeamBudget", budget))
        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(fast_server.app, lifespan="off", log_level="warning"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started and monotonic() < deadline:
                sleep(0.05)
            assert server.started
            audit.enabled = True
            names = ["loop", "loop-dispatch"] if fixed_loop else selected or (["one", "six", "evidence"] if supplement else
                                                               ["one", "six", "budget"])
            for name in names:
                scenario = scenarios[name]
                audit.case = name
                folder = output / name
                folder.mkdir()
                write(folder / "request.json", scenario)
                try:
                    command = ["node", "tests/task40_browser.cjs", f"http://127.0.0.1:{listener.getsockname()[1]}",
                               str(folder / "request.json"), str(folder / "browser.json")]
                    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                               timeout=155, env={**os.environ, "AD_TASK40_PASSWORD": password,
                                                                 "PLAYWRIGHT_BROWSERS_PATH": "0"})
                    if completed.stderr:
                        (folder / "browser-stderr.txt").write_text(redactor.clean(completed.stderr), encoding="utf-8")
                    browser = json.loads((folder / "browser.json").read_text(encoding="utf-8"))
                    terminal = browser.get("sse", [{}])[-1]
                    trace = observability.get_trace(terminal.get("trace_id"))
                    reads = [row for row in audit.reads if row["case"] == name]
                    for row in reads:
                        row["arguments"].pop("connect", None)
                    calls = [row for row in audit.rows() if row["case"] == name]
                    with closing(sqlite3.connect(output / "isolated.sqlite3")) as conn:
                        committed = conn.execute(COMMIT_SQL, (terminal.get("run_id"),)).fetchall()
                    write(folder / "trace.json", trace)
                    write(folder / "reads.json", reads)
                    write(folder / "sdk.json", calls)
                    write(folder / "commit.json", {"sql": COMMIT_SQL, "params": [terminal.get("run_id")], "rows": committed})
                    report[name] = grade(scenario, browser, trace, reads, calls, committed)
                    if name == "six" and not report[name]["crossed_five"] and not selected:
                        names.append("ordered")
                except Exception:
                    report[name] = {"failure": redactor.clean(format_exc())}
                write(output / "report.json", report)
        finally:
            server.should_exit = True
            thread.join(30)
            write(output / "sdk.json", audit.rows())
    return not any("failure" in row for row in report.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--case", action="append", choices=("one", "six", "budget", "ordered", "evidence"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--loop-supplement", action="store_true")
    modes.add_argument("--fixed-loop", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if run(args.output.resolve(), args.case, supplement=args.loop_supplement,
                              fixed_loop=args.fixed_loop) else 1)
