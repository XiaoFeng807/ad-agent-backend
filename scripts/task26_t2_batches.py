"""Launch the formal frozen T2 matrix in isolated parallel batches after smoke."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter

from scripts.run_task26_t2 import LIMIT, SMOKE, select_cases
from scripts.task26_t2_replay import OUTPUT, SAMPLE, read_json, sha, write_json


def check_smoke(asgi, browser):
    for path, case_id in zip((browser, asgi), SMOKE):
        raw = read_json(path / case_id / "raw.json")
        assert raw["events"][-1]["type"] == "done"
        assert raw["sdk_requests"] and all(row.get("status") == 200 and row.get("response_model") == "deepseek-flash"
                                          for row in raw["sdk_requests"])
    ui = read_json(browser / SMOKE[0] / "browser.json")
    assert ui["events"][-1]["type"] == "done" and not ui.get("failure")


def run_lane(output, name, command, environment):
    with (output / f"{name}.log").open("wb") as stream:
        code = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                              stdout=stream, stderr=subprocess.STDOUT, check=False).returncode
    return {"batch": name, "exit_code": code}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-asgi", type=Path, required=True)
    parser.add_argument("--smoke-browser", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=OUTPUT / "formal")
    args = parser.parse_args()
    check_smoke(args.smoke_asgi, args.smoke_browser)
    output = args.output.resolve()
    output.mkdir(parents=True)
    env = dict(os.environ, PYTHON=sys.executable)
    env.setdefault("AD_BROWSER_CHANNEL", "chrome")
    python = [sys.executable, "-B", "-X", "utf8", "-m", "scripts.run_task26_t2"]
    browsers = [case["id"] for case in select_cases(read_json(SAMPLE), "formal-browser")]
    lanes = {}
    for index in range(1, 7):
        batch = f"asgi-{index}"
        lanes[batch] = [*python, "asgi", f"asgi-{index}/6", str(output / batch)]
    for index in range(3):
        batch = f"chromium-{index + 1}"
        lanes[batch] = ["node", "tests/task26_t2_browser.cjs", ",".join(browsers[index::3]), str(output / batch)]
    write_json(output / "launch.json", {"lanes": lanes, "sample_sha256": sha(SAMPLE), "sdk_limit": LIMIT})
    started = perf_counter()
    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = [pool.submit(run_lane, output, name, command, env) for name, command in lanes.items()]
        result = [future.result() for future in futures]
    write_json(output / "batches.json", {"batches": result, "elapsed_seconds": round(perf_counter() - started, 3)})
    return int(any(row["exit_code"] for row in result))


if __name__ == "__main__":
    raise SystemExit(main())
