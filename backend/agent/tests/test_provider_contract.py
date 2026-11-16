"""Capture real OpenAI SDK HTTP bodies; no external model calls or credentials."""

import json
import os
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from backend.agent.agent import Agent
from backend.agent.fault_tolerance import TIMEOUT_CONFIG
from backend.agent.llm_provider import ProviderFactory, create_completion
from backend.agent.orchestrator import OrchestratorAgent
from backend.agent.tests.test_home_delegation import HomeDelegationFixture, dispatch, reply, worker_summary
from backend.agent.tests.test_home_sse_tracing import (
    FALLBACK, FixedModel, HomeSSEFixture, REPLY, tool_call,
)
from backend.agent.tests.test_team_runtime import PLAN, task
from backend.config.config import settings


PROVIDER_CASES = {
    "openai": ("offline-openai", "openai-contract-model", "api.openai.com", "/v1/chat/completions"),
    "deepseek": ("offline-generic", "deepseek-contract-model", "api.deepseek.com", "/chat/completions"),
    "glm": ("offline-glm", "glm-4.5-air", "open.bigmodel.cn", "/api/paas/v4/chat/completions"),
}


class SDKCapture:
    def __init__(self, model=None):
        self.model = model or FixedModel()
        self.requests = []
        self.fail_dispatch = False
        self.expected_key = None
        self.string_plan_ids = False

    def handle(self, request):
        if self.expected_key:
            assert request.headers["authorization"] == f"Bearer {self.expected_key}"
        body = json.loads(request.content)
        self.requests.append({"body": body, "timeout": request.extensions["timeout"],
                              "host": request.url.host, "path": request.url.path})
        names = {tool["function"]["name"] for tool in body.get("tools") or []}
        if self.fail_dispatch and "query_data" in names:
            return httpx.Response(500, json={"error": {"message": "offline failure"}})
        if body.get("stream"):
            chunk = {"id": "offline", "object": "chat.completion.chunk", "created": 0,
                     "model": body["model"], "choices": [
                         {"index": 0, "delta": {"content": FALLBACK}, "finish_reason": None}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        msg = self.model.create(**deepcopy(body)).choices[0].message
        calls = [{"id": call.id, "type": "function", "function": {
            "name": call.function.name, "arguments": call.function.arguments,
        }} for call in msg.tool_calls or []]
        if self.string_plan_ids:
            for call in calls:
                if call["function"]["name"] == "get_plan_detail":
                    args = json.loads(call["function"]["arguments"])
                    call["function"]["arguments"] = json.dumps({"plan_id": str(args["plan_id"])})
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": msg.content,
                                                   "tool_calls": calls or None},
                         "finish_reason": "tool_calls" if calls else "stop"}],
        })

    def install(self, stack, provider):
        self.expected_key = PROVIDER_CASES[provider][0]
        for key, value in {"LLM_PROVIDER": provider, "MODEL": "deepseek-contract-model",
                           "OPENAI_MODEL": "openai-contract-model", "API_KEY": "offline-generic",
                           "DEEPSEEK_API_KEY": "offline-deepseek", "OPENAI_API_KEY": "offline-openai",
                           "GLM_API_KEY": "offline-glm", "GLM_MODEL": "glm-4.5-air",
                           "BASE_URL": "https://api.deepseek.com"}.items():
            stack.enter_context(patch.object(settings, key, value))
        stack.enter_context(patch.object(ProviderFactory, "_instances", {}))

        def client(**kwargs):
            transport = httpx.Client(transport=httpx.MockTransport(self.handle))
            sdk = OpenAI(**kwargs, http_client=transport, max_retries=0)
            stack.callback(sdk.close)
            return sdk

        stack.enter_context(patch("openai.OpenAI", side_effect=client))

    def assert_models(self, case, provider):
        case.assertTrue(self.requests)
        _, expected, host, path = PROVIDER_CASES[provider]
        case.assertEqual({request["body"]["model"] for request in self.requests}, {expected})
        case.assertEqual({request["host"] for request in self.requests}, {host})
        case.assertEqual({request["path"] for request in self.requests}, {path})
        for request in self.requests:
            case.assertEqual(request["timeout"]["read"], TIMEOUT_CONFIG["llm_call"])
            if provider in {"deepseek", "glm"}:
                case.assertEqual(request["body"]["thinking"], {"type": "disabled"})
            else:
                case.assertNotIn("thinking", request["body"])
        if path := os.environ.get("AD_PROVIDER_EVIDENCE"):
            evidence = {"scope": "offline_sdk_capture", "case": case.id(), "provider": provider,
                        "requests": [{"model": row["body"]["model"], "host": row["host"],
                                      "timeout": row["timeout"], "tools": row["body"].get("tools"),
                                      "tool_receipts": [{"tool_call_id": msg["tool_call_id"],
                                          "json_keys": sorted(json.loads(msg["content"]))
                                          if isinstance(json.loads(msg["content"]), dict) else [],
                                          "marker": json.loads(msg["content"]).get("marker")
                                          if isinstance(json.loads(msg["content"]), dict) else None}
                                          for msg in row["body"]["messages"] if msg["role"] == "tool"]}
                                     for row in self.requests]}
            with Path(path).open("a", encoding="utf-8") as archive:
                archive.write(json.dumps(evidence, ensure_ascii=False) + "\n")


