"""Replay R4-B fixed, authenticated synthetic excerpts through the sealed generation entry point."""

import argparse
import asyncio
import inspect
import json
import os
import platform
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from secrets import token_urlsafe
from selectors import SelectSelector
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

# Load a sandbox configuration before importing application modules.
os.environ.update(AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", LLM_PROVIDER="mock")
os.environ.pop("OPENAI_LOG", None)

import httpx
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from openai import APIConnectionError, APITimeoutError
from openai.types.chat import ChatCompletion

from backend.agent.llm_provider import ProviderFactory
from backend.database import database, listing_operations as listing, product_documents as documents
from backend.rag import product_evidence as rag
from scripts import eval_product_sealed as sealed
from scripts.eval_product_generalization_t1 import readback
from scripts.product_evidence import setup_fixture, use_sandbox, write_json
from scripts.verify_product_evidence import CHUNKS, login

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/product_generation_r4b"
V3 = ROOT / "artifacts/TASK-044-T2-RUN/v3/versions.json"
FROZEN = FIXTURE / "freeze.json"


class InProcessSelector(SelectSelector):
    """Wake TestClient's Windows worker without a TCP socket pair."""

    def __init__(self):
        super().__init__()
        self.wakeup = Event()

    def select(self, timeout=None):
        if self.get_map():
            raise RuntimeError("R4-B ASGI worker cannot perform socket I/O")
        self.wakeup.wait(timeout)
        self.wakeup.clear()
        return []


class InProcessLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__(selector=InProcessSelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        if self._selector is not None:
            self._selector.wakeup.set()


def read(path):
    return json.loads(path.read_bytes())


def digest(data):
    return sha256(data).hexdigest()


def json_digest(data):
    return digest(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def require(condition, label):
    if not condition:
        raise ValueError(label)


def source_index(plan):
    return {doc["id"]: (release, doc)
            for release in plan["releases"] for doc in release["documents"]}


def excerpt(index, ref):
    release, doc = index[ref["doc"]]
    original = (FIXTURE / "sources" / doc["file"]).read_bytes().decode()
    location, = [loc for loc in rag.source_regions(doc["kind"], original) if loc["location"] == ref["location"]]
    return release, doc, original, location, original[location["start_offset"]:location["end_offset"]]


def wire(profile, case, evidence):
    request = {"question": case["query"], "evidence": evidence}
    return {"model": profile["model"], "messages": [{"role": "system", "content": sealed.GENERATION_PROMPT},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)}],
            "response_format": {"type": "json_object"}, **profile["options"], "thinking": {"type": "disabled"}}


def freeze_spec(plan):
    v3 = read(V3)
    profile = {"model": v3["glm_model"], "options": v3["generation_options"], "sdk_max_retries": 0}
    require(profile == {"model": "glm-4.5-air", "options": {"temperature": 0, "max_tokens": 900, "timeout": 30},
                        "sdk_max_retries": 0}, "unexpected v3 generation profile")
    index = source_index(plan)
    require(len(index) == sum(len(release["documents"]) for release in plan["releases"]), "duplicate document ID")
    inputs = {}
    for case in plan["cases"]:
        refs = case["evidence"]
        require(len({(ref["doc"], ref["location"]) for ref in refs}) == len(refs), case["id"] + " duplicate excerpt")
        evidence = []
        for i, ref in enumerate(refs, 1):
            release, _, _, _, quote = excerpt(index, ref)
            require(release["target"] == case["target"] and release["version"] == case["version"],
                    case["id"] + " misbound fixture input")
            evidence.append({"id": i, "text": quote})
        for fact in case["expected"]["facts"]:
            ids = fact["ids"] + [item["id"] for item in fact["conditions"]]
            require(all(1 <= i <= len(refs) for i in ids), case["id"] + " score references unknown ID")
        inputs[case["id"]] = {"evidence": evidence, "sdk_payload_sha256": json_digest(wire(profile, case, evidence))}
    require(len(inputs) == len(plan["cases"]) == 10, "first-run denominator has changed")
    paths = [FIXTURE / "plan.json", FIXTURE / "rubric.md", *sorted((FIXTURE / "sources").iterdir()),
             V3, Path(__file__), ROOT / "scripts/eval_product_sealed.py", ROOT / "backend/agent/llm_provider.py"]
    return {"profile": profile, "profile_sha256": json_digest(profile),
            "prompt_sha256": digest(sealed.GENERATION_PROMPT.encode()),
            "readback_sha256": digest(inspect.getsource(readback).encode()),
            "sha256": {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in paths}, "inputs": inputs}


def fixed_row(case):
    # The existing generation protocol expects this precheck flag. The fixed manifest and readback attest
    # the source order here; no retrieval or ranking ran.
    return {"input": {"query": case["query"], "target": case["target"], "version": case["version"]},
            "done": True, "ranking_attested": True, "retrieval_trace": {"status": case.get("routing", "supported")}}


def chunk_id(conn, doc, loc):
    row, = conn.execute("""
        SELECT c.chunk_id FROM product_documents d JOIN product_document_chunks c ON c.source_id=d.id
        WHERE d.document_id=? AND c.strategy='structured' AND c.start_offset=? AND c.end_offset=?
        """, (doc["id"], loc["start_offset"], loc["end_offset"])).fetchall()
    return row["chunk_id"]


def verify(client, conn, users, auth, index, case, *, capture_wire=True):
    row, pool, originals, entries, blocks = fixed_row(case), {}, {}, [], []
    for i, ref in enumerate(case["evidence"], 1):
        release, doc, original, loc, quote = excerpt(index, ref)
        cid = chunk_id(conn, doc, loc)
        entry = {"id": i, "doc": doc["id"], "filename": doc["file"], "scope": release["target"],
                 "version": release["version"], "principal": case["principal"], "quote": quote, **loc}
        entries.append(entry)
        originals[doc["file"]] = original
        hit = rag.read_chunk(conn, users[case["principal"]], cid)
        if hit is not None:
            pool[cid] = {k: v for k, v in hit.items() if k != "original"}
        blocks.append(f"{doc['file']} · {loc['location']} · {loc['section']}\n{quote}\n原文回查（需登录） {CHUNKS}{cid}")
    row.update(ranked={"pool": pool}, reply="\n".join(blocks))
    checks = readback(client, auth[case["principal"]], row, originals, capture_wire=capture_wire)
    require(len(checks) == len(entries), case["id"] + " source count mismatch")
    scope = rag._scope(case["target"])
    current = documents.current_chunk_ids(conn, users[case["principal"]], scope, case["version"], list(pool))
    active = documents.active_version(conn, scope)
    for entry, check in zip(entries, checks):
        same_scope = entry["scope"] == case["target"]
        same_version = entry["version"] == case["version"] == active
        matched = (check.get("quote") == entry["quote"] and check.get("document_id") == entry["doc"]
                   and check.get("location") == entry["location"])
        check["ok"] = check["ok"] and matched and same_scope and same_version and check["chunk_id"] in current
        entry.update(readback=check, binding={"same_scope": same_scope, "same_active_version": same_version,
                                            "current_authorized_chunk": check["chunk_id"] in current})
    return row, checks, entries, active


def dispatch(provider, profile, case, row, checks, expected_wire=None):
    sdk, http, finish = [], [], {}
    live_client = getattr(provider, "_client", None)

    def on_request(req):
        # Inspect the JSON body only. Keep URLs, headers and authentication out of the journal.
        payload = json.loads(req.content)
        expected_body = {key: value for key, value in expected_wire.items() if key != "timeout"}
        timeout = req.extensions.get("timeout", {})
        require(payload == expected_body, case["id"] + " unapproved request body")
        require(timeout == dict.fromkeys(("connect", "read", "write", "pool"), expected_wire["timeout"]),
                case["id"] + " HTTP timeout differs from freeze")
        http.append({"status": None, "body": payload, "body_sha256": digest(req.content), "timeout": timeout})

    def on_response(resp):
        http[-1]["status"] = resp.status_code

    def capture(actual_provider, budget, **kwargs):
        request = {k: v for k, v in kwargs.items() if k not in {"role", "step", "sdk_max_retries"}}
        sdk.append({"request": request, "role": kwargs["role"], "step": kwargs["step"],
                    "sdk_max_retries": kwargs["sdk_max_retries"]})
        require(budget is None and kwargs["sdk_max_retries"] == profile["sdk_max_retries"], "generation protocol drift")
        if expected_wire is not None:
            require({**request, "thinking": {"type": "disabled"}} == expected_wire,
                    case["id"] + " SDK input differs from freeze")
        response = actual_completion(actual_provider, budget, **kwargs)
        finish["reason"] = response.choices[0].finish_reason if response.choices else None
        return response

    actual_completion = sealed.create_completion
    events = {"request": [on_request], "response": [on_response]}
    with patch.object(sealed, "create_completion", capture):
        if live_client is None:
            result = sealed.generate(provider, row, checks, profile["options"])
        else:
            with patch.object(live_client._client, "_event_hooks", events):
                result = sealed.generate(provider, row, checks, profile["options"])
    parsed = None
    if isinstance(result.get("raw"), str):
        try:
            parsed = json.loads(result["raw"])
        except ValueError:
            pass
    return {**result, "parsed_json": parsed, "finish_reason": finish.get("reason"), "sdk_capture": sdk,
            "http_attempts": http, "http_request_count": len(http), "http_response_count": sum(
                attempt["status"] is not None for attempt in http)}


class FixedProvider:
    def __init__(self, model, content=None, finish="stop", error=None):
        self.model = model
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.content = content
        self.finish = finish
        self.error = error

    def create(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error(request=httpx.Request("POST", "https://example.invalid"))
        return ChatCompletion(id="offline", created=0, object="chat.completion", model="offline-fixed",
                              choices=[{"index": 0, "finish_reason": self.finish,
                                        "message": {"role": "assistant", "content": self.content}}])


def probes(profile, case, checks, row):
    cases = (
        ("O01", '{"answer":"63 mm[1]","citations":[1]}', "stop", None, "generated"),
        ("O02", '{invalid', "stop", None, "invalid_output"),
        ("O03", '{"answer":"63 mm[1]","citations":[1]}', "length", None, "invalid_output"),
        ("O04", '{"answer":"63 mm[2]","citations":[2]}', "stop", None, "invalid_output"),
        ("O05", None, "stop", APITimeoutError, "provider_error"),
        ("O06", None, "stop", APIConnectionError, "provider_error"),
    )
    records = []
    for name, content, finish, error, expected in cases:
        provider = FixedProvider(profile["model"], content, finish, error)
        result = dispatch(provider, profile, case, row, checks)
        records.append({"id": name, "expected": expected, "fixed_provider_received": provider.calls, "result": result,
                        "pass": result["status"] == expected and provider.calls == 1})
    return records


def guard(client, conn, users, auth, index, case, profile):
    initial = case if case["action"] != "wrong_sku" else {**case, "target": index[case["evidence"][0]["doc"]][0]["target"]}
    _, before, before_entries, _ = verify(client, conn, users, auth, index, initial)
    require(all(check["ok"] for check in before), case["id"] + " positive control failed")
    scope = rag._scope(case["target"])
    action = case["action"]
    if action == "transfer_shop":
        with conn:
            conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                         (users["other"], *scope[:2]))
        positive = {**case, "principal": "other"}
    elif action == "activate_previous":
        rag.activate(conn, case["target"], "proof-v0")
        positive = {**case, "version": "proof-v0", "evidence": [{"doc": "frame-obsolete", "location": "row:2"}]}
    else:
        positive = initial
    try:
        row, checks, entries, active = verify(client, conn, users, auth, index, case)
        _, positive_checks, positive_entries, _ = verify(client, conn, users, auth, index, positive)
        scope_error = listing.scope_error(conn, users[case["principal"]], *scope)
        if scope_error:
            row["retrieval_trace"]["status"] = "denied"
        fixed = FixedProvider(profile["model"])
        result = dispatch(fixed, profile, case, row, checks)
        passed = (result["status"] == case["expected_status"] and result["requests"] == fixed.calls == 0
                  and all(check["http"] == case["expected_http"] and not check["ok"] for check in checks)
                  and all(check["ok"] for check in positive_checks))
        return {"id": case["id"], "passed": passed, "active_version": active, "scope_error": scope_error,
                "before": before_entries, "attempted": entries, "positive_control": positive_entries,
                "fixed_provider_received": fixed.calls, "result": result}
    finally:
        if action == "transfer_shop":
            with conn:
                conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                             (users["owner"], *scope[:2]))
        elif action == "activate_previous":
            rag.activate(conn, case["target"], case["version"])


