"""Frozen, synthetic StorePilot SKUs used only by the isolated browser acceptance."""

from datetime import date, timedelta


SHOP = "t45-ops"
MARKET = "US"
AS_OF = "2026-11-14"
KEYS = ("baseline_start", "baseline_end", "current_start", "current_end")
WINDOWS = ("2026-10-31", "2026-11-06", "2026-11-07", "2026-11-13")
LAST_WEEK = ("2026-11-02", "2026-11-08")
MISSING_DAY = "2026-11-10"
TOTE, RISER, CASE = "T45-THERMAL-TOTE", "T45-DESK-RISER", "T45-CABLE-CASE"

# Each option is an independently readable English paraphrase of the source statement.
PRODUCTS = {
    TOTE: ("雾岚保温袋", 3, 7, [
        ("capacity", "The main compartment holds 12 L.", "12 L capacity in the main compartment."),
        ("liner", "The liner is polyethylene vinyl acetate (PEVA).", "Lined with polyethylene vinyl acetate (PEVA)."),
        ("carry", "Two webbing handles are attached to the bag.", "Two attached webbing handles for carrying."),
        ("zip", "A zipper closes the main compartment.", "Zipper closure on the main compartment."),
        ("care", "Wipe the liner with a damp cloth.", "Wipe the liner clean with a damp cloth."),
    ]),
    RISER: ("栖木显示器架", 4, 11, [
        ("width", "The platform is 42 cm wide.", "Platform measures 42 cm wide."),
        ("height", "The platform sits 9 cm above the desk.", "Raises the platform 9 cm above the desk."),
        ("load", "The centered load limit is 10 kg on a level desk.", "Supports a centered load up to 10 kg on a level desk."),
        ("material", "The platform is bamboo.", "Bamboo platform."),
        ("feet", "Four silicone pads are fitted under the stand.", "Four silicone pads fitted under the stand."),
    ]),
    CASE: ("微格线材包", 2, 19, [
        ("depth", "The case is 4 cm deep when closed.", "Measures 4 cm deep when closed."),
        ("pockets", "There are two interior mesh pockets.", "Two interior mesh pockets."),
        ("loops", "The interior has six elastic loops.", "Six elastic loops inside the case."),
        ("fabric", "The shell is woven polyester.", "Woven polyester outer shell."),
        ("closure", "The case closes with a two-pull zipper.", "Two-pull zipper closure."),
    ]),
}
ORIGINAL = ["Everyday design.", "For routine use.", "Simple style.", "Product accessory.", "Standard model."]
SERIES = {
    TOTE: [12, 10, 13, 9, 14, 9, 10, 5, 7, 5, 4, 6, 4, 5],
    RISER: [12, 10, 13, 9, 14, 9, 10, 10, 9, 8, 10, 10, 9, 8],
    CASE: [5, 5, 6, 5, 5, 6, 5, 5, 0, 8, 7, 7, 7, 7],
}
SIMPLE = "这款上周卖了多少件？"
COMPLEX = ("雾岚保温袋、栖木显示器架、微格线材包这几款最近一周哪款销量下滑最明显？"
           "跟前一周比一下，结合商品资料给入选商品写一版英文五点，我确认后再应用。")
CONTROL = "看看{title}最近两周的销量。只有两窗销量数据完整才根据商品资料重写英文五点，我确认后再应用。"
TOTE_CONTROL = CONTROL.format(title=PRODUCTS[TOTE][0]) + "英文五点必须写明12 L。"


def cases():
    return [
        {"id": "simple-tote", "group": "task", "sku": TOTE, "message": SIMPLE, "context": TOTE},
        {"id": "simple-riser", "group": "task", "sku": RISER, "message": SIMPLE, "context": RISER},
        *[
            {"id": variant, "group": "task", "variant": variant, "message": COMPLEX}
            for variant in ("base", "swap", "missing-day", "missing-fact", "confirmed-zero")
        ],
        {"id": "control-tote", "group": "control", "sku": TOTE, "message": TOTE_CONTROL, "approve": True},
        {"id": "control-riser", "group": "control", "variant": "swap", "sku": RISER,
         "message": CONTROL.format(title=PRODUCTS[RISER][0])},
        {"id": "control-missing-day", "group": "control", "variant": "missing-day",
         "sku": TOTE, "message": TOTE_CONTROL},
        {"id": "control-missing-fact", "group": "control", "variant": "missing-fact",
         "sku": TOTE, "message": TOTE_CONTROL},
        {"id": "control-confirmed-zero", "group": "control", "variant": "confirmed-zero",
         "sku": CASE, "message": CONTROL.format(title=PRODUCTS[CASE][0])},
    ]


def source(variant):
    start = date.fromisoformat(WINDOWS[0])
    output = []
    for sku, (title, version, facts_version, rows) in PRODUCTS.items():
        counts = SERIES[sku]
        if variant == "swap" and sku in (TOTE, RISER):
            counts = SERIES[RISER if sku == TOTE else TOTE]
        series = {(start + timedelta(days=i)).isoformat(): units for i, units in enumerate(counts)}
        if variant == "missing-day" and sku == TOTE:
            del series[MISSING_DAY]
        if variant == "confirmed-zero" and sku == CASE:
            series.update({day: 0 for day in list(series)[7:]})
        removed = variant == "missing-fact" and sku == TOTE
        facts = [{"fact_id": name, "statement": statement, "options": [option]}
                 for name, statement, option in rows if not (removed and name == "capacity")]
        output.append({"target": {"shop_id": SHOP, "marketplace_id": MARKET, "sku": sku},
                       "title": title, "listing_version": version, "bullets": ORIGINAL,
                       "price_cents": 1900 + 100 * version, "inventory_units": 27 + version,
                       "series": series, "sales_version": 2 if series != {
                           (start + timedelta(days=i)).isoformat(): n for i, n in enumerate(SERIES[sku])
                       } else 1, "facts_version": facts_version + int(removed), "facts": facts})
    return output
