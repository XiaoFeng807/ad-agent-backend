"""AD-056 regression coverage using synthetic receipts and independent SQLite reads."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.database import chat_groups, chat_storage, database, product_receipt_history
from backend.rag import product_evidence as rag
from tests.test_product_evidence import FixedEmbedding


USER, OTHER = 11, 22
SOURCE_PATH = "/api/product-evidence/chunks/"
MAIN = {"shop_id": "history-main", "marketplace_id": "US", "sku": "SYN-10"}
SECOND = {**MAIN, "shop_id": "history-second"}
QUESTION = "商品资料：history-main US SYN-10 校验值是多少？"
LEADING_EXPLANATION = "证据不足。本次检索缺少以下依据：清洁 · 内衬保养。"
EXPLANATION = "证据不足。本次检索缺少以下依据：质保期。"
NOTICE = "部分或全部商品资料引用已失效，无法回查。"


class ProductEvidenceHistoryTests(unittest.TestCase):
    def setUp(self):
        import fast_server

        root = Path(self.enterContext(TemporaryDirectory()))
        self.db = root / "history.sqlite3"
        for key, value in {
            "DIAGNOSIS_SANDBOX_DB_PATH": str(self.db), "DIAGNOSIS_MYSQL_ENABLED": False,
            "JWT_SECRET": "synthetic-history-regression-only",
        }.items():
            self.enterContext(patch.object(database.settings, key, value))
        database.init_db()
        with closing(database.get_db()) as conn:
            with conn:
                conn.executemany("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                                 [(uid, name, database.hash_password("synthetic"))
                                  for uid, name in ((USER, "owner"), (OTHER, "other"))])
                for target in (MAIN, SECOND):
                    shop, site, sku = target.values()
                    conn.execute("INSERT INTO listing_test_shops VALUES (?, ?, ?)", (shop, site, USER))
                    conn.execute("INSERT INTO listing_test_products VALUES (?, ?, ?, 1, ?, ?, 100, 1)",
                                 (shop, site, sku, "合成校验器", "[]"))
            self.old = self.add_source(conn, root, MAIN, "history-r1", "原校验", 17)
            self.add_source(conn, root, MAIN, "history-r2", "新校验", 27)
            self.valid = self.add_source(conn, root, SECOND, "history-r1", "独立校验", 38)
            self.other_version = self.add_source(conn, root, SECOND, "history-r3", "另一版本", 48)
            rag.activate(conn, MAIN, "history-r1")
            rag.activate(conn, SECOND, "history-r1")
        self.client = self.enterContext(TestClient(fast_server.app))
        self.owner = self.login("owner")
        self.other = self.login("other")

    def login(self, name):
        result = self.client.post("/api/login", json={"username": name, "password": "synthetic"})
        return {"Authorization": "Bearer " + result.raise_for_status().json()["data"]["token"]}

    def add_source(self, conn, root, target, version, section, value):
        filename = f"synthetic-{value}.txt"
        (root / filename).write_bytes(f"## {value} | {section}\n校验值为 {value}。\n".encode("utf-8"))
        rag.import_release(conn, {"target": target, "version": version,
                                 "documents": [{"id": "manual", "kind": "manual", "file": filename}]},
                           root, embedding=FixedEmbedding())
        chunk_id = conn.execute(
            """SELECT c.chunk_id FROM product_document_chunks c JOIN product_documents d ON d.id=c.source_id
               WHERE d.filename=? AND c.strategy='structured'""", (filename,),
        ).fetchone()["chunk_id"]
        return f"{filename} · page:{value} · {section}\n原文回查（需登录） {SOURCE_PATH}{chunk_id}"

    def original(self, block, headers=None):
        response = self.client.get(SOURCE_PATH + block.rsplit("/", 1)[1], headers=headers or self.owner)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response

    def history(self, group=None, *, exclude=None, headers=None):
        endpoint = f"/api/chat/conversations/{group}/history" if group else "/api/chat/history"
        response = self.client.get(endpoint, headers=headers or self.owner,
                                   params={"exclude_chat_view_id": exclude} if exclude else {})
        response.raise_for_status()
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["data"]

    def ordinary_turn(self, receipt, *, question=QUESTION, view=None, bound=True):
        question_id = chat_storage.save_message(USER, "user", question, 1, view)
        answer_id = chat_storage.save_message(USER, "assistant", receipt, 2, view,
                                              user_message_id=question_id if bound else None)
        return question_id, answer_id

    def group_turn(self, receipt, *, group=None, question=QUESTION):
        if group is None:
            group = chat_groups.create(USER, "合成工作群")["conversation_id"]
        run, _ = chat_groups.accept(USER, group, "synthetic-history-trace", question, 1)
        answer_id, _ = run.save_reply(receipt, 2)
        return group, run.run_id, answer_id

    def read_archive(self):
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as conn:
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in (
                "conversations", "conversation_turns", "chat_group_entries", "chat_group_runs",
            )}

    def cut_version(self):
        with closing(database.get_db()) as conn:
            rag.activate(conn, MAIN, "history-r2")

    def test_legacy_receipt_keeps_leading_explanation_after_version_switch(self):
        receipt = "\n\n".join((LEADING_EXPLANATION, "SYN-10 · history-r1", self.old))
        self.ordinary_turn(receipt, bound=False)
        group, _, _ = self.group_turn(receipt)
        archive = self.read_archive()
        self.assertEqual(self.history()[-1]["content"], receipt)
        self.assertEqual(self.history(group)["entries"][-1]["content"], receipt)
        self.assertEqual(self.original(self.old).status_code, 200)

        self.cut_version()

        expected = "\n\n".join((LEADING_EXPLANATION, NOTICE))
        with self.subTest(history="ordinary"):
            self.assertEqual(self.history()[-1]["content"], expected)
        with self.subTest(history="group"):
            self.assertEqual(self.history(group)["entries"][-1]["content"], expected)
        self.assertEqual(self.original(self.old).status_code, 404)
        self.assertEqual(self.read_archive(), archive)

    def test_legacy_heading_can_include_a_blank_line_in_the_version(self):
        version = "history annotated\n\nrevision details"
        with closing(database.get_db()) as conn:
            source = self.add_source(conn, self.db.parent, MAIN, version, "多段版本", 57)
            rag.activate(conn, MAIN, version)
        receipt = "\n\n".join((LEADING_EXPLANATION, f"SYN-10 · {version}", source, EXPLANATION))
        self.ordinary_turn(receipt)
        group, _, _ = self.group_turn(receipt)
        archive = self.read_archive()
        self.assertEqual(self.history()[-1]["content"], receipt)
        self.assertEqual(self.history(group)["entries"][-1]["content"], receipt)

        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                         (OTHER, MAIN["shop_id"], MAIN["marketplace_id"]))

        expected = "\n\n".join((LEADING_EXPLANATION, NOTICE, EXPLANATION))
        self.assertEqual(self.history()[-1]["content"], expected)
        self.assertEqual(self.history(group)["entries"][-1]["content"], expected)
        self.assertEqual(self.original(source).status_code, 404)
        self.assertEqual(self.read_archive(), archive)

    def test_legacy_version_with_heading_shaped_paragraph_is_fully_hidden_when_inactive(self):
        version = "history-annotated\n\n附记 · 仅供仓储"
        with closing(database.get_db()) as conn:
            source = self.add_source(conn, self.db.parent, MAIN, version, "多段版本", 57)
            rag.activate(conn, MAIN, version)
        receipt = "\n\n".join((LEADING_EXPLANATION, f"SYN-10 · {version}", source, EXPLANATION))
        self.ordinary_turn(receipt, bound=False)
        group, _, _ = self.group_turn(receipt)
        archive = self.read_archive()
        self.assertEqual(self.history()[-1]["content"], receipt)
        self.assertEqual(self.history(group)["entries"][-1]["content"], receipt)

        self.cut_version()

        expected = "\n\n".join((LEADING_EXPLANATION, NOTICE, EXPLANATION))
        self.assertEqual(self.original(source).status_code, 404)
        self.assertEqual(self.read_archive(), archive)
        with self.subTest(history="ordinary"):
            self.assertEqual(self.history()[-1]["content"], expected)
        with self.subTest(history="group"):
            self.assertEqual(self.history(group)["entries"][-1]["content"], expected)

    def test_mixed_receipt_preserves_active_source_shared_heading_and_explanation(self):
        heading = "SYN-10 · history-r1"
        receipt = "\n\n".join((LEADING_EXPLANATION, heading, self.old, self.valid, EXPLANATION))
        self.ordinary_turn(receipt)
        group, run_id, answer_id = self.group_turn(receipt)
        archive = self.read_archive()
        group_before = self.history(group)
        self.assertEqual(self.history()[-1]["content"], receipt)
        self.assertEqual(group_before["entries"][-1]["content"], receipt)
        self.assertEqual((self.original(self.old).status_code, self.original(self.valid).status_code), (200, 200))

        self.cut_version()
        expected = "\n\n".join((LEADING_EXPLANATION, heading, self.valid, NOTICE, EXPLANATION))
        self.assertEqual(self.history(), [
            {"role": "user", "priority": 1, "content": QUESTION},
            {"role": "assistant", "priority": 2, "content": expected},
        ])
        group_before["entries"][-1]["content"] = expected
        self.assertEqual(self.history(group), group_before)
        self.assertEqual(group_before["entries"][-1]["entry_id"], f"entry:{answer_id}")
        self.assertEqual(group_before["entries"][-1]["run_id"], run_id)
        self.assertEqual((self.original(self.old).status_code, self.original(self.valid).status_code), (404, 200))
        self.assertEqual(self.original(self.valid).json()["original"], "## 38 | 独立校验\n校验值为 38。\n")
        self.assertEqual(self.read_archive(), archive)
        self.assertEqual(self.history(headers=self.other), [])
        self.assertEqual(self.client.get(f"/api/chat/conversations/{group}/history", headers=self.other).status_code,
                         404)

    def test_mixed_receipt_drops_heading_when_it_belongs_to_stale_version(self):
        receipt = "\n\n".join(("SYN-10 · history-r1", self.old, self.other_version))
        self.ordinary_turn(receipt)
        self.cut_version()
        with closing(database.get_db()) as conn:
            rag.activate(conn, SECOND, "history-r3")
        self.assertEqual(self.history()[-1]["content"], self.other_version + "\n\n" + NOTICE)
        self.assertEqual((self.original(self.old).status_code, self.original(self.other_version).status_code), (404, 200))

    def test_legacy_group_and_ordinary_paging_keep_unbound_turn_and_unrelated_messages(self):
        old_receipt = "\n\n".join((EXPLANATION, "SYN-10 · history-r1", self.old))
        old_question_id, old_answer_id = self.ordinary_turn(old_receipt, bound=False)
        # A user may quote the whole receipt. An ordinary reply may mention a URL.
        unrelated_question = "请帮我记录下面的文本。\n\n" + old_receipt
        unrelated_answer = "测试日志中出现链接 " + self.old.split()[-1]
        other_question_id, other_answer_id = self.ordinary_turn(
            unrelated_answer, question=unrelated_question, view="active-view",
        )
        legacy = f"legacy-{USER}"
        self.assertIn(legacy, [group["conversation_id"] for group in chat_groups.list_groups(USER)])
        self.group_turn("群内新记录", group=legacy, question="群内新问题")
        archive = self.read_archive()
        self.assertEqual(len(archive["conversation_turns"]), 1)
        self.assertEqual(archive["conversation_turns"][0][:2], (other_question_id, other_answer_id))
        self.cut_version()

        expected_old = EXPLANATION + "\n\n" + NOTICE
        filtered = self.history(exclude="active-view")
        self.assertEqual([row["content"] for row in filtered], [QUESTION, expected_old])
        self.assertEqual([row["content"] for row in self.history()],
                         [QUESTION, expected_old, unrelated_question, unrelated_answer])
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            latest = chat_storage.read_messages(conn, USER, limit=2)
            previous = chat_storage.read_messages(conn, USER, before=other_question_id, limit=2)
            through = chat_storage.read_messages(conn, USER, through=old_answer_id, limit=1)
        self.assertEqual([row["content"] for row in latest], [unrelated_question, unrelated_answer])
        self.assertEqual(previous, filtered)
        self.assertEqual(through, [filtered[1]])

        entries = self.history(legacy)["entries"]
        self.assertEqual([row["entry_id"] for row in entries[:4]], [
            f"legacy:{key}" for key in (old_question_id, old_answer_id, other_question_id, other_answer_id)
        ])
        self.assertEqual([row["content"] for row in entries],
                         [QUESTION, expected_old, unrelated_question, unrelated_answer, "群内新问题", "群内新记录"])
        self.assertEqual(self.read_archive(), archive)

    def test_history_snapshot_is_consistent_during_a_release_switch(self):
        receipt = "\n\n".join(("SYN-10 · history-r1", self.old))
        self.ordinary_turn(receipt)
        check = product_receipt_history.product_documents.visible_receipt_headers

        def switch_after_message_read(*args):
            self.cut_version()
            return check(*args)

        with patch.object(product_receipt_history.product_documents, "visible_receipt_headers",
                          side_effect=switch_after_message_read):
            self.assertEqual(self.history()[-1]["content"], receipt)
        self.assertEqual(self.history()[-1]["content"], NOTICE)
        self.assertEqual(self.original(self.old).status_code, 404)