def preflight(client, conn, users, auth, plan, frozen):
    index = source_index(plan)
    profile = frozen["profile"]
    checked, inventory = [], []
    for case in plan["cases"]:
        row, checks, entries, active = verify(client, conn, users, auth, index, case)
        controls = []
        for control in case.get("controls", []):
            candidate = {**case, **control, "evidence": [control["reference"]]}
            _, control_checks, located, _ = verify(client, conn, users, auth, index, candidate)
            check, = control_checks
            require(check["http"] == control["http"] and check["ok"] == (control["http"] == 200),
                    case["id"] + " isolation control failed")
            controls += located
        require(active == case["version"] and all(check["ok"] for check in checks), case["id"] + " failed readback")
        evidence = [{"id": i, "text": check["quote"]} for i, check in enumerate(checks, 1)]
        payload = wire(profile, case, evidence)
        expected = frozen["inputs"][case["id"]]
        require(expected == {"evidence": evidence, "sdk_payload_sha256": json_digest(payload)},
                case["id"] + " frozen ID, order, quote or payload mismatch")
        checked.append((case, row, checks, {"id": case["id"], "active_version": active, "readback": entries,
                                           "controls": controls, "expected_wire": payload}))
        if not checks:
            for release in plan["releases"]:
                if release["target"] != case["target"] or release["version"] != active:
                    continue
                refs = [{"doc": doc["id"], "location": loc["location"]} for doc in release["documents"]
                        for loc in rag.source_regions(doc["kind"], (FIXTURE / "sources" / doc["file"]).read_bytes().decode())]
                _, all_checks, locations, _ = verify(client, conn, users, auth, index, {**case, "evidence": refs})
                require(all(hit["ok"] for hit in all_checks), case["id"] + " no-answer inventory not readable")
                inventory.append({"id": case["id"], "active_source_inventory": locations})
    guards = [guard(client, conn, users, auth, index, case, profile) for case in plan["guards"]]
    case, row, checks, _ = checked[0]
    return checked, inventory, guards, probes(profile, case, checks, row)


