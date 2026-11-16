"""Join browser, Envoy, ASGI entry, Trace and SQLite evidence for StorePilot."""

import argparse
from collections import Counter
import json
from pathlib import Path

from scripts.accept_task42_t1 import read, write
from scripts.storepilot_gateway import CASES, PAUSE


CONTROL_HEADERS = {
    "x-envoy-internal", "x-envoy-retry-on", "x-envoy-retry-grpc-on", "x-envoy-max-retries",
    "x-envoy-retriable-header-names", "x-envoy-retriable-status-codes", "x-envoy-hedge-on-per-try-timeout",
    "x-envoy-upstream-rq-timeout-ms", "x-envoy-upstream-rq-per-try-timeout-ms", "x-envoy-upstream-rq-timeout-alt-response",
    "x-envoy-upstream-stream-duration-ms", "x-envoy-expected-rq-timeout-ms", "x-envoy-attempt-count", "grpc-timeout",
}


def json_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("{")]


def audit(folder, gateway):
    case = folder.name
    browser = read(folder / "browser.json")
    assert not browser.get("failure") and not browser["pageErrors"], browser.get("failure") or browser["pageErrors"]
    stream = browser["stream"]
    journal = json_lines(folder / "transport.jsonl")
    traces = {row["request_id"]: row for row in json_lines(folder / "traces.jsonl")
              if "request_id" in row and row.get("source") != "listing_readback"}
    entries = [row for row in journal if row["event"] == "fastapi-entry"]
    requests = {"chat-stream": [stream["request_id"]], "approval-confirm": [
        browser[name]["request_id"] for name in ("post", "manualPost") if name in browser]}
    status = {"chat-stream": [stream["status"]], "approval-confirm": [
        browser[name]["status"] for name in ("post", "manualPost") if name in browser]}
    all_ids = set(requests["chat-stream"] + requests["approval-confirm"])
    assert None not in all_ids and "" not in all_ids and len(all_ids) == sum(map(len, requests.values())), requests
    assert Counter(row["request_id"] for row in entries) == Counter(
        all_ids if case != "before-headers" else []), entries
    assert set(traces) == (all_ids if case != "before-headers" else set()), traces.keys()
    access = []
    query_access = []
    if gateway is not None:
        access = [row for row in gateway if row.get("request_id") in all_ids]
        assert len(access) == len(all_ids), access
        assert not any(CONTROL_HEADERS.intersection(row["controls"]) for row in entries), entries
        for route, ids in requests.items():
            for request_id, expected_status in zip(ids, status[route], strict=True):
                [row] = [row for row in access if row["request_id"] == request_id]
                assert row["route"] == route and row["method"] == "POST", row
                assert int(row["upstream_attempts"]) == 1 and int(row["status"]) == expected_status, row
        for name in ("history", "evidence", "pending", "independentGet", "refreshGet"):
            if name in browser:
                packet = browser[name]
                [row] = [row for row in gateway if row.get("request_id") == packet["request_id"]]
                assert row["method"] == "GET" and row["route"] == "query", row
                assert int(row["upstream_attempts"]) == 1 and int(row["status"]) == packet["status"], row
                query_access.append({"browser_record": name, **row})

    if case in {"lost-done", "lost-confirm"}:
        [commit] = [point for point in browser["points"] if point["event"] == "independent-sql-commit"]
        [cut] = [row for row in journal if row["event"] == "fault-triggered"]
        assert commit["epoch_ms"] * 1_000_000 <= int(cut["epoch_ns"]), (commit, cut)

    sse = traces.get(stream["request_id"])
    if sse:
        assert sse["trace_id"] == browser["trace_id"]
        assert any(step.get("runtime_call_id") == browser["run_id"] for step in sse["steps"])
        completed = case not in PAUSE or case == "normal"
        expected_trace_status = "error" if case == "max-duration" else "completed" if completed else "interrupted"
        assert sse["status"] == expected_trace_status, sse["status"]
        slots = read(folder / "sql-after.json").get("slots", [])
        if slots:
            [slot] = slots
            assert slot["run_id"] == browser["run_id"] and slot["released_ns"] > slot["acquired_ns"]
            [release] = [step for step in sse["steps"] if step["stage"] == "admission" and step["action"] == "released"]
            assert release["evidence"]["released_ns"] == slot["released_ns"]
        workers = Counter(step["runtime_call_id"] for step in sse["steps"] if step["stage"] == "worker"
                          and step["action"] == "created")
        reclaimed = Counter(step["runtime_call_id"] for step in sse["steps"] if step["stage"] == "worker"
                            and step["action"] == "reclaimed")
        if not completed:
            assert workers and workers == reclaimed, (workers, reclaimed)
            expected_error = "deadline_exceeded" if case == "max-duration" else "run_cancelled"
            assert any(step.get("error_code") == expected_error for step in sse["steps"])
            assert len([row for row in journal if row["event"] == "model-released"]) == 1
            assert not browser["after"]["answers"] and not browser["after"]["proposals"]
            assert browser["after"]["products"] == browser["before"]["products"]
        if gateway is not None and case in {"idle-timeout", "max-duration"}:
            [row] = access
            reason = "stream_idle_timeout" if case == "idle-timeout" else "max_duration_timeout"
            assert row["code_details"] == reason, row
            expected_ms = 5000 if case == "idle-timeout" else 105000
            assert expected_ms - 500 <= int(row["duration_ms"]) <= expected_ms + 5000, row
            if case == "max-duration":
                assert sum(row["event"] == "test-heartbeat" for row in journal) >= 80

    for index, request_id in enumerate(requests["approval-confirm"]):
        trace = traces[request_id]
        if case == "rollback":
            assert trace["status"] == "error"
            expected_action = "rolled_back"
        else:
            assert trace["status"] == "completed"
            expected_action = "replayed" if index else "applied"
        [step] = [step for step in trace["steps"] if step["action"] == expected_action]
        assert step["evidence"]["action_id"] == browser["action_id"]
        assert step["evidence"]["run_id"] == browser["run_id"]
        if case != "rollback":
            receipt = browser["independentGet"]["body"]["data"]["receipt"]
            assert step["evidence"]["receipt_id"] == receipt["receipt_id"]
            assert (trace["trace_id"] == receipt["execution_trace_id"]) == (not index)

    frames = stream["frames"]
    progress_ms = next((row["ms"] for row in frames if row["event"]["type"] == "thinking"), None)
    done_ms = next((row["ms"] for row in frames if row["event"]["type"] == "done"), None)
    gaps = [right["ms"] - left["ms"] for left, right in zip(frames, frames[1:])]
    receipt = browser.get("independentGet", {}).get("body", {}).get("data", {}).get("receipt") or {}
    result = {"passed": True, "message": browser["message"], "request_id": stream["request_id"],
              "headers_ms": stream["headers_ms"], "progress_ms": progress_ms, "done_ms": done_ms,
              "longest_frame_gap_ms": max(gaps, default=None), "timing": browser["points"],
              "ui_error": browser.get("ui_error"), "gateway": access, "query_gateway": query_access, "fastapi_entries": entries,
              "trace_ids": {key: trace["trace_id"] for key, trace in traces.items()},
              "run_id": browser.get("run_id"), "action_id": browser.get("action_id"),
              "receipt_id": receipt.get("receipt_id"),
              "transport": [row for row in journal if row["event"] != "test-heartbeat"]}
    write(folder / "joined.json", result)
    return result


