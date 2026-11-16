"""Freeze the wording-only T3A variant against the original SQL oracle."""

from datetime import datetime, timezone
from hashlib import sha256
import json

from scripts.task27_t3_fixture import DOCS, ROOT, digest, require, write

VARIANT = "t3a"
CASE_CAP = 12
RUN_CAP = 36
CASES = ("branch-1", "swap-1", "gap-1")
BASE = DOCS / "frozen.json"
MANIFEST = ROOT / "docs/task27-t3a/frozen-messages.json"
ORIGINAL_SUFFIX = (
    "请把清单内计划的编号、名称、当前状态和日预算写在清单中。"
    "证据有缺项时请保留可核实的窗口和条文，说明具体缺项并暂缓出清单。"
)
VARIANT_SUFFIX = (
    "先完成两条计划的四份窗口指标和指定版本条文核验，再确定复核清单。"
    "清单确定之后，只读取入选计划的详情，并将编号、名称、当前状态和日预算写入清单。"
    "任一必要窗口或条文缺项时，跳过详情读取，保留可核实的窗口和条文，说明缺项并暂缓出清单。"
)


def manifest_for(frozen, base_path):
    by_id = {case["id"]: case for case in frozen["cases"]}
    rows = []
    for case_id in CASES:
        case = by_id[case_id]
        prefix, suffix, rest = case["message"].rpartition(ORIGINAL_SUFFIX)
        require(prefix and suffix and not rest, "original_message_suffix_mismatch")
        message = prefix + VARIANT_SUFFIX
        rows.append({"id": case_id, "base_message_sha256": case["message_sha256"],
                     "message": message, "message_sha256": sha256(message.encode("utf-8")).hexdigest()})
    return {"task": "TASK-027-T3A", "base_frozen_sha256": digest(base_path), "cases": rows}


def apply_variant(frozen, base_path):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    require({key: value for key, value in manifest.items() if key != "frozen_utc"} ==
            manifest_for(frozen, base_path), "variant_frozen_fixture_mismatch")
    by_id = {case["id"]: case for case in frozen["cases"]}
    cases = [{**by_id[row["id"]], "message": row["message"], "message_sha256": row["message_sha256"]}
             for row in manifest["cases"]]
    return {**frozen, "task": manifest["task"], "frozen_utc": manifest["frozen_utc"],
            "base_frozen_sha256": manifest["base_frozen_sha256"], "cases": cases}


if __name__ == "__main__":
    require(not MANIFEST.exists(), "variant_already_frozen")
    frozen = json.loads(BASE.read_text(encoding="utf-8"))
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    write(MANIFEST, {**manifest_for(frozen, BASE), "frozen_utc": datetime.now(timezone.utc).isoformat()})