def run(plan, frozen, args, output):
    profile = frozen["profile"]
    provider = None
    if args.mode == "live":
        key = os.environ.get("GLM_API_KEY") or dotenv_values(ROOT / ".env").get("GLM_API_KEY") or ""
        require(bool(key.strip()), "local GLM credential missing")
        database.settings.GLM_API_KEY = key
        database.settings.GLM_MODEL = profile["model"]
        provider = ProviderFactory.create("glm")
        require(provider.model == profile["model"], "configured provider model differs")
    database.settings.JWT_SECRET = token_urlsafe(48)
    with TemporaryDirectory(prefix="r4b-sandbox-", dir=output) as sandbox:
        use_sandbox(Path(sandbox) / "isolated.sqlite3")
        database.init_db()
        from fast_server import app

        backend_options = {"loop_factory": InProcessLoop} if os.name == "nt" else {}
        with closing(database.get_db()) as conn, closing(TestClient(app, backend_options=backend_options)) as client:
            users = setup_fixture(conn, plan)
            for release in plan["releases"]:
                require(rag.import_release(conn, release, FIXTURE / "sources",
                                           embedding=lambda texts: [[1.0, 0.0]] * len(texts)), "source import failed")
                if release["version"] == "proof-v1":
                    rag.activate(conn, release["target"], release["version"])
            auth = {name: login(client, name) for name in users}
            checked, inventory, guards, offline = preflight(client, conn, users, auth, plan, frozen)
            write_json(output / "sources.json", [item[3] for item in checked])
            write_json(output / "no-answer-inventory.json", inventory)
            write_json(output / "gates.json", guards)
            write_json(output / "offline-probes.json", offline)
            require(all(gate["passed"] for gate in guards) and all(probe["pass"] for probe in offline), "control failed")
            if provider is None:
                return 0
            statuses = []
            with (output / "rows.jsonl").open("x", encoding="utf-8") as journal:
                index = source_index(plan)
                for case, _, _, evidence in checked:
                    # Recheck the fixed authorization and locations just before each actual dispatch.
                    row, checks, _, active = verify(client, conn, users, auth, index, case, capture_wire=False)
                    require(active == case["version"] and all(check["ok"] for check in checks), "source invalidated")
                    result = dispatch(provider, profile, case, row, checks, evidence["expected_wire"])
                    journal.write(json.dumps({"id": case["id"], "attempt": 1, "result": result}, ensure_ascii=False) + "\n")
                    journal.flush()
                    statuses.append(result["status"])
                    print(case["id"], result["status"], result["finish_reason"], result["http_request_count"], flush=True)
            return 0 if all(status == "generated" for status in statuses) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("freeze", "offline", "live"), required=True)
    parser.add_argument("--output", type=Path, help="New artifact directory inside the workspace")
    args = parser.parse_args()
    plan = read(FIXTURE / "plan.json")
    current = freeze_spec(plan)
    if args.mode == "freeze":
        with FROZEN.open("x", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(digest(FROZEN.read_bytes()))
        return 0
    require(current == read(FROZEN), "frozen plan, source, prompt or protocol changed")
    require(args.output is not None, "--output is required")
    output = args.output.resolve()
    output.mkdir(parents=True)
    config = {"mode": args.mode, "started_utc": datetime.now(timezone.utc).isoformat(),
              "freeze_sha256": digest(FROZEN.read_bytes()), "profile": current["profile"],
              "profile_sha256": current["profile_sha256"], "prompt_sha256": current["prompt_sha256"],
              "command": [sys.executable, "-B", "-X", "utf8", "-m", "scripts.eval_product_generation_r4b", *sys.argv[1:]],
              "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "runtime": {"os": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                          "openai": version("openai"), "httpx": version("httpx"), "fastapi": version("fastapi")},
              "eligible_first_runs": list(current["inputs"]), "explicit_retries": 0}
    write_json(output / "run-config.json", config)
    try:
        exit_code = run(plan, current, args, output)
    except Exception as error:
        # Preserve partial journals and classified errors without connecting details or tokens.
        reason = str(error) if type(error) is ValueError else None
        write_json(output / "exit.json", {"exit_code": 1, "error_type": type(error).__name__, "reason": reason})
        print(type(error).__name__, reason or "see preserved journal", file=sys.stderr)
        return 1
    write_json(output / "exit.json", {"exit_code": exit_code, "completed_utc": datetime.now(timezone.utc).isoformat()})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
