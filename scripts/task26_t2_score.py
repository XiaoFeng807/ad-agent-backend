"""Score frozen T2 attempts against independent SQL and archive per-case evidence."""

import argparse
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from fractions import Fraction
import hashlib
import json
from math import isclose
from pathlib import Path
import re
from statistics import median

from scripts.run_task26_t2 import LIMIT
from scripts.run_task26_t1 import read_only
from scripts.task26_t2_replay import ARTIFACT, OUTPUT, PROVENANCE, ROOT, SAMPLE, ctr, read_json, sha, write_json


METRIC = "get_plan_metrics_by_date"
LOW = "ctr-review-v1-low-volume-01"
RULE = "ctr-review-v1-low-ctr-01"
WINDOW = "ctr-review-v1-windows-01"
PERCENT = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*[%％]")
REVIEW = r"(?:待复核|需要复核|需复核|触发复核|应标记.{0,5}复核|应复核)"
NO_REVIEW = r"(?:未触发|不触发|无需|不需|不需要|未达到|未命中|不满足|不符合|未达|不进入|不标记|不判定|未标记|不应|不能|无法|不构成|不予|不适用|不给出|不做|不涉及|不考虑|未给出|不满足).{0,36}(?:复核|阈值|门槛|条件)"
NO_COMPARISON = r"(?:无法|不能|不可|不足以|暂不|不宜|不应|不做|不得|不能够).{0,35}(?:比较|计算|判定|判断|评估|得出|应用|适用|计算|复核|下结论|认定)"
UNDEFINED = r"无定义|未定义|不适用|无法计算|不能计算|不可计算|分母为[零0]|无法定义"
CAUSAL = re.compile(r"(?:下降|上升|变化|波动|偏低|下滑).{0,12}(?:主要是|主要由于|归因于|是由|由.{1,14}导致|说明|表明).{0,28}(?:素材|出价|竞价|预算|流量|受众|转化|疲劳)|(?:素材疲劳|预算不足|竞价上涨|流量质量下降).{0,10}(?:导致|造成|引发)")
UNSUPPORTED_STATISTICS = re.compile(r"完全在.{0,16}噪声范围|(?:属于|只是|就是|系|仅是).{0,12}(?:噪声|随机波动)|"
                                    r"(?:具有|具备|达到|通过|存在|没有|不具备|不具有)统计显著性|"
                                    r"(?:无|没有|不具备)统计意义")


def dates(start, end):
    return (start + timedelta(days=offset) for offset in range((end - start).days + 1))


def metric_oracle(conn, provenance, case, start_date, end_date):
    first, last = map(date.fromisoformat, (start_date, end_date))
    rows = [dict(zip(("metric_date", "impressions", "clicks"), row)) for row in conn.execute(
        "SELECT metric_date, impressions, clicks FROM plan_daily_metrics "
        "WHERE source_plan_id=? AND metric_date BETWEEN ? AND ? ORDER BY metric_date",
        (case["source_plan_id"], start_date, end_date),
    ) if row[0] != case["missing_date"]]
    expected = [d.isoformat() for d in dates(first, last)]
    observed = {row["metric_date"] for row in rows}
    missing = [day for day in expected if day not in observed
               or not provenance["complete_start_date"] <= day <= provenance["complete_end_date"]]
    return dict(success=True, plan_id=case["plan_id"], source_plan_id=case["source_plan_id"],
                source_id=provenance["source_id"], data_version=provenance["data_version"],
                scope="plan", start_date=start_date, end_date=end_date,
                coverage=dict(complete=not missing, expected_days=len(expected),
                              observed_days=len(rows), missing_dates=missing),
                impressions=sum(row["impressions"] for row in rows), clicks=sum(row["clicks"] for row in rows),
                daily=rows)