class ProviderSDKTests(HomeSSEFixture):
    def tool_overrides(self):
        return {"get_dashboard_data": lambda **kwargs: {"success": True, "marker": "sdk-data"},
                "search_knowledge": lambda **kwargs: {"success": True, "marker": "sdk-rule"}}

    async def test_planner_orchestrator_and_workers_send_provider_model_and_receipts(self):
        from backend.agent.agent_pool import AgentPool
        for provider in PROVIDER_CASES:
            with self.subTest(provider=provider), ExitStack() as stack:
                wire = SDKCapture()
                wire.install(stack, provider)
                pool = AgentPool(min_size=1, max_size=1)
                stack.enter_context(patch.object(self.server, "agent_pool", pool))
                events = []
                await self.request(events=events)
                self.assertEqual(events[-1]["type"], "done")
                self.assertEqual("".join(e.get("content", "") for e in events if e["type"] == "text"), REPLY)
                coordinator = pool.acquire()
                try:
                    coordinator.orchestrator.analysis_agent.chat([{"role": "user", "content": "hello"}])
                finally:
                    pool.release(coordinator)
                wire.assert_models(self, provider)
                bodies = [request["body"] for request in wire.requests]
                self.assertTrue(any(body.get("max_tokens") == 500 for body in bodies), "planner request missing")
                definitions = {tool["function"]["name"]: tool["function"]
                               for body in bodies for tool in body.get("tools") or []}
                self.assertTrue({"query_data", "query_knowledge_base", "get_dashboard_data",
                                 "search_knowledge", "call_peer", "compare_plans"} <= definitions.keys())
                self.assertEqual(definitions["query_data"]["parameters"]["required"], ["query"])
                receipts = {msg["tool_call_id"]: json.loads(msg["content"])
                            for body in bodies for msg in body["messages"] if msg["role"] == "tool"}
                self.assertEqual(receipts["call_dashboard"]["marker"], "sdk-data")
                self.assertEqual(receipts["call_search"]["marker"], "sdk-rule")
                self.assertEqual(receipts["call_data"]["result"], "private-data-subagent-reply")
                self.assertEqual(receipts["call_knowledge"]["result"], "private-knowledge-subagent-reply")
                for body in bodies:
                    ids = {call["id"] for msg in body["messages"] for call in msg.get("tool_calls") or []}
                    self.assertTrue({m["tool_call_id"] for m in body["messages"] if m["role"] == "tool"} <= ids)


class GLMWorkerSDKTests(HomeDelegationFixture):
    async def test_group_dispatch_and_worker_tool_receipt_use_glm(self):
        from backend.agent.agent_pool import AgentPool
        from backend.database import chat_groups

        def main(receipts):
            if receipts:
                return reply("已核验本人计划")
            return reply("", dispatch("team", task("lookup")))

        self.worker = lambda _, receipts: (
            worker_summary("已取得计划详情") if receipts else reply("", tool_call("leaf", name=PLAN, plan_id=101))
        )
        self.select(main)
        with ExitStack() as stack:
            wire = SDKCapture(self.model)
            wire.install(stack, "glm")
            pool = AgentPool(min_size=1, max_size=1)
            stack.enter_context(patch.object(self.server, "agent_pool", pool))
            group_id = chat_groups.create(17, "GLM worker test")["conversation_id"]
            await self.request(body={"conversation_id": group_id, "message": "请查本人计划 #101"})
            self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
            self.assertEqual(chat_groups.history(17, group_id)["entries"][-1]["content"], "已核验本人计划")
            self.assertEqual(self.worker_receipts[-1]["leaf"]["result"]["id"], 101)
            self.assertEqual(self.main_receipts["team"]["evidence"][0]["result"]["id"], 101)
            self.assertTrue(any(row["body"].get("response_format") == {"type": "json_object"}
                                for row in wire.requests))
            wire.assert_models(self, "glm")


