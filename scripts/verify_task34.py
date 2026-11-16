"""Chromium acceptance for workgroup sandbox pause, using the #103 independent review."""

import json
import re
import subprocess
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from scripts.verify_task28 import ROOT, login, main, t5, write
from scripts.verify_task32 import CASE, audit_progress


def verify_pause(url, output, password, sync, provider, audit, sql, oracle, redactor, report):
    import fast_server
    from backend.config.config import settings
    from backend.database import chat_groups, database, diagnosis_pauses

    folder = output / "group-pause"
    folder.mkdir()
    report["cases"]["task34_group_pause"] = "fail"
    audit.case = "group-pause"
    provider.split_old_metrics = False
    provider.probe_old_plan = False
    sync.reset("idle")
    control = FastAPI()

    @control.post("/hold")
    def hold():
        sync.reset("late")
        sync.metric_filter = lambda args: args[0] == 103
        return {}

    fast_server.app.mount("/__task34", control)
    diagnosis_pauses.register_sandbox_target(17, 103)
    result_path = folder / "browser.json"
    fixture_path = folder / "fixture.json"
    sandbox = database.sandbox_db_path()
    write(fixture_path, {"message": CASE.message.replace("#102", "#103"), "sqlite": str(sandbox)})
    completed = subprocess.run(
        ["node", "tests/task34_browser.cjs", url, str(fixture_path), str(result_path)],
        cwd=ROOT, env={**t5.browser_env(), "AD_TASK34_PASSWORD": password},
        capture_output=True, encoding="utf-8", timeout=200,
    )
    if completed.returncode:
        error = re.sub(r"(Bearer )\S+", r"\1[redacted]", redactor.clean(completed.stderr))
        (folder / "browser-error.txt").write_text(error, encoding="utf-8")
        raise AssertionError(error)
    browser = t5.read(result_path)
    receipt = browser["http"]["post"]["body"]["data"]
    traces = [json.loads(line) for line in (output / "trace_log_file.jsonl").read_text(encoding="utf-8").splitlines()]
    execution, = [row for row in traces if row["trace_id"] == receipt["execution_trace_id"]]
    source, = [row for row in traces if row["trace_id"] == receipt["source_trace_id"]]
    readbacks = [row for row in traces if any(
        step["action"] == "read_pause_status" and receipt["receipt_id"] in step.get("detail", "")
        for step in row["steps"]
    )]
    assert source["trace_id"] == browser["source"]["trace_id"]
    assert any(step["action"] == "pause_committed" and receipt["receipt_id"] in step.get("detail", "")
               for step in execution["steps"])
    assert len(readbacks) >= 2
    write(folder / "traces.json", [source, execution, *readbacks])
    write(folder / "stop-timeline.json", redactor.clean(sync.timeline))

    # Keep the same isolated fixture while disabling the sandbox configuration.
    with httpx.Client(base_url=url, timeout=45, trust_env=False) as owner:
        login(owner, "task27-owner", password)
        before = sql("SELECT * FROM chat_group_pause_receipts ORDER BY receipt_id", rows=True)
        with (patch.object(database, "DB_PATH", str(sandbox)),
              patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", "")):
            path = browser["http"]["post"]["path"]
            rejected = owner.post(path)
            assert rejected.status_code == 404 and "未启用" in rejected.json()["detail"]
            group = chat_groups.create(17, "沙箱关闭校验")
            run, _ = chat_groups.accept(17, group["conversation_id"], "disabled-test", CASE.message, 1)
            entry_id, _ = run.save_reply("沙箱关闭校验", 1, evidence=receipt["evidence"])
            assert not sql("SELECT * FROM chat_group_pause_proposals WHERE entry_id=?", (entry_id,), rows=True)
        assert sql("SELECT * FROM chat_group_pause_receipts ORDER BY receipt_id", rows=True) == before
    write(folder / "disabled.json", {"status": rejected.status_code, "body": rejected.json(),
                                     "new_proposals": 0, "receipts_unchanged": True})
    report["cases"]["task34_group_pause"] = "pass"


if __name__ == "__main__":
    main(CASE, __doc__, extra_browser=verify_pause, audit_browser=audit_progress)
