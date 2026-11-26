"""Pair archived R4-B drafts with fixed or live review on verified public evidence."""

import argparse
import json
import os
import platform
import sqlite3
import sys
from contextlib import closing
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import eval_product_generation_r4b as baseline
from scripts import product_generation_guard as guard


FIXTURES = baseline.ROOT / "tests/fixtures/product_generation_r5b"
HISTORICAL = baseline.ROOT / "artifacts/TASK-044-T2-FORENSICS"
DEFAULT_REPLAY = HISTORICAL / "guard-live-01/rows.jsonl"
_SOURCE_KEYS = ("chunk_id", "document_id", "location", "scope", "version", "quote", "original_sha256")


def _load_replay(path, frozen):
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    archived = {item["id"]: item for item in records}
    baseline.require(len(records) == len(archived) and set(archived) == set(frozen["inputs"]),
                     "replay denominator differs from freeze")
    return archived


def _verify_draft(archived, source, frozen_input):
    current = source["expected_wire"]
    baseline.require(archived["source"]["expected_wire"] == current, "archived question or generation input drifted")
    generated = archived["generated"]
    record, = generated["http_attempts"]
    body = record["body"]
    wire = {key: value for key, value in current.items() if key != "timeout"}
    baseline.require(body == wire and record["body_sha256"] == baseline.digest(
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    ) and record["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), current["timeout"]),
        "archived HTTP bytes or parameters differ from freeze")
    sdk, = generated["sdk_capture"]
    baseline.require({**sdk["request"], "thinking": {"type": "disabled"}} == current and sdk["sdk_max_retries"] == 0,
                     "archived SDK input differs from freeze")
    parsed = json.loads(generated["raw"])
    baseline.require(generated["status"] == "generated" and generated["finish_reason"] == "stop"
                     and parsed == generated["parsed_json"]
                     and all(parsed[k] == generated[k] for k in ("answer", "citations")),
                     "archived draft bytes differ from parsed answer")
    entries = archived["source"]["readback"]
    evidence = [{"id": e["id"], "text": e["quote"]} for e in entries]
    baseline.require(evidence == frozen_input["evidence"]
                     and _pins([e["readback"] for e in entries]) == _pins([e["readback"] for e in source["readback"]]),
                     "archived source text, order, ID or binding differs from current evidence")
    return {
        "evidence_sha256": baseline.json_digest(evidence),
        "raw_sha256": baseline.digest(generated["raw"].encode()),
        "answer_sha256": baseline.digest(generated["answer"].encode()),
        "wire_sha256": frozen_input["sdk_payload_sha256"],
        "http_body_sha256": record["body_sha256"],
    }


def _pins(checks):
    return [tuple(check[key] for key in _SOURCE_KEYS) for check in checks if check["ok"]]


def _current_check(client, conn, auth_instance, users, auth, index, case, checks):
    scope = baseline.rag._scope(case["target"])
    principal = users[case["principal"]]
    headers = auth[case["principal"]]
    token = headers["Authorization"].removeprefix("Bearer ")

    def scope_state():
        session = auth_instance.validate_token(token, connection=conn)
        return {"session_current": bool(session and session["user_id"] == principal),
                "sku_readable": baseline.listing.scope_error(conn, principal, *scope) is None,
                "active_version": baseline.documents.active_version(conn, scope)}

    before = scope_state()
    _, fresh, _, active = baseline.verify(client, conn, users, auth, index, case, capture_wire=False)
    after = scope_state()
    allowed = {"session_current": True, "sku_readable": True, "active_version": case["version"]}
    return {"current": before == after == allowed and active == case["version"] and all(c["ok"] for c in fresh)
            and _pins(fresh) == _pins(checks),
            "before": before, "after": after,
            "get": [{"chunk_id": c["chunk_id"], "http": c["http"], "ok": c["ok"]} for c in fresh]}


def _template(row, missing):
    # These labels are separately verified against the fixture's source inventory.
    blocks = [row["reply"]]
    if row["retrieval_trace"]["status"] == "conflict":
        blocks.insert(0, "以下现行资料对同一对象和工况的记载不一致。")
    if missing:
        blocks.append("证据不足。本次检索缺少以下依据：" + "、".join(missing) + "。")
    return "\n".join(filter(None, blocks))


def _review(provider, row, checks, generated, still_current, *, verified_template):
    attempts = []

    def on_request(request):
        wire = json.loads(request.content)
        expected = {"question": row["input"]["query"],
                    "evidence": [{"id": i, "text": check["quote"]} for i, check in enumerate(checks, 1)],
                    "answer": generated["answer"], "citations": generated["citations"]}
        if row["retrieval_trace"]["status"] == "conflict":
            expected["source_relationship"] = "confirmed_same_context_conflict"
        system, user = wire["messages"]
        baseline.require(system == {"role": "system", "content": guard._REVIEW}
                         and user["role"] == "user" and json.loads(user["content"]) == expected,
                         "live review payload differs from the pinned public replay")
        attempts.append({"body_sha256": baseline.digest(request.content), "status": None})

    def on_response(response):
        attempts[-1]["status"] = response.status_code

    live_client = getattr(provider, "_client", None)
    if live_client is None:
        result = guard.review_generation(provider, row, checks, generated, still_current=still_current,
                                         verified_template=verified_template)
        sdk_received = provider.calls
    else:
        events = {"request": [on_request], "response": [on_response]}
        endpoint = type(live_client.chat.completions)
        sdk_create = endpoint.create
        sdk_received = 0

        def capture(completions, **request):
            nonlocal sdk_received
            sdk_received += 1
            return sdk_create(completions, **request)

        with patch.object(live_client._client, "_event_hooks", events), patch.object(endpoint, "create", capture):
            result = guard.review_generation(provider, row, checks, generated, still_current=still_current,
                                             verified_template=verified_template)
    baseline.require(sdk_received == result["review_requests"] and sdk_received <= 1,
                     "review dispatch differs from SDK receipt")
    return result, {"sdk_received": sdk_received, "http_attempts": attempts}


def _verify_template(template, source, missing, status):
    entries = source["readback"]
    links = [baseline.CHUNKS + entry["readback"]["chunk_id"] for entry in entries]
    blocks = []
    for entry, link in zip(entries, links, strict=True):
        baseline.require(entry["readback"]["ok"] and entry["quote"] == entry["readback"]["quote"],
                         "template evidence failed readback")
        heading = " · ".join(entry[key] for key in ("filename", "location", "section"))
        blocks.append(f"{heading}\n{entry['quote']}\n原文回查（需登录） {link}")
    expected = _template({"reply": "\n".join(blocks), "retrieval_trace": {"status": status}}, missing)
    baseline.require(template == expected, "template adds or changes a fact, condition, attribution or gap")
    return {"exact_verified_excerpts": len(entries), "links": len(links), "missing": missing}


def run(output, replay, mode, reference=None, *, replay_review=False, protocol_probe=False):
    plan = baseline.read(baseline.FIXTURE / "plan.json")
    frozen = baseline.read(baseline.FROZEN)
    baseline.require(frozen == baseline.freeze_spec(plan), "original generation freeze changed")
    profile = frozen["profile"]
    archived = _load_replay(replay, frozen)
    archived_initial = _load_replay(DEFAULT_REPLAY, frozen)
    archived_second = _load_replay(HISTORICAL / "guard-review-02/rows.jsonl", frozen)
    reference_rows = _load_replay(reference, frozen) if reference else None
    missing_path = FIXTURES / "verified-missing.json"
    fixed_path = FIXTURES / "fixed-reviews.json"
    verified_missing = baseline.read(missing_path)
    fixed_reviews = baseline.read(fixed_path) if mode == "fixed" else None
    baseline.require(fixed_reviews is None or fixed_reviews.keys() == archived.keys(), "fixed review denominator drifted")
    baseline.database.settings.JWT_SECRET = baseline.token_urlsafe(48)
    output.mkdir(parents=True)
    baseline.write_json(output / "config.json", {
        "mode": mode, "python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
        "openai": version("openai"), "command": [sys.executable, *sys.orig_argv[1:]],
        "freeze_sha256": baseline.digest(baseline.FROZEN.read_bytes()),
        "profile": profile, "prompt_sha256": frozen["prompt_sha256"],
        "review_prompt_sha256": baseline.digest(guard._REVIEW.encode()),
        "runner_sha256": baseline.digest(Path(__file__).read_bytes()),
        "guard_sha256": baseline.digest(Path(guard.__file__).read_bytes()),
        "verified_missing_sha256": baseline.digest(missing_path.read_bytes()),
        "fixed_reviews_sha256": baseline.digest(fixed_path.read_bytes()) if fixed_reviews is not None else None,
        "replay_sha256": baseline.digest(replay.read_bytes()), "retry": 0,
        "reference_sha256": baseline.digest(reference.read_bytes()) if reference else None,
        "replay_review_response": replay_review,
        "protocol_probe": protocol_probe,
    })
    provider = None
    if mode == "live":
        key = os.environ.get("GLM_API_KEY") or baseline.dotenv_values(baseline.ROOT / ".env").get("GLM_API_KEY") or ""
        if not key.strip():
            baseline.write_json(output / "run.json", {"mode": mode, "generation_requests": 0, "sdk_received": 0,
                                                       "http_requests": 0, "http_responses": 0, "skipped": len(archived),
                                                       "passed": 0, "rejected": 0, "errors": {"credential_missing": len(archived)},
                                                       "exit_code": 2})
            return 2
        baseline.database.settings.GLM_API_KEY = key
        baseline.database.settings.GLM_MODEL = profile["model"]
        provider = baseline.ProviderFactory.create("glm")
        baseline.require(provider.model == profile["model"], "unexpected review model")

    totals = {"mode": mode, "generation_requests": 0, "sdk_received": 0, "http_requests": 0, "http_responses": 0,
              "passed": 0, "rejected": 0, "skipped": 0, "errors": {}, "deliveries": {}, "issue_rows": {},
              "archived_generation_statuses": {}, "verified_template_deliveries": 0}
    if protocol_probe:
        totals["protocol_probes"] = {"sdk_received": 0, "verified_template_deliveries": 0}
    with TemporaryDirectory(prefix="guard-", dir=output) as sandbox:
        baseline.use_sandbox(Path(sandbox) / "isolated.sqlite3")
        baseline.database.init_db()
        from fast_server import app, auth_instance

        options = {"loop_factory": baseline.InProcessLoop} if os.name == "nt" else {}
        with (
            closing(baseline.database.get_db()) as conn,
            closing(baseline.TestClient(app, backend_options=options)) as client,
            (output / "rows.jsonl").open("x", encoding="utf-8") as journal,
        ):
            users = baseline.setup_fixture(conn, plan)
            for release in plan["releases"]:
                baseline.require(baseline.rag.import_release(
                    conn, release, baseline.FIXTURE / "sources", embedding=lambda texts: [[1.0, 0.0]] * len(texts),
                ), "source import failed")
                if release["version"] == "proof-v1":
                    baseline.rag.activate(conn, release["target"], release["version"])
            auth = {name: baseline.login(client, name) for name in users}
            checked, inventory, gates, probes = baseline.preflight(client, conn, users, auth, plan, frozen)
            baseline.require(all(item["passed"] for item in gates) and all(item["pass"] for item in probes),
                             "preflight authorization or transport control failed")
            baseline.write_json(output / "gates.json", {"authorization": gates, "provider_probes": probes})
            baseline.write_json(output / "source-inventory.json", inventory)
            index = baseline.source_index(plan)
            for case, row, checks, source in checked:
                case_id = case["id"]
                record = archived[case_id]
                pin_hashes = _verify_draft(record, source, frozen["inputs"][case_id])
                before = archived_second[case_id]
                baseline.require(record["generated"] == archived_initial[case_id]["generated"] == before["generated"]
                                 and record["source"]["expected_wire"] == before["source"]["expected_wire"],
                                 "comparison does not use the same archived draft and evidence")
                generated = record["generated"]
                r5b = reference_rows[case_id] if reference_rows else None
                if r5b is not None:
                    baseline.require(r5b["hashes"] == pin_hashes and r5b["generated"] == generated
                                     and r5b["source"]["readback"] == source["readback"],
                                     "R5-B reference differs from the archived draft or readback")
                missing = verified_missing.get(case_id, [])
                row["reply"] = _template(row, missing)
                template_audit = _verify_template(row["reply"], source, missing, row["retrieval_trace"]["status"])
                rechecks = []

                def still_current():
                    recheck = _current_check(client, conn, auth_instance, users, auth, index, case, checks)
                    rechecks.append(recheck)
                    return recheck["current"]

                if replay_review:
                    fixed_content = r5b["review"]["review_raw"]
                    reviewer = baseline.FixedProvider(profile["model"], fixed_content,
                                                      finish=r5b["review"].get("review_finish_reason", "stop"))
                elif fixed_reviews is not None:
                    reviewer = baseline.FixedProvider(profile["model"], json.dumps(fixed_reviews[case_id], ensure_ascii=False))
                else:
                    reviewer = provider
                review, transport = _review(reviewer, row, checks, generated, still_current,
                                            verified_template=row["reply"])
                status = review["review_status"]
                attempts = transport["http_attempts"]
                totals["sdk_received"] += transport["sdk_received"]
                totals["http_requests"] += len(attempts)
                totals["http_responses"] += sum(attempt["status"] is not None for attempt in attempts)
                if status in {"passed", "rejected", "skipped"}:
                    totals[status] += 1
                else:
                    totals["errors"][status] = totals["errors"].get(status, 0) + 1
                delivery = (review["delivery"] or {}).get("kind", "withdrawn")
                if delivery == "template":
                    baseline.require(review["delivery"]["answer"] == row["reply"] and rechecks[-1]["current"],
                                     "delivered template differs from the current verified template")
                totals["deliveries"][delivery] = totals["deliveries"].get(delivery, 0) + 1
                totals["verified_template_deliveries"] += int(delivery == "template")
                generated_status = generated["status"]
                totals["archived_generation_statuses"][generated_status] = (
                    totals["archived_generation_statuses"].get(generated_status, 0) + 1
                )
                categories = review["defects"] + (["input_gap"] if review["input_gaps"] else [])
                for category in categories:
                    totals["issue_rows"][category] = totals["issue_rows"].get(category, 0) + 1

                protocol = None
                if protocol_probe:
                    normal_rechecks, rechecks = rechecks, []
                    invalid = baseline.FixedProvider(profile["model"], '{"input_gaps":[],"issues":')
                    invalid_review, invalid_transport = _review(invalid, row, checks, generated, still_current,
                                                                verified_template=row["reply"])
                    baseline.require(invalid_review["review_status"] == "invalid_review"
                                     and invalid_review["review_raw"] == '{"input_gaps":[],"issues":'
                                     and invalid_review["delivery"] == {"kind": "template", "answer": row["reply"]}
                                     and rechecks[-1]["current"], "protocol probe failed safe delivery")
                    totals["protocol_probes"]["sdk_received"] += invalid_transport["sdk_received"]
                    totals["protocol_probes"]["verified_template_deliveries"] += 1
                    protocol = {"review": invalid_review, "transport": invalid_transport, "rechecks": rechecks,
                                "template_audit": template_audit}
                    rechecks = normal_rechecks

                journal.write(json.dumps({
                    "id": case_id, "hashes": pin_hashes, "source": source, "generated": generated,
                    "generation_requests_this_run": 0, "before": before["review"], "review": review,
                    "r5b_reference_review": r5b["review"] if r5b else None,
                    "verified_template": row["reply"], "verified_input_gaps": missing,
                    "template_audit": template_audit if delivery == "template" else None,
                    "review_transport": transport, "rechecks": rechecks,
                    "protocol_probe": protocol,
                }, ensure_ascii=False) + "\n")
                journal.flush()
                print(case_id, status, review["defects"], delivery, flush=True)
    totals["exit_code"] = 2 if totals["errors"] or totals["skipped"] else 0
    baseline.write_json(output / "run.json", totals)
    return totals["exit_code"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=("fixed", "live"), default="fixed")
    parser.add_argument("--replay", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--reference", type=Path, help="R5-B rows for the same archive, with original review verdicts")
    parser.add_argument("--replay-review", action="store_true", help="return the reference's saved responses through the fixed SDK")
    parser.add_argument("--protocol-probe", action="store_true", help="pair each fixed replay with an invalid SDK response")
    args = parser.parse_args()
    if args.replay_review and (not args.reference or args.mode != "fixed"):
        parser.error("--replay-review requires --reference and fixed mode")
    if args.protocol_probe and args.mode != "fixed":
        parser.error("--protocol-probe requires fixed mode")
    raise SystemExit(run(args.output.resolve(), args.replay, args.mode, args.reference,
                         replay_review=args.replay_review, protocol_probe=args.protocol_probe))
