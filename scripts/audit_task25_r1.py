"""Split unchanged T1 evidence by case and independently check R1 release gates."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from scripts.verify_task25_t1 import dump, marker, read, write


def audit(root, fixed):
    baseline = Path("logs/task25-t1/main-replay-20261112-170331")
    old = read(baseline / "report.json")
    assert old["cases"] == 16 and set(old["failed"]) == {"ttl_edge", "ttl_invalid", "legacy_mismatch"}
    current = read(root / "report.json")
    assert current["status"] == "passed" and current["cases"] == 16 and not current["failed"]
    assert read(fixed / "report.json")["passed"]
    timezone_reports = [read(fixed / tz / "report.json") for tz in ("UTC0", "PST8PDT")]
    assert all(row["passed"] and row["executed"] == 48 and row["skipped"] == 0 for row in timezone_reports)
    assert [row["local_epoch"] for row in timezone_reports] == ["1970-01-01T00:00:00", "1969-12-31T16:00:00"]
    cases = read(root / "cases.json")
    assert cases.keys() == read(baseline / "cases.json").keys()
    events = [json.loads(line) for line in (root / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    traces = [json.loads(line) for line in (root / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    seeds = read(root / "seed.json")
    denied = {
        "restart": ("sibling", "other"), "sibling": ("origin", "other"), "other": ("origin", "sibling"),
        "foreign_group": ("sibling",), "foreign_owner": ("other",), "ttl_edge": ("ttl_edge",),
        "ttl_expired": ("ttl_expired",), "ttl_invalid": ("ttl_invalid",),
        "group_mismatch": ("origin", "partner"), "legacy_mismatch": ("legacy_first", "legacy_second"),
        "no_hit": ("origin", "sibling", "other"),
        **{f"filter_failure_{scope}": ("origin", "sibling", "other", "legacy_first", "legacy_second")
           for scope in ("group", "legacy")},
    }
    allowed = {"restart": ("origin",), "sibling": ("sibling",), "other": ("other",), "ttl_valid": ("ttl_valid",)}
    results = {}
    for name, case in cases.items():
        assert case["passed"]
        label = name if name.startswith("write_failure_") else "recall_" + name
        sdk = [event for event in events if event["case"] == label and event["kind"] == "sdk"]
        assert sdk
        messages = [m for request in sdk for m in request["request"]["messages"]]
        denied_counts = {marker(source, role): dump(messages).count(marker(source, role))
                         for source in denied.get(name, ()) for role in ("Q", "A")}
        assert not any(denied_counts.values())
        trace_id = case.get("trace_id") or case["trace"]
        trace, = [trace for trace in traces if trace["trace_id"] == trace_id]
        assert trace["status"] == "completed"
        sse, = [event for event in events if event["case"] == label and event["kind"] == "sse"]
        assert [e["type"] for e in sse["events"] if e["type"] in {"done", "error", "stopped", "superseded"}] == ["done"]
        alignment = None
        if call_id := case.get("tool_call_id"):
            first, = [i for i, item in enumerate(sdk) if any(c["id"] == call_id for c in item["response"]["tool_calls"])]
            after, = [i for i, item in enumerate(sdk)
                       if any(m.get("tool_call_id") == call_id for m in item["request"]["messages"])]
            assert first < after
            receipt, = [m for m in sdk[after]["request"]["messages"] if m.get("tool_call_id") == call_id]
            assert receipt["role"] == "tool" and receipt == case["tool_receipt"]
            steps = [step for step in trace["steps"] if step.get("tool_call_id") == call_id]
            assert [step["stage"] for step in steps] == ["tool_call", "tool_result"]
            alignment = {"tool_call_id": call_id, "sdk_before": first, "sdk_after": after,
                         "trace_id": trace_id, "stages": [step["stage"] for step in steps]}
        positive_roles = {marker(source, role): sorted({m["role"] for m in messages if marker(source, role) in dump(m)})
                          for source in allowed.get(name, ()) for role in ("Q", "A")}
        # T1 removes group A's source turns from recent history. Groups B/C are
        # same-scope controls whose own source remains legitimate recent history.
        if name in ("restart", "ttl_valid"):
            assert all(roles == ["tool"] for roles in positive_roles.values())
        else:
            assert all("tool" in roles for roles in positive_roles.values())
        path = root / "per-case" / name
        path.mkdir(parents=True, exist_ok=True)
        write(path / "sdk-requests.json", sdk)  # Complete SDK payloads; only synthetic fixture text.
        write(path / "trace.json", trace)
        write(path / "sse.json", sse)
        results[name] = {"passed": True, "denied_marker_counts": denied_counts,
                         "positive_marker_roles": positive_roles, "alignment": alignment}
        write(path / "audit.json", results[name])
    with closing(sqlite3.connect((root.resolve() / "isolated.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        pairs = [dict(row) for row in conn.execute("""SELECT t.*,q.role AS question_role,a.role AS answer_role,
            q.user_id AS question_owner,a.user_id AS answer_owner,q.content AS question,a.content AS answer
            FROM conversation_turns t JOIN conversations q ON q.id=t.user_message_id
            JOIN conversations a ON a.id=t.assistant_message_id ORDER BY t.assistant_message_id""")]
        assistants = conn.execute("SELECT count(*) FROM conversations WHERE role='assistant'").fetchone()[0]
    assert len(pairs) == assistants and all(row["question_role"] == "user" and row["answer_role"] == "assistant"
                                          and row["question_owner"] == row["answer_owner"] == row["user_id"]
                                          and row["user_message_id"] < row["assistant_message_id"] for row in pairs)
    write(root / "committed-pairs.json", pairs)
    encoded = [event for event in events if event["kind"] == "encode"]
    assert all(event["shape"][1] == 512 and event["normalized"] for event in encoded)
    life = [e for e in events if e["kind"] in {"process_started", "process_stopped"}]
    assert len(life) == 4 and life[0]["pid"] == life[1]["pid"] != life[2]["pid"] == life[3]["pid"]
    assert life[0]["revision"] == life[2]["revision"] == "7999e1d3359715c523056ef9478215996d62a620"
    files = [Path("scripts/verify_task25_t1.py"), Path("locks/bge.txt"), baseline / "report.json", baseline / "cases.json"]
    report = {"passed": True, "real_bge": {"cases": len(cases), "write_encodes": sum(e["operation"] == "add" for e in encoded),
                                          "query_encodes": sum(e["operation"] == "query" for e in encoded),
                                          "dimension": 512, "server_pids": [life[0]["pid"], life[2]["pid"]]},
              "fixed": {"per_timezone": [r["executed"] for r in timezone_reports],
                        "local_epochs": [r["local_epoch"] for r in timezone_reports], "t1_cases": 16},
              "real_glm_requests": 0, "mysql_live_cases": 0, "committed_ordinary_pairs": len(pairs),
              "baseline": {"passed": 13, "total": 16, "preserved": True},
              "sha256": {str(path).replace("\\", "/"): sha256(path.read_bytes()).hexdigest() for path in files},
              "cases": results}
    write(root / "r1-audit.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--fixed-dir", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.evidence_dir, args.fixed_dir)
    print(dump({key: value for key, value in report.items() if key != "cases"}))
