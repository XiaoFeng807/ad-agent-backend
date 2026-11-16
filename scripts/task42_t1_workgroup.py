"""Capture TASK-042-T1 workgroup evidence on fresh local SQLite databases."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from contextvars import ContextVar
from functools import wraps
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
import subprocess
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from scripts.accept_task42_t1 import configure, read, serve, write


GROUPS = "/api/chat/conversations"
SKU = "TASK42-BOTTLE"
DATES = "2026-10-30 到 2026-11-12"
SIMPLE = f"请查 SKU {SKU} 在 {DATES} 的销量，前后两周分别卖了多少件？"
ORIGINAL_SIMPLE = f"SKU {SKU} 在 {DATES} 的前后两周分别卖了多少件？"
FULL = f"请比较 SKU {SKU} 在 {DATES} 的前后两周销量，结合商品资料重写五条英文五点。请保留准确的容量信息，先让我确认再应用。"
READ_KEYS = {"get_listing_sales": "metrics", "get_listing_current": "listing", "get_listing_facts": "facts"}
SQL = Path(__file__).with_name("task42_t1_listing.sql").read_text(encoding="utf-8")


def capture(call, callback):
    @wraps(call)
    def wrapped(*args, **kwargs):
        result = call(*args, **kwargs)
        callback(args, kwargs, result)
        return result
    return wrapped


def connection(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def snapshot(path):
    tables = ("listing_test_shops", "listing_test_products", "listing_test_facts", "listing_test_sales",
              "listing_change_proposals", "listing_change_receipts")
    with connection(path) as conn:
        return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1,2,3")]
                for table in tables}


def oracle(path, target):
    params = dict(target, baseline_start="2026-10-30", baseline_end="2026-11-05",
                  current_start="2026-11-06", current_end="2026-11-12")
    with connection(path) as conn:
        rows = [dict(row) for row in conn.execute(SQL, params)]
    return rows


def expected_metrics(rows, target):
    result = {"source": "local_listing_test", "target": target, "version": rows[0]["version"],
              "units_delta": rows[0]["delta"], "units_change_pct": rows[0]["relative_pct"], "conversion_rate": None}
    for window in ("baseline", "current"):
        points = [row for row in rows if row["window"] == window]
        result[window] = {"start_date": points[0]["day"], "end_date": points[-1]["day"],
                          "days": [{"date": p["day"], "units": p["units"]} for p in points],
                          "covered_days": points[0]["covered"], "units": points[0]["total"]}
    return result


def body(response):
    return response.json().get("data")


def login(client, name, password):
    response = client.post("/api/login", json={"username": name, "password": password})
    return {"Authorization": "Bearer " + body(response)["token"]}


def http_round(client, headers, case, message):
    group = body(client.post(GROUPS, headers=headers, json={"title": f"TASK-042-T1 {case}"}))
    base = GROUPS + "/" + group["conversation_id"]
    request = {"conversation_id": group["conversation_id"], "message": message}
    response = client.post("/api/chat/stream", json=request, headers=headers)
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    return {"request": request, "status": response.status_code, "events": events, "base": base}


def finish_http(client, headers, other, report, path, action_mode):
    from backend.database import database

    done = next((e for e in report["events"] if e["type"] == "done"), None)
    if not done:
        return
    base = report["base"]
    run = base + "/runs/" + done["run_id"]
    report["history"] = body(client.get(base + "/history", headers=headers))
    report["evidence"] = body(client.get(run + "/evidence", headers=headers))
    assistant = next((e for e in report["history"]["entries"] if e["kind"] == "assistant"), {})
    actions = assistant.get("listing_actions") or []
    report["action_count"] = len(actions)
    report["before_confirm"] = snapshot(path)
    if not actions:
        return
    action = run + "/listing-actions/" + actions[0]["action_id"]
    report["pending"] = body(client.get(action, headers=headers))
    if action_mode == "concurrent":
        report["foreign"] = [client.get(action, headers=other).status_code,
                             client.post(action + "/confirm", headers=other).status_code]
        report["after_foreign"] = snapshot(path)
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(lambda _: client.post(action + "/confirm", headers=headers), range(4)))
    elif action_mode in {"stale", "fault"}:
        with closing(database.get_db()) as conn, conn:
            if action_mode == "stale":
                conn.execute("UPDATE listing_test_products SET version=version+1 WHERE shop_id='task42-test' AND sku=?", (SKU,))
            else:
                conn.execute("CREATE TRIGGER fail_t1_receipt BEFORE INSERT ON listing_change_receipts "
                             "BEGIN SELECT RAISE(ABORT, 'task42_t1_fixture'); END")
        report["at_confirm"] = snapshot(path)
        responses = [client.post(action + "/confirm", headers=headers)]
        if action_mode == "fault":
            with closing(database.get_db()) as conn, conn:
                conn.execute("DROP TRIGGER fail_t1_receipt")
    elif action_mode == "approve":
        responses = [client.post(action + "/confirm", headers=headers)]
    else:
        return
    report["approvals"] = [{"status": r.status_code, "body": r.json()} for r in responses]
    response = client.get(action, headers=headers)
    report["get"] = {"status": response.status_code, "body": response.json()}


def check(checks, stage, label, condition):
    checks.append({"stage": stage, "check": label, "passed": bool(condition)})


def model_calls(row):
    response = row.get("response") or {}
    if "body" in response:
        return (response["body"]["choices"][0]["message"].get("tool_calls") or [])
    return response.get("tool_calls") or []


def score(report, expected, sdk, traces, captures, live):
    checks = []
    add = lambda stage, label, ok: check(checks, stage, label, ok)
    done = [e for e in report["events"] if e["type"] in {"done", "error", "stopped", "superseded"}]
    if live and (not sdk or any(r["response"].get("status") != 200 for r in sdk)):
        add("选路", "GLM HTTP", False)
        return checks
    add("选路", "one SSE done", [e["type"] for e in done] == ["done"])
    if not done or done[-1]["type"] != "done":
        return checks
    source = next((t for t in traces if t["trace_id"] == done[0]["trace_id"]), {})
    steps = source.get("steps", [])
    evidence = report.get("evidence") or {}
    history = report.get("history") or {}
    assistant = next((e for e in history.get("entries", []) if e["kind"] == "assistant"), {})
    add("证据交接", "Trace, SSE and committed history", bool(steps) and assistant.get("content") in
        [e.get("content") for e in report["events"] if e["type"] == "text"])
    before, after = report["sql_before"], report["sql_after"]
    if expected == "ambiguous":
        add("选路", "ask for shop before any SDK or data read",
            not sdk and not captures["reads"] and "店铺编号" in assistant.get("content", ""))
        add("执行", "zero writes", before == after)
        return checks
    target = report["target"]
    add("选路", "request entered Listing workgroup", evidence.get("kind") == "listing_operations_v1")
    if evidence.get("kind") != "listing_operations_v1":
        return checks
    add("取数", "independent SQLite daily series, totals and relative delta",
        evidence.get("metrics") == expected_metrics(report["sql_oracle"], target))
    add("取数", "exact effective SQL reader target, dates and owner",
        bool(captures["reads"]) and all(r["target"] == target and r["user_id"] == report["user_id"]
        and (r["tool"] != "get_listing_sales" or r["dates"] == ["2026-10-30", "2026-11-05", "2026-11-06", "2026-11-12"])
        for r in captures["reads"]))
    artifacts = evidence.get("artifacts") or {}
    actual_receipts = {r["artifact_id"]: r for r in captures["receipts"]}
    expected_tools = {"get_listing_sales"} if expected == "simple" else set(READ_KEYS)
    windows = ("baseline_start", "baseline_end", "current_start", "current_end")
    sales_dates = dict(zip(windows, ("2026-10-30", "2026-11-05", "2026-11-06", "2026-11-12")))
    add("证据交接", "every selected artifact joins runtime result, board receipt and exact payload",
        set(artifacts) == expected_tools and all(
            any(s["stage"] == "tool_result" and s["action"] == name and s["status"] == "ok"
                and s["runtime_call_id"] == ref["runtime_call_id"] for s in steps)
            and (row := actual_receipts.get(ref["artifact_id"])) is not None
            and row["tool_call_id"] == ref["runtime_call_id"]
            and row["arguments"] == {**target, **(sales_dates if name == "get_listing_sales" else {})}
            and row["result"] == evidence[READ_KEYS[name]]
            for name, ref in artifacts.items()))
    draft = [r for r in sdk if r.get("step") == "listing_draft"]
    workers = [r for r in sdk if r.get("step") == "team_worker"]
    if captures["batches"] or workers or expected == "delegate":
        task_by_id = {task["task_id"]: task for batch in captures["batches"]
                      for task in batch["submitted"]["tasks"]}
        first = {}
        for row in workers:
            value = json.loads(row["body"]["messages"][1]["content"])
            first.setdefault(value["task"]["task_id"], value)
        received = [item for value in first.values() for rows in value["inputs"].values() for item in rows]
        received += [item for value in first.values() for item in value.get("direct_inputs", [])]
        add("证据交接", "worker first input reconciles accepted task and original receipt payload",
            bool(first) and set(first) <= set(task_by_id) and all(
                task_by_id[tid]["objective"] == value["task"]["objective"] for tid, value in first.items())
            and all(item["artifact_id"] in actual_receipts and
                    all(actual_receipts[item["artifact_id"]].get(key) == item[key]
                        for key in ("source", "arguments", "result", "tool_call_id") if key in item) for item in received)
            and (expected != "delegate" or set(x["artifact_id"] for x in received)
                 >= {x["artifact_id"] for x in artifacts.values()}))
    proposal = evidence.get("proposal")
    actions = report["action_count"]
    add("执行", "approval before any product or receipt change",
        all(before[k] == report["before_confirm"][k] for k in before if k != "listing_change_proposals"))
    if expected in {"simple", "missing", "unsupported"}:
        add("草案" if expected != "simple" else "选路", "zero approvable proposals and appropriate user reply",
            proposal is None and actions == 0 and
            (expected != "missing" or "insufficient_listing_facts" in evidence.get("gaps", [])
             and "商品事实不足" in assistant.get("content", "") and not draft) and
            (expected != "simple" or "70 件" in assistant.get("content", "") and "49 件" in assistant.get("content", "")
             and not draft))
        add("执行", "no writes", before == after)
    else:
        add("证据交接", "draft model actually received selected tool payloads and artifact IDs", len(draft) == 1 and
            all(json.loads(draft[0]["body"]["messages"][-1]["content"]).get(k) == evidence.get(k)
                for k in ("metrics", "listing", "facts", "artifacts")))
        facts = {r["fact_id"]: r for r in evidence.get("facts", {}).get("facts", [])}
        bullets = (proposal or {}).get("bullets", [])
        add("草案", "five distinct verified English options and requested capacity",
            actions == 1 and len(bullets) == 5 and len({b["fact_id"] for b in bullets}) == 5 and
            all(b["text"] in facts[b["fact_id"]]["options"] for b in bullets) and
            any(b["fact_id"] == "capacity" and report["capacity"] in b["text"] for b in bullets))
        if proposal and len(draft) == 1:
            response = draft[0]["response"]
            output = response["body"]["choices"][0]["message"] if "body" in response else response
            add("草案", "persisted draft equals model output", json.loads(output["content"])["bullets"] ==
                [{k: b[k] for k in ("fact_id", "text")} for b in bullets])
        if report["mode"] in {"stale", "fault"}:
            status = 409 if report["mode"] == "stale" else 503
            add("执行", "rejection, no writes or receipt after failure",
                [r["status"] for r in report.get("approvals", [])] == [status] and after == report.get("at_confirm"))
        elif report["mode"] in {"concurrent", "approve", "edge"}:
            approvals = report.get("approvals", [])
            get = report.get("get", {}).get("body", {}).get("data") or {}
            receipts = after["listing_change_receipts"]
            control = lambda row: (row["shop_id"], row["marketplace_id"], row["sku"]) != tuple(target[k]
                for k in ("shop_id", "marketplace_id", "sku"))
            old, new = before["listing_test_products"], after["listing_test_products"]
            old_target = next(row for row in old if not control(row))
            new_target = next(row for row in new if not control(row))
            add("执行", "POST and independent GET agree with exactly one receipt and only target bullets/version",
                len(approvals) == (4 if report["mode"] == "concurrent" else 1)
                and all(r["status"] == 200 and r["body"]["data"] == get for r in approvals)
                and len(receipts) == len(before["listing_change_receipts"]) + 1
                and any(r["receipt_id"] == get.get("receipt_id") for r in receipts)
                and {k for k in new_target if new_target[k] != old_target[k]} == {"bullets", "version"}
                and new_target["version"] == old_target["version"] + 1 == get["listing"]["version"]
                and json.loads(new_target["bullets"]) == get["listing"]["bullets"] == proposal["after_bullets"]
                and [r for r in old if control(r)] == [r for r in new if control(r)]
                and all(before[k] == after[k] for k in ("listing_test_shops", "listing_test_facts", "listing_test_sales"))
                and any(t["trace_id"] == get["receipt"]["execution_trace_id"] and
                        any(s["stage"] == "listing_action" and s["action"] == "applied" for s in t["steps"])
                        for t in traces))
            if report["mode"] == "concurrent":
                add("执行", "cross-owner GET/POST denied with no writes",
                    report.get("foreign") == [404, 404] and report.get("after_foreign") == report["before_confirm"])
            if report["mode"] == "edge":
                add("执行", "Edge refresh reads the same receipt", report["browser"]["passed"] and
                    report["browser"]["refresh"] == get)
    return checks


def capture_runtime(stack, box, listings, model, audit):
    from backend.agent import listing_run, llm_provider, team_runtime

    link = ContextVar("task42_model_link", default={})

    def trace_model(original):
        @wraps(original)
        def observed(trace_id, stage, action, *args, **kwargs):
            if stage == "model_call":
                link.set({"trace_id": trace_id, "step": action, "model_call_id": kwargs.get("runtime_call_id"),
                          "worker_id": kwargs.get("parent_call_id"), "task_id": kwargs.get("detail")})
            return original(trace_id, stage, action, *args, **kwargs)
        return observed

    for module in (llm_provider, team_runtime):
        original = module.add_trace_step
        stack.enter_context(patch.object(module, "add_trace_step", (audit.trace_model if audit else trace_model)(original)))
    if not audit:
        def record_model(_args, payload, response):
            reply = response.choices[0].message
            box["sdk"].append({**link.get(), "body": payload, "response": {
                "content": reply.content, "tool_calls": reply.tool_calls}})
        completions = model.chat.completions
        stack.enter_context(patch.object(completions, "create", capture(completions.create, record_model)))

    def read_capture(tool):
        def record(args, _kwargs, result):
            _, user_id, shop, market, sku, *dates = args
            box["reads"].append({"tool": tool, "user_id": user_id,
                                 "target": {"shop_id": shop, "marketplace_id": market, "sku": sku},
                                 "dates": dates, "found": result is not None})
        return record

    for attr, tool in (("read_sales", "get_listing_sales"), ("read_listing", "get_listing_current"),
                       ("read_facts", "get_listing_facts")):
        stack.enter_context(patch.object(listings, attr, capture(getattr(listings, attr), read_capture(tool))))

    def direct(args, _kwargs, artifact):
        if artifact:
            _, source, result = args
            box["receipts"].append({"source": source, "artifact_id": artifact, "tool_call_id": result["runtime_call_id"],
                                    "result": result["result"], "arguments": result["arguments"]})

    def worker(args, receipt, accepted):
        if accepted:
            _, task_id, attempt, _, worker_id = args
            box["receipts"].append({**receipt, "task_id": task_id, "attempt": attempt, "worker_id": worker_id})

    @wraps(listing_run.delegate_tasks)
    def delegate(plan, **kwargs):
        batch = {"submitted": plan, "dispatch_id": kwargs.get("dispatch_id")}
        box["batches"].append(batch)
        try:
            result = original_delegate(plan, **kwargs)
        except ValueError as error:
            batch["rejected"] = str(error)
            raise
        batch["returned"] = result
        return result

    original_delegate = listing_run.delegate_tasks
    runtime = team_runtime.DelegationRun
    stack.enter_context(patch.object(runtime, "accept_direct", capture(runtime.accept_direct, direct)))
    stack.enter_context(patch.object(runtime, "_record", capture(runtime._record, worker)))
    stack.enter_context(patch.object(listing_run, "delegate_tasks", delegate))


def case_seed(owner, other, capacity, expected):
    from backend.database import database, listing_operations as listings

    with closing(database.get_db()) as conn, conn:
        target = listings.seed_test_product(conn, owner)
        listings.seed_test_product(conn, owner, sku="TASK42-CONTROL")
        listings.seed_test_product(conn, other, shop_id="t1-foreign")
        if expected == "ambiguous":
            listings.seed_test_product(conn, owner, shop_id="t1-secondary")
        key = tuple(target.values())
        facts = json.loads(conn.execute("SELECT facts FROM listing_test_facts WHERE shop_id=? AND marketplace_id=? AND sku=?",
                                        key).fetchone()[0])
        original = json.loads(json.dumps(facts))
        if expected == "missing":
            facts = facts[:4]
        elif capacity != "500 mL":
            fact = facts[0]
            assert fact["fact_id"] == "capacity"
            fact["statement"] = fact["statement"].replace("500 mL", capacity)
            fact["options"] = [s.replace("500 mL", capacity) for s in fact["options"]]
            assert [row["fact_id"] for old, row in zip(original, facts) if old != row] == ["capacity"]
        conn.execute("UPDATE listing_test_facts SET facts=? WHERE shop_id=? AND marketplace_id=? AND sku=?",
                     (json.dumps(facts), *key))
        return target


def edge_round(origin, client, folder, password, path, before):
    started = monotonic()
    env = dict(os.environ, AD_TASK42_USERNAME="t1-operator", AD_TASK42_PASSWORD=password,
               AD_TASK42_SQLITE=str(path), AD_TASK42_NATURAL="1", AD_TASK42_MESSAGE=FULL)
    response = subprocess.run(["node", "tests/task42_listing_browser.cjs", origin, str(folder)],
                              env=env, capture_output=True, timeout=120, check=False)
    browser_path = folder / "listing-browser.json"
    report = read(browser_path) if browser_path.exists() else {}
    result = edge_record(report, before)
    result.update(browser_exit=response.returncode, browser_seconds=round(monotonic() - started, 3))
    if report.get("request"):
        base = GROUPS + "/" + report["request"]["conversation_id"]
        # The browser login rotates the initial API session.
        headers = login(client, "t1-operator", password)
        result.update(edge_history(client, headers, base))
    return result


def edge_record(report, before):
    result = {"browser": report, "events": report.get("events", []),
              "request": report.get("request"), "evidence": report.get("evidence")}
    if "sqlBefore" in report:
        browser_sql = report["sqlBefore"]
        products = sorted([browser_sql["row"], *browser_sql["otherProducts"]],
                          key=lambda r: (r["shop_id"], r["marketplace_id"], r["sku"]))
        result["before_confirm"] = {**before, "listing_test_products": products,
                                    "listing_change_receipts": browser_sql["receipts"]}
    if "readback" in report:
        result["get"] = {"status": 200, "body": {"data": report["readback"]}}
        result["approvals"] = [{"status": 200, "body": {"data": report["approval"]}}]
    return result


def edge_history(client, headers, base):
    response = client.get(base + "/history", headers=headers)
    history = body(response) or {}
    entry = next((e for e in history.get("entries", []) if e["kind"] == "assistant"), {})
    return {"history": history, "history_status": response.status_code,
            "action_count": len(entry.get("listing_actions") or [])}


def workgroups(root, live, routing=False):
    import httpx
    from dotenv import dotenv_values

    if live:
        assert read(root / "fixed" / "summary.json")["passed"], "Fixed-model gate failed"
        assert all(x["passed"] for x in read(root / "olist" / "chart-api.json").values())
        assert read(root / "olist" / "chart-browser.json")["passed"]
        assert read(root / "original-fixed-replay" / "listing-audit.json")["passed"]
        assert "\nOK" in (root / "original-python-replay.log").read_text(encoding="utf-8")
    output = (root / ("live" if live else "routing" if routing else "fixed")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    configure(output / "bootstrap.sqlite3", "glm" if live else "mock")
    if live:
        os.environ["GLM_MODEL"] = "glm-4.5-air"
        if not os.environ.get("GLM_API_KEY"):
            key = dotenv_values(".env").get("GLM_API_KEY")
            assert key, "Local GLM credential unavailable"
            os.environ["GLM_API_KEY"] = key
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.captcha import captcha
    from backend.database import database, listing_operations as listings
    from backend.memory import conversation_memory
    from scripts.run_real_diagnosis import Redactor
    from scripts.run_task27_t5 import GLMAudit
    from tests.home_diagnosis_server import demo_captcha
    from tests.test_listing_workgroup import ListingModel

    password, foreign = token_urlsafe(24), token_urlsafe(24)
    redactor = Redactor([password, foreign, os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY")])
    provider = ProviderFactory.create() if live else ListingModel()
    audit = GLMAudit(output / "glm-sdk-ledger.sqlite3", redactor, uuid4().hex, 48) if live else None
    if audit:
        audit.run_cap = 48
        audit.enabled = True
        hooks = provider._client._client.event_hooks
        hooks["request"].append(audit.request)
        hooks["response"].append(audit.response)
    base_cases = [("sales", "simple", "none", "500 mL", SIMPLE),
                  ("full", "direct" if live else "delegate", "edge" if live else "concurrent", "500 mL", FULL),
                  ("changed", "direct", "approve" if live else "none", "750 mL", FULL),
                  ("missing", "missing", "none", "500 mL", FULL)]
    cases = base_cases if live else base_cases + [
        ("unsupported", "unsupported", "none", "500 mL", FULL),
        ("ambiguous", "ambiguous", "none", "500 mL", FULL),
        ("stale", "direct", "stale", "500 mL", FULL),
        ("receipt_fault", "direct", "fault", "500 mL", FULL),
    ]
    if routing:
        cases = [("sales", "simple", "none", "500 mL", ORIGINAL_SIMPLE)]
    results = {}
    with ExitStack() as stack:
        if audit:
            stack.callback(provider._client.close)
        stack.enter_context(patch.object(ProviderFactory, "create", return_value=provider))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        stack.enter_context(patch.object(conversation_memory, "store_conversation", return_value=None))
        from fast_server import app

        for name, expected, mode, capacity, message in cases:
            folder = output / name
            folder.mkdir()
            path = folder / "sandbox.sqlite3"
            if audit:
                audit.case = name
            else:
                provider.mode = expected
            captures = {"reads": [], "receipts": [], "batches": [], "sdk": []}
            report = {"case": name, "expected": expected, "mode": mode, "capacity": capacity, "events": []}
            with ExitStack() as turn:
                turn.enter_context(patch.object(database.settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(path)))
                for attr, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                                       ("FUNC_LOG_FILE", "functions.jsonl")):
                    turn.enter_context(patch.object(observability, attr, folder / filename))
                database.init_db()
                with closing(database.get_db()) as conn, conn:
                    owner = conn.execute("INSERT INTO users(username,password) VALUES (?,?)",
                                         ("t1-operator", database.hash_password(password))).lastrowid
                    other = conn.execute("INSERT INTO users(username,password) VALUES (?,?)",
                                         ("t1-foreign", database.hash_password(foreign))).lastrowid
                target = case_seed(owner, other, capacity, expected)
                report.update(target=target, user_id=owner, sql_oracle=oracle(path, target), sql_before=snapshot(path))
                capture_runtime(turn, captures, listings, provider, audit)
                started = monotonic()
                try:
                    with serve(app) as origin, httpx.Client(base_url=origin, timeout=100, trust_env=False) as client:
                        headers = login(client, "t1-operator", password)
                        other_headers = login(client, "t1-foreign", foreign)
                        begin = monotonic()
                        if mode == "edge":
                            report.update(edge_round(origin, client, folder, password, path, report["sql_before"]))
                        else:
                            report.update(http_round(client, headers, name, message))
                            report["sse_seconds"] = round(monotonic() - begin, 3)
                            finish_http(client, headers, other_headers, report, path, mode)
                        report["interaction_seconds"] = round(monotonic() - begin, 3)
                except Exception as error:
                    report["exception"] = redactor.clean(f"{type(error).__name__}: {error}")
                report["seconds"] = round(monotonic() - started, 3)
                report["sql_after"] = snapshot(path)
            sdk = [row for row in audit.rows() if row["case"] == name] if audit else captures.pop("sdk")
            traces = read_traces(folder)
            try:
                checks = score(report, expected, sdk, traces, captures, live)
            except (KeyError, ValueError, TypeError, StopIteration, IndexError) as error:
                checks = [{"stage": "证据交接", "check": f"scoring data missing: {error}", "passed": False}]
            failures = [r for r in checks if not r["passed"]]
            stage_order = ["取数", "选路", "证据交接", "草案", "执行"]
            workers = sum(s["stage"] == "worker" and s["action"] == "created" for t in traces for s in t["steps"])
            requested_tools = [call["function"]["name"] for r in sdk if r.get("step") == "listing_operations"
                               for call in model_calls(r)]
            runtime_reads = {s["runtime_call_id"]: s["action"] for t in traces for s in t["steps"]
                             if s["stage"] == "business_read" and s["action"] in READ_KEYS}
            runtime_reads.update({r["tool_call_id"]: r["source"] for r in captures["receipts"]
                                  if r["source"] in READ_KEYS and r.get("task_id") and not r.get("retry_of")})
            business_reads = Counter(runtime_reads.values())
            report.update(checks=checks, passed=not failures and not report.get("exception"),
                          first_deviation=min((r["stage"] for r in failures), key=stage_order.index, default=None),
                          sdk_count=len(sdk), workers=workers, requested_tools=requested_tools,
                          business_reads=dict(business_reads), reader_invocations=len(captures["reads"]))
            write(folder / "round.json", redactor.clean(report))
            write(folder / "sdk.json", redactor.clean(sdk))
            write(folder / "runtime.json", redactor.clean(captures))
            keys = ("passed", "first_deviation", "seconds", "interaction_seconds", "sse_seconds", "sdk_count", "workers",
                    "requested_tools", "business_reads", "reader_invocations", "exception")
            results[name] = {key: report[key] for key in keys if key in report}
            print(name, json.dumps(results[name], ensure_ascii=False), flush=True)
        sdk_rows = audit.rows() if audit else []
        if audit:
            write(output / "sdk.json", sdk_rows)
        comparison = {} if routing else compare_facts(output)
        passed = all(r["passed"] for r in results.values()) and (routing or comparison_passed(comparison))
        write(output / "summary.json", {"passed": passed, "cases": results, "fact_comparison": comparison,
              "glm_sdk_attempts": len(sdk_rows), "sdk_limit": 48 if live else 0})
    return int(not passed)


def compare_facts(output):
    def facts(report):
        return json.loads(next(r["facts"] for r in report["sql_before"]["listing_test_facts"]
                               if r["shop_id"] == "task42-test" and r["sku"] == SKU))

    first, second = (read(output / name / "round.json") for name in ("full", "changed"))
    return {"changed_facts": [a["fact_id"] for a, b in zip(facts(first), facts(second), strict=True) if a != b],
            "same_request": (first.get("request") or {}).get("message") ==
            (second.get("request") or {}).get("message") == FULL}


def comparison_passed(comparison):
    return comparison["changed_facts"] == ["capacity"] and comparison["same_request"]


def read_traces(folder):
    path = folder / "traces.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def verify_live(root):
    """Recover the independent history check for an already finished Edge run without resending it."""
    import httpx
    from shutil import copyfile

    live = root / "live"
    folder = live / "full"
    original = read(folder / "round.json")
    browser = read(folder / "listing-browser.json")
    assert browser["passed"], "Edge itself must have completed"
    assert original.get("exception") == "TypeError: 'NoneType' object is not subscriptable"
    # Use an SQLite backup that includes WAL, then provision an audit login on that copy.
    path = (folder / "history-verification.sqlite3").resolve()
    assert not path.exists()
    with connection(folder / "sandbox.sqlite3") as source, closing(sqlite3.connect(path)) as dest:
        source.backup(dest)
    configure(path)
    from backend.database import database
    from fast_server import app

    password = token_urlsafe(24)
    with closing(database.get_db()) as conn, conn:
        conn.execute("UPDATE users SET password=? WHERE username='t1-operator'", (database.hash_password(password),))
    restored = edge_record(browser, original["sql_before"])
    base = GROUPS + "/" + browser["request"]["conversation_id"]
    with serve(app) as origin, httpx.Client(base_url=origin, timeout=20, trust_env=False) as client:
        restored.update(edge_history(client, login(client, "t1-operator", password), base))
    report = {**original, **restored}
    report.pop("exception")
    assert snapshot(path) == original["sql_after"], "Copied listing data drifted"
    report["checks"] = score(report, report["expected"], read(folder / "sdk.json"), read_traces(folder),
                             read(folder / "runtime.json"), live=True)
    report["passed"] = all(check["passed"] for check in report["checks"])
    report["first_deviation"] = next((check["stage"] for check in report["checks"] if not check["passed"]), None)
    summary = read(live / "summary.json")
    copyfile(folder / "round.json", folder / "round-initial.json")
    copyfile(live / "summary.json", live / "summary-initial.json")
    write(folder / "round.json", report)
    summary["cases"]["full"].pop("exception")
    summary["cases"]["full"].update(passed=report["passed"], first_deviation=report["first_deviation"])
    summary["fact_comparison"] = compare_facts(live)
    summary["passed"] = all(case["passed"] for case in summary["cases"].values()) and comparison_passed(summary["fact_comparison"])
    write(live / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return int(not summary["passed"])
