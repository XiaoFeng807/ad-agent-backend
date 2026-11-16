"""Join the G2 browser samples, Envoy attempts, application Trace and independent SQL."""

import argparse
from collections import Counter
from math import ceil
from pathlib import Path

from scripts.accept_task42_t1 import read, write
from scripts.audit_storepilot_gateway import CONTROL_HEADERS, audit as audit_g1, json_lines


REGRESSIONS = ("normal", "client-abort", "upstream-abort", "lost-done", "rollback")


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[ceil(len(ordered) * fraction) - 1], 3) if ordered else None


def peak(slots):
    points = []
    for slot in slots:
        acquired, released = slot["acquired_ns"], slot["released_ns"]
        assert released is not None and acquired < released, slot
        points.extend(((acquired, 1), (released, -1)))
    active = maximum = 0
    for _, change in sorted(points):
        active += change
        maximum = max(maximum, active)
    assert not active
    return maximum


def step(trace, stage, action):
    [result] = [row for row in trace["steps"] if (row["stage"], row["action"]) == (stage, action)]
    return result


def assert_gateway(access, packet, route, *, gateway=None, attempts=1):
    row = access[packet["request_id"]]
    assert row["status"] == packet["status"] and row["route"] == route, row
    assert int(row["upstream_attempts"] or 0) == attempts, row
    if gateway is not None:
        assert row["gateway"] == str(8080 + gateway), (packet, row)
    return row


def audit_round(folder, access):
    plan = read(folder / "plan.json")
    browser = read(folder / "browser.json")
    assert not browser.get("failure") and not browser["page_errors"], browser.get("failure") or browser["page_errors"]
    sql = read(folder / "sql-after.json")
    journal = json_lines(folder / "transport.jsonl")
    traces = {row["request_id"]: row for row in json_lines(folder / "traces.jsonl") if "request_id" in row}
    entries = Counter(row["request_id"] for row in journal if row["event"] == "fastapi-entry")
    assert not any(CONTROL_HEADERS.intersection(row["controls"]) for row in journal if row["event"] == "fastapi-entry")
    slots = {row["run_id"]: row for row in sql["slots"]}
    assert {row["run_id"] for row in sql["runs"]} == set(slots), sql["runs"]
    global_peak = peak(slots.values())
    user_peaks = {user: peak(row for row in slots.values() if row["user_id"] == user)
                  for user in {row["user_id"] for row in slots.values()}}
    if plan["mode"] == "B":
        assert global_peak <= 3 and max(user_peaks.values(), default=0) <= 2, (global_peak, user_peaks)

    if plan["case"] == "rate":
        assert not sql["runs"] and not read(folder / "model-responses.json")
        denials = [row for row in browser["rate"] if row["status"] == 429]
        for packet in browser["rate"]:
            denied = packet["status"] == 429
            assert_gateway(access, packet, "chat-stream", gateway=packet["gateway"], attempts=0 if denied else 1)
            assert entries[packet["request_id"]] == (0 if denied else 1)
            if denied:
                trace = traces[packet["request_id"]]
                assert trace["source"] == "gateway_start_rate" and trace["status"] == "rejected"
                assert step(trace, "admission", "rejected")["error_code"] == "account_rate_exceeded"
        result = {"rate_rejected": len(denials), "rate_429_p95_ms": percentile([row["ms"] for row in denials], .95),
                  "rate_attempts": len(browser["rate"]), "browser": browser["browser"]}
        write(folder / "joined.json", result)
        return result

    def validate_accepted(trace, run_id):
        slot = slots[run_id]
        occupied = step(trace, "admission", "occupied")
        released = step(trace, "admission", "released")
        assert occupied["runtime_call_id"] == released["runtime_call_id"] == run_id
        assert occupied["evidence"]["acquired_ns"] == slot["acquired_ns"]
        assert released["evidence"]["released_ns"] == slot["released_ns"]
        model_calls = [row for row in trace["steps"] if row["stage"] == "model_call"]
        assert model_calls and occupied["epoch_ns"] <= model_calls[0]["epoch_ns"], trace
        workers = {row["runtime_call_id"]: row for row in trace["steps"]
                   if (row["stage"], row["action"]) == ("worker", "created")}
        reclaimed = {row["runtime_call_id"]: row for row in trace["steps"]
                     if (row["stage"], row["action"]) == ("worker", "reclaimed")}
        assert workers and workers.keys() == reclaimed.keys(), trace
        for key in workers:
            assert workers[key]["epoch_ns"] <= reclaimed[key]["epoch_ns"] <= slot["released_ns"], trace

    agents = {row["name"]: row for row in browser["agents"]}
    accepted = [row for row in agents.values() if row["status"] == 200]
    rejected = [row for row in agents.values() if row["status"] == 429]
    assert len(accepted) + len(rejected) == len(agents)
    expected_runs = {row["run_id"] for row in accepted}
    if "seed_request_id" in browser:
        seed_trace = traces[browser["seed_request_id"]]
        seed_run = browser["action"]["run_id"]
        validate_accepted(seed_trace, seed_run)
        expected_runs.add(seed_run)
    assert set(slots) == expected_runs
    for agent in agents.values():
        request_id = agent["request_id"]
        assert entries[request_id] == 1, agent
        assert_gateway(access, agent, "chat-stream", gateway=agent["gateway"])
        trace = traces[request_id]
        if agent["status"] == 429:
            assert trace["status"] == "rejected" and len(trace["steps"]) == 1, trace
            assert step(trace, "admission", "rejected")["error_code"] == agent["body"]["detail"]
            assert not any(run["conversation_id"] == agent["conversation_id"] for run in sql["runs"])
        else:
            assert trace["trace_id"] == agent["trace_id"]
            validate_accepted(trace, agent["run_id"])

    result = {"mode": plan["mode"], "case": plan["case"], "browser": browser["browser"],
              "agent_accepted": len(accepted), "agent_rejected": len(rejected), "peak": global_peak,
              "account_peaks": user_peaks, "rejection_reasons": Counter(row["body"]["detail"] for row in rejected),
              "agent_429_p95_ms": percentile([row["ms"] for row in rejected], .95)}

    if plan["case"] == "lifecycle":
        verify_timelines(folder, browser, agents, traces, slots, access)
    if plan["case"] == "approval":
        result["approval"] = verify_approval(browser, traces, access, journal, sql)
    if plan["case"] == "mix":
        assert len(agents) == plan["agent_submissions"]
        assert len(accepted) == global_peak == (6 if plan["mode"] == "A" else 3)
        samples = browser["samples"]
        assert len(samples) == plan["get_ticks"] * plan["get_lanes"]
        assert all(count == 1 for count in Counter((row["tick"], row["gateway"], row["kind"]) for row in samples).values())
        for sample in samples:
            if sample.get("request_id"):
                assert_gateway(access, sample, "query", gateway=sample["gateway"])
            if sample["status"] == 200 and sample["kind"] == "receipt":
                receipt = step(traces[sample["request_id"]], "listing_action", "read")["evidence"]
                assert receipt["receipt_id"] == browser["ui_readback"]["body"]["data"]["receipt"]["receipt_id"]
        successful = [row["ms"] for row in samples if row["status"] == 200]
        get_success_rate = len(successful) / len(samples)
        result.update(get_count=len(samples), get_success=len(successful), get_success_rate=get_success_rate,
                      get_error_rate=1 - get_success_rate, get_p95_ms=percentile(successful, .95),
                      get_statuses=Counter(row["status"] for row in samples),
                      get_schedule_lag_p95_ms=percentile([row["lag_ms"] for row in samples if "lag_ms" in row], .95))
        result["get_p95_by_kind_ms"] = {kind: percentile([row["ms"] for row in samples
                                                          if row["status"] == 200 and row["kind"] == kind], .95)
                                        for kind in ("history", "receipt")}
        assert_gateway(access, browser["ui_readback"], "query", gateway=0)
        assert_gateway(access, browser["evidence"], "query", gateway=1)
        assert len(successful) == len(samples), result
    write(folder / "joined.json", result)
    return result