def business_projection(folder):
    browser = read(folder / "browser.json")
    evidence = browser["evidence"]["body"]["data"]
    products = browser["after"]["products"]
    return {"answer": browser["after"]["answers"][0]["content"], "sales": evidence["metrics"],
            "products": products, "bullets": evidence["proposal"]["bullets"],
            "approval": browser["independentGet"]["body"]["data"]["state"],
            "seed": read(folder / "environment.json")["seed_copy_sha256"],
            "replay": read(folder / "model-responses.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--direct", default="direct")
    parser.add_argument("--gateway", default="envoy")
    parser.add_argument("--gateway-log", type=Path, help="All attempts in one VM log; defaults to case-local envoy.log")
    parser.add_argument("--direct-only", action="store_true")
    args = parser.parse_args()
    direct = args.root / args.direct / "normal"
    report = {"direct": audit(direct, None)}
    write(args.root / "direct-report.json", report["direct"])
    if not args.direct_only:
        report["gateway"] = {}
        for case in CASES:
            folder = args.root / args.gateway / case
            report["gateway"][case] = audit(folder, json_lines(args.gateway_log or folder / "envoy.log"))
        report["same_business_result"] = business_projection(direct) == business_projection(args.root / args.gateway / "normal")
        assert report["same_business_result"], "A/B business or replay results differ"
        write(args.root / "gateway-report.json", report)


if __name__ == "__main__":
    main()
