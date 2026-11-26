import hashlib
import json
import os
import runpy
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from openai import APITimeoutError

from scripts import product_generation_guard as guard


_FIXTURE = Path(__file__).parent / "fixtures/product_generation_r5b"
_TIMING_QUESTION = "清洗间隔最长多久？按什么条件计时？胶垫厚度是多少？"
_TIMING_VALUE = "清洗间隔\t45\t分钟\t按 J74 准备\t建议最长清洗间隔"
_TIMING_CONDITION = "## 74 | J74 清洗准备\n须先合上排水阀，从水位回到刻线时开始计时。"
_GROUPS = {
    "timing": ("partial_support", _TIMING_QUESTION, [_TIMING_VALUE, _TIMING_CONDITION],
               "胶垫厚度无原文依据。"),
    "timing-gap": ("partial_support", _TIMING_QUESTION, [_TIMING_VALUE],
                   "缺少 J74 的操作与计时条件，胶垫厚度无原文依据。"),
    "bare-link": ("partial_support", "气阀检验等待多久？外壳有多硬？",
                  ["气阀检验等待时间\t51\ts\t按规程 P83 操作\t定位杆回到止挡后开始计时",
                   "## 83 | P83 气阀准备\n先移开防尘罩，让定位杆回到止挡。"],
                  "外壳硬度无原文依据。"),
    "same-excerpt": ("supported", "转接盘径向负载的上限是多少、有哪些适用条件？",
                     ["转接盘水平放置且锁舌闭合时，径向负载上限为 32 kg。"], ""),
    "estimate": ("supported", "光阑关闭时，归零需要多久？",
                 ["光阑关闭时，预计归零耗时 17 秒。"], ""),
    "conflict": ("conflict", "投影盒的投影片插到底并锁住滑轨时，中心偏移是多少？",
                 ["## 62 | J62 检验卡\n投影片插入到底且锁住滑轨时，投影盒的中心偏移为 9 mm。",
                  "## 67 | J67 装配记录\n投影片插入到底且锁住滑轨时，投影盒的中心偏移为 13 mm。"],
                 "关于同一投影盒、同一工况，两份资料的记载不一致。"),
}


class FixedReviewer:
    model = "fixed-reviewer"

    def __init__(self, *, issues=(), gaps=(), on_receive=lambda: None, error=None):
        self.calls = []
        self.content = json.dumps({"input_gaps": list(gaps), "issues": list(issues)}, ensure_ascii=False)
        self.on_receive = on_receive
        self.error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.calls.append(request)
        self.on_receive()
        if self.error:
            raise self.error(request=httpx.Request("POST", "https://example.invalid"))
        return SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=self.content)),
        ])


def _input(group):
    status, question, quotes, note = _GROUPS[group]
    checks = [{"ok": True, "quote": text} for text in quotes]
    reply = "\n".join([f"[{index}] {quote}" for index, quote in enumerate(quotes, 1)] + ([note] if note else []))
    row = {"done": True, "ranking_attested": True, "retrieval_trace": {"status": status},
           "input": {"query": question}, "reply": reply}
    return row, checks


def _draft(text, citations):
    return {"status": "generated", "answer": text, "citations": list(citations)}


def _cases():
    return json.loads((_FIXTURE / "synthetic-cases.json").read_bytes())


def _review_fixture(reviewer, row, checks, draft, *, still_current):
    return guard.review_generation(reviewer, row, checks, draft, still_current=still_current,
                                   verified_template=row["reply"])


