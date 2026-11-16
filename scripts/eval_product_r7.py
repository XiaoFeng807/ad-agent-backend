"""Verify the default product SSE path, explicit rollback, and timeout recovery."""

import argparse
import json
import os
import subprocess
import sys
from contextlib import closing, nullcontext
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch

from backend import observability
from backend.database import database
from backend.rag import product_chat_search, product_evidence as rag
from backend.rag.product_rerank_model import Reranker
from scripts.eval_product_chat_rerank import CITATION, TraceReader
from scripts.eval_product_query_expansion_r4 import read_scope
from scripts.eval_product_r5 import freeze, loopback, request, timeouts
from scripts.eval_product_r6 import MISSING, Meter, append, candidates, inspect, scope_probes
from scripts.eval_product_rerank import copy_catalog
from scripts.product_evidence import p95, write_json
from scripts.verify_product_evidence import CHUNKS, login

ROOT = Path(__file__).resolve().parents[1]
MODES = ("default", "env-zero", "dotenv-zero", "restarted")


def committed(full_trace):
    ids, = [s["evidence"]["final_citation_ids"] for s in full_trace["steps"] if s["action"] == "save_reply"]
    return ids


def sample(client, headers, q, traces, meter, group, allowed, raw, repeat, order):
    before = meter.counts()
    row = request(client, headers, q)
    trace = traces.read(row["trace_id"])
    usage = {key: value - before[key] for key, value in meter.counts().items()}
    ids, cites = candidates(trace), CITATION.findall(row["reply"])
    timing = {"retrieval_ms": trace["retrieval_ms"], "done_ms": row["elapsed_ms"]}
    final = committed(traces.read(row["trace_id"], full=True))
    append(raw, {"id": q["id"], "group": group, "repeat": repeat, "order": order, **timing, **usage,
                 "trace_id": row["trace_id"], "path": trace["path"], "candidate_ids": ids,
                 "final_citation_ids": final, "fallback_reason": trace.get("fallback_reason")})
    assert not any(value for key, value in usage.items() if key.startswith("gpu_")), (q["id"], usage)
    assert trace["scored_chunks"] == 0 and not trace.get("fallback_reason"), trace
    assert trace["cpu_path"] == trace["path"] == ("r4_cpu" if group == "B" else "default"), trace
    assert len(ids) == len(set(ids)) and set(cites) <= set(ids) <= allowed, (q["id"], ids, cites)
    assert cites == final == trace.get("citation_ids", []), (q["id"], trace, final, cites)
    if q["access"]:
        assert usage["encoding_calls"] > 0, (q["id"], usage)
    else:
        assert trace["status"] == "denied" and not ids and not any(usage.values()), (q["id"], trace, usage)
    return {**row, "trace": trace, "usage": usage, "timing": timing}


def compare(client, headers, writer, uid, q, row, chunks, http_raw, group, expected):
    inspect(client, headers, writer, uid, q, row, chunks, http_raw, group)
    assert row["reply"] == expected["reply"] and row["complete"] == expected["complete"], (q["id"], group)
    assert candidates(row["trace"]) == candidates(expected["trace"]), (q["id"], group, "candidate order")
    assert [{k: v for k, v in cite.items() if k != "http_ms"} for cite in row["sources"]] == [
        {k: v for k, v in cite.items() if k != "http_ms"} for cite in expected["sources"]
    ], (q["id"], group, "citation order and location")


def latency(client, headers, writer, users, rows, traces, meter, raw, output, baseline):
    pairs = []
    for index, q in enumerate(row for row in rows if row["access"]):
        rag.activate(writer, q["target"], q["version"])
        _, allowed = read_scope(writer, users[q["principal"]], q)
        for repeat in range(1, 6):
            order = "AB" if (index * 5 + repeat) % 2 else "BA"
            pair = {"id": q["id"], "repeat": repeat, "order": order}
            for group in order:
                with patch.object(database.settings, "PRODUCT_R4_CPU_ENABLED", False) if group == "A" else nullcontext():
                    row = sample(client, headers, q, traces, meter, group, allowed, raw, repeat, order)
                assert row["reply"] == baseline[q["id"]][group]["reply"], (q["id"], group, repeat)
                assert candidates(row["trace"]) == candidates(baseline[q["id"]][group]["trace"])
                pair[group] = row["timing"]
            pair["added"] = {metric: pair["B"][metric] - pair["A"][metric] for metric in pair["A"]}
            pairs.append(pair)
    assert len(pairs) == 260
    write_json(output / "pairs.json", pairs)
    return {key: {metric: p95([pair[key][metric] for pair in pairs]) for metric in ("retrieval_ms", "done_ms")}
            for key in ("A", "B", "added")}


