"""Verify real BGE search, Agent chat, and source lookup across two FastAPI processes."""

import argparse
import json
import os
import socket
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from secrets import token_urlsafe
from time import monotonic, sleep

import httpx

from backend.database import database
from backend.rag import product_evidence as rag
from backend.rag.product_query_expansion import EXPANSIONS
from scripts.product_evidence import PASSWORD, use_sandbox, write_json

SEARCH = "/api/product-evidence/search"
CHUNKS = "/api/product-evidence/chunks/"
SHOP = {"shop_id": "evidence-main", "marketplace_id": "US"}
TARGET = {**SHOP, "sku": "METRO-500"}
MATERIAL = "接触饮料的内胆是什么材质？"
WASH = "杯身和杯盖都能进洗碗机吗？"


@contextmanager
def server(db, output, label, secret, *, extra_env=None):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "AD_OFFLINE_ACCEPTANCE": "1", "PYTHON_DOTENV_DISABLED": "1",
           "DIAGNOSIS_SANDBOX_DB_PATH": str(db), "DIAGNOSIS_MYSQL_ENABLED": "0",
           "LLM_PROVIDER": "mock", "JWT_SECRET": secret}
    if extra_env:
        env.update(extra_env)
    with (output / f"{label}.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", "-B", "-X", "utf8", "-m", "uvicorn", "fast_server:app",
             "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=35, trust_env=False) as client:
                deadline = monotonic() + 25
                while monotonic() < deadline and process.poll() is None:
                    try:
                        if client.get("/api/health", timeout=1).is_success:
                            break
                    except httpx.TransportError:
                        sleep(.1)
                else:
                    raise RuntimeError(f"FastAPI startup failed, see {label}.log")
                yield client, process.pid
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)


def login(client, principal):
    response = client.post("/api/login", json={
        "username": f"product-evidence-{principal}", "password": PASSWORD,
    })
    return {"Authorization": "Bearer " + response.raise_for_status().json()["data"]["token"]}


def activate(target, version):
    with closing(database.get_db()) as conn:
        rag.activate(conn, target, version)


def material(client, owner):
    return client.post(SEARCH, headers=owner, json={**TARGET, "query": MATERIAL}).raise_for_status().json()


