"""Frozen, synthetic product and finding expectations for TASK-045-T3."""

from scripts import task45_t1_fixture as base


QUESTION = ("雾岚保温袋、栖木显示器架、微格线材包这三款商品下周要做活动。"
            "帮我逐款对比最近一周和前一周的销量，再核对页面五点有没有和商品资料对不上的地方。"
            "告诉我每款的问题和依据。有明确规格冲突的商品里，优先处理销量降幅最大的那款。"
            "这次先给我检查清单。")
CASES = ("base", "swap", "missing-fact", "simple")
FIXED_ONLY = ("tie", "mixed-shop", "mixed-market", "unauthorized", "unresolved")
FOREIGN = "T45-EXTRA"
CONFLICTS = {
    base.TOTE: ("capacity", "12 L capacity in the main compartment.", "15 L capacity in the main compartment."),
    base.RISER: ("load", "Supports a centered load up to 10 kg on a level desk.",
                 "Supports a centered load up to 15 kg on a level desk."),
}


def source(case):
    rows = base.source(case)
    for row in rows:
        sku = row["target"]["sku"]
        options = [option for _, _, option in base.PRODUCTS[sku][-1]]
        if sku in CONFLICTS:
            _, good, conflicting = CONFLICTS[sku]
            options[options.index(good)] = conflicting
        row["bullets"] = options
    if case == "tie":
        rows[1]["series"] = dict(rows[0]["series"])
        rows[1]["sales_version"] += 1
    return rows


def expected_findings(case):
    result = {}
    for sku, product in base.PRODUCTS.items():
        fields = base.PRODUCTS[sku][-1]
        result[sku] = [{"bullet": number, "status": (
            "unverifiable" if case == "missing-fact" and sku == base.TOTE and fact_id == "capacity"
            else "conflict" if sku in CONFLICTS and fact_id == CONFLICTS[sku][0] else "supported"),
            "fact_id": fact_id, "statement": statement} for number, (fact_id, statement, _) in enumerate(fields, 1)]
    return result


def question(case):
    if case == "simple":
        return base.SIMPLE
    if case == "unresolved":
        return QUESTION.replace(base.PRODUCTS[base.CASE][0], "未授权商品")
    return QUESTION