def permissions(client, headers, writer, users, rows, traces, meter, raw, chunks, output, http_raw):
    # The frozen corpus contains both cross-account directions and the authorized same-name SKUs/sites.
    base = rows["Q26"]
    probes = {}
    for name, scope in {"wrong_site": {"marketplace_id": "GB"}, "wrong_shop": {"shop_id": "evidence-absent"},
                        "wrong_sku": {"sku": "METRO-ABSENT"}}.items():
        q = {**base, "id": name, "access": False, "target": {**base["target"], **scope}}
        probes[name] = sample(client, headers, q, traces, meter, "B", set(), raw, "scope", "B")
    for key in ("Q01", "Q02", "Q03", "Q50"):
        q = rows[key]
        principal = "other" if q["principal"] == "owner" else "owner"
        rag.activate(writer, q["target"], q["version"])
        for source in q["B"]["sources"]:
            cid = source["chunk_id"]
            status = client.get(CHUNKS + cid, headers=headers[principal]).status_code
            assert status == 404, (key, cid, status)
            probes[f"{key}-{cid}"] = {"opposite_account_http": status, "chunk_id": cid}
    write_json(output / "scope-static.json", probes)
    scopes = scope_probes(client, headers, writer, users, base, traces, chunks, output, http_raw,
                          groups=("B",), configure=lambda group: nullcontext())
    for probe in scopes:
        sse = probe["sse"]
        assert committed(probe["trace"]) == CITATION.findall(sse["reply"]) == [], probe
    return len(scopes)


def gpu_timeout_probes(client, headers, writer, users, rows, traces, late, chunks, output):
    headers["owner"] = login(client, "owner")
    with patch.object(database.settings, "PRODUCT_RERANK_USER_IDS", frozenset({users["owner"]})):
        queue = product_chat_search.get_queue()
        model = queue._model()
        assert isinstance(model, Reranker) and model.device.type == "cuda"
        write_json(output / "timeout-model.json", model.metadata)
        focused = [{**rows[key], "off": rows[key]["B"]} for key in MISSING]
        faults = timeouts(client, headers, writer, users, model, focused, traces, late, chunks, output, path="r4_cpu")
        assert len(faults) == 6
        for key, row in faults.items():
            trace = row["trace"]
            assert trace["cpu_path"] == "r4_cpu" and trace["candidate_ids"] == rows[key]["candidate_chunk_ids"]
            assert committed(row["full_trace"]) == CITATION.findall(rows[key]["B"]["reply"])
        rollback = output / "timeout-rollback"
        rollback.mkdir()
        probe = {**rows["Q13"], "off": rows["Q13"]["A"]}
        with patch.object(database.settings, "PRODUCT_R4_CPU_ENABLED", False):
            row = timeouts(client, headers, writer, users, model, [probe], traces, late, chunks, rollback)["Q13"]
        assert row["trace"]["cpu_path"] == "default" and committed(row["full_trace"]) == []
        # The GPU input and the selected CPU candidates remain independently visible in Trace.
        assert candidates(row["trace"]) == candidates(probe["off"]["trace"])
    return p95([row["elapsed_ms"] for row in faults.values()])