def nested_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from nested_objects(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nested_objects(item)


def evidence_chunks(value):
    for item in nested_objects(value):
        if "chunk_id" in item and ("excerpt" in item or "content" in item):
            yield item["chunk_id"], item.get("excerpt", item.get("content"))


def matches_calculation(actual, expected):
    return all(actual.get(field) is None if number is None else (
        type(actual.get(field)) in {int, float} and isclose(actual[field], float(number), rel_tol=0, abs_tol=1e-9)
    ) for field, number in expected.items())


def date_in(text, value):
    year, month, day = map(int, value.split("-"))
    return bool(re.search(rf"(?<!\d)(?:(?:{year})[-/年])?0?{month}[-/月]0?{day}(?:日|号)?(?!\d)", text))


def integer_in(text, value):
    text = re.sub(r"(?<=\d)[,，](?=\d{3}(?!\d))", "", text)
    return bool(re.search(rf"(?<![\d.]){value}(?![\d.]|\s*[%％])", text))


def percent_in(text, value):
    if value is None:
        return False
    for match in PERCENT.finditer(text.replace("−", "-")):
        token = match[1]
        decimals = len(token.partition(".")[2])
        if abs(abs(float(token)) - abs(float(100 * value))) <= 0.5 * 10 ** -decimals + 1e-9:
            return True
    return False


def compare_wording(text, expected):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    decision_lines = [line for line in lines if re.search(r"结论|判定|结果|复核状态|复核建议|最终", line)]
    concrete = []
    for line in lines:
        for clause in re.split(r"[。；;]", line):
            if not re.search(REVIEW, clause) or re.search(NO_REVIEW, clause):
                continue
            if re.search(r"(?:达到|各达|至少|≥|超过|需).{0,50}(?:才|时)[，, ]*标记|"
                         r"两个可比窗口|若|如果|只有|规则要求|条件为|定义为|是否|无从|"
                         r"v2|不做|不适用|不参与|不报告|不作|不得", clause, re.I):
                continue
            if re.search(r"(?:因此|所以|故|判定为|标记为|结论[：:]|结果[：:])[^，,]{0,25}" + REVIEW, clause):
                concrete.append(clause)
    negative = any(re.search(NO_REVIEW, line) for line in lines)
    positive = bool(concrete)
    if expected == "review":
        correct = positive
    elif expected in {"below_threshold", "flat_or_rising"}:
        correct = negative and not positive
    elif expected == "low_exposure":
        correct = bool(re.search(r"样本(?:量)?不足|曝光(?:量)?不足|曝光门槛.{0,8}未达|曝光未达", text)) and not positive
    elif expected == "zero_impressions":
        correct = bool(re.search(r"零曝光|无曝光|0\s*(?:次)?曝光|曝光.{0,16}(?:为|=|是|：)\s*[零0]|曝光.{0,12}(?:分母为[零0]|CTR.{0,12}(?:无定义|未定义))", text)) and bool(re.search(UNDEFINED, text)) and not positive
    elif expected == "zero_baseline_ctr":
        correct = bool(re.search(r"基准.{0,40}(?:CTR.{0,14}(?:0\s*%|为\s*[零0])|点击.{0,10}(?:为\s*[零0]|0\s*次))", text)) and bool(re.search(UNDEFINED, text)) and not positive
    elif expected == "missing":
        correct = bool(re.search(NO_COMPARISON, text)) and not positive
    else:
        correct = not positive
    return correct, positive, decision_lines


def score_ctr_reply(evidence, receipts, projected, plan_id, rules, originals, final):
    """Check the new reply against SQL, the retrieved original, and the SSE text."""
    policy = evidence.get("policy") or {}
    verified_policy = (
        evidence.get("version") == "v1" and RULE in rules and policy.get("chunk_id") == RULE
        and policy.get("min_impressions") == 1000 and policy.get("min_relative_decline_pct") == 20
        and policy.get("content_sha256") == hashlib.sha256(originals[RULE].encode()).hexdigest()
    )
    windows = evidence.get("windows", [])
    checks = {"final_ctr_policy": verified_policy,
              "final_ctr_windows": len(windows) == len(receipts) == 2 and evidence.get("plan_id") == plan_id}
    statuses, rates, missing_labels = [], [], []
    for label, row, receipt in zip(("基准窗", "当前窗"), windows, receipts):
        coverage = receipt["coverage"]
        impressions = receipt["impressions"] if coverage["complete"] else None
        clicks = receipt["clicks"] if coverage["complete"] else None
        rate = ctr(receipt) if coverage["complete"] else None
        threshold = "未知" if impressions is None else "已满足" if impressions >= 1000 else "未达到"
        statuses.append(threshold)
        rates.append(rate)
        if not coverage["complete"]:
            missing_labels.append(label)
        fields = {"label": label, "coverage": coverage, "impressions": impressions, "clicks": clicks,
                  "threshold": threshold, **{key: receipt[key] for key in ("start_date", "end_date")}}
        imp_text, clicks_text = ("未知" if value is None else f"{value:,}" for value in (impressions, clicks))
        rate_text = f"{float(rate * 100):.2f}%" if rate is not None else "无法计算"
        window_line = (f"| {label} | {receipt['start_date']} 至 {receipt['end_date']} | "
                       f"{coverage['observed_days']}/{coverage['expected_days']} 天 | "
                       f"{'、'.join(coverage['missing_dates']) or '无'} | {imp_text} | {clicks_text} | "
                       f"{rate_text} | {threshold} |")
        wire = projected.get(row.get("runtime_call_id"), {})
        checks["final_ctr_windows"] &= (all(row.get(key) == value for key, value in fields.items())
                                         and matches_calculation(row, {"ctr_pct": rate * 100 if rate is not None else None})
                                         and row.get("artifact_id") == wire.get("artifact_id")
                                         and all(wire.get("result", {}).get(key) == receipt[key]
                                                 for key in ("plan_id", "start_date", "end_date", "coverage"))
                                         and window_line in final.splitlines())
    combined = "未达到" if "未达到" in statuses else "未知" if "未知" in statuses else "已满足"
    checks["final_ctr_threshold"] = (evidence.get("combined_threshold") == combined
                                     and f"两窗曝光条件{combined}。" in final
                                     and ("已确认样本不足" in final) == (combined == "未达到"))
    base, current = rates if len(rates) == 2 else (None, None)
    relative = (1 - current / base) * 100 if base and current is not None else None
    if missing_labels:
        statement = f"{'和'.join(missing_labels)}缺报，无法计算相对变化，暂缓复核判定。"
    elif base == 0 and current is not None:
        statement = "基准窗 CTR 为 0.00%，相对变化无定义，暂缓复核判定。"
    elif relative is None:
        statement = "至少一窗零曝光，无法计算相对变化，暂缓复核判定。"
    else:
        direction = "下降" if relative > 0 else "上升" if relative < 0 else "持平"
        statement = f"当前 CTR 较基准窗相对{direction} {abs(float(relative)):.2f}%。"
    checks["final_ctr_relative_reason"] = (statement in final and matches_calculation(
        evidence, {"relative_decline_pct": relative}))
    return checks


def apply_adjudication(row, decision):
    if not decision or not row.get("raw_sha256") or row["raw_sha256"] != decision.get("raw_sha256"):
        return
    row["automated_issues"] = row["issues"].copy()
    assert set(decision.get("remove", [])) <= set(row["issues"])
    row["issues"] = [issue for issue in row["issues"] if issue not in decision.get("remove", [])]
    row["issues"].extend(decision.get("add", []))
    row["passed"] = not row["issues"]
    row["adjudication"] = decision["reason"]


def score(case, path, conn, provenance):
    raw = read_json(path)
    assert raw["case"] == case
    events = raw["events"]
    final = "".join(row.get("content", "") for row in events if row["type"] == "text")
    plain = re.sub(r"[*`_]", "", final).replace("−", "-")
    trace_ids = sorted({event["trace_id"] for event in events if "trace_id" in event})
    trace = raw["traces"][0] if len(raw["traces"]) == 1 else {}
    steps = trace.get("steps", [])
    tools = [step for step in steps if step["stage"] == "tool_call"]
    results = [step for step in steps if step["stage"] == "tool_result"]
    sdk = raw["sdk_requests"]
    response_calls = {call["id"]: call for request in sdk for call in request.get("response_calls", [])}
    feedback = {row["tool_call_id"]: json.loads(row["content"])
                for request in sdk for row in request["tool_receipts"] if row.get("content")}
    projected_windows = {row["runtime_call_id"]: row for row in nested_objects(list(feedback.values()))
                         if "runtime_call_id" in row and isinstance(row.get("result"), dict)
                         and "ctr_pct" in row["result"] and row["result"].get("aggregation") == "window_total"}
    args_seen, cached, exact_dates, missing_evidence, incorrect_metrics, incorrect_feedback = [], [], set(), [], [], []
    distinct_receipts = {}
    first_date = case["windows"][0]["start_date"]
    last_date = case["windows"][-1]["end_date"]
    wrong_queries = []
    for item in raw["metric_calls"]:
        params = item["kwargs"]
        start, end = params["start_date"], params["end_date"]
        key = params["plan_id"], start, end
        args_seen.append(key)
        data = item["receipt"]
        if params["plan_id"] != case["plan_id"] or not (first_date <= start <= end <= last_date):
            wrong_queries.append(key)
        if params["plan_id"] != case["plan_id"]:
            continue
        expected = metric_oracle(conn, provenance, case, start, end)
        diff = sorted(key for key in expected if expected[key] != data.get(key))
        if diff:
            incorrect_metrics.append({"query": key, "different_fields": diff})
            continue
        distinct_receipts[key] = data
        if data["coverage"]["complete"]:
            exact_dates.add((start, end))
        else:
            missing_evidence.extend(data["coverage"]["missing_dates"])
    for step in steps:
        if step["stage"] == "tool_reuse" and step["action"] == METRIC:
            cached.append(step.get("tool_call_id"))
    for step in results:
        if step["action"] != METRIC:
            continue
        args = step.get("evidence", {}).get("arguments")
        if args is None:
            continue
        key = args["plan_id"], args["start_date"], args["end_date"]
        data = distinct_receipts.get(key)
        wire = feedback.get(step.get("tool_call_id"), {}).get("result")
        if data is None or not wire:
            continue
        fields = ["plan_id", "source_id", "source_plan_id", "data_version", "scope", "start_date", "end_date", "coverage"]
        if data["coverage"]["complete"]:
            fields.extend(("clicks", "impressions"))
        different = [field for field in fields if wire.get(field) != data[field]]
        if "ctr_pct" in wire:
            expected_rate = ctr(data) if data["coverage"]["complete"] else None
            if not matches_calculation(wire, {"ctr_pct": expected_rate * 100 if expected_rate is not None else None}):
                different.append("ctr_pct")
            if not data["coverage"]["complete"]:
                different.extend(field for field in ("clicks", "impressions") if field in wire)
        if different:
            incorrect_feedback.append({"query": key, "different_fields": different})
    rules = {chunk for chunk, original in evidence_chunks(list(feedback.values()))
             if raw["rule_originals"].get(chunk) == original}
    rule_receipts, duplicate_rules, redundant_v1_reads, seen_v1 = [], [], [], set()
    for step in tools:
        if step["action"] != "search_knowledge":
            continue
        call_id = step["tool_call_id"]
        chunks = sorted({chunk for chunk, _ in evidence_chunks(feedback.get(call_id, {}))})
        if not chunks:
            continue
        if any(item["chunks"] == chunks for item in rule_receipts):
            duplicate_rules.append(call_id)
        v1 = {RULE, LOW, WINDOW}.intersection(chunks)
        if v1 and v1 <= seen_v1:
            redundant_v1_reads.append(call_id)
        seen_v1.update(v1)
        rule_receipts.append({"call_id": call_id, "chunks": chunks})
    expected = case["expected"]
    missing_fixture = None
    if expected == "missing":
        fixture_path = path.with_name("missing-history.sqlite3")
        if fixture_path.exists():
            fixture_sha256 = sha(fixture_path)
            with closing(read_only(fixture_path)) as fixture:
                fixture.execute("ATTACH DATABASE ? AS original", (ARTIFACT.as_uri() + "?mode=ro",))
                deleted = fixture.execute("SELECT * FROM original.plan_daily_metrics EXCEPT SELECT * FROM plan_daily_metrics").fetchall()
                added = fixture.execute("SELECT * FROM plan_daily_metrics EXCEPT SELECT * FROM original.plan_daily_metrics").fetchall()
            missing_fixture = {"sha256": fixture_sha256, "deleted_source_rows": deleted, "added_source_rows": added,
                               "one_expected_delete": (len(deleted) == 1 and deleted[0][:2] ==
                                                       (case["source_plan_id"], case["missing_date"]) and not added
                                                       and fixture_sha256 == raw["fixture_sha256"])}
    accepted_rules = {WINDOW} if expected == "single" else (
        {LOW} if expected == "low_exposure" else {RULE, LOW, WINDOW}
        if expected in {"zero_impressions", "zero_baseline_ctr", "missing"} else {RULE}
    )
    cited = {chunk for chunk in accepted_rules & rules if chunk in final}
    windows = case["windows"]
    pairs = [(w["start_date"], w["end_date"]) for w in windows]
    if projected_windows:
        valid_ranges = set(pairs)
        if case["request"] in {"overall", "missing"} or case.get("probe"):
            valid_ranges.add((first_date, last_date))
        wrong_queries.extend(key for key in args_seen if key[1:] not in valid_ranges and key not in wrong_queries)
    base = ctr(windows[0])
    current = ctr(windows[-1])
    drop = 1 - current / base if len(windows) == 2 and base and current is not None else None
    calculation = dict(baseline_ctr_pct=base * 100 if base is not None else None,
                       current_ctr_pct=current * 100 if current is not None else None,
                       decline_pp=(base - current) * 100 if base is not None and current is not None else None,
                       relative_decline_pct=drop * 100 if drop is not None else None)
    wire_calculations = [item for item in nested_objects(list(feedback.values())) if "baseline_ctr_pct" in item
                         and "relative_decline_pct" in item]
    applicable = [windows[0]] if expected == "missing" else windows
    # A verified terminal gap is sufficient evidence to withhold the comparison.
    has_baseline = pairs[0] in exact_dates
    if expected == "missing" and not has_baseline:
        applicable = []
    value_checks = []
    for window in applicable:
        value = ctr(window)
        lines = [line for line in plain.splitlines() if date_in(line, window["start_date"])
                 and date_in(line, window["end_date"])]
        numeric = [line for line in lines if integer_in(line, window["impressions"]) and integer_in(line, window["clicks"])]
        value_checks.append(dict(dates=bool(lines), impressions=integer_in(plain, window["impressions"]),
                                 clicks=integer_in(plain, window["clicks"]),
                                 ctr=bool(re.search(UNDEFINED, plain)) if value is None else percent_in("\n".join(numeric) or plain, value)))
    correct, positive, decisions = compare_wording(plain, expected)
    browser_file = path.with_name("browser.json")
    browser = read_json(browser_file) if browser_file.exists() else {}
    delegated = [row for row in tools if row["action"] == "delegate_tasks"]
    worker_starts = [row for row in steps if row["stage"] == "worker" and row["action"] == "created"]
    worker_ends = [row for row in steps if row["stage"] == "worker" and row["action"] == "reclaimed"]
    duplicates = [key for key, count in Counter(args_seen).items() if count > 1]
    relative_lines = "\n".join(line for line in plain.splitlines() if re.search(r"相对|环比", line))
    complete_reporting = bool(re.search(r"完整|无缺报|无缺失|未缺报|7\s*/\s*7|7\s*天.{0,5}齐|覆盖.{0,12}7\s*天", plain))
    has_query = case["missing_date"] in missing_evidence if expected == "missing" else set(pairs) <= exact_dates
    extra_single_calls = expected == "single" and (len(args_seen) != 1 or bool(delegated))
    unsupported = [match.group() for match in CAUSAL.finditer(plain)
                   if not re.search(r"不能|无法|未|不|可能|需|待|假设", plain[max(0, match.start() - 12):match.end()])]
    checks = {
        "real_model": bool(sdk) and all(row.get("status") == 200 and row.get("response_model") == "deepseek-flash" for row in sdk),
        "sse_terminal_commit": raw["http_status"] == 200 and not raw["failure"] and bool(final.strip())
        and [row["type"] for row in events if row["type"] in {"done", "error"}] == ["done"]
        and events[-1]["type"] == "done" and len(trace_ids) == 1 and trace.get("status") == "completed"
        and raw["committed_at_terminal"] == [final],
        "chromium_ui": case["transport"] != "chromium" or (not browser.get("failure") and browser.get("events") == events and bool(browser.get("rendered"))),
        "correct_queries_and_scope": has_query and not wrong_queries,
        "raw_and_projected_receipts": not incorrect_metrics and not incorrect_feedback,
        "sdk_calculation_matches_sql": not wire_calculations if expected in {"single", "missing"} else (
            bool(wire_calculations) and all(matches_calculation(item, calculation) for item in wire_calculations)),
        "missing_fixture_integrity": expected != "missing" or bool(missing_fixture and missing_fixture["one_expected_delete"]),
        "final_plan_scope": not ({int(value) for value in re.findall(r"计划\s*#(\d+)", plain)} - {case["plan_id"]}),
        "final_window_dates": all(row["dates"] for row in value_checks),
        "final_window_numbers": all(row["impressions"] and row["clicks"] and row["ctr"] for row in value_checks),
        "final_coverage": date_in(plain, case["missing_date"]) and bool(re.search(r"缺报|缺失|缺口|未覆盖", plain))
        if expected == "missing" else complete_reporting,
        "final_relative": expected == "missing" or drop is None or percent_in(relative_lines, drop),
        "rule_retrieved_and_cited": bool(cited),
        "business_decision": correct,
        "no_unsupported_cause": not unsupported,
        "no_duplicate_reads": not duplicates and not cached,
        "no_duplicate_rule_receipts": not duplicate_rules,
        "single_direct_query": not extra_single_calls,
        "worker_lifecycle": sorted(row["runtime_call_id"] for row in worker_starts) ==
        sorted(row["runtime_call_id"] for row in worker_ends),
    }
    if projected_windows:
        checks["no_unsupported_statistics"] = not UNSUPPORTED_STATISTICS.search(plain)
        verified_pair = [distinct_receipts.get((case["plan_id"], *pair)) for pair in pairs]
        if len(verified_pair) == 2 and all(verified_pair):
            replies = [step["evidence"] for step in steps if (step["stage"], step["action"]) == ("receipt", "ctr_reply")]
            checks["final_ctr_receipts"] = len(replies) == 1
            if len(replies) == 1:
                checks.update(score_ctr_reply(replies[0], verified_pair, projected_windows, case["plan_id"],
                                              rules, raw["rule_originals"], final))
    if case.get("probe"):
        results_by_id = {row.get("runtime_call_id"): row for row in results}
        first_metric = next((row for row in tools if row["action"] == METRIC), {})
        first_args = results_by_id.get(first_metric.get("runtime_call_id"), {}).get("evidence", {}).get("arguments")
        first_is_whole_worker = (first_metric.get("role") == "team_worker" and first_args is not None
                                 and (first_args["start_date"], first_args["end_date"]) == (first_date, last_date))
        worker_roles = {row["action"]: row.get("parent_call_id") for row in tools if row.get("role") == "team_worker"}
        checks["separate_metric_and_rule_workers"] = (
            len(worker_starts) >= 2 and {METRIC, "search_knowledge"} <= worker_roles.keys()
            and worker_roles[METRIC] != worker_roles["search_knowledge"]
        )
        if case["probe"] in {"whole_then_split", "worker_missing"}:
            checks["whole_worker_receipt_first"] = first_is_whole_worker
            handoffs = {row["tool_call_id"] for row in delegated if any(
                isinstance(item, dict) and item.get("runtime_call_id") == first_metric.get("runtime_call_id")
                for item in feedback.get(row["tool_call_id"], {}).get("evidence", []))}
            split_after_handoff = {call["id"] for request in sdk if handoffs.intersection(
                item["tool_call_id"] for item in request["tool_receipts"])
                for call in request.get("response_calls", []) if call["function"]["name"] == METRIC}
            if case["probe"] == "worker_missing":
                checks["stop_after_whole_gap"] = len(args_seen) == 1 and has_query
            else:
                checks["split_after_whole_receipt"] = (first_is_whole_worker and len(args_seen) == 3 and has_query
                                                       and len(split_after_handoff) == 2)
    issues = [name for name, ok in checks.items() if not ok]
    if positive and expected not in {"single", "review"}:
        issues.append("false_review")
    if expected == "review" and not correct:
        issues.append("missed_review")
    if not has_query and checks["sse_terminal_commit"]:
        issues.append("ended_before_required_evidence")
    if any(step["action"] == "get_daily_report_by_date" for step in tools):
        issues.append("user_daily_report_queried")
    return {
        **{key: case[key] for key in ("id", "source_plan_id", "plan_id", "stratum", "expected", "request", "transport")},
        "attempted": True, "trace_id": trace_ids[0] if trace_ids else None,
        "raw": str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path),
        "raw_sha256": sha(path), "elapsed_seconds": raw["elapsed_seconds"],
        "sdk": len(sdk), "sdk_seconds": round(sum(row.get("elapsed_seconds", 0) for row in sdk), 3),
        "http_statuses": dict(Counter(str(row.get("status")) for row in sdk)),
        "response_models": sorted({row.get("response_model") for row in sdk if row.get("response_model")}),
        "tool_calls": len(tools), "tool_counts": dict(Counter(row["action"] for row in tools)),
        "metric_calls": len(args_seen), "metric_queries": args_seen, "duplicate_queries": duplicates,
        "cached_query_attempts": cached, "wrong_queries": wrong_queries,
        "metric_receipt_diffs": incorrect_metrics, "projected_receipt_diffs": incorrect_feedback,
        "direct_metric_calls": sum(step["action"] == METRIC and step.get("role") == "orchestrator" for step in tools),
        "worker_metric_calls": sum(step["action"] == METRIC and step.get("role") == "team_worker" for step in tools),
        "delegations": len(delegated), "workers": len(worker_starts),
        "followup_opened": any(row["stage"] == "evidence_followup" and row["action"] == "opened" for row in steps),
        "unmatched_trace_call_ids": [row.get("tool_call_id") for row in tools if row.get("tool_call_id") not in response_calls],
        "matched_windows": sorted(exact_dates), "missing_dates_verified": sorted(set(missing_evidence)),
        "missing_fixture": missing_fixture,
        "expected_windows": windows, "expected_relative_decline": str(drop) if drop is not None and expected != "missing" else None,
        "expected_ctr": [str(value) if value is not None else None for value in map(ctr, windows)],
        "wire_calculations": wire_calculations,
        "rule_chunks_retrieved": sorted(rules), "valid_rule_chunks_cited": sorted(cited),
        "rule_read_receipts": rule_receipts, "duplicate_rule_receipt_calls": duplicate_rules,
        "rule_calls_without_new_v1_chunks": redundant_v1_reads,
        "plan_id_in_reply": integer_in(plain, case["plan_id"]),
        "final_value_checks": value_checks, "decision_lines": decisions,
        "checks": checks, "issues": issues, "passed": not issues, "final_reply": final,
    }