def verify_timelines(folder, browser, agents, traces, slots, access):
    def node(name, stage, action):
        agent = agents[name]
        trace = traces[agent["request_id"]]
        event = step(trace, stage, action)
        return {"event": name, "stage": stage, "action": action, "epoch_ns": event["epoch_ns"],
                "gateway": agent["origin"], "request_id": agent["request_id"],
                "run_id": agent.get("run_id"), "trace_id": trace["trace_id"],
                **({"runtime_call_id": event["runtime_call_id"]} if "runtime_call_id" in event else {}),
                **({"error_code": event["error_code"]} if "error_code" in event else {})}

    winner, loser = sorted(("race-beta", "race-gamma"), key=lambda name: agents[name]["status"])
    fairness = [node(name, "admission", action) for name, action in (
        ("alpha-tool", "occupied"), ("alpha-second", "occupied"), ("alpha-over", "rejected"),
        ("beta-progress", "occupied"), ("beta-progress", "released"), (winner, "occupied"), (loser, "rejected"),
    )]
    progress = agents["beta-progress"]["progress"]
    fairness.append({**fairness[3], "stage": "browser", "action": "thinking", "epoch_ns": progress["epoch_ms"] * 1_000_000})
    drain = [node(name, stage, action) for name, stage, action in (
        ("alpha-tool", "transport", "disconnected"), ("alpha-while-tool-drains", "admission", "rejected"),
        ("alpha-tool", "fixture_call", "tool_exited"), ("alpha-tool", "worker", "reclaimed"),
        ("alpha-tool", "admission", "released"), ("alpha-reentry-error", "admission", "occupied"),
        ("alpha-reentry-error", "admission", "released"),
    )]
    assert all(left["epoch_ns"] < right["epoch_ns"] for left, right in zip(drain, drain[1:])), drain
    first = agents["alpha-tool"]
    assert access[first["request_id"]]["code_details"] == "downstream_remote_disconnect"
    trace = traces[first["request_id"]]
    worker_id = drain[2]["runtime_call_id"]
    assert drain[3]["runtime_call_id"] == worker_id and trace["status"] == "interrupted"
    [late] = [row for row in trace["steps"] if row["stage"] == "tool_result" and row.get("parent_call_id") == worker_id
              and row.get("error_code") == "expired_receipt"]
    assert drain[2]["epoch_ns"] <= late["epoch_ns"] <= drain[3]["epoch_ns"]
    assert {slot["run_id"] for slot in browser["full"]} == {
        agents[name]["run_id"] for name in ("alpha-tool", "alpha-second", "beta-progress")}
    assert slots[first["run_id"]]["released_ns"] < slots[agents["alpha-reentry-error"]["run_id"]]["acquired_ns"]
    write(folder / "timeline-fairness.json", sorted(fairness, key=lambda row: row["epoch_ns"]))
    write(folder / "timeline-drain.json", drain)


