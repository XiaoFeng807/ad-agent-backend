"""Freeze the independent SQL oracle and exact user messages for TASK-027-T3."""

from contextlib import closing
from datetime import datetime, timezone
from hashlib import file_digest, sha256
import json
from pathlib import Path
import sqlite3

from backend.rag.review_rule_demo import DEMO_RULES
from scripts.run_task26_t1 import ARTIFACT, ROOT, read_only

DOCS = ROOT / "docs/task27-t3"
OWNER = 17
PLANS = [
    {"id": 101, "plan_name": "北岸搜索", "status": "active", "daily_budget": 310, "user_id": OWNER},
    {"id": 102, "plan_name": "南桥搜索", "status": "paused", "daily_budget": 720, "user_id": OWNER},
]
PLAN_SQL = "SELECT id, plan_name, status, daily_budget, user_id FROM ad_plans WHERE user_id=? ORDER BY id"
ALERT_SQL = "SELECT type, message, level, is_read, user_id FROM alerts WHERE user_id=? ORDER BY id"
ALERT = {"type": "ctr_review", "message": "投放监控触发严重 CTR 波动告警，需核实历史及复核条文。",
         "level": "critical", "is_read": 0, "user_id": OWNER}
LATE = (("2026-10-17", "2026-10-23"), ("2026-10-24", "2026-10-30"))
EARLIER = (("2026-10-16", "2026-10-22"), ("2026-10-23", "2026-10-29"))
BRANCHES = (
    ("周例会需要核销 CTR 监控告警。", "v1", LATE, ("13289562", "27589150"), 101),
    ("请整理搜索投放交接用的 CTR 复核清单。", "v1", EARLIER, ("26998434", "32135670"), 102),
    ("质检要抽查 CTR 告警是否达到复核标准。", "v2", LATE, ("28752986", "10321103"), 101),
    ("请为预算例会准备 CTR 告警的复核记录。", "v2", EARLIER, ("11028540", "26998434"), 102),
)
POLICY = {"v1": (1000, 20), "v2": (2000, 15)}
RUBRIC = {
    "simple": "实际读取对应计划状态，终态及群历史有效，状态回答一致。实际详情、额外读取和过度组队分别统计。",
    "business": [
        "两个计划各自使用指定的两窗，正确披露覆盖、可核实的曝光、点击、CTR 及相对变化。百分点容差 0.01，计数精确。",
        "实际检索指定规则版本，引用该版本的低 CTR 分块，准确应用两窗曝光和相对下降门槛。",
        "两计划历史及条文均经实际查证后，才实际读取应入清单计划的详情。答复中给出该编号、名称、状态和日预算。正确直查与委派同分。",
        "缺项时继续核对条文和未受影响计划，报告缺项类型及已知范围，暂缓完整清单。缺绑定的另一个窗口无需重复试读；缺日的另一个窗口仍应保留。任何依赖详情读取或披露均不通过。",
        "业务通过要求 HTTP 成功、唯一 done 终态及回复写入本轮群。逐项记录与最早偏离。",
    ],
    "collaboration": [
        "自主委派以实际模型工具调用为准。Worker 数和重叠区间分别从 Trace 和带单调时钟的执行入口获取。",
        "正样本交接须有下游 Worker 首轮输入中的已准入上游工具产物。匹配上游任务、产物 ID、输入内容及下游实际详情参数。与主 Agent 转述或直查区分。",
        "完整交接链需要从下游收到的实际指标及规则证据独立重算出目标。部分交接单列，完整原样本与换绑样本都满足才计配对覆盖。",
        "缺项的下游阻塞须有已声明的依赖和实际调度证据。缺项时底层详情读取数和回复披露数分别统计。",
        "重复读取按同轮底层工具名及注入属主后的规范参数累计。拒绝、缓存命中及复用另记。",
    ],
}


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def write(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def message(prefix, version, windows):
    baseline, current = (" 至 ".join(window) for window in windows)
    return (
        f"{prefix}先看我当前未读告警的级别；若发现严重 CTR 波动，请调查 #101 和 #102。"
        f"历史基准窗为 {baseline}，当前窗为 {current}。"
        "逐计划核对两窗的覆盖、曝光、点击、CTR 和相对变化。"
        f"独立核对《点击率下降复核》 {version} 条文，给出分块编号、曝光与下降门槛，并据此形成复核清单。"
        "请把清单内计划的编号、名称、当前状态和日预算写在清单中。"
        "证据有缺项时请保留可核实的窗口和条文，说明具体缺项并暂缓出清单。"
    )


def freeze():
    provenance = json.loads((ROOT / "docs/criteo-provenance.json").read_text(encoding="utf-8"))
    require(digest(ARTIFACT) == provenance["artifact_sha256"], "Criteo artifact hash mismatch")
    sql = {key: (DOCS / f"{key}.sql").read_text(encoding="utf-8-sig") for key in ("window", "review")}
    cases = [{"id": f"simple-{i}", "kind": "simple", "message": f"帮我查一下广告计划 #{p['id']} 现在是什么状态？",
              "bindings": {}, "expected": {"detail": p}} for i, p in enumerate(PLANS, 1)]

    with closing(read_only(ARTIFACT)) as conn:
        conn.row_factory = sqlite3.Row
        manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
        require(manifest == {key: value for key, value in provenance.items() if key != "artifact_sha256"},
                "Criteo artifact metadata mismatch")

        def build(index, kind, sources, omitted=None):
            prefix, version, windows, _, original_target = BRANCHES[index - 1]
            minimum, decline = POLICY[version]
            rule = next(rule for rule in DEMO_RULES if rule[1:3] == ("ctr-review#low-ctr", version))
            plans = {}
            for p, source in zip(PLANS, sources):
                rows = []
                for window in windows:
                    exclusion = omitted["date"] if omitted and omitted["source"] == source else None
                    params = [source, *window, exclusion]
                    row = dict(conn.execute(sql["window"], params).fetchone())
                    row["missing_dates"] = row["missing_dates"].split(",") if row["missing_dates"] else []
                    rows.append({**row, "sql_params": params, "start_date": window[0], "end_date": window[1]})
                baseline, current = rows
                params = [baseline["impressions"], current["impressions"], baseline["ctr_pct"],
                          current["ctr_pct"], minimum, decline]
                result = dict(conn.execute(sql["review"], params).fetchone())
                plans[str(p["id"])] = {"windows": rows, **result, "review_sql_params": params,
                                       "unbound": source is None}
            eligible = [int(plan) for plan, row in plans.items() if row["qualifies"]]
            target = eligible[0] if len(eligible) == 1 and all(row["qualifies"] is not None for row in plans.values()) else None
            expected_target = {"branch": original_target, "swap": 203 - original_target, "gap": None}[kind]
            require(target == expected_target, f"unexpected target for {kind}-{index}: {target}")
            if target:
                require(all(row["observed_days"] == 7 for plan in plans.values() for row in plan["windows"]),
                        "incomplete positive window")
            return {"id": f"{kind}-{index}", "kind": kind, "pair": index, "message": message(prefix, version, windows),
                    "bindings": {str(p["id"]): source for p, source in zip(PLANS, sources)}, "excluded_row": omitted,
                    "expected": {"metrics": plans, "rule": {"chunk_id": rule[0], "rule_id": rule[1], "version": rule[2],
                                                           "title": rule[3], "content": rule[4], "min_impressions": minimum,
                                                           "min_relative_decline_pct": decline},
                                 "detail": next((p for p in PLANS if p["id"] == target), None)}}

        for index, (_, _, _, sources, _) in enumerate(BRANCHES, 1):
            cases.extend((build(index, "branch", sources), build(index, "swap", sources[::-1])))
        cases.append(build(1, "gap", (BRANCHES[0][3][0], None)))
        cases.append(build(2, "gap", BRANCHES[1][3], {"source": BRANCHES[1][3][0], "date": "2026-10-27"}))
    for case in cases:
        case["message_sha256"] = sha256(case["message"].encode()).hexdigest()
    return {"task": "TASK-027-T3", "frozen_utc": datetime.now(timezone.utc).isoformat(),
            "source": provenance, "source_path": str(ARTIFACT.relative_to(ROOT)), "owner": OWNER,
            "plans": PLANS, "alert": ALERT, "plan_sql": PLAN_SQL, "alert_sql": ALERT_SQL, "sql": sql,
            "rule_corpus": DEMO_RULES, "sdk_limit": 160, "rubric": RUBRIC, "cases": cases}


def require(valid, reason):
    if not valid:
        raise ValueError(reason)


if __name__ == "__main__":
    import sys

    output = Path(sys.argv[1])
    if output.exists():
        raise FileExistsError(output)
    write(output, freeze())
