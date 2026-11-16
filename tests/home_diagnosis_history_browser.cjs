// Real homepage, two FastAPI processes, and one temporary sandbox SQLite database.
const assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

const tasksPath = "/api/diagnosis/tasks";
const streamPath = "/api/chat/stream";
const user = "diagnosis-demo";
const otherUser = "second-demo";
const rule = "ctr-review-v1#low-ctr";
const firstDate = "2026-10-01";
const originalDate = "2026-10-02";
const nextDate = "2026-10-10";
const python = process.env.PYTHON || "python";
const pathname = request => new URL(request.url()).pathname;

async function stop(child) {
  if (child?.pid && child.exitCode === null && child.signalCode === null) {
    const exited = once(child, "exit");
    child.kill();
    await exited;
  }
}

function start(fixture, env) {
  const child = spawn(python, [
    "-B", "-X", "utf8", "-m", "tests.home_diagnosis_server", fixture, "--pause-sandbox",
  ], { windowsHide: true, stdio: ["ignore", "pipe", "inherit"], env });
  const origin = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("FastAPI startup timed out")), 90000);
    child.once("error", error => { clearTimeout(timeout); reject(error); });
    child.once("exit", code => { clearTimeout(timeout); reject(new Error(`FastAPI exited ${code}`)); });
    readline.createInterface({ input: child.stdout }).on("line", line => {
      if (line.startsWith("DEMO_URL=")) { clearTimeout(timeout); resolve(line.slice(9)); }
    });
  });
  return { child, origin };
}

async function data(response) {
  assert.equal(response.status(), 200, await response.text());
  const json = await response.json();
  assert.equal(json.code, 200);
  return json.data;
}

