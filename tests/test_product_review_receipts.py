"""Fixed SDK checks for isolated product evidence review receipts."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.database.listing_operations import ListingNotFound
from backend.rag import product_candidate_admission as admission


QUESTION = "套环宽度是多少？轴套外径是多少？配重干重是多少？"


def source(document, quote, location="row:2"):
    return {
        "chunk_id": document, "document_id": document, "filename": f"{document}.txt",
        "location": location, "section": quote.split("\t", 1)[0], "quote": quote,
        "start_offset": 0, "end_offset": len(quote),
    }


def sdk(receive):
    def complete(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        response = receive(payload)
        content = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])

    return SimpleNamespace(model="fixed-receipt-test", chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))


def supported(request, span):
    return {"id": request, "status": "supported", "claims": [{"span": span, "requires": []}]}


def missing(request):
    return {"id": request, "status": "missing", "claims": []}


class ProductReviewReceiptTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            1: source("collar", "套环宽度\t27 mm\t尺寸以装入状态为准"),
            2: source("bushing", "轴套外径\t43 mm\t尺寸从外沿测量"),
            3: source("weight", "配重干重\t316 g\t不含外盒与挂绳"),
        }
        self.requests = []
        self.observations = []

    def select(self, review, is_current=lambda _: True, *, question=QUESTION, proposed=None):
        def receive(payload):
            if "clauses" in payload:
                groups = proposed or [([clause["id"]], [clause["id"]]) for clause in payload["clauses"]]
                return {"parts": [{"clauses": clause_ids, "status": "supported" if source_ids else "missing",
                                   "sources": [{"id": sid, "quote": self.sources[sid]["quote"]} for sid in source_ids]}
                                  for clause_ids, source_ids in groups]}
            self.requests.append(payload)
            return review(payload)

        with (patch.object(admission, "_sources", return_value=self.sources),
              patch.object(admission.ProviderFactory, "create", return_value=sdk(receive)),
              admission.observe_evaluation(self.observations.append)):
            return admission.select({"candidates": []}, question, None, still_current=is_current)

    def assert_outcome(self, outcome, *, supported):
        self.assertEqual({item["id"]: item["chunk_ids"] for item in outcome["fact_checks"]},
                         {rid: [self.sources[rid]["chunk_id"]] if rid in supported else [] for rid in self.sources})

    def assert_requests(self, expected):
        self.assertEqual([[request["id"] for request in payload["requests"]] for payload in self.requests], expected)

    def test_named_receipt_uses_complete_offsets_and_keeps_explicit_missing(self):
        def review(payload):
            references = {row[0]: tuple(row[1:]) for row in payload["spans"]}
            for item in payload["sources"]:
                self.assertEqual(references[item["id"]], (item["id"], 0, len(item["text"])))
                self.assertEqual(item["text"], self.sources[item["id"]]["quote"])
            return {"decisions": [supported(1, 1), missing(2), supported(3, 3)]}

        outcome = self.select(review)

        self.assert_requests([[1, 2, 3]])
        self.assert_outcome(outcome, supported={1, 3})

    def test_only_omitted_request_is_retried_and_can_recover(self):
        replies = iter([
            {"decisions": [supported(1, 1), missing(3)]},
            {"decisions": [supported(2, 2)]},
        ])

        def review(payload):
            allowed = {subrequest_id for request in payload["requests"] for subrequest_id in request["candidates"]}
            self.assertEqual({item["id"] for item in payload["sources"]}, allowed)
            self.assertEqual({span[1] for span in payload["spans"]}, allowed)
            return next(replies)

        outcome = self.select(review)

        self.assert_requests([[1, 2, 3], [2]])
        self.assert_outcome(outcome, supported={1, 2})

    def test_echoed_span_must_match_registry_in_full(self):
        def review(payload):
            registered = {span[0]: span for span in payload["spans"]}
            request_ids = [request["id"] for request in payload["requests"]]
            exact = supported(1, registered[1])
            shifted = supported(2, [2, 2, 1, registered[2][-1]])
            decisions = [exact, shifted, missing(3)] if request_ids == [1, 2, 3] else [shifted]
            return {"decisions": decisions}

        self.assert_outcome(self.select(review), supported={1})
        self.assert_requests([[1, 2, 3], [2]])

    def test_malformed_item_cannot_remove_siblings_and_retry_is_bounded(self):
        malformed = {"id": 2, "status": "supported", "claims": [{"span": 2, "requires": "wrong shape"}]}
        replies = iter([
            {"decisions": [supported(1, 1), malformed, supported(3, 3)]},
            {"decisions": [malformed]},
        ])

        outcome = self.select(lambda _: next(replies))

        self.assert_requests([[1, 2, 3], [2]])
        self.assert_outcome(outcome, supported={1, 3})

    def test_duplicate_request_id_cannot_confirm_evidence(self):
        valid = supported(2, 2)
        malformed = {"id": 2, "status": "supported", "claims": [{"span": 2, "requires": "wrong shape"}]}
        for correction, expected in (([valid], {1, 2}), ([valid, malformed], {1})):
            with self.subTest(correction=correction):
                self.requests.clear()
                self.observations.clear()
                replies = iter([
                    {"decisions": [malformed, supported(1, 1), valid, missing(3)]},
                    {"decisions": correction},
                ])

                outcome = self.select(lambda _: next(replies))

                self.assert_requests([[1, 2, 3], [2]])
                self.assert_outcome(outcome, supported=expected)
                self.assertTrue(any(event.get("reason") == "duplicate_review_decision"
                                    and event.get("request_id") == 2 for event in self.observations))

    def test_retry_checks_authorization_before_sdk_dispatch(self):
        authorized = True

        def review(_):
            nonlocal authorized
            authorized = False
            return {"decisions": [supported(1, 1), missing(3)]}

        with self.assertRaises(ListingNotFound):
            self.select(review, is_current=lambda _: authorized)

        self.assert_requests([[1, 2, 3]])
        self.assertTrue(any(event["event"] == "authorization_denied" for event in self.observations))

    def test_named_conflict_retains_both_claims_and_their_linked_condition(self):
        self.sources = {
            1: source("range-a", "定位尺行程范围\t15–40\tmm\t检测架闭合\t条件详见《prep.txt》的「检验前准备」"),
            2: source("range-b", "定位尺行程范围\t15–45\tmm\t检测架闭合\t条件详见《prep.txt》的「检验前准备」"),
            3: source("prep", "先把两只定位销插到底，再锁住基准挡块。", location="page:M1"),
        }
        self.sources[3]["section"] = "检验前准备"

        first = {"span": 1, "requires": [3]}
        second = {"span": 2, "requires": [3]}
        for claims, status, requests in (([first, second], "conflict", [[1]]), ([first], "missing", [[1], [1]]),
                                         ([first, {**second, "requires": []}], "missing", [[1]])):
            with self.subTest(claims=claims):
                self.requests.clear()

                def review(payload):
                    self.assertEqual({item["id"]: item.get("requires") for item in payload["sources"]},
                                     {1: [3], 2: [3], 3: None})
                    return {"decisions": [{"id": 1, "status": "conflict", "claims": claims}]}

                outcome = self.select(review, question="检测架闭合，定位尺行程范围是多少？", proposed=[([1, 2], [1, 2])])

                self.assert_requests(requests)
                (check,) = outcome["fact_checks"]
                self.assertEqual(check["status"], status)
                actual = [(claim["source"]["document_id"], [link["document_id"] for link in claim["requires"]])
                          for claim in check["claims"]]
                expected = [("range-a", ["prep"]), ("range-b", ["prep"])] if status == "conflict" else []
                self.assertEqual(actual, expected)

    def binding(self, receive, *, pending=None, is_current=lambda _: True):
        source_ids = {id(citation): sid for sid, citation in self.sources.items()}
        if pending is None:
            pending = {sid: (citation["section"], [admission._claim(sid, [], self.sources, citation["quote"])])
                       for sid, citation in self.sources.items() if sid <= 2}

        def completion(payload):
            self.requests.append(payload)
            return receive(payload)

        return admission._bind_details(sdk(completion), None, pending, source_ids, still_current=is_current)

    def assert_binding_requests(self, expected):
        self.assertEqual([[(check["request"], check["source"]) for check in payload["checks"]]
                          for payload in self.requests], expected)

    def test_binding_retry_accepts_only_the_expected_pair(self):
        initial = {"confirmed": [{"request": 1, "source": 1, "subject": "套环宽度"}, {"request": 2, "source": 2}]}
        for replacement, expected in (({"request": 1, "source": 1}, {(1, 1), (2, 2)}),
                                      ({"request": 1, "source": 2}, {(2, 2)}),
                                      ({"request": 1, "source": 1, "subject": "套环宽度"}, {(2, 2)})):
            with self.subTest(replacement=replacement):
                self.requests.clear()
                replies = iter([initial, {"confirmed": [replacement]}])

                self.assertEqual(self.binding(lambda _: next(replies)), expected)

                self.assert_binding_requests([[(1, 1), (2, 2)], [(1, 1)]])
                self.assertEqual([item["id"] for item in self.requests[1]["sources"]], [1])

    def test_binding_still_requires_the_correct_subject(self):
        claim = "活动脚宽度为 27 mm"
        self.sources[1] = source("feet", f"{claim}，承托脚宽度为 35 mm。", location="page:M1")
        pending = {1: ("活动脚宽度是多少？", [admission._claim(1, [], self.sources, claim)])}
        for subject, expected in (("活动脚宽度", {(1, 1)}), ("承托脚宽度", set()), (None, set())):
            with self.subTest(subject=subject):
                self.requests.clear()

                def response(payload):
                    self.assertEqual(set(payload["checks"][0]["subjects"]), {"活动脚宽度", "承托脚宽度"})
                    decision = {"request": 1, "source": 1}
                    if subject is not None:
                        decision["subject"] = subject
                    return {"confirmed": [decision]}

                self.assertEqual(self.binding(response, pending=pending), expected)
                self.assert_binding_requests([[(1, 1)]])

    def test_binding_retry_checks_authorization_before_sdk_dispatch(self):
        authorized = True

        def response(_):
            nonlocal authorized
            authorized = False
            return {"confirmed": [{"request": 1, "source": 1, "subject": "套环宽度"}]}

        with self.assertRaises(ListingNotFound):
            self.binding(response, is_current=lambda _: authorized)

        self.assert_binding_requests([[(1, 1), (2, 2)]])
