"""Compare grouped case identifiers and pass results against the R2 archives."""

import argparse
from collections import Counter
import json
from pathlib import Path


def cases(report):
    keys = Counter()
    nonpassing = []
    for group_name, group in report["groups"].items():
        if group_name == "diagnosis":
            subgroups = group["groups"]
        else:
            subgroups = {"": group}
        for subgroup_name, subgroup in subgroups.items():
            for case in subgroup["cases"]:
                name = case if isinstance(case, str) else case["name"]
                key = tuple(part for part in (group_name, subgroup_name, name) if part)
                keys[key] += 1
                if isinstance(case, dict) and (
                    case.get("status", "passed") != "passed" or not case.get("executed", True) or case.get("failures")
                ):
                    nonpassing.append(key)
    return keys, nonpassing


def compare(before, after, mode, expected):
    prior, current = (json.loads(path.read_text(encoding="utf-8-sig")) for path in (before, after))
    prior_ids, _ = cases(prior)
    current_ids, nonpassing = cases(current)
    totals = {name: current["baseline_total"][name] + current["new_total"][name]
              for name in ("executed", "passed", "failed", "skipped")}
    report = {
        "before_report": str(before), "after_report": str(after), "expected": expected,
        "previous_identifiers": len(prior_ids), "current_identifiers": len(current_ids),
        "retained": len(prior_ids.keys() & current_ids.keys()),
        "removed": sorted((prior_ids - current_ids).elements()),
        "added": sorted((current_ids - prior_ids).elements()),
        "duplicate_ids": sorted(key for key, count in current_ids.items() if count != 1),
        "nonpassing_cases": nonpassing, "totals": totals,
    }
    report["ok"] = (
        prior["mode"] == current["mode"] == mode and prior["ok"] and current["ok"]
        and totals == {"executed": expected, "passed": expected, "failed": 0, "skipped": 0}
        and len(prior_ids) == len(current_ids) == expected
        and not any(report[name] for name in ("removed", "added", "duplicate_ids", "nonpassing_cases"))
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("core", type=Path)
    parser.add_argument("full", type=Path)
    parser.add_argument("--output", type=Path, default=Path("logs/task26-t1/baseline-id-comparison.json"))
    args = parser.parse_args()
    report = {
        "core": compare(Path("logs/task26-r2/core-final/run-z6o78f2i/report.json"), args.core, "core", 287),
        "full": compare(Path("logs/task26-r2/full-final/run-aacnas4i/report.json"), args.full, "full", 290),
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return int(not all(group["ok"] for group in report.values()))


if __name__ == "__main__":
    raise SystemExit(main())
