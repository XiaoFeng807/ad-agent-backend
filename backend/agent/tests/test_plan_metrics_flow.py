"""Real ASGI/SQLite/RAG chain, fixed model and explicitly synthetic metrics."""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from backend import observability
from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.agent.tests.test_ad_diagnosis_eval import StatefulDiagnosisModel
from backend.agent.tests.test_home_sse_tracing import HomeSSEFixture, tool_call
from backend.agent.tests.test_plan_metrics import synthetic_artifact
from backend.config.config import settings
from backend.database import database
from backend.rag import rag_knowledge
from backend.rag.review_rule_demo import seed_review_rules
from backend.tools import tools
from scripts.eval_review_retrieval import FixedReviewEmbedding


class PlanMetricsDiagnosisModel(StatefulDiagnosisModel):
    def create(self, messages, tools=None, **kwargs):
        names={tool["function"]["name"] for tool in tools or []}
        if "get_plan_metrics_by_date" in names:
            self.inputs.append((names,deepcopy(messages)))
            state=json.loads(next(line.removeprefix("【本轮诊断参数】") for line in messages[0]["content"].splitlines()
                                  if line.startswith("【本轮诊断参数】")))
            return self.response(None,[tool_call("call_plan",name="get_plan_detail",plan_id=str(state["plan_id"])),
                *(tool_call("call_"+window,name="get_plan_metrics_by_date",plan_id=str(state["plan_id"]),**state[window])
                  for window in ("baseline","current"))])
        return super().create(messages,tools=tools,**kwargs)


class PlanMetricsFlowTests(HomeSSEFixture):
    model_class=PlanMetricsDiagnosisModel

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(patch.object(tools,"get_db",self.connect))
        _,artifact,manifest=synthetic_artifact(self.database.parent,((1000,100),(1000,80),(2000,100),(2000,95)))
        self.enterContext(patch.object(settings,"PLAN_METRICS_DB_PATH",str(artifact)))
        with closing(self.connect()) as conn,conn:
            conn.execute("INSERT INTO ad_plans(id,plan_name,user_id) VALUES (101,'fixture',17)")
            conn.execute("INSERT INTO plan_metric_bindings VALUES (101,?,?, 'fixture-campaign')",
                         (manifest["source_id"],manifest["data_version"]))
        self.enterContext(patch.object(settings,"RAG_DB_PATH",str(self.database.parent/"rag")))
        self.enterContext(patch.object(rag_knowledge,"BGEEmbedding",FixedReviewEmbedding))
        self.rag=self.enterContext(closing(rag_knowledge.get_rag_knowledge()))
        self.addCleanup(rag_knowledge._open_knowledge.cache_clear)
        seed_review_rules(self.rag)
        self.client=await self.enterAsyncContext(AsyncClient(transport=ASGITransport(app=self.server.app),base_url="http://test",
                                                            headers={"authorization":"Bearer "+self.token}))

    async def diagnose(self,task_id=None):
        first=task_id is None
        params={"plan_id":101,"rule_id":"ctr-review-v1#low-ctr"} if first else {"task_id":task_id}
        for key,day in (("baseline",1 if first else 3),("current",2 if first else 4)):
            params[key]={"start_date":f"2026-10-{day:02}","end_date":f"2026-10-{day:02}"}
        response=await self.client.post("/api/chat/stream",json={"diagnosis":params})
        self.assertEqual(response.status_code,200)
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events],["thinking","text","done"])
        return events

    async def test_two_rounds_new_pool_confirm_first_and_read_saved_numbers(self):
        first=await self.diagnose()
        proposal=first[-1]["review_proposal"]
        self.assertEqual(proposal["evidence_gaps"],[])
        self.assertEqual(proposal["calculation"]["relative_decline_pct"],20)
        self.assertTrue(proposal["rule_evaluation"]["matched"])
        with patch.object(ProviderFactory,"create",return_value=PlanMetricsDiagnosisModel()):
            self.server.agent_pool=AgentPool(min_size=1,max_size=1)
        second=await self.diagnose(proposal["task_id"])
        self.assertEqual(second[-1]["task_id"],proposal["task_id"])
        self.assertNotEqual(second[-1]["trace_id"],proposal["source_trace_id"])
        self.assertFalse(second[-1]["review_proposal"]["rule_evaluation"]["matched"])
        confirmed=await self.client.post(f"/api/diagnosis/review-proposals/{proposal['action_id']}/confirm")
        self.assertEqual(confirmed.status_code,200)
        receipt=confirmed.json()["data"]
        # Clear runtime trace and reinitialize storage, then read only the persisted snapshot.
        with patch.object(observability,"_traces",{}):
            database.init_db()
            response=await self.client.get(f"/api/diagnosis/reviews/{receipt['review_id']}")
        readback=response.json()["data"]
        self.assertTrue(all(readback[k]==v for k,v in proposal.items()))
        self.assertEqual(readback["metrics"]["current"]["clicks"],80)
        self.assertEqual(readback["citations"],first[1]["citations"])
        other=self.sign_token(user_id=18)
        with closing(self.connect()) as conn,conn:
            conn.execute("INSERT INTO sessions(user_id,token) VALUES (18,?)",(other,))
        denied=await self.client.get(f"/api/diagnosis/reviews/{receipt['review_id']}",headers={"authorization":"Bearer "+other})
        self.assertEqual(denied.status_code,404)

    async def test_temporary_single_metric_retry_uses_same_shared_budget(self):
        attempts=[]
        original=tools.function_map["get_plan_metrics_by_date"]
        def flaky(**args):
            attempts.append(args)
            if args["start_date"]=="2026-10-02" and len([a for a in attempts if a["start_date"]=="2026-10-02"])==1:
                raise TimeoutError("synthetic transient")
            return original(**args)
        with patch.dict(tools.function_map,{"get_plan_metrics_by_date":flaky}):
            # The registry is built once per pool instance.
            with patch.object(ProviderFactory,"create",return_value=PlanMetricsDiagnosisModel()):
                self.server.agent_pool=AgentPool(min_size=1,max_size=1)
            events=await self.diagnose()
        self.assertEqual(len(attempts),3)
        self.assertEqual(events[1]["evidence_gaps"],[])
        trace=observability.get_trace(events[-1]["trace_id"])
        calls=[s for s in trace["steps"] if s["stage"]=="tool_call"]
        retries=[s for s in calls if s.get("retry_of")]
        self.assertEqual(len(retries),1)
        self.assertLessEqual(len(calls),12)
        self.assertTrue(any(s["runtime_call_id"]==retries[0]["retry_of"] and s["action"]=="get_plan_metrics_by_date" for s in calls))

    async def test_unmapped_source_fails_with_specific_evidence_gap(self):
        with closing(self.connect()) as conn,conn:
            conn.execute("DELETE FROM plan_metric_bindings")
        events=await self.diagnose()
        self.assertIsNone(events[1]["calculation"])
        self.assertIn("plan_history_not_mapped",events[1]["content"])
        self.assertIsNone(events[-1]["review_proposal"]["rule_evaluation"])