class ProductGenerationGuardTests(unittest.TestCase):
    def test_literal_measurement_keeps_subject_property_and_value_together(self):
        source = "导向板宽度为 46 mm。定位板宽度为 58 mm。导向板厚度为 7 mm。"
        row = {"done": True, "ranking_attested": True, "retrieval_trace": {"status": "supported"},
               "input": {"query": "导向板的宽度是多少？"}, "reply": "导向板宽度为 46 mm [1]。"}
        checks = [{"ok": True, "quote": source}]
        for draft, delivery in (
            ("导向板宽度为 46 mm [1]。", "generation"),
            ("导向板宽度为 4.6 cm [1]。", "generation"),
            ("导向板的宽度是 **46 mm** [1]。", "generation"),
            ("导向板宽度为 58 mm [1]。", "template"),
            ("导向板宽度为 **58 mm** [1]。", "template"),
            ("导向板宽度为 60 mm [1]。", "template"),
            ("定位板宽度为 58 mm [1]。", "template"),
            ("导向板高度为 7 mm [1]。", "template"),
        ):
            with self.subTest(draft=draft):
                result = _review_fixture(FixedReviewer(), row, checks, _draft(draft, [1]),
                                         still_current=lambda: True)
                self.assertEqual(result["delivery"]["kind"], delivery)
                if delivery == "template":
                    self.assertIn("citation_attribution", result["local_defects"])
                    self.assertEqual(result["delivery"]["answer"], row["reply"])

    def test_explicit_prerequisites_with_empty_review(self):
        rule = "滑槽干燥且定位销闭合时，探头的重复定位误差上限为 8 mm。用完后擦拭铭牌。"
        claim = "探头的重复定位误差上限是 8 毫米[1]。"
        table = "基准漂移量\t12\tmm\t依照规程 X19 测量\t读取指针"
        manual = "## X19 基准测试\n测量前须闭合隔振锁。测试结束可取下罩壳。"
        reading = "基准漂移量为 12 mm[1]。"
        cases = [
            ("same_passage", [rule], "滑槽干燥且定位销闭合时，" + claim, []),
            ("same_passage_omitted", [rule], "滑槽干燥时，" + claim, ["omitted_condition"]),
            ("condition_on_wrong_conclusion", [rule], "定位销闭合仅用于收纳；滑槽干燥时，" + claim,
             ["condition_mismatch"]),
            ("equivalent_condition", [rule], "滑槽干透且定位销关严时，" + claim, []),
            ("explicit_continuation", [rule], "先让滑槽干透并关严定位销。在上述条件下，" + claim, []),
            ("numbered_preparation", [table, manual], reading + "测量前须关严隔振锁[2]。", []),
            ("numbered_same_context", [table, manual], "隔振锁闭合后，基准漂移量为 12 mm[1][2]。", []),
            ("numbered_cited_but_omitted", [table, manual], "基准漂移量为 12 mm[1][2]。内部刻度供读数参考。",
             ["omitted_condition"]),
            ("numbered_wrong_context", [table, manual], reading + "收纳时闭合隔振锁[2]。",
             ["condition_mismatch"]),
            ("numbered_missing_input", [table], reading + "缺少编号手册，适用条件无法确认。", []),
        ]
        new_records = []
        before_path = os.environ.get("AD_GENERATION_GUARD_R6_BEFORE_SOURCE")
        before_guard = runpy.run_path(before_path)["review_generation"] if before_path else None
        for case, evidence, answer, defects in cases:
            with self.subTest(case=case):
                checks = [{"ok": True, "quote": quote} for quote in evidence]
                gaps = ["测量的适用条件"] if case == "numbered_missing_input" else []
                question = ("基准漂移量是多少？在什么条件下测量？" if evidence[0] == table
                            else "探头的重复定位误差上限是多少？")
                cited = list(range(1, len(evidence) + 1))
                row = {"done": True, "ranking_attested": True, "retrieval_trace": {"status": "partial_support" if gaps else "supported"},
                       "input": {"query": question}, "reply": "\n".join(evidence + gaps)}
                reviewer = FixedReviewer(gaps=gaps)
                generated = _draft(answer, cited)
                result = _review_fixture(reviewer, row, checks, generated, still_current=lambda: True)
                self.assertEqual(result["defects"], defects)
                self.assertEqual(result["input_gaps"], gaps)
                self.assertEqual(result["review_findings"], [])
                self.assertEqual(result["delivery"]["kind"], "template" if defects else "generation")
                request, = reviewer.calls
                before = None
                if before_guard:
                    fixed = FixedReviewer(gaps=gaps)
                    before = before_guard(fixed, row, checks, generated, still_current=lambda: True)
                    self.assertEqual(fixed.calls, [request])
                new_records.append({"id": case, "input": json.loads(request["messages"][1]["content"]),
                                    "before": before, "review": result, "sdk_received": len(reviewer.calls)})
        if path := os.environ.get("AD_GENERATION_GUARD_R6_CASES"):
            Path(path).write_text(json.dumps(new_records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_unverified_template_is_never_delivered(self):
        row, checks = _input("estimate")
        row["reply"] = "资料[1]保证归零一定在 17 秒完成。"
        reviewer = FixedReviewer()
        reviewer.content = '{"input_gaps":[],"issues":'
        result = guard.review_generation(reviewer, row, checks, _draft("预计归零需 17 秒[1]。", [1]),
                                         still_current=lambda: True)
        self.assertEqual(result["review_raw"], reviewer.content)
        self.assertEqual(result["review_status"], "invalid_review")
        self.assertEqual(result["fallback_status"], "unverified_template")
        self.assertIsNone(result["delivery"])

    def test_numbered_manual_procedure_binds_each_conclusion_and_timing(self):
        quotes = [
            "离心滤筒沥液等待时间\t72\ts\t依照规程 F82 操作\t从回流口停止滴液后开始计时",
            "## F82 滤筒沥液\n先将滤筒水平放置。确认回流口停止滴液。",
            "集液罐冲洗等待时间\t72\ts\t参照规程 Q53 准备\t从冲洗阀关到止挡后开始计时",
            "## Q53 集液罐冲洗\n先拆下取样盖。把冲洗阀关到止挡。",
        ]
        claim = "离心滤筒沥液等待时间为 72 秒[1]。"
        steps = "准备与计时步骤如下：先将滤筒水平放置。确认回流口停止滴液[2]。"
        start = "计时从回流口停止滴液后开始。"
        reversed_start = "从回流口开始滴液后起算。"
        second_claim = "根据资料[3]，集液罐冲洗等待时间是 72 s。"
        second_steps = "操作和计时顺序如下：先拆下取样盖。把冲洗阀关到止挡[4]。"
        second_start = "从冲洗阀关到止挡后起算。"
        drafts = [
            ("correct", claim + steps + start, False, True, "核对滤筒沥液条件的归属。"),
            ("table_timing_correct", claim + steps + start, False, True, "核对规格表给出的沥液起点。"),
            ("wrong_object", claim.replace("离心滤筒", "储液瓶") + steps + start, False, False,
             "规格表[1]的72秒适用于离心滤筒沥液，草稿把手册[2]挂给了储液瓶。"),
            ("wrong_attribute", claim.replace("沥液等待", "烘干运行") + steps + start, False, False,
             "规格表[1]没有滤筒烘干运行的时间，手册[2]只限定沥液等待。"),
            ("wrong_start", claim + steps + reversed_start, False, False,
             "规格表[1]从回流口停止滴液后起算，草稿改成开始滴液后起算。"),
            ("table_timing_wrong", claim + steps + reversed_start, False, False,
             "草稿改写了规格表[1]的滤筒沥液起点。"),
            ("later_conflict", claim + steps + start + reversed_start, False, False,
             "草稿追加了相反起点，规格表[1]只支持从停止滴液后起算。"),
            ("missing_start", claim + steps, False, False,
             "手册[2]的步骤写明停止滴液，草稿缺少规格表[1]要求的计时起点。"),
            ("same_value", claim + steps + start + second_claim + second_steps + second_start, True, True,
             "核对两项同值等待时间分别采用的条件。"),
            ("swapped_citations", claim.replace("[1]", "[3]") + steps + start
             + second_claim.replace("[3]", "[1]") + second_steps + second_start, True, False,
             "两项等待时间均为72秒，但草稿对调了规格表引用。"),
            ("swapped_steps", claim + second_steps + start + second_claim + steps + second_start, True, False,
             "滤筒沥液和集液罐冲洗的准备步骤被对调。"),
            ("late_parallel_conflict", claim + steps + start + second_claim + second_steps + second_start
             + "离心滤筒沥液从回流口开始滴液后起算。", True, False,
             "草稿在第二项后重述第一项起点，改成了开始滴液后起算。"),
        ]
        before_path = os.environ.get("AD_GENERATION_GUARD_B2_BEFORE_SOURCE")
        before_guard = runpy.run_path(before_path)["review_generation"] if before_path else None
        records = []
        for name, answer, parallel, passed, detail in drafts:
            with self.subTest(case=name):
                evidence = quotes if parallel else quotes[:2]
                citations = list(range(1, len(evidence) + 1))
                checks = [{"ok": True, "quote": quote} for quote in evidence]
                question = ("离心滤筒沥液和集液罐冲洗各要等多久，怎样准备和起算？" if parallel
                            else "离心滤筒沥液要等多久，怎样准备和起算？")
                row = {"done": True, "ranking_attested": True, "retrieval_trace": {"status": "supported"},
                       "input": {"query": question}, "reply": "\n".join(evidence)}
                draft = _draft(answer, citations)
                issues = [{"code": "condition_mismatch", "source_ids": [2],
                           "source_span": "确认回流口停止滴液", "detail": detail}]
                if parallel:
                    issues.append({"code": "condition_mismatch", "source_ids": [4],
                                   "source_span": "把冲洗阀关到止挡", "detail": detail})
                if name.startswith("table_timing"):
                    issues = [{"code": "condition_mismatch", "source_ids": [1],
                               "source_span": "从回流口停止滴液后开始计时", "detail": detail}]
                reviewer = FixedReviewer(issues=issues)
                result = _review_fixture(reviewer, row, checks, draft, still_current=lambda: True)
                request, = reviewer.calls
                self.assertEqual(result["review_findings"], [] if passed else issues)
                if passed:
                    self.assertEqual(result["review_status"], "passed")
                    self.assertEqual(result["delivery"], {"kind": "generation", "answer": answer, "citations": citations})
                    self.assertEqual([item["proof"]["source_id"] for item in result["contradicted_findings"]],
                                     [1, 3] if parallel else [1])
                else:
                    self.assertEqual(result["review_status"], "rejected")
                    self.assertEqual(result["contradicted_findings"], [])
                    self.assertEqual(result["delivery"], {"kind": "template", "answer": row["reply"]})
                before = None
                if before_guard:
                    baseline = FixedReviewer(issues=issues)
                    before = before_guard(baseline, row, checks, draft, still_current=lambda: True)
                    self.assertEqual(baseline.calls, [request])
                    self.assertEqual(before["review_raw"], result["review_raw"])
                blank_result = None
                if name in {"correct", "wrong_start", "later_conflict", "missing_start", "same_value"}:
                    blank = FixedReviewer()
                    blank_result = _review_fixture(blank, row, checks, draft, still_current=lambda: True)
                    self.assertEqual(blank.calls, [request])
                    expected_blank = [] if passed else ["omitted_condition" if name == "missing_start"
                                                       else "condition_mismatch"]
                    self.assertEqual(blank_result["defects"], expected_blank)
                    self.assertEqual(blank_result["delivery"]["kind"], "generation" if passed else "template")
                records.append({"id": name, "input": json.loads(request["messages"][1]["content"]),
                                "before": before, "review": result, "empty_review": blank_result})
        if path := os.environ.get("AD_GENERATION_GUARD_B2_RECEIPT"):
            Path(path).write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_condition_binding_and_same_source_modality(self):
        frozen = json.loads((_FIXTURE / "condition-binding-cases.json").read_bytes())
        before_path = os.environ.get("AD_GENERATION_GUARD_BEFORE_SOURCE")
        before_guard = runpy.run_path(before_path)["review_generation"] if before_path else None
        receipt = []
        for case in frozen["cases"]:
            with self.subTest(case=case["id"]):
                group = frozen["groups"][case["group"]]
                evidence = group["evidence"]
                checks = [{"ok": True, "quote": quote} for quote in evidence]
                blocks = [f"[{i}] {quote}" for i, quote in enumerate(evidence, 1)]
                if group["status"] == "conflict":
                    blocks.insert(0, "以下现行资料对同一对象和工况的记载不一致。")
                if group["gaps"]:
                    blocks.append("缺少依据：" + "、".join(group["gaps"]) + "。")
                row = {"done": True, "ranking_attested": True, "retrieval_trace": {"status": group["status"]},
                       "input": {"query": group["question"]}, "reply": "\n".join(blocks)}
                draft = _draft(case["draft"], case["citations"])
                reviewer = FixedReviewer(issues=case["issues"], gaps=group["gaps"])
                if "raw" in case:
                    reviewer.content = case["raw"]
                result = _review_fixture(reviewer, row, checks, draft, still_current=lambda: True)
                request, = reviewer.calls
                payload = json.loads(request["messages"][1]["content"])
                self.assertEqual(payload["evidence"], [{"id": i, "text": text} for i, text in enumerate(evidence, 1)])
                self.assertEqual((payload["answer"], payload["citations"]), (case["draft"], case["citations"]))
                self.assertEqual(result["review_status"], case["status"])
                self.assertEqual(result["defects"], case["defects"])
                self.assertEqual(result["input_gaps"], None if case["status"] == "invalid_review" else group["gaps"])
                if case["status"] == "passed":
                    self.assertEqual(result["delivery"], {"kind": "generation", "answer": case["draft"],
                                                          "citations": case["citations"]})
                else:
                    self.assertEqual(result["delivery"], {"kind": "template", "answer": row["reply"]})
                before = None
                if before_guard:
                    before = before_guard(reviewer, row, checks, draft, still_current=lambda: True)
                    if case["group"] == "inspection":
                        self.assertEqual(before["review_status"], "passed")
                    self.assertEqual(reviewer.calls[1]["messages"][1], request["messages"][1])
                input_hash = hashlib.sha256(request["messages"][1]["content"].encode()).hexdigest()
                receipt.append({"id": case["id"], "input": payload, "input_sha256": input_hash,
                                "before": before, "review": result})
        if path := os.environ.get("AD_GENERATION_GUARD_BINDING_RECEIPT"):
            Path(path).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_frozen_synthetic_verdicts_and_exact_review_payload(self):
        # Independent fixtures model reviewer input. The attribution and modality
        # checks also run with an uncritical fixed response.
        review_findings = {
            "C02": [("omitted_condition", [2], "未写水位回到刻线才计时。", "从水位回到刻线时开始计时")],
            "C06": [("omitted_condition", [2], "未写水位回到刻线才计时。", "从水位回到刻线时开始计时")],
            "C09": [("omitted_condition", [1], "未写锁舌闭合。", "锁舌闭合")],
            "D01": [("incomplete_answer", [1, 2], "固定审阅误称没有写装配记录的值。"),
                    ("citation_attribution", [1, 2], "固定审阅误称来源前缀标记失效。")],
            "D02": [("conflict_side_missing", [2], "没有写装配记录的 13 mm。")],
            "D04": [("incomplete_answer", [1, 2], "没有指出同一投影盒和同一工况出现冲突。"),
                    ("unsupported_claim", [1, 2], "J62、J67 是文档编号，草稿写成了型号。")],
            "D05": [("incomplete_answer", [1, 2], "没有逐方复述来源。"),
                    ("unsupported_claim", [1, 2], "两值未形成允许区间。")],
        }
        reported_gaps = {
            "timing": ["胶垫厚度"],
            "timing-gap": ["编号手册的操作与计时条件", "胶垫厚度"],
            "bare-link": ["外壳硬度"],
        }
        receipt = []
        for case in _cases():
            with self.subTest(case=case["id"]):
                row, checks = _input(case["group"])
                answer = _draft(case["draft"], case["citations"])
                issues = [dict(code=code, source_ids=ids, detail=detail, **({"source_span": span[0]} if span else {}))
                          for code, ids, detail, *span in review_findings.get(case["id"], [])]
                reviewer = FixedReviewer(issues=issues, gaps=reported_gaps.get(case["group"], []))
                result = _review_fixture(reviewer, row, checks, answer, still_current=lambda: True)
                self.assertEqual(result["defects"], sorted(case["defects"]))
                self.assertEqual(result["input_gaps"], case["input_gaps"])
                self.assertEqual(result["review_requests"], 1)
                self.assertEqual(len(reviewer.calls), 1)
                if case["id"] == "D01":
                    self.assertEqual(len(result["contradicted_findings"]), 2)
                if case["id"] == "C13":
                    self.assertEqual(result["local_defects"], ["omitted_condition"])
                    self.assertCountEqual([condition["source_id"] for condition in result["local_omitted_conditions"]],
                                          [1, 2])
                payload = json.loads(reviewer.calls[0]["messages"][1]["content"])
                wanted = {"question": row["input"]["query"], "answer": answer["answer"],
                          "citations": answer["citations"], "evidence": [
                              {"id": i, "text": check["quote"]} for i, check in enumerate(checks, 1)
                          ]}
                if case["group"] == "conflict":
                    wanted["source_relationship"] = "confirmed_same_context_conflict"
                self.assertEqual(payload, wanted)
                if case["defects"]:
                    self.assertEqual(result["review_status"], "rejected")
                    self.assertEqual(result["delivery"], {"kind": "template", "answer": row["reply"]})
                else:
                    self.assertEqual(result["review_status"], "passed")
                    self.assertEqual(result["delivery"], {"kind": "generation", "answer": case["draft"],
                                                          "citations": case["citations"]})
                receipt.append({"id": case["id"], "input": payload, "generated": answer, "review": result})
        if path := os.environ.get("AD_GENERATION_GUARD_SYNTHETIC_RECEIPT"):
            Path(path).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_input_gap_does_not_excuse_incomplete_output(self):
        row, checks = _input("timing-gap")
        reviewer = FixedReviewer(gaps=["J74 操作条件", "胶垫厚度"], issues=[
            {"code": "incomplete_answer", "source_ids": [], "detail": "草稿没有说明胶垫厚度缺证。"}
        ])
        result = _review_fixture(
            reviewer, row, checks, _draft("建议清洗间隔不超过 45 分钟[1]。", [1]), still_current=lambda: True,
        )
        self.assertEqual(result["defects"], ["incomplete_answer"])
        self.assertEqual(result["input_gaps"], ["J74 操作条件", "胶垫厚度"])
        self.assertEqual(result["delivery"]["answer"], row["reply"])

    def test_marker_set_and_missing_verified_source_are_separate(self):
        row, checks = _input("timing")
        result = _review_fixture(
            FixedReviewer(), row, checks, _draft("建议清洗间隔不超过 45 分钟[1]。", [1, 2]),
            still_current=lambda: True,
        )
        self.assertEqual(result["defects"], ["citation_attribution", "missing_verified_source"])

    def test_verbatim_condition_contradicts_omission_finding(self):
        row, checks = _input("timing")
        reviewer = FixedReviewer(issues=[{"code": "omitted_condition", "source_ids": [2],
                                          "source_span": "从水位回到刻线时开始计时", "detail": "未写计时起点。"}])
        good = _cases()[0]
        result = _review_fixture(
            reviewer, row, checks, _draft(good["draft"], good["citations"]), still_current=lambda: True,
        )
        self.assertEqual(result["defects"], [])
        self.assertEqual(result["review_status"], "passed")
        self.assertEqual(len(result["contradicted_findings"]), 1)

    def test_independent_modality_checks_cited_statement(self):
        row, checks = _input("estimate")
        checks.append({"ok": True, "quote": "电机断电后保证维持锁止。"})
        draft = _draft("在光阑关闭时，归零一定耗时 17 秒[1]。电机断电后保证维持锁止[2]。", [1, 2])
        result = _review_fixture(FixedReviewer(), row, checks, draft, still_current=lambda: True)
        self.assertEqual(result["defects"], ["strengthened_claim"])

    def test_review_timeout_and_invalid_contract_fall_back(self):
        row, checks = _input("conflict")
        good = _cases()[-5]
        draft = _draft(good["draft"], good["citations"])
        reviewers = [(FixedReviewer(error=APITimeoutError), "provider_error")]
        for content in ("{}", '{"input_gaps":[],"issues":[{"code":"omitted_condition","source_ids":[3],"detail":"错引"}]}',
                        '{"input_gaps":[],"issues":[{"code":"omitted_condition","source_ids":[],"detail":"无原文"}]}'):
            reviewer = FixedReviewer()
            reviewer.content = content
            reviewers.append((reviewer, "invalid_review"))
        for reviewer, status in reviewers:
            result = _review_fixture(reviewer, row, checks, draft, still_current=lambda: True)
            self.assertEqual(result["review_status"], status)
            self.assertIsNone(result["input_gaps"])
            self.assertEqual(result["delivery"], {"kind": "template", "answer": row["reply"]})
        reviewer = FixedReviewer()
        result = _review_fixture(reviewer, row, checks, {"status": "provider_error"}, still_current=lambda: True)
        self.assertEqual(result["review_status"], "generation_failed")
        self.assertEqual(result["delivery"]["answer"], row["reply"])
        self.assertEqual(reviewer.calls, [])

    def test_initial_scope_denial_or_readback_failure_never_reaches_sdk(self):
        row, checks = _input("timing")
        reviewer = FixedReviewer()
        draft = _draft("建议清洗间隔不超过 45 分钟[1]。", [1])
        for status, ok in (("partial_support", False), ("sku_conflict", True)):
            row["retrieval_trace"]["status"] = status
            checks[0]["ok"] = ok
            result = guard.review_generation(reviewer, row, checks, draft, still_current=lambda: True)
            self.assertEqual(result["review_status"], "skipped")
            self.assertIsNone(result["delivery"])
        self.assertEqual(reviewer.calls, [])


class ProductGenerationGuardAuthorizationTests(unittest.TestCase):
    def test_real_permission_and_version_changes_at_review_boundaries(self):
        from scripts import eval_product_generation_guard as runner
        from scripts import eval_product_generation_r4b as baseline

        records = []
        receipt = os.environ.get("AD_GENERATION_GUARD_RECEIPT")
        plan = baseline.read(baseline.FIXTURE / "plan.json")
        index = baseline.source_index(plan)
        with ExitStack() as stack:
            scratch = Path(receipt).resolve().parent if receipt else baseline.ROOT
            sandbox = Path(stack.enter_context(TemporaryDirectory(prefix="guard-auth-", dir=scratch)))
            settings = baseline.database.settings
            original_config = (settings.DIAGNOSIS_MYSQL_ENABLED, settings.DIAGNOSIS_SANDBOX_DB_PATH, settings.JWT_SECRET)

            def restore_config():
                settings.DIAGNOSIS_MYSQL_ENABLED, settings.DIAGNOSIS_SANDBOX_DB_PATH, settings.JWT_SECRET = original_config

            stack.callback(restore_config)
            settings.JWT_SECRET = baseline.token_urlsafe(48)
            baseline.use_sandbox(sandbox / "isolated.sqlite3")
            baseline.database.init_db()
            from fast_server import app, auth_instance

            conn = stack.enter_context(closing(baseline.database.get_db()))
            options = {"loop_factory": baseline.InProcessLoop} if os.name == "nt" else {}
            client = stack.enter_context(closing(baseline.TestClient(app, backend_options=options)))
            users = baseline.setup_fixture(conn, plan)
            for release in plan["releases"]:
                self.assertTrue(baseline.rag.import_release(
                    conn, release, baseline.FIXTURE / "sources", embedding=lambda texts: [[1.0, 0.0]] * len(texts),
                ))
                if release["version"] == "proof-v1":
                    baseline.rag.activate(conn, release["target"], release["version"])
            auth = {name: baseline.login(client, name) for name in users}
            case_with_source = plan["cases"][0]
            empty_case = next(case for case in plan["cases"] if not case["evidence"])
            scenarios = [
                (action, timing, case_with_source) for action in ("session", "read_right", "version")
                for timing in ("before_sdk", "after_sdk")
            ] + [("session", "before_sdk", empty_case), ("version", "after_sdk_error", case_with_source)]
            for action, timing, case in scenarios:
                with self.subTest(action=action, timing=timing, has_sources=bool(case["evidence"])):
                    row, checks, _, _ = baseline.verify(client, conn, users, auth, index, case)
                    self.assertTrue(all(c["ok"] for c in checks))
                    scope = baseline.rag._scope(case["target"])

                    def set_owner(user_id):
                        with conn:
                            conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                                         (user_id, *scope[:2]))

                    def mutate():
                        if action == "session":
                            baseline.login(client, case["principal"])
                        elif action == "read_right":
                            set_owner(users["other"])
                        else:
                            baseline.rag.activate(conn, case["target"], "proof-v0")

                    rechecks = []

                    def still_current():
                        if timing == "before_sdk" and len(rechecks) == 1:
                            mutate()
                        recheck = runner._current_check(client, conn, auth_instance, users, auth, index, case, checks)
                        rechecks.append(recheck)
                        return recheck["current"]

                    row["reply"] = runner._template(row, ["侧壳表面硬度", "运行噪声"] if not checks else [])
                    draft = _draft("63 mm[1]" if checks else "两项均缺少原文。", [1] if checks else [])
                    sdk_calls = int(timing != "before_sdk")
                    reviewer = FixedReviewer(on_receive=mutate if sdk_calls else lambda: None,
                                             error=APITimeoutError if timing == "after_sdk_error" else None)
                    try:
                        result = _review_fixture(reviewer, row, checks, draft, still_current=still_current)
                        self.assertEqual(result["review_status"], "source_invalidated")
                        self.assertIsNone(result["delivery"])
                        self.assertEqual(result["review_requests"], sdk_calls)
                        self.assertEqual(len(reviewer.calls), sdk_calls)
                        self.assertTrue(rechecks[0]["current"])
                        self.assertFalse(rechecks[-1]["current"])
                        if checks:
                            expected_http = {"session": 401, "read_right": 404, "version": 404}[action]
                            self.assertEqual([entry["http"] for entry in rechecks[-1]["get"]], [expected_http])
                        else:
                            self.assertEqual(rechecks[-1]["get"], [])
                            self.assertFalse(rechecks[-1]["before"]["session_current"])
                        records.append({"action": action, "checkpoint": timing, "has_sources": bool(checks),
                                        "sdk_received": len(reviewer.calls), "rechecks": rechecks, "review": result})
                    finally:
                        if action == "session":
                            auth[case["principal"]] = baseline.login(client, case["principal"])
                        elif action == "read_right":
                            set_owner(users[case["principal"]])
                        else:
                            baseline.rag.activate(conn, case["target"], case["version"])
        if receipt:
            baseline.write_json(Path(receipt), records)