def aggregate(rows):
    durations = [row["elapsed_seconds"] for row in rows if row.get("elapsed_seconds") is not None]
    return dict(cases=len(rows), passed=sum(row["passed"] for row in rows),
                attempted=sum(row["attempted"] for row in rows),
                independent_plans=len({row["source_plan_id"] for row in rows}),
                median_seconds=round(median(durations), 3) if durations else None,
                max_seconds=max(durations, default=None),
                sdk=sum(row.get("sdk", 0) for row in rows), tool_calls=sum(row.get("tool_calls", 0) for row in rows),
                metric_calls=sum(row.get("metric_calls", 0) for row in rows),
                delegated_cases=sum(bool(row.get("delegations")) for row in rows),
                direct_metric_calls=sum(row.get("direct_metric_calls", 0) for row in rows),
                worker_metric_calls=sum(row.get("worker_metric_calls", 0) for row in rows),
                wrong_plan_or_window_queries=sum(len(row.get("wrong_queries", [])) for row in rows),
                duplicate_plan_window_queries=sum(len(row.get("duplicate_queries", [])) +
                                                  len(row.get("cached_query_attempts", [])) for row in rows),
                duplicate_rule_receipts=sum(len(row.get("duplicate_rule_receipt_calls", [])) for row in rows),
                rule_reads_without_new_v1_chunks=sum(len(row.get("rule_calls_without_new_v1_chunks", [])) for row in rows),
                workers=sum(row.get("workers", 0) for row in rows),
                issues=dict(Counter(issue for row in rows for issue in row["issues"])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal", type=Path, action="append", help="Raw archive root, repeatable")
    parser.add_argument("--output", type=Path, default=OUTPUT / "rescore")
    parser.add_argument("--sample", type=Path, default=SAMPLE)
    parser.add_argument("--cases", help="Comma-separated subset of the frozen sample")
    args = parser.parse_args()
    sample = read_json(args.sample)
    provenance = read_json(PROVENANCE)
    cases = sample["cases"]
    if args.cases:
        selected = set(args.cases.split(","))
        cases = [case for case in cases if case["id"] in selected]
        if len(cases) != len(selected):
            parser.error("--cases must identify cases in --sample")
    paths = [path for folder in args.formal or [OUTPUT / "formal"] for path in folder.rglob("raw.json")]
    by_case = {path.parent.name: path for path in paths}
    assert len(paths) == len(by_case)
    rows = []
    with closing(read_only(ARTIFACT)) as conn:
        for case in cases:
            if path := by_case.get(case["id"]):
                rows.append(score(case, path.resolve(), conn, provenance))
            else:
                rows.append({**case, "attempted": False, "passed": False, "issues": ["missing_formal_archive"]})
    adjudication_path = ROOT / "docs/task26-t2-adjudication.json"
    if adjudication_path.exists():
        decisions = read_json(adjudication_path)
        for row in rows:
            apply_adjudication(row, decisions.get(row["id"]))
    with closing(read_only(OUTPUT / "sdk-ledger.sqlite3")) as conn:
        ledger = [{"run": run, "case": case, "outbound_attempts": count} for run, case, count in conn.execute(
            "SELECT run_id, case_id, COUNT(*) FROM sdk_attempts GROUP BY run_id, case_id ORDER BY run_id, case_id"
        )]
    sdk_total = sum(row["outbound_attempts"] for row in ledger)
    assert sdk_total <= LIMIT
    group = lambda key: {name: aggregate([row for row in rows if row[key] == name])
                         for name in sorted({row[key] for row in rows})}
    summary = {"formal": aggregate(rows), "strata": group("stratum"), "request": group("request"),
               "transport": group("transport"), "sample_sha256": sha(args.sample),
               "sdk_limit": LIMIT, "total_sdk_attempts": sdk_total, "sdk_ledger": ledger,
               "http_statuses": dict(sum((Counter(row.get("http_statuses", {})) for row in rows), Counter())),
               "response_models": sorted({model for row in rows for model in row.get("response_models", [])})}
    failures = [{key: row[key] for key in ("id", "stratum", "transport", "source_plan_id", "plan_id",
                                          "issues", "trace_id", "raw", "raw_sha256", "final_reply", "adjudication") if key in row}
                for row in rows if not row["passed"]]
    write_json(args.output / "case-results.json", rows)
    write_json(args.output / "failures.json", failures)
    write_json(args.output / "real-summary.json", summary)
    print(json.dumps(summary["formal"], ensure_ascii=False))


if __name__ == "__main__":
    main()
