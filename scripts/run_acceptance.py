"""Run the core or full offline acceptance suite."""

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import signal
import sqlite3
import subprocess
import sys
from tempfile import mkdtemp

from scripts.acceptance_worker import EXPECTED


ROOT = Path(__file__).resolve().parents[1]
BASELINE = {"diagnosis": 93, "mcp": 5, "provider": EXPECTED["provider"], "entry": 5, "metrics": 21, "gate": 8,
            "memory": EXPECTED["memory"]}
BROWSER_SCRIPTS = {"public_browser": "home_plan_metrics_browser.cjs", "plan_scope_browser": "home_plan_scope_browser.cjs",
                   "business_scope_browser": "home_business_scope_browser.cjs",
                   "diagnosis_pause_browser": "home_diagnosis_pause_browser.cjs",
                   "diagnosis_history_browser": "home_diagnosis_history_browser.cjs"}
COUNT_KEYS = ("executed", "passed", "failed", "skipped")


def offline_environment():
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "NODE_PATH", "AD_PROVIDER_EVIDENCE", "AD_TEAM_TEST_EVIDENCE",
                "PLAN_METRICS_DEMO_ARTIFACT", "AD_EVAL_RESULTS"):
        env.pop(key, None)
    env.update(AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", PYTHONNOUSERSITE="1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False",
               PYTHON=sys.executable, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
               API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", GLM_API_KEY="", JWT_SECRET="",
               LLM_PROVIDER="deepseek", MODEL="deepseek-flash", BASE_URL="https://api.deepseek.com",
               RAG_DB_PATH="", PLAN_METRICS_DB_PATH="", DIAGNOSIS_SANDBOX_DB_PATH="")
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    env.setdefault("AD_BROWSER_CHANNEL", "chromium")
    return env