class ProviderFallbackTests(unittest.TestCase):
    def test_synchronous_fallback_uses_selected_model(self):
        for provider in PROVIDER_CASES:
            with self.subTest(provider=provider), ExitStack() as stack:
                wire = SDKCapture()
                wire.fail_dispatch = True
                wire.install(stack, provider)
                reply = OrchestratorAgent().chat([{"role": "user", "content": "hello"}])
                self.assertEqual(reply, FALLBACK)
                wire.assert_models(self, provider)
                self.assertGreaterEqual(len(wire.requests), 3)

    def test_streaming_fallback_and_retry_use_selected_model(self):
        for provider in PROVIDER_CASES:
            with self.subTest(provider=provider), ExitStack() as stack:
                wire = SDKCapture()
                original = wire.handle
                failures = [True]

                def fail_once(request):
                    if failures:
                        failures.pop()
                        body = json.loads(request.content)
                        wire.requests.append({"body": body, "timeout": request.extensions["timeout"],
                                              "host": request.url.host, "path": request.url.path})
                        return httpx.Response(500, json={"error": {"message": "offline retry"}})
                    return original(request)

                wire.handle = fail_once
                wire.install(stack, provider)
                orchestrator = OrchestratorAgent()
                stack.enter_context(patch.object(orchestrator, "chat", side_effect=RuntimeError("offline")))
                output = list(orchestrator.chat_stream([{"role": "user", "content": "hello"}], raise_on_error=True))
                self.assertIn(FALLBACK, "".join(output))
                wire.assert_models(self, provider)
                self.assertTrue(any(r["body"].get("stream") for r in wire.requests))
                self.assertEqual(len(wire.requests), 3)

    def test_provider_direct_methods_use_same_model(self):
        for provider in PROVIDER_CASES:
            with self.subTest(provider=provider), ExitStack() as stack:
                wire = SDKCapture()
                wire.install(stack, provider)
                client = ProviderFactory.create()
                client._chat([{"role": "user", "content": "hello"}])
                list(client.chat_stream([{"role": "user", "content": "hello"}]))
                wire.assert_models(self, provider)

    def test_deepseek_default_mode_preserves_caller_extra_body(self):
        with ExitStack() as stack:
            wire = SDKCapture()
            wire.install(stack, "deepseek")
            provider = ProviderFactory.create()
            extra_body = {"request_marker": "retained"}
            provider.chat.completions.create(
                model=provider.model, messages=[{"role": "user", "content": "hello"}],
                extra_body=extra_body,
            )
            self.assertEqual(extra_body, {"request_marker": "retained"})
            self.assertEqual(wire.requests[0]["body"]["request_marker"], "retained")
            wire.assert_models(self, "deepseek")

    def test_config_changes_refresh_cached_sdk_and_existing_provider_keeps_model(self):
        with ExitStack() as stack:
            wire = SDKCapture()
            wire.install(stack, "openai")
            first = ProviderFactory.create("OpenAI")
            self.assertIs(first, ProviderFactory.create())
            with patch.object(settings, "OPENAI_MODEL", "changed-model"):
                second = ProviderFactory.create()
                self.assertIsNot(first, second)
                for client in (first, second):
                    client._chat([{"role": "user", "content": "hello"}])
            self.assertEqual([r["body"]["model"] for r in wire.requests],
                             ["openai-contract-model", "changed-model"])

    def test_deepseek_dedicated_key_fallback_keeps_generic_model(self):
        with ExitStack() as stack:
            wire = SDKCapture()
            wire.install(stack, "deepseek")
            stack.enter_context(patch.object(settings, "API_KEY", ""))
            wire.expected_key = "offline-deepseek"
            agent = Agent()
            self.assertEqual(agent.chat([{"role": "user", "content": "hello"}]), '{"steps": []}')
            wire.assert_models(self, "deepseek")

    def test_glm_uses_only_its_own_key_and_text_only_turn_omits_tools(self):
        with ExitStack() as stack:
            wire = SDKCapture()
            wire.install(stack, "glm")
            with patch.object(settings, "GLM_API_KEY", ""):
                with self.assertRaisesRegex(ValueError, "GLM_API_KEY"):
                    ProviderFactory.create()
            provider = ProviderFactory.create()
            tools = [{"type": "function", "function": {"name": "lookup"}}]
            messages = [{"role": "user", "content": "hello"}]
            create_completion(provider, None, role="orchestrator", step="summary",
                              model=provider.model, messages=messages, tools=tools, tool_choice="none")
            self.assertNotIn("tool_choice", wire.requests[-1]["body"])
            self.assertNotIn("tools", wire.requests[-1]["body"])
            wire.assert_models(self, "glm")


if __name__ == "__main__":
    unittest.main()
