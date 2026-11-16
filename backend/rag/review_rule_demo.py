"""Import immutable demo chunks into a separately selected Chroma directory."""

import argparse
from contextlib import closing


SOURCE = "广告投放复核手册（演示）"
DEMO_RULES = (
    ("ctr-review-v1-low-ctr-01", "ctr-review#low-ctr", "v1", "点击率下降复核",
     "两个可比窗口各达到 1,000 次计划曝光，且当前 CTR 较基准窗口相对下降至少 20% 时，标记为待复核。"
     "先核对同一计划的历史曝光和点击，确认后检查素材与流量构成。用户级汇总不得代替计划指标。"),
    ("ctr-review-v2-low-ctr-01", "ctr-review#low-ctr", "v2", "点击率下降复核",
     "两个可比窗口各达到 2,000 次计划曝光，且当前 CTR 较基准窗口相对下降至少 15% 时，标记为待复核。"
     "先核对同一计划的历史曝光和点击，确认后检查素材与流量构成。用户级汇总不得代替计划指标。"),
    ("ctr-review-v1-low-volume-01", "ctr-review#low-volume", "v1", "点击率样本量",
     "任一窗口的计划曝光不足 1,000 次时，记为样本不足。补齐曝光和点击数据后再比较点击率。"),
    ("ctr-review-v1-windows-01", "ctr-review#windows", "v1", "比较窗口口径",
     "基准窗口和当前窗口应覆盖相同天数，并尽量对齐星期分布。按计划分别汇总曝光和点击，"
     "再以总点击除以总曝光计算窗口 CTR。"),
    ("creative-review-v1-fatigue-01", "creative-review#fatigue", "v1", "素材疲劳复核",
     "同一受众的触达频次升高且点击率持续下滑时，按素材拆分流量和点击表现，排查素材疲劳。"),
    ("bid-review-v1-cpc-01", "bid-review#cpc", "v1", "点击成本复核",
     "平均点击成本 CPC 突然上升时，先比较同一版位的竞价和流量占比，再排查出价变化。"),
    ("budget-review-v1-pacing-01", "budget-review#pacing", "v1", "预算消耗进度",
     "日预算提前耗尽时，按小时核对花费与预算消耗速度，区分流量高峰和投放节奏异常。"),
    ("conversion-review-v1-tracking-01", "conversion-review#tracking", "v1", "转化跟踪复核",
     "转化骤降时，先验证转化事件的上报链路、归因窗口和去重设置，再核对落地页是否正常。"),
)


def seed_review_rules(rag):
    existing = set(rag.collection.get(ids=[rule[0] for rule in DEMO_RULES], include=[])["ids"])
    for chunk_id, rule_id, version, title, content in DEMO_RULES:
        if chunk_id not in existing:
            rag.add_text(content, {
                "rule_id": rule_id, "version": version, "title": title, "source": SOURCE,
            }, doc_id=chunk_id)


if __name__ == "__main__":
    from backend.rag.rag_knowledge import RAGKnowledge

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", help="演示用 Chroma 数据目录")
    args = parser.parse_args()
    with closing(RAGKnowledge(db_path=args.db_path)) as rag:
        seed_review_rules(rag)
        print(f"演示规则导入完成，集合共 {rag.collection.count()} 个分块")
