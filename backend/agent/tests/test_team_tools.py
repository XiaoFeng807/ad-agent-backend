"""Unit tests for the prototype's per-call authorization boundary."""

from threading import Event, Thread
import unittest

from backend.agent.team_tools import GuardedTools, TeamBudget


class TeamToolTests(unittest.TestCase):
    def test_rejects_unauthorized_tool_and_forged_identity_before_execution(self):
        calls = []
        tools = GuardedTools(user_id=17, allowed_tools={"get_plan_detail"},
                             functions={"get_plan_detail": lambda **args: calls.append(args)},
                             budget=TeamBudget())
        self.assertEqual(tools.call("search_knowledge", {"query": "x"}, tool_call_id="1")["error_code"],
                         "unauthorized_tool")
        self.assertEqual(tools.call("get_plan_detail", {"plan_id": 1, "user_id": 18},
                                    tool_call_id="2")["error_code"], "invalid_tool_arguments")
        self.assertEqual(calls, [])

    def test_injects_authenticated_identity_and_checks_parameters(self):
        calls = []
        def read(**args):
            calls.append(args)
            return {"id": args["plan_id"], "user_id": args["user_id"]}
        tools = GuardedTools(user_id=17, allowed_tools={"get_plan_detail"},
                             functions={"get_plan_detail": read}, budget=TeamBudget())
        self.assertFalse(tools.call("get_plan_detail", {"plan_id": True}, tool_call_id="1")["success"])
        self.assertTrue(tools.call("get_plan_detail", {"plan_id": 9}, tool_call_id="2")["success"])
        self.assertEqual(calls, [{"plan_id": 9, "user_id": 17}])

    def test_budget_exhaustion_prevents_execution(self):
        calls = []
        tools = GuardedTools(user_id=17, allowed_tools={"get_plan_detail"},
                             functions={"get_plan_detail": lambda **args: calls.append(args) or
                                        {"id": args["plan_id"], "user_id": args["user_id"]}},
                             budget=TeamBudget(tool_limit=1))
        self.assertTrue(tools.call("get_plan_detail", {"plan_id": 1}, tool_call_id="1")["success"])
        self.assertEqual(tools.call("get_plan_detail", {"plan_id": 2}, tool_call_id="2")["error_code"],
                         "budget_exhausted_or_cancelled")
        self.assertEqual(len(calls), 1)

    def test_cancelled_inflight_result_is_not_admitted(self):
        entered, release = Event(), Event()
        budget = TeamBudget()
        def read(**args):
            entered.set()
            release.wait(5)
            return {"id": args["plan_id"], "user_id": args["user_id"]}
        tools = GuardedTools(user_id=17, allowed_tools={"get_plan_detail"},
                             functions={"get_plan_detail": read}, budget=budget)
        results = []
        thread = Thread(target=lambda: results.append(tools.call("get_plan_detail", {"plan_id": 1}, tool_call_id="1")))
        thread.start()
        self.assertTrue(entered.wait(5))
        budget.cancel()
        release.set()
        thread.join(5)
        self.assertEqual(results[0]["error_code"], "expired_receipt")
        self.assertEqual(tools.call("get_plan_detail", {"plan_id": 2}, tool_call_id="2")["error_code"],
                         "budget_exhausted_or_cancelled")

    def test_only_failed_item_is_retried_once_with_original_scope(self):
        calls = []
        def read(**args):
            calls.append(args)
            if args["plan_id"] == 2:
                return {"success": False, "error_code": "temporary_tool_failure"}
            return {"id": args["plan_id"], "user_id": args["user_id"]}
        tools = GuardedTools(user_id=17, allowed_tools={"get_plan_detail"},
                             functions={"get_plan_detail": read}, budget=TeamBudget())
        good = tools.call_with_retry("get_plan_detail", {"plan_id": 1}, tool_call_id="good")
        bad = tools.call_with_retry("get_plan_detail", {"plan_id": 2}, tool_call_id="bad")
        self.assertEqual(len(good), 1)
        self.assertEqual(len(bad), 2)
        self.assertEqual(calls, [{"plan_id": 1, "user_id": 17}, {"plan_id": 2, "user_id": 17},
                                 {"plan_id": 2, "user_id": 17}])


if __name__ == "__main__":
    unittest.main()