def verify_artifact(artifact):
    """Reject missing, synthetic, stale or modified public data before any browser run."""
    artifact = Path(artifact).resolve()
    if not artifact.is_file():
        raise ValueError(f"missing public aggregate: {artifact}; see README public-data import steps")
    reference = json.loads((ROOT / "docs/criteo-provenance.json").read_text(encoding="utf-8"))
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != reference["artifact_sha256"]:
        raise ValueError("public aggregate SHA256 mismatch")
    with closing(sqlite3.connect(artifact.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("public aggregate SQLite integrity check failed")
        manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
        for key, value in reference.items():
            if key != "artifact_sha256" and manifest.get(key) != value:
                raise ValueError(f"public aggregate provenance mismatch: {key}")
        rows, campaigns = conn.execute(
            "SELECT SUM(impressions), COUNT(DISTINCT source_plan_id) FROM plan_daily_metrics").fetchone()
        if (rows, campaigns) != (reference["rows"], reference["campaigns"]):
            raise ValueError("public aggregate row/campaign count mismatch")
    return {"path": str(artifact), "sha256": digest, "source_id": manifest["source_id"],
            "data_version": manifest["data_version"], "rows": rows, "campaigns": campaigns, "ok": True}


def run_logged(command, directory, env, timeout=600):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "stdout.log").open("wb") as out, (directory / "stderr.log").open("wb") as err:
        process = None
        try:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=out, stderr=err,
                                       start_new_session=os.name != "nt",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            if process is not None and process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            err.write(f"acceptance subprocess {type(error).__name__}\n".encode())
            return 1


def validate_group(group, expected, exit_code):
    issues = group.setdefault("issues", [])
    if group.get("expected") != expected:
        issues.append("unexpected suite baseline")
    if group.get("executed", 0) == 0:
        issues.append("zero execution")
    if group.get("skipped", 0):
        issues.append("required case skipped")
    if group.get("executed") != expected or group.get("passed") != expected:
        issues.append("required case count/pass mismatch")
    if exit_code != 0:
        issues.append(f"subprocess exit {exit_code}")
    group["exit_code"] = exit_code
    group["ok"] = bool(group.get("ok", True)) and not issues and not group.get("failed", 0)
    return group


def read_group(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"executed": 0, "passed": 0, "failed": 0, "skipped": 0,
                "issues": ["missing or invalid suite report"], "ok": False}


def diagnosis_group(directory, code):
    reports = list(directory.glob("run-*/report.json"))
    raw = read_group(reports[0]) if len(reports) == 1 else read_group(directory / "missing")
    rows = [row for group in raw.get("groups", {}).values() for row in group["cases"]]
    counts = {"executed": sum(row["executed"] for row in rows),
              "passed": sum(not row["failures"] for row in rows),
              "failed": sum(bool(row["failures"]) and row["executed"] for row in rows),
              "skipped": sum(any(detail.startswith("跳过 ") for detail in row["failures"]) for row in rows)}
    group = {"expected": raw.get("expected"), **counts, "ok": raw.get("ok", False),
             "report": str(reports[0]) if len(reports) == 1 else None,
             "groups": raw.get("groups", {}), "issues": raw.get("retrieval_failures", [])}
    for subgroup in group["groups"].values():
        group["issues"].extend(subgroup["issues"])
        subgroup["failed"] = sum(bool(row["failures"]) and row["executed"] for row in subgroup["cases"])
        subgroup["skipped"] = sum(any(d.startswith("跳过 ") for d in row["failures"]) for row in subgroup["cases"])
    return validate_group(group, 93, code)


def browser_group(directory, code, expected):
    raw = read_group(directory / "report.json")
    names = raw.get("cases", [])
    group = {"expected": expected, "executed": raw.get("executed", len(names)), "passed": len(names),
             "failed": raw.get("failed", 0), "skipped": raw.get("skipped", 0),
             "cases": names, "ok": raw.get("passed", False), "issues": raw.get("issues", [])}
    if len(set(names)) != expected:
        group["issues"].append("browser case count/uniqueness mismatch")
    return validate_group(group, expected, code)


def environment_report(env):
    versions = {}
    for package in ("fastapi", "uvicorn", "chromadb", "scikit-learn", "openai", "mcp", "httpx", "pydantic-settings"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "missing"
    locks = {}
    for name in ("pyproject.toml", "uv.lock", "locks/acceptance.txt", "package.json", "package-lock.json"):
        path = ROOT / name
        if path.is_file():
            locks[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        node = json.loads(subprocess.check_output(
            ["node", "-e", "console.log(JSON.stringify({node:process.version,playwright:require('./node_modules/playwright/package.json').version,playwright_path:require.resolve('./node_modules/playwright')}))"],
            cwd=ROOT, env=env, timeout=15, text=True))
    except (OSError, subprocess.SubprocessError, ValueError):
        node = {"status": "missing local Node/Playwright"}
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(), "node": node,
            "isolated_venv": sys.prefix != sys.base_prefix, "packages": versions, "lock_sha256": locks,
            "dotenv_disabled": True, "pythonpath_removed": True, "node_path_removed": True,
            "browser_channel": env["AD_BROWSER_CHANNEL"], "browser_cache": env["PLAYWRIGHT_BROWSERS_PATH"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("core", "full"), default="core")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs/acceptance")
    parser.add_argument("--plan-metrics-db", type=Path, default=ROOT / "data/plan_metrics/criteo.sqlite3")
    parser.add_argument("--inject-wrong-citation", action="store_true")
    parser.add_argument("--inject-fault", choices=("critical-skip", "zero-execution"))
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = Path(mkdtemp(prefix="run-", dir=args.output_dir.resolve()))
    print(f"Evidence: {output}", flush=True)
    env = offline_environment()
    baseline_expected = {**BASELINE, **({"public_browser": 3} if args.mode == "full" else {})}
    baseline_expected.update(budget=EXPECTED["budget"], plan_scope=EXPECTED["plan_scope"], plan_scope_browser=3,
                             business_scope=EXPECTED["business_scope"], business_scope_browser=4,
                             diagnosis_pause=EXPECTED["diagnosis_pause"], diagnosis_pause_browser=3,
                             diagnosis_history=EXPECTED["diagnosis_history"], diagnosis_history_browser=5,
                             sandbox_memory=EXPECTED["sandbox_memory"], natural_diagnosis=EXPECTED["natural_diagnosis"],
                             natural_review_scope=EXPECTED["natural_review_scope"],
                             natural_multi_agent=EXPECTED["natural_multi_agent"], natural_request=EXPECTED["natural_request"],
                             team_contracts=EXPECTED["team_contracts"], team_runtime=EXPECTED["team_runtime"])
    new_expected = {"home_delegation": EXPECTED["home_delegation"]}
    expected = {**baseline_expected, **new_expected}
    report = {"mode": args.mode, "baseline_expected": sum(baseline_expected.values()),
              "new_expected": sum(new_expected.values()), "groups": {}, "issues": [],
              "excluded": [] if args.mode == "full" else ["public_browser: 3 cases require public aggregate"],
              "real_bge": {"executed": 0, "status": "not requested; use eval_review_retrieval --embedding bge"},
              "real_model": {"requests": 0, "rounds": 0, "status": "not requested; use run_real_diagnosis"},
              "environment": environment_report(env)}
    if sys.version_info[:2] != (3, 12):
        report["issues"].append("acceptance requires Python 3.12")
    if args.mode == "full":
        try:
            report["public_data"] = verify_artifact(args.plan_metrics_db)
        except (OSError, ValueError, sqlite3.Error, KeyError) as error:
            report["issues"].append(str(error))
    if not report["issues"]:
        for name, count in expected.items():
            directory = output / name
            py = [sys.executable, "-B", "-X", "utf8", "-m"]
            if name == "diagnosis":
                command = py + ["scripts.eval_diagnosis", "--output-dir", str(directory)]
                if args.inject_wrong_citation:
                    command.append("--inject-wrong-citation")
            elif name in BROWSER_SCRIPTS:
                command = ["node", str(ROOT / "tests" / BROWSER_SCRIPTS[name]), str(directory)]
                if name == "public_browser":
                    env["PLAN_METRICS_DEMO_ARTIFACT"] = str(args.plan_metrics_db.resolve())
            else:
                command = py + ["scripts.acceptance_worker", name, str(directory / "report.json")]
                if name == "provider":
                    env["AD_PROVIDER_EVIDENCE"] = str(directory / "sdk-wire.jsonl")
                if name in {"team_runtime", "home_delegation"}:
                    env["AD_TEAM_TEST_EVIDENCE"] = str(directory)
                if name == "mcp" and args.inject_fault:
                    command += ["--inject-fault", args.inject_fault]
            print(f"Running {name} (expected {count})", flush=True)
            code = run_logged(command, directory, env)
            env.pop("AD_PROVIDER_EVIDENCE", None)
            env.pop("AD_TEAM_TEST_EVIDENCE", None)
            env.pop("PLAN_METRICS_DEMO_ARTIFACT", None)
            if name == "diagnosis":
                group = diagnosis_group(directory, code)
            elif name in BROWSER_SCRIPTS:
                group = browser_group(directory, code, count)
            else:
                group = validate_group(read_group(directory / "report.json"), count, code)
            group["evidence_dir"] = str(directory)
            report["groups"][name] = group
            print(f"{name}: " + ", ".join(f"{key}={group.get(key, 0)}" for key in COUNT_KEYS)
                  + f"; exit={code}", flush=True)
    report["not_run"] = [name for name in expected if name not in report["groups"]]
    for name in report["not_run"]:
        report["groups"][name] = {"expected": expected[name], **dict.fromkeys(COUNT_KEYS, 0),
                                 "ok": False, "issues": ["not run: preflight failed"],
                                 "evidence_dir": str(output / name)}
    baseline, new = Counter(dict.fromkeys(COUNT_KEYS, 0)), Counter(dict.fromkeys(COUNT_KEYS, 0))
    for name, group in report["groups"].items():
        (baseline if name in baseline_expected else new).update({key: group.get(key, 0) for key in COUNT_KEYS})
    report["baseline_total"], report["new_total"] = dict(baseline), dict(new)
    report["ok"] = not report["issues"] and not report["not_run"] and all(g["ok"] for g in report["groups"].values())
    report["exit_code"] = int(not report["ok"])
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Baseline: {dict(baseline)}; new: {dict(new)}; exit={report['exit_code']}", flush=True)
    for issue in report["issues"]:
        print(f"FAIL {issue}")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
