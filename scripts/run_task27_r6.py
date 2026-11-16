"""Run the frozen R5 browser cases with a separate R6 GLM ledger."""

from scripts.run_task27_r5 import ROOT, main


if __name__ == "__main__":
    raise SystemExit(main(archive=ROOT / "logs/task27-r6", task="TASK-027-R6"))