def verify_approval(browser, traces, access, journal, sql):
    post = browser["post"]
    manual = browser["manual_post"]
    first_access = assert_gateway(access, post, "approval-confirm", gateway=0)
    manual_access = assert_gateway(access, manual, "approval-confirm", gateway=0)
    before_manual = browser["entries_before_retry"]
    assert [row["request_id"] for row in before_manual] == [post["request_id"]]
    [cut] = [row for row in journal if row["event"] == "fault-triggered"]
    [commit] = [row for row in browser["events"] if row["event"] == "independent-sql-commit"]
    assert commit["epoch_ms"] * 1_000_000 <= int(cut["epoch_ns"])
    independent = browser["independent_get"]
    refresh = browser["refresh_get"]
    readback = independent["body"]["data"]["receipt"]
    assert manual["body"]["data"]["receipt"] == refresh["body"]["data"]["receipt"] == readback
    match = {"run_id": browser["action"]["run_id"], "action_id": readback["action_id"],
             "receipt_id": readback["receipt_id"]}
    assert step(traces[post["request_id"]], "listing_action", "applied")["evidence"] == match
    assert step(traces[manual["request_id"]], "listing_action", "replayed")["evidence"] == match
    assert traces[post["request_id"]]["trace_id"] == readback["execution_trace_id"]
    reads = []
    for gateway, packet in ((1, independent), (0, refresh)):
        reads.append(assert_gateway(access, packet, "query", gateway=gateway))
        assert step(traces[packet["request_id"]], "listing_action", "read")["evidence"] == {
            **match, "execution_trace_id": readback["execution_trace_id"]}
    def keyed(rows):
        return {(row["shop_id"], row["marketplace_id"], row["sku"]): row for row in rows}

    before = keyed(browser["sqlBeforeApproval"]["products"])
    after = keyed(browser["sqlAtCut"]["products"])
    assert after == keyed(sql["products"]) and before.keys() == after.keys()
    target = browser["pending"]["body"]["data"]["proposal"]["target"]
    target_key = (target["shop_id"], target["marketplace_id"], target["sku"])
    assert {key for key in before if before[key] != after[key]} == {target_key}
    assert {key for key in before[target_key] if before[target_key][key] != after[target_key][key]} == {"version", "bullets"}
    assert after[target_key]["version"] == before[target_key]["version"] + 1
    assert browser["sqlAtCut"]["receipts"] == sql["receipts"] and len(sql["receipts"]) == 1
    return {**match, "post": first_access, "manual_post": manual_access, "readbacks": reads, "sql_before_cut": commit,
            "cut": cut, "trace_ids": {packet["request_id"]: traces[packet["request_id"]]["trace_id"]
                                      for packet in (post, independent, manual, refresh)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root
    gateway = json_lines(root / "envoy.log")
    access = {row["request_id"]: row for row in gateway}
    assert len(access) == len(gateway), "Duplicate Envoy gateway request ID"
    frozen = root / "frozen"
    manifest = read(frozen / "manifest.json")
    report = {case: audit_round(frozen / case, access) for case in ("rate", "lifecycle", "approval")}
    report["pairs"] = []
    for repeat in range(1, manifest["load"]["repeats"] + 1):
        a, b = (audit_round(frozen / f"mix-{repeat}-{mode}", access) for mode in ("A", "B"))
        report["pairs"].append({"repeat": repeat, "A": a, "B": b, "p95_b_minus_a_ms": round(b["get_p95_ms"] - a["get_p95_ms"], 3)})
    report["g1_regression"] = {}
    for case in REGRESSIONS:
        folder = root / "g1-regression" / "dual" / case
        report["g1_regression"][case] = audit_g1(folder, gateway)
        assert len(read(folder / "sql-after.json")["slots"]) == 1
    write(root / "g2-report.json", report)


if __name__ == "__main__":
    main()
