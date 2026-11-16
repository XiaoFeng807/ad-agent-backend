"""Replay the R4 ranking and read back every distinct candidate over HTTP."""

import argparse
import json
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe

from backend.database import database
from scripts.product_evidence import use_sandbox, write_json
from scripts.verify_product_evidence import CHUNKS, SEARCH, TARGET, activate, chat, check_sources, login, server

PROBES = (
    ({"sku": "METRO-500"}, "Can I fit a replacement cap based on appearance alone?", "owner", "LID-M500"),
    ({"sku": "METRO-750"}, "Can I fit a replacement cap based on appearance alone?", "owner", "LID-M750"),
    ({"marketplace_id": "CA"}, "Can I fit a replacement cap based on appearance alone?", "owner", "LID-M500"),
    ({"shop_id": "evidence-other"}, "Can I fit a replacement cap based on appearance alone?", "other", "LID-M500"),
)


def request_search(client, headers, target, query):
    return client.post(SEARCH, headers=headers, json={**target, "query": query, "mode": "vector", "expansion": "both"})


def replay(client, headers, rows):
    checks = []
    for row in rows:
        activate(row["target"], row["version"])
        response = request_search(client, headers[row["principal"]], row["target"], row["query"])
        check = {"id": row["id"], "http": response.status_code}
        if row["access"]:
            result = response.raise_for_status().json()
            expected = {**row["target"], "version": row["version"]}
            assert all(all(hit[key] == value for key, value in expected.items())
                       for hit in [*result["candidates"], *result["evidence"]]), row["id"]
            assert [hit["chunk_id"] for hit in result["candidates"]] == row["candidate_chunk_ids"], row["id"]
            assert result["evidence"] == row["evidence"] and result["status"] == row["status"], row["id"]
            check.update(candidates=len(result["candidates"]), citations=len(result["evidence"]),
                         version=result["version"], ranking_matches=True)
        else:
            assert response.status_code == 404, row["id"]
        checks.append(check)
    return checks


def check_permissions(client, headers):
    checks = []
    for overrides, query, principal, expected_part in PROBES:
        target = {**TARGET, **overrides}
        activate(target, "v1")
        expected = {**target, "version": "v1"}
        result = request_search(client, headers[principal], target, query).raise_for_status().json()
        assert len(result["candidates"]) == 20 and len(result["evidence"]) == 2
        assert all(all(hit[key] == value for key, value in expected.items())
                   for hit in [*result["candidates"], *result["evidence"]])
        part = next(cite for cite in result["evidence"] if cite["document_id"] == "specs")
        assert expected_part in part["quote"] and "B42" in part["quote"]
        other = headers["other" if principal == "owner" else "owner"]
        denied = request_search(client, other, target, query)
        denied_lookup = client.get(CHUNKS + part["chunk_id"], headers=other)
        assert (denied.status_code, denied_lookup.status_code) == (404, 404)
        checks.append({**expected, "principal": principal, "part": part, "cross_account_search": denied.status_code,
                       "cross_account_lookup": denied_lookup.status_code})
    return checks


def check_named_part(client, owner):
    query = "Does LID-M750 fit this SKU?"
    checks = []
    for sku in ("METRO-500", "METRO-750"):
        target = {**TARGET, "sku": sku}
        result = request_search(client, owner, target, query).raise_for_status().json()
        assert all(hit["sku"] == sku for hit in result["candidates"])
        if sku == "METRO-500":
            assert not result["evidence"]
        else:
            assert len(result["evidence"]) == 1 and "LID-M750" in result["evidence"][0]["quote"]
        checks.append({"target": target, "status": result["status"], "reason": result["reason"],
                       "evidence": result["evidence"]})
    return checks


def verify(run, output):
    db = (run / "evaluation.sqlite3").resolve()
    assert db.is_file() and json.loads((run / "summary.json").read_bytes())["passed"]
    rows = json.loads((run / "r4.json").read_bytes())
    all_records = json.loads((run / "originals.json").read_bytes())
    ids = {key for row in rows for key in row["candidate_chunk_ids"]}
    records = {}
    for key in sorted(ids):
        record = all_records[key]
        scope = tuple(record[name] for name in ("shop_id", "marketplace_id", "sku"))
        records.setdefault((scope, record["version"]), []).append(record)
    output.mkdir(parents=True)
    use_sandbox(db)
    report = {"gpu_rerank_enabled": False, "deduplicated_candidates": len(ids)}
    with server(db, output, "http", token_urlsafe(48), extra_env={"PRODUCT_RERANK_ENABLED": "0"}) as (client, _):
        headers = {name: login(client, name) for name in ("owner", "other")}
        report["frozen_http"] = replay(client, headers, rows)
        report["permission_probes"] = check_permissions(client, headers)
        owner = headers["owner"]
        report["same_name_part_code"] = check_named_part(client, owner)
        with closing(database.get_db()) as conn:
            report["readback"] = check_sources(client, owner, conn, records, switch="v1")
        assert len(report["readback"]) == len(ids)
        assert all(row["fields_ok"] and row["original_ok"] for row in report["readback"])
        versions = {version: [row["chunk_id"] for row in all_records.values()
                              if all(row[key] == value for key, value in TARGET.items())
                              and row["version"] == version and row["chunk_id"] in ids] for version in ("v1", "v2")}
        activate(TARGET, "v2")
        report["after_version_switch"] = {}
        for version, keys in versions.items():
            expected = 404 if version == "v1" else 200
            assert keys
            checks = [{"chunk_id": key, "http": client.get(CHUNKS + key, headers=owner).status_code} for key in keys]
            assert all(check["http"] == expected for check in checks)
            report["after_version_switch"][version] = checks
        microwave = next(row for row in rows if row["id"] == "Q26")
        switched = request_search(client, owner, TARGET, microwave["query"]).raise_for_status().json()
        assert switched["version"] == "v2" and len(switched["candidates"]) == 20 and len(switched["evidence"]) == 1
        assert all(hit["version"] == "v2" for hit in [*switched["candidates"], *switched["evidence"]])
        report["search_after_version_switch"] = {
            "version": switched["version"], "candidate_chunk_ids": [hit["chunk_id"] for hit in switched["candidates"]],
            "evidence": switched["evidence"],
        }
        for row in rows:
            if row["id"] not in ("Q21", "Q26", "Q39"):
                continue
            activate(row["target"], row["version"])
            shop, site, sku = (row["target"][key] for key in ("shop_id", "marketplace_id", "sku"))
            result = chat(client, owner, f"商品资料：{shop} {site} {sku} {row['query']}")
            assert all(cite["quote"] in result["reply"] for cite in row["evidence"]), row["id"]
            report.setdefault("chat_with_gpu_disabled", {})[row["id"]] = result
    write_json(output / "service.json", report)
    print(f"HTTP frozen questions: {len(rows)}. Verified unique candidates: {len(ids)}.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    verify(args.run, args.output)


if __name__ == "__main__":
    main()