async function run() {
  const output = path.resolve(process.argv[2] || "logs/task19-browser");
  await fs.mkdir(output, { recursive: true });
  const fixture = await fs.mkdtemp(path.join(output, "fixture-"));
  const env = {
    ...process.env, AD_OFFLINE_ACCEPTANCE: "1", PYTHON_DOTENV_DISABLED: "1", ANONYMIZED_TELEMETRY: "False",
    JWT_SECRET: randomBytes(32).toString("hex"), API_KEY: "", DEEPSEEK_API_KEY: "", OPENAI_API_KEY: "",
    DIAGNOSIS_SANDBOX_DB_PATH: "", PLAN_METRICS_DB_PATH: "",
  };
  const db = path.join(fixture, "pause.sqlite3");
  function sqlite(script, ...args) {
    return execFileSync(python, ["-B", "-X", "utf8", "-c", script, db, ...args], {
      env, windowsHide: true, encoding: "utf8",
    }).trim();
  }
  function businessHash() {
    return sqlite(`
import hashlib, sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True) as conn:
    data = "\\n".join(row for row in conn.iterdump() if not row.startswith(
        ('INSERT INTO "sessions"', 'INSERT INTO "sqlite_sequence"')))
    print(hashlib.sha256(data.encode()).hexdigest())
`);
  }

  let server, browser;
  const report = { kind: "real FastAPI, temporary SQLite, fixed model and embedding", cases: [] };
  try {
    server = start(fixture, env);
    let origin = await server.origin;
    browser = await chromium.launch(browserOptions());
    report.browser = browser.version();
    const page = await browser.newPage({ viewport: { width: 1360, height: 1350 } });
    page.setDefaultTimeout(20000);
    const traffic = [];
    page.on("request", request => {
      if (pathname(request).startsWith("/api/diagnosis/") || pathname(request) === streamPath) {
        traffic.push([request.method(), pathname(request)]);
      }
    });
    const responseFor = (url, method = "GET") => page.waitForResponse(response =>
      pathname(response) === url && response.request().method() === method);
    const field = name => page.locator(`#diagnosisForm input[name="${name}"]`);
    const taskLabel = page.locator("#diagnosisTask");
    const rounds = page.locator(".diagnosis-round");
    const first = rounds.first();
    const reviewPanel = first.locator(".diagnosis-review");
    const pausePanel = first.locator(".diagnosis-pause");
    const reviewButton = reviewPanel.getByRole("button", { name: "确认创建本地复核单" });
    const pauseButton = pausePanel.getByRole("button", { name: "批准暂停沙箱计划" });
    const setDate = async (name, date) => {
      await field(name + "_start").fill(date);
      await field(name + "_end").fill(date);
    };

    async function login(username) {
      await page.waitForFunction(() => window._captchaId);
      await page.locator("#loginUser").fill(username);
      await page.locator("#loginPass").fill("demo-password");
      await page.locator("#loginCaptcha").fill("0726");
      const loginResponse = responseFor("/api/login", "POST");
      await page.locator("#loginCaptcha").press("Enter");
      const token = (await data(await loginResponse)).token;
      await page.locator("#navUsername").getByText(username, { exact: true }).waitFor();
      return { authorization: "Bearer " + token };
    }

    async function openDiagnosis() {
      await page.locator('[data-page="chat"]').click();
      await page.getByRole("button", { name: "投放诊断", exact: true }).click();
    }

    async function openRestored(taskId) {
      await openDiagnosis();
      await taskLabel.getByText("任务 ID " + taskId, { exact: true }).waitFor();
      await first.locator('.diagnosis-status[data-state="done"]').waitFor();
    }

    async function readOnlySince(from, hash, expected) {
      assert.deepEqual(traffic.slice(from), expected.map(url => ["GET", url]));
      assert.equal(businessHash(), hash);
    }

    report.current_case = "refresh and same-account relogin restore the original round without execution";
    await page.goto(origin);
    let owner = await login(user);
    await openDiagnosis();
    await page.locator("#diagnosisHistoryStatus").getByText("暂无历史诊断", { exact: true }).waitFor();
    await page.locator("#diagnosisMode").click();
    await field("plan_id").fill("101");
    await field("rule_id").fill(rule);
    await setDate("baseline", firstDate);
    await setDate("current", originalDate);
    await page.locator("#diagnosisSubmit").click();
    await pauseButton.waitFor();
    const taskId = (await taskLabel.textContent()).slice("任务 ID ".length);
    const taskUrl = tasksPath + "/" + taskId;
    const original = (await data(await page.request.get(origin + taskUrl, { headers: owner }))).turns[0];
    const sourceTrace = original.snapshot.source_trace_id;
    const originalEvidence = await pausePanel.locator("[data-diagnosis-evidence]").textContent();
    const originalWindows = await first.locator(".diagnosis-windows").textContent();
    const before = businessHash();
    let from = traffic.length;
    await page.reload();
    owner = await login(user);
    await openRestored(taskId);
    assert.equal(await rounds.count(), 1);
    assert.equal(await first.locator(".diagnosis-reply").textContent(), original.reply);
    assert.equal(await first.locator(".diagnosis-trace code").textContent(), sourceTrace);
    assert.equal(await first.locator(".diagnosis-windows").textContent(), originalWindows);
    assert.equal(await pausePanel.locator("[data-diagnosis-evidence]").textContent(), originalEvidence);
    assert.match(await first.locator(".diagnosis-citation summary").textContent(), /v1/);
    assert.equal(await pauseButton.isEnabled(), true);
    await readOnlySince(from, before, [tasksPath, taskUrl]);
    await first.screenshot({ path: path.join(output, "restored-pending.png") });
    report.refresh = { initialRounds: 1, restoredRounds: await rounds.count(), taskId, sourceTrace,
      turnId: original.turn_id, originalWindows, businessHashBefore: before, businessHashAfter: businessHash(),
      requests: traffic.slice(from) };
    report.cases.push(report.current_case);

    report.current_case = "a restarted FastAPI restores the same task and continuation changes only the new window";
    const oldPid = server.child.pid;
    await stop(server.child);
    server = start(fixture, env);
    origin = await server.origin;
    assert.notEqual(server.child.pid, oldPid);
    await page.goto(origin);
    owner = await login(user);
    from = traffic.length;
    await openRestored(taskId);
    await readOnlySince(from, before, [tasksPath, taskUrl]);
    assert.equal(await field("plan_id").evaluate(input => input.readOnly), true);
    assert.equal(await field("rule_id").evaluate(input => input.readOnly), true);
    assert.equal(await field("current_start").inputValue(), originalDate);
    await setDate("current", nextDate);
    const request = page.waitForRequest(req => pathname(req) === streamPath);
    await page.locator("#diagnosisSubmit").click();
    assert.deepEqual((await request).postDataJSON(), { diagnosis: { task_id: taskId,
      baseline: { start_date: firstDate, end_date: firstDate },
      current: { start_date: nextDate, end_date: nextDate } } });
    const second = rounds.nth(1);
    await second.locator('.diagnosis-status[data-state="done"]').waitFor();
    assert.match(await second.locator(".diagnosis-gaps").textContent(), /缺窗\/覆盖不全/);
    assert.equal(await first.locator(".diagnosis-windows").textContent(), originalWindows);
    const continued = await data(await page.request.get(origin + taskUrl, { headers: owner }));
    assert.deepEqual(continued.turns[0], original);
    assert.equal(continued.turns[1].snapshot.current.start_date, nextDate);
    assert.notEqual(continued.turns[1].snapshot.source_trace_id, sourceTrace);
    report.restart = { oldPid, newPid: server.child.pid, taskId, originalTurnId: original.turn_id,
      continuedTurnId: continued.turns[1].turn_id, newTrace: continued.turns[1].snapshot.source_trace_id,
      newCurrent: continued.turns[1].snapshot.current };
    report.cases.push(report.current_case);

    report.current_case = "clicks approve the restored original actions and relogin uses only independent GET readbacks";
    const reviewConfirm = "/api/diagnosis/review-proposals/" + original.review.action_id + "/confirm";
    const pauseRead = "/api/diagnosis/pause-actions/" + original.pause.action_id;
    const confirmReview = responseFor(reviewConfirm, "POST");
    await reviewButton.click();
    const reviewReceipt = await data(await confirmReview);
    const reviewRead = "/api/diagnosis/reviews/" + reviewReceipt.review_id;
    await reviewPanel.getByText("已保存的人工复核单", { exact: true }).waitFor();
    const confirmPause = responseFor(pauseRead + "/confirm", "POST");
    await pauseButton.click();
    const pauseReceipt = await data(await confirmPause);
    await pausePanel.getByText("实际状态 paused", { exact: true }).waitFor();
    for (const receipt of [reviewReceipt, pauseReceipt]) {
      assert.equal(receipt.turn_id, original.turn_id);
      assert.equal(receipt.current.start_date, originalDate);
      assert.equal(receipt.source_trace_id, sourceTrace);
    }
    const approved = businessHash();
    from = traffic.length;
    await page.reload();
    owner = await login(user);
    const rereadReview = responseFor(reviewRead);
    const rereadPause = responseFor(pauseRead);
    await openRestored(taskId);
    assert.deepEqual(await data(await rereadReview), reviewReceipt);
    assert.deepEqual(await data(await rereadPause), { receipt: pauseReceipt, actual_status: "paused" });
    await pausePanel.getByText("实际状态 paused", { exact: true }).waitFor();
    await reviewPanel.getByText("已保存的人工复核单", { exact: true }).waitFor();
    assert.equal(await pausePanel.locator("[data-diagnosis-evidence]").textContent(), originalEvidence);
    await readOnlySince(from, approved, [tasksPath, taskUrl, reviewRead, pauseRead]);
    await first.screenshot({ path: path.join(output, "restored-receipts.png") });
    report.approval = { reviewId: reviewReceipt.review_id, pauseId: pauseReceipt.action_id,
      receiptId: pauseReceipt.receipt_id, executionTrace: pauseReceipt.execution_trace_id, actualStatus: "paused",
      businessHashBefore: approved, businessHashAfter: businessHash(), requests: traffic.slice(from) };

    sqlite(`
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as conn:
    conn.execute("DELETE FROM diagnosis_sandbox_targets WHERE plan_id=101")
`);
    const unregistered = businessHash();
    from = traffic.length;
    await page.reload();
    await login(user);
    await openRestored(taskId);
    await pausePanel.getByText("已批准 · 沙箱暂停", { exact: true }).waitFor();
    const unavailable = "沙箱计划的登记或归属已变化。当前无法读回暂停回执。";
    await pausePanel.getByText(unavailable, { exact: true }).waitFor();
    assert.equal(await pausePanel.locator("button").isVisible(), false);
    await reviewPanel.getByText("已保存的人工复核单", { exact: true }).waitFor();
    await readOnlySince(from, unregistered, [tasksPath, taskUrl, reviewRead]);
    report.approval.unregistered = { status: unavailable, requests: traffic.slice(from) };
    report.cases.push(report.current_case);

    report.current_case = "logout and account switch discard real late task-list and detail responses";
    async function logout() {
      const previousCaptcha = await page.evaluate(() => window._captchaId);
      await page.getByRole("button", { name: /退出登录/ }).click();
      await page.waitForFunction(previous => window._captchaId !== previous, previousCaptcha);
    }
    for (const delayedPath of [tasksPath, taskUrl]) {
      await logout();
      owner = await login(user);
      const ready = Promise.withResolvers();
      const release = Promise.withResolvers();
      const delivered = Promise.withResolvers();
      await page.route(url => url.pathname === delayedPath, async route => {
        try {
          const response = await route.fetch();
          ready.resolve(response.status());
          await release.promise;
          // Chromium may cancel the old request when its AbortSignal is cleared.
          await route.fulfill({ response }).catch(() => {});
        } catch (error) {
          ready.reject(error);
        } finally {
          delivered.resolve();
        }
      }, { times: 1 });
      await openDiagnosis();
      assert.equal(await ready.promise, 200);
      try {
        await logout();
        const other = await login(otherUser);
        await openDiagnosis();
        await page.locator("#diagnosisHistoryStatus").getByText("暂无历史诊断", { exact: true }).waitFor();
        const foreign = await page.request.get(origin + taskUrl, { headers: other });
        const unknown = await page.request.get(origin + tasksPath + "/unknown", { headers: other });
        assert.equal(foreign.status(), 404);
        assert.equal(unknown.status(), 404);
        assert.deepEqual(await foreign.json(), await unknown.json());
        for (const url of [reviewRead, pauseRead]) {
          assert.equal((await page.request.get(origin + url, { headers: other })).status(), 404);
        }
      } finally {
        release.resolve();
        await delivered.promise;
      }
      assert.equal(await rounds.count(), 0);
      assert.equal(await taskLabel.textContent(), "");
      assert.deepEqual(await page.locator("#diagnosisHistory option").allTextContents(), ["选择历史任务"]);
    }
    report.isolation = { delayed: [tasksPath, taskUrl], foreignStatus: 404, otherAccountRounds: await rounds.count() };
    report.cases.push(report.current_case);

    report.current_case = "legacy and uncommitted rounds restore available fields and expose no approval buttons";
    sqlite(`
import json, sqlite3, sys
with sqlite3.connect(sys.argv[1]) as conn:
    task_id = sys.argv[2]
    dates = {"start_date": "2026-10-05", "end_date": "2026-10-05"}
    conn.execute("INSERT INTO diagnosis_turns (user_id, task_id, message, reply) VALUES (17, ?, '旧提问', '旧回复')", (task_id,))
    conn.execute("INSERT INTO diagnosis_turns (user_id, task_id, message, snapshot) VALUES (17, ?, '未提交提问', ?)",
                 (task_id, json.dumps({"baseline": dates, "current": dates, "source_trace_id": "accepted-trace"})))
`, taskId);
    await logout();
    owner = await login(user);
    await openRestored(taskId);
    const legacy = rounds.nth(2);
    const uncommitted = rounds.nth(3);
    assert.match(await legacy.textContent(), /旧提问.*基准 不可恢复.*当前 不可恢复.*Trace ID 不可恢复.*旧回复/s);
    assert.match(await legacy.textContent(), /指标不可恢复.*证据缺项不可恢复.*规则引用不可恢复/s);
    assert.match(await uncommitted.locator(".diagnosis-windows").textContent(), /2026-10-05/);
    assert.equal(await uncommitted.locator(".diagnosis-trace code").textContent(), "accepted-trace");
    assert.equal(await uncommitted.locator(".diagnosis-status").textContent(), "未提交");
    for (const round of [legacy, uncommitted]) {
      assert.equal(await round.locator(".diagnosis-review").isVisible(), false);
      assert.equal(await round.locator(".diagnosis-pause").isVisible(), false);
    }

    const legacyRule = "ctr-review#low-ctr";
    const dateWindow = date => ({ start_date: date, end_date: date });
    const legacyCreation = await page.request.post(origin + streamPath, { headers: owner, data: {
      diagnosis: { plan_id: 101, rule_id: legacyRule,
        baseline: dateWindow(firstDate), current: dateWindow(originalDate) },
    } });
    assert.equal(legacyCreation.status(), 200);
    const legacyDone = (await legacyCreation.text()).split("\n").filter(line => line.startsWith("data: "))
      .map(line => JSON.parse(line.slice(6))).at(-1);
    assert.equal(legacyDone.type, "done");
    const legacyTaskId = legacyDone.task_id;
    await logout();
    owner = await login(user);
    await openRestored(legacyTaskId);
    assert.equal(await field("rule_id").inputValue(), legacyRule);
    assert.equal(await field("rule_id").evaluate(input => input.readOnly), true);
    await setDate("current", nextDate);
    const legacyFollowup = page.waitForRequest(request => pathname(request) === streamPath);
    await page.locator("#diagnosisSubmit").click();
    assert.deepEqual((await legacyFollowup).postDataJSON(), { diagnosis: { task_id: legacyTaskId,
      baseline: dateWindow(firstDate), current: dateWindow(nextDate) } });
    await rounds.nth(1).locator('.diagnosis-status[data-state="done"]').waitFor();
    const legacyTask = await data(await page.request.get(origin + tasksPath + "/" + legacyTaskId, { headers: owner }));
    assert.deepEqual(legacyTask.turns.map(turn => turn.snapshot.current.start_date), [originalDate, nextDate]);
    assert.ok(legacyTask.turns.every(turn => turn.snapshot.rule_id === legacyRule));
    report.legacyContinuation = { rule: legacyRule, sameTaskId: true, committedRounds: legacyTask.turns.length };
    report.cases.push(report.current_case);
    report.current_case = null;
    report.passed = true;
    console.log(`${report.cases.length}/5 diagnosis history browser cases passed (Chromium ${report.browser})`);
  } finally {
    report.executed = report.cases.length + Number(Boolean(report.current_case));
    report.failed = Number(Boolean(report.current_case));
    await browser?.close();
    await stop(server?.child);
    try {
      await fs.copyFile(path.join(fixture, "traces.jsonl"), path.join(output, "traces.jsonl"));
    } catch (error) {
      if (error.code !== "ENOENT" || report.passed) throw error;
    } finally {
      await fs.writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2) + "\n");
      await fs.rm(fixture, { recursive: true, force: true, maxRetries: 3 });
    }
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