def chat(client, headers, message, **extra):
    response = client.post("/api/chat/stream", headers=headers, json={"message": message, **extra})
    events = [json.loads(line[6:]) for line in response.raise_for_status().text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "done", events
    return {"reply": "".join(event["content"] for event in events if event["type"] == "text"),
            "trace_id": events[-1]["trace_id"]}


def check_sources(client, headers, conn, originals, *, switch):
    from hashlib import sha256

    checks = []
    for (_, version), rows in originals.items():
        target = {key: rows[0][key] for key in ("shop_id", "marketplace_id", "sku")}
        rag.activate(conn, target, version)
        for record in rows:
            chunk_id = record["chunk_id"]
            response = client.get(CHUNKS + chunk_id, headers=headers)
            source = response.raise_for_status().json()
            fields_ok = all(source[key] == record[key] for key in (
                "shop_id", "marketplace_id", "sku", "version", "document_id", "filename", "kind",
                "start_offset", "end_offset", "locations", "content",
            ))
            original_ok = (sha256(source["original"].encode()).hexdigest() == record["original_sha256"]
                           and source["original"][source["start_offset"]:source["end_offset"]] == source["content"])
            checks.append({"chunk_id": chunk_id, "version": version, "http": response.status_code,
                           "fields_ok": fields_ok, "original_ok": original_ok})
    activate(TARGET, switch)
    return checks


def check_expansion(client, owner, other, foreign, users):
    report = {}
    targets = [TARGET, {**TARGET, "sku": "METRO-750"}, {**TARGET, "marketplace_id": "CA"}, foreign]
    query = "杯身和杯盖的清洗限制是什么，空杯重量是多少？"
    for group in EXPANSIONS:
        results = []
        for target in targets:
            headers = other if target == foreign else owner
            user = users["other" if target == foreign else "owner"]
            response = client.post(SEARCH, headers=headers, json={**target, "query": query,
                                                                 "mode": "vector", "expansion": group})
            result = response.raise_for_status().json()
            expected = {**target, "version": "v1"}
            assert result["version"] == "v1" and result["candidates"] and not result["evidence"]
            assert all(all(hit[key] == value for key, value in expected.items()) for hit in result["candidates"])
            with closing(database.get_db(read_only=True)) as conn:
                conn.execute("BEGIN")
                audited = rag.ProductEvidence().search(conn, user, target, query, mode="vector", expansion=group,
                                                       audit=True)
            channels = {name: rows for name, rows in audited["channels"].items() if name != "keyword"}
            expected_channels = {"vector"}
            if group in ("aliases", "both"):
                expected_channels.add("alias")
            if group in ("parts", "both"):
                expected_channels.update(("part_1", "part_2"))
            assert channels.keys() == expected_channels and audited["candidates"] == result["candidates"]
            stages = {"scoring_pool": audited["pool"], **channels, "merged": audited["candidates"],
                      "citations": audited["evidence"]}
            scope_errors = {name: sum(any(hit[key] != value for key, value in expected.items()) for hit in rows)
                            for name, rows in stages.items()}
            assert not any(scope_errors.values())
            opposite = owner if target == foreign else other
            denied = client.post(SEARCH, headers=opposite, json={**target, "query": query,
                                                               "mode": "vector", "expansion": group})
            chunk_id = result["candidates"][0]["chunk_id"]
            lookup = client.get(CHUNKS + chunk_id, headers=opposite)
            assert (denied.status_code, lookup.status_code) == (404, 404)
            results.append({**expected, "queries": audited["queries"], "scope_errors": scope_errors,
                            "counts": {name: len(rows) for name, rows in stages.items()},
                            "cross_account_search": denied.status_code, "cross_account_lookup": lookup.status_code})
        report[group] = results
    return report


def verify(run, output, *, browser=False):
    db = run / "evaluation.sqlite3"
    if not db.is_file():
        raise ValueError("run the BGE evaluation before service verification")
    lookup = json.loads((run / "originals.json").read_text(encoding="utf-8"))
    originals = {}
    for row in lookup.values():
        key = (tuple(row[name] for name in ("shop_id", "marketplace_id", "sku")), row["version"])
        originals.setdefault(key, []).append(row)
    output.mkdir(parents=True)
    use_sandbox(db)
    secret = token_urlsafe(48)
    report = {}
    foreign = {"shop_id": "evidence-other", "marketplace_id": "US", "sku": "METRO-500"}
    activate(TARGET, "v1")
    with server(db, output, "before-restart", secret) as (client, pid):
        owner, other = login(client, "owner"), login(client, "other")
        old = material(client, owner)
        assert old["version"] == "v1" and "304 stainless steel" in old["evidence"][0]["quote"]
        old_id = old["evidence"][0]["chunk_id"]
        report["before_restart_pid"] = pid
        report["ordinary_chat"] = chat(client, owner, f"商品资料：evidence-main US METRO-500 {MATERIAL}")
        assert "304 stainless steel" in report["ordinary_chat"]["reply"]
        group = client.post("/api/chat/conversations", headers=owner, json={"title": "商品资料回归"}).raise_for_status()
        conversation_id = group.json()["data"]["conversation_id"]
        report["group_chat"] = chat(client, owner, "商品资料：evidence-main US METRO-500 能保温几小时？",
                                    conversation_id=conversation_id)
        assert report["group_chat"]["reply"].startswith("证据不足")
        if browser:
            from urllib.parse import urlsplit

            url = urlsplit(str(client.base_url))
            with (output / "browser.log").open("wb") as log:
                subprocess.run(["node", "tests/product_evidence_browser.cjs", url.geturl(),
                                str(output / "browser.json"), conversation_id],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
            owner = login(client, "owner")
        report["denials"] = {}
        for mode in ("vector", "hybrid"):
            report["denials"][f"cross_shop_{mode}"] = client.post(
                SEARCH, headers=owner, json={**foreign, "query": MATERIAL, "mode": mode},
            ).status_code
        own_foreign = client.post(SEARCH, headers=other, json={**foreign, "query": MATERIAL}).raise_for_status().json()
        foreign_id = own_foreign["candidates"][0]["chunk_id"]
        report["denials"].update(
            foreign_lookup=client.get(CHUNKS + foreign_id, headers=owner).status_code,
            reverse_lookup=client.get(CHUNKS + old_id, headers=other).status_code,
            unauthenticated_lookup=client.get(CHUNKS + old_id).status_code,
        )
        assert report["denials"] == dict(cross_shop_vector=404, cross_shop_hybrid=404, foreign_lookup=404,
                                         reverse_lookup=404, unauthenticated_lookup=401)
        if (run / "both.json").is_file():
            for target in ({**TARGET, "sku": "METRO-750"}, {**TARGET, "marketplace_id": "CA"}, foreign):
                activate(target, "v1")
            with closing(database.get_db(read_only=True)) as conn:
                users = {principal: conn.execute("SELECT id FROM users WHERE username=?",
                                                 (f"product-evidence-{principal}",)).fetchone()["id"]
                         for principal in ("owner", "other")}
            report["expansion_scope"] = check_expansion(client, owner, other, foreign, users)
            report["partial_chat"] = chat(client, owner, f"商品资料：evidence-main US METRO-500 {WASH}",
                                          conversation_id=conversation_id)
            assert report["partial_chat"]["reply"].startswith("证据不足") and "Cap wash" in report["partial_chat"]["reply"]
            for version in ("v1", "v2"):
                activate(TARGET, version)
                expanded = client.post(SEARCH, headers=owner, json={**TARGET, "query": WASH, "mode": "vector",
                                                                   "expansion": "both"}).raise_for_status().json()
                assert expanded["version"] == version
                assert {cite["location"] for cite in expanded["evidence"]} == {"page:5", "page:6"}
                for cite in expanded["evidence"]:
                    source = client.get(CHUNKS + cite["chunk_id"], headers=owner).raise_for_status().json()
                    assert source["version"] == version
                    assert cite["quote"] == source["original"][cite["start_offset"]:cite["end_offset"]]
                report.setdefault("expanded_wash", {})[version] = expanded["evidence"]
            stale = [client.get(CHUNKS + cite["chunk_id"], headers=owner).status_code
                     for cite in report["expanded_wash"]["v1"]]
            assert stale == [404, 404]
            report["expanded_wash_old_after_switch"] = stale
        with closing(database.get_db()) as conn:
            report["lookup_before"] = check_sources(client, owner, conn, originals, switch="v1")
        activate(TARGET, "v2")
        new = material(client, owner)
        assert new["version"] == "v2" and "316 stainless steel" in new["evidence"][0]["quote"]
        new_id = new["evidence"][0]["chunk_id"]
        report["old_after_switch"] = client.get(CHUNKS + old_id, headers=owner).status_code
        assert report["old_after_switch"] == 404
        report["new_after_switch"] = client.get(CHUNKS + new_id, headers=owner).raise_for_status().json()
        report["old_citation"] = old["evidence"][0]
        report["new_citation"] = new["evidence"][0]
    with server(db, output, "after-restart", secret) as (client, pid):
        report["after_restart_pid"] = pid
        assert pid != report["before_restart_pid"]
        assert material(client, owner)["evidence"] == new["evidence"]
        assert client.get(CHUNKS + new_id, headers=owner).raise_for_status().json() == report["new_after_switch"]
        report["old_after_restart"] = client.get(CHUNKS + old_id, headers=owner).status_code
        assert report["old_after_restart"] == 404
        with closing(database.get_db()) as conn:
            report["lookup_after"] = check_sources(client, owner, conn, originals, switch="v2")
        assert client.get(CHUNKS + foreign_id, headers=owner).status_code == 404
    for stage in ("lookup_before", "lookup_after"):
        assert len(report[stage]) == len(lookup) and all(row["fields_ok"] and row["original_ok"] for row in report[stage])
    write_json(output / "service.json", report)
    print(json.dumps({"unique_chunks": len(lookup), "passed_before_restart": len(report["lookup_before"]),
                      "passed_after_restart": len(report["lookup_after"]),
                      "denials": report["denials"], "old_after_switch": report["old_after_switch"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    verify(args.run.resolve(), args.output.resolve(), browser=args.browser)


if __name__ == "__main__":
    main()
