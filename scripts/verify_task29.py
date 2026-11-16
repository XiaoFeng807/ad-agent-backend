"""Replay plan replacement in SQLite work groups with fixed completions or real GLM."""

from scripts.verify_task28 import ChangeCase, DATES, main


if __name__ == "__main__":
    main(ChangeCase(
        request="保留日期和规则，把 #102 换成 #103",
        dates={version: DATES["old"] for version in ("old", "new")},
        bindings={101: "27589150", 102: "26998434", 103: "13289562"},
        targets={"old": 102, "new": 103},
        plan_change=True,
    ), __doc__)
