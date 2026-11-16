"""Serve the real app with isolated SQLite data for the workbench browser checks."""

import argparse
import json
import subprocess
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch

from scripts.accept_task42_t1 import configure, serve
from scripts.run_task27_t5 import browser_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    output = parser.parse_args().output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    path = output / "sandbox.sqlite3"
    configure(path)

    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.captcha import captcha
    from backend.database import database, listing_operations as listing
    from tests.home_diagnosis_server import demo_captcha
    from tests.test_listing_workgroup import ListingModel

    database.init_db()
    password = token_urlsafe(24)
    with closing(database.get_db()) as conn, conn:
        owner = conn.execute("INSERT INTO users (username,password) VALUES (?,?)",
                             ("workbench-qa", database.hash_password(password))).lastrowid
        conn.execute("INSERT INTO users (username,password) VALUES (?,?)",
                     ("workbench-qa-empty", database.hash_password(password)))
        listing.seed_test_product(conn, owner)
        listing.seed_test_product(conn, owner, sku="WORKBENCH-CONTROL")
        row = conn.execute("SELECT facts FROM listing_test_facts WHERE sku=?", (listing.TEST_SKU,)).fetchone()
        facts = json.loads(row["facts"])
        facts[0]["statement"] = "The test bottle has a documented capacity of 500 mL for carrying water."
        facts[0]["options"][1] = ("Holds 500 mL of water for everyday use, with the capacity specified on the "
                                   "product sheet so you can compare it with other bottles before choosing a size.")
        conn.execute("UPDATE listing_test_facts SET facts=? WHERE sku=?", (json.dumps(facts), listing.TEST_SKU))
        row = conn.execute("SELECT bullets FROM listing_test_products WHERE sku=?", (listing.TEST_SKU,)).fetchone()
        bullets = json.loads(row["bullets"])
        bullets[0] = ("A bottle for daily use, designed to carry water during a commute, an afternoon at work, "
                      "or any other part of a regular day when you want to have water nearby.")
        conn.execute("UPDATE listing_test_products SET bullets=? WHERE sku=?", (json.dumps(bullets), listing.TEST_SKU))
        conn.executemany("INSERT INTO conversations (user_id,role,content) VALUES (?,?,?)", [
            (owner, "user", "旧聊天问题"), (owner, "assistant", "旧聊天原文"),
        ])

    class WorkbenchModel(ListingModel):
        def create(self, **payload):
            result = super().create(**payload)
            messages = payload["messages"]
            if messages[0]["content"].startswith("Produce only a JSON object"):
                draft_input = json.loads(messages[-1]["content"])
                if "备选措辞" in draft_input["request"]:
                    result.choices[0].message.content = json.dumps({"bullets": [
                        {"fact_id": fact["fact_id"], "text": fact["options"][0]}
                        for fact in draft_input["facts"]["facts"][:5]
                    ]})
            return result

    with (patch.object(ProviderFactory, "create", return_value=WorkbenchModel()),
          patch.object(captcha, "generate_captcha", demo_captcha),
          patch("backend.memory.conversation_memory.store_conversation"),
          patch.object(observability, "TRACE_LOG_FILE", output / "traces.jsonl"),
          patch.object(observability, "FUNC_LOG_FILE", output / "functions.jsonl"),
          patch.object(observability, "ERROR_LOG_FILE", output / "errors.jsonl")):
        from fast_server import app

        with serve(app) as origin:
            return subprocess.run(
                ["node", "tests/workbench_browser.cjs", origin, str(output), str(path)],
                env={**browser_env(), "AD_WORKBENCH_PASSWORD": password}, timeout=200,
            ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
