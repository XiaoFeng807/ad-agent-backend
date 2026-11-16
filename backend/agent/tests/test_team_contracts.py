"""Tests for the request-local team plan and receipt contracts."""

import unittest

from pydantic import ValidationError

from backend.agent.team_contracts import ReceiptBoard, TeamPlan


def task(task_id, *, dependencies=(), refs=(), capabilities=("read",)):
    return {"task_id": task_id, "objective": "collect evidence", "input_refs": refs,
            "capabilities": capabilities, "dependencies": dependencies,
            "expected_output": "receipt", "stop_condition": "evidence received"}


def plan(*tasks):
    return TeamPlan.model_validate({"reason": "independent evidence", "tasks": tasks})


def receipt(board, task_id, attempt, artifact_id, *, status="succeeded", result=None, **kwargs):
    return board.accept(run_id=board.run_id, task_id=task_id, attempt=attempt,
                        artifact_id=artifact_id, status=status, source="test",
                        tool_call_id=f"call-{artifact_id}", result=result, **kwargs)


class TeamContractTests(unittest.TestCase):
    def test_graph_rejects_cycles_missing_and_undeclared_references(self):
        for tasks in ((task("a", dependencies=("b",)), task("b", dependencies=("a",))),
                      (task("a", dependencies=("missing",)),),
                      (task("a"), task("b", refs=("a",)))):
            with self.subTest(tasks=tasks), self.assertRaises(ValidationError):
                plan(*tasks)

    def test_allowlist_is_server_owned(self):
        candidate = plan(task("a", capabilities=("read", "write")))
        with self.assertRaisesRegex(ValueError, "unauthorized_capability"):
            candidate.authorize({"read"})

    def test_parallel_same_capability_and_dependency_inputs_are_isolated(self):
        board = ReceiptBoard(plan(task("a"), task("b"),
                                  task("c", dependencies=("a", "b"), refs=("a", "b"))))
        a, _ = board.start("a")
        b, _ = board.start("b")
        with self.assertRaisesRegex(ValueError, "dependency_not_accepted"):
            board.start("c")
        receipt(board, "a", a, "same", result={"value": 1})
        receipt(board, "b", b, "same", result={"value": 2})
        _, inputs = board.start("c")
        self.assertEqual([inputs[key][0]["result"]["value"] for key in ("a", "b")], [1, 2])
        inputs["a"][0]["result"]["value"] = 999
        self.assertEqual(board.snapshot()["receipts"][0]["result"]["value"], 1)

    def test_cross_run_and_cancelled_receipts_are_rejected(self):
        first, second = ReceiptBoard(plan(task("a"))), ReceiptBoard(plan(task("a")))
        attempt, _ = first.start("a")
        second.start("a")
        self.assertFalse(second.accept(run_id=first.run_id, task_id="a", attempt=attempt,
                                       artifact_id="x", status="succeeded", source="test",
                                       tool_call_id="call-x"))
        first.cancel()
        self.assertFalse(receipt(first, "a", attempt, "late"))
        self.assertEqual(first.snapshot()["receipts"], [])

    def test_direct_receipt_contains_its_board_run_id(self):
        board = ReceiptBoard(None, run_id="listing-run")
        direct = {"artifact_id": "sales", "source": "get_listing_sales", "status": "succeeded"}
        self.assertTrue(board.accept_direct(direct))
        self.assertEqual(board.get_artifact("sales"), {**direct, "run_id": "listing-run"})
        self.assertEqual(board.tool_receipts("get_listing_sales"), [board.get_artifact("sales")])

    def test_retry_keeps_success_and_rejects_late_first_attempt(self):
        board = ReceiptBoard(plan(task("a")))
        first, _ = board.start("a")
        receipt(board, "a", first, "good", result=1, final=False)
        receipt(board, "a", first, "bad", status="temporary_failure", error_code="temporary_tool_failure")
        second, _ = board.start("a")
        self.assertFalse(receipt(board, "a", first, "late"))
        receipt(board, "a", second, "bad", result=2, retry_of="call-bad")
        self.assertEqual([(r["artifact_id"], r["status"]) for r in board.snapshot()["receipts"]],
                         [("good", "succeeded"), ("bad", "temporary_failure"), ("bad", "succeeded")])

    def test_retry_limit_and_duplicate_artifacts(self):
        board = ReceiptBoard(plan(task("a")))
        first, _ = board.start("a")
        receipt(board, "a", first, "x", final=False)
        with self.assertRaisesRegex(ValueError, "duplicate_artifact"):
            receipt(board, "a", first, "x")
        receipt(board, "a", first, "failed", status="temporary_failure")
        second, _ = board.start("a")
        receipt(board, "a", second, "failed", status="temporary_failure")
        with self.assertRaisesRegex(ValueError, "retry_limit"):
            board.start("a")


if __name__ == "__main__":
    unittest.main()