def run(mode, r4, baseline_path, output):
    output.mkdir(parents=True, exist_ok=True)
    assert os.environ.get("PRODUCT_R4_CPU_ENABLED") == ("0" if mode == "env-zero" else None)
    assert "PRODUCT_RERANK_ENABLED" not in os.environ and Path(".env").exists() == (mode == "dotenv-zero")
    assert database.settings.PRODUCT_R4_CPU_ENABLED == (mode in {"default", "restarted"})
    assert not database.settings.PRODUCT_RERANK_ENABLED
    questions, chunks = freeze(r4, output)
    baseline = {q["id"]: q for q in json.loads(baseline_path.read_bytes())}
    copy_catalog(r4 / "evaluation.sqlite3", output / "evaluation.sqlite3")
    observability.TRACE_LOG_FILE = output / "traces.jsonl"
    observability.PRODUCT_RERANK_LATE_FILE = output / "late.jsonl"
    database.settings.JWT_SECRET = token_urlsafe(48)
    from fast_server import app

    group = "B" if database.settings.PRODUCT_R4_CPU_ENABLED else "A"
    write_json(output / "config.json", {"pid": os.getpid(), "mode": mode, "cwd": str(Path.cwd()),
               "r4_env": os.environ.get("PRODUCT_R4_CPU_ENABLED"), "gpu_env": os.environ.get("PRODUCT_RERANK_ENABLED"),
               "dotenv_configured": Path(".env").exists(), "effective_r4": database.settings.PRODUCT_R4_CPU_ENABLED,
               "effective_gpu": database.settings.PRODUCT_RERANK_ENABLED, "embedding": rag.BGE_ID,
               "r6_sha256": sha256(baseline_path.read_bytes()).hexdigest()})
    meter = Meter()
    traces = TraceReader(observability.TRACE_LOG_FILE)
    late = TraceReader(observability.PRODUCT_RERANK_LATE_FILE)
    with loopback(app) as client, closing(database.get_db()) as writer, \
            (output / "samples.jsonl").open("w", encoding="utf-8") as raw, \
            (output / "http.jsonl").open("w", encoding="utf-8") as http_raw, \
            patch.object(rag.BGEEmbedding, "embed_query", lambda embedding, texts: meter.encode(embedding, texts)), \
            patch.object(product_chat_search, "get_queue", meter.queue):
        users = {p: writer.execute("SELECT id FROM users WHERE username=?", (f"product-evidence-{p}",)).fetchone()[0]
                 for p in ("owner", "other")}
        headers = {p: login(client, p) for p in users}
        rows = []
        for q in questions:
            uid = users[q["principal"]]
            rag.activate(writer, q["target"], q["version"])
            _, allowed = read_scope(writer, uid, q)
            assert q["access"] == bool(allowed), q["id"]
            row = sample(client, headers, q, traces, meter, group, allowed, raw, 0, group)
            compare(client, headers, writer, uid, q, row, chunks, http_raw, group, baseline[q["id"]][group])
            rows.append({**q, group: row})
            write_json(output / "questions.json", rows)
        n = 44 if group == "B" else 38
        answerable = [q for q in rows if q["has_answer"]]
        multi = [q for q in answerable if len(q["required"]) > 1]
        unanswered = [q for q in rows if q["access"] and not q["has_answer"]]
        assert len(answerable) == 44 and sum(q[group]["complete"] for q in answerable) == n
        assert len(multi) == 7 and sum(q[group]["complete"] for q in multi) == (7 if group == "B" else 4)
        assert len(unanswered) == 8 and not any(q[group]["sources"] for q in unanswered)
        assert {q["id"] for q in answerable if not q[group]["complete"]} == (set(MISSING) if group == "A" else set())
        assert product_chat_search._queue is None
        summary = {"passed": True, "mode": mode, "questions": len(rows), "answerable": [n, 44],
                   "multi": [sum(q[group]["complete"] for q in multi), 7], "false_citations": [0, 8],
                   "denied_before_encode": [q["id"] for q in rows if not q["access"]],
                   "http_verified": sum(len(q[group]["sources"]) for q in rows), "gpu_cpu_run": meter.counts()}
        print(f"{mode}: 54 SSE responses match R6 group {group}", flush=True)
        if mode == "default":
            by_id = {q["id"]: {**q, "A": baseline[q["id"]]["A"]} for q in rows}
            retained = sum(q["A"]["complete"] and q["B"]["complete"] for q in by_id.values())
            assert retained == 38
            summary["original_complete_retained"] = retained
            summary["scope_changes"] = permissions(client, headers, writer, users, by_id, traces, meter, raw,
                                                    chunks, output, http_raw)
            headers["owner"] = login(client, "owner")
            summary["p95_ms"] = latency(client, headers, writer, users, rows, traces, meter, raw, output, baseline)
            summary["paired_samples"] = 52 * 5
            summary["http_p95_ms"] = p95([s["http_ms"] for q in rows for s in q["B"]["sources"]])
            summary["timeout_done_p95_ms"] = gpu_timeout_probes(client, headers, writer, users, by_id, traces,
                                                              late, chunks, output)
        write_json(output / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r4-run", type=Path, required=True)
    parser.add_argument("--r6-questions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", choices=MODES)
    args = parser.parse_args()
    r4, baseline, output = (p.resolve() for p in (args.r4_run, args.r6_questions, args.output))
    if args.worker:
        run(args.worker, r4, baseline, output)
        return
    output.mkdir(parents=True)
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {"PRODUCT_R4_CPU_ENABLED", "AD_OFFLINE_ACCEPTANCE"}
           and not key.upper().startswith("PRODUCT_RERANK_")}
    env.update(PYTHON_DOTENV_DISABLED="1", LLM_PROVIDER="mock", DIAGNOSIS_MYSQL_ENABLED="0",
               PYTHONPATH=os.pathsep.join(filter(None, (str(ROOT), env.get("PYTHONPATH")))))
    sandbox = output / "config-sandbox"
    sandbox.mkdir()
    for mode in MODES:
        child = dict(env)
        if mode == "env-zero":
            child["PRODUCT_R4_CPU_ENABLED"] = "0"
        elif mode == "dotenv-zero":
            (sandbox / ".env").write_text("PRODUCT_R4_CPU_ENABLED=0\n", encoding="utf-8")
        elif mode == "restarted":
            (sandbox / ".env").unlink()
        with (output / f"{mode}.log").open("w", encoding="utf-8") as log:
            subprocess.run([sys.executable, "-u", "-B", "-X", "utf8", "-m", "scripts.eval_product_r7",
                            "--worker", mode, "--r4-run", str(r4), "--r6-questions", str(baseline),
                            "--output", str(output / mode)], cwd=sandbox, env=child, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        print(f"{mode} passed", flush=True)


if __name__ == "__main__":
    main()
