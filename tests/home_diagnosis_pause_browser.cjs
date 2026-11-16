// Real homepage and two FastAPI processes share one temporary sandbox database.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

function serverOrigin(child) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("sandbox server startup timeout")), 90000);
    child.once("error", error => { clearTimeout(timeout); reject(error); });
    child.once("exit", code => { clearTimeout(timeout); reject(new Error(`sandbox server exited ${code}`)); });
    readline.createInterface({ input: child.stdout }).on("line", line => {
      if (line.startsWith("DEMO_URL=")) { clearTimeout(timeout); resolve(line.slice(9)); }
    });
  });
}

async function stopServer(child) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return;
  const exited = once(child, "exit");
  child.kill();
  await exited;
}

async function data(response) {
  assert.equal(response.status(), 200);
  const json = await response.json();
  assert.equal(json.code, 200);
  return json.data;
}

async function run() {
  const output = path.resolve(process.argv[2] || "logs/task18-browser");
  await fs.mkdir(output, { recursive: true });
  const fixture = await fs.mkdtemp(path.join(output, "fixture-"));
  const env = {
    ...process.env, AD_OFFLINE_ACCEPTANCE: "1", PYTHON_DOTENV_DISABLED: "1", ANONYMIZED_TELEMETRY: "False",
    JWT_SECRET: randomBytes(32).toString("hex"), API_KEY: "", DEEPSEEK_API_KEY: "", OPENAI_API_KEY: "",
    DIAGNOSIS_SANDBOX_DB_PATH: "", PLAN_METRICS_DB_PATH: "",
  };
  function startServer() {
    return spawn(process.env.PYTHON || "python", [
      "-B", "-X", "utf8", "-m", "tests.home_diagnosis_server", fixture, "--pause-sandbox",
    ], { windowsHide: true, stdio: ["ignore", "pipe", "inherit"], env });
  }

  let child, browser;
  const report = { kind: "synthetic sandbox; fixed model and embedding", cases: [] };
  try {
    child = startServer();
    const origin = await serverOrigin(child);
    browser = await chromium.launch(browserOptions());
    report.browser = browser.version();
    const page = await browser.newPage({ viewport: { width: 1360, height: 1050 } });
    page.setDefaultTimeout(15000);
    await page.goto(origin);
    await page.waitForFunction(() => window._captchaId);
    await page.locator("#loginUser").fill("diagnosis-demo");
    await page.locator("#loginPass").fill("demo-password");
    await page.locator("#loginCaptcha").fill("0726");
    const login = page.waitForResponse(response => new URL(response.url()).pathname === "/api/login");
    await page.locator("#loginCaptcha").press("Enter");
    const owner = { authorization: "Bearer " + (await data(await login)).token };
    await page.locator("#navUsername").getByText("diagnosis-demo", { exact: true }).waitFor();
    const secondLogin = await page.request.post(origin + "/api/login", {
      data: { username: "second-demo", password: "demo-password" },
    });
    const other = { authorization: "Bearer " + (await data(secondLogin)).token };

    async function plans(base) {
      const results = await Promise.all([owner, other].map(headers =>
        page.request.get(base + "/api/plans", { headers }).then(data)));
      return results.flat().map(({ id, status }) => ({ id, status })).sort((a, b) => a.id - b.id);
    }

    report.current_case = "pending approval shows the target and original evidence while all plans stay active";
    report.before = await plans(origin);
    assert.deepEqual(report.before, [101, 102, 201].map(id => ({ id, status: "active" })));
    await page.locator('[data-page="chat"]').click();
    await page.getByRole("button", { name: "投放诊断", exact: true }).click();
    await page.locator("#diagnosisMode").click();
    const field = name => page.locator(`#diagnosisForm input[name="${name}"]`);
    await field("plan_id").fill("101");
    await field("rule_id").fill("ctr-review-v1#low-ctr");
    for (const suffix of ["start", "end"]) {
      await field("baseline_" + suffix).fill("2026-10-01");
      await field("current_" + suffix).fill("2026-10-02");
    }
    await page.locator("#diagnosisSubmit").click();
    const rounds = page.locator(".diagnosis-round");
    const first = rounds.first();
    const panel = first.locator(".diagnosis-pause");
    const approve = panel.getByRole("button", { name: "批准暂停沙箱计划" });
    await approve.waitFor();
    const original = await panel.locator("[data-diagnosis-snapshot]").textContent();
    const evidence = await panel.locator("[data-diagnosis-evidence]").textContent();
    assert.match(await panel.locator(".diagnosis-pause-target").textContent(), /#101.*预期状态 active → paused/);
    assert.match(original, /2026-10-01.*2026-10-02/);
    assert.match(evidence, /曝光 1000 · 点击 100.*曝光 1000 · 点击 80.*相对降幅 20\.000000%.*ctr-review-v1-low-ctr-01/);
    const reviewPanel = first.locator(".diagnosis-review");
    await reviewPanel.getByRole("button", { name: "确认创建本地复核单" }).click();
    await reviewPanel.getByText("已保存的人工复核单", { exact: true }).waitFor();
    assert.deepEqual(await plans(origin), report.before);

    for (const suffix of ["start", "end"]) await field("current_" + suffix).fill("2026-10-10");
    await page.locator("#diagnosisSubmit").click();
    const second = rounds.nth(1);
    await second.locator('.diagnosis-status[data-state="done"]').waitFor();
    assert.match(await second.locator(".diagnosis-gaps").textContent(), /缺窗\/覆盖不全/);
    assert.equal(await second.locator(".diagnosis-pause").isVisible(), false);
    assert.equal(await panel.locator("[data-diagnosis-snapshot]").textContent(), original);
    await panel.screenshot({ path: path.join(output, "pending-approval.png") });
    report.cases.push(report.current_case);

    report.current_case = "approval updates only one plan and renders a fresh server readback";
    const confirmedResponse = page.waitForResponse(response =>
      /^\/api\/diagnosis\/pause-actions\/[^/]+\/confirm$/.test(new URL(response.url()).pathname));
    const readResponse = page.waitForResponse(response =>
      /^\/api\/diagnosis\/pause-actions\/[^/]+$/.test(new URL(response.url()).pathname));
    await approve.click();
    const receipt = await data(await confirmedResponse);
    const read = await data(await readResponse);
    assert.deepEqual(read, { receipt, actual_status: "paused" });
    await panel.getByText("实际状态 paused", { exact: true }).waitFor();
    assert.equal(await panel.locator("[data-diagnosis-evidence]").textContent(), evidence);
    assert.match(await panel.locator(".diagnosis-pause-receipt").textContent(), new RegExp(receipt.receipt_id));
    assert.ok(original.includes(receipt.source_trace_id));
    report.after = await plans(origin);
    assert.deepEqual(report.after, [{ id: 101, status: "paused" }, ...report.before.slice(1)]);
    await panel.screenshot({ path: path.join(output, "confirmed-readback.png") });
    report.receipt = receipt;
    report.cases.push(report.current_case);

    report.current_case = "a new FastAPI process reads the same persisted receipt and plan state";
    const oldPid = child.pid;
    await stopServer(child);
    child = startServer();
    const restarted = await serverOrigin(child);
    assert.notEqual(child.pid, oldPid);
    const actionUrl = restarted + "/api/diagnosis/pause-actions/" + receipt.action_id;
    const restartedRead = await data(await page.request.get(actionUrl, { headers: owner }));
    assert.deepEqual(restartedRead, read);
    assert.deepEqual(await data(await page.request.post(actionUrl + "/confirm", { headers: owner })), receipt);
    assert.deepEqual(await plans(restarted), report.after);
    report.restart_readback = restartedRead.actual_status;
    report.cases.push(report.current_case);
    report.current_case = null;
    report.passed = true;
    console.log(`${report.cases.length}/3 sandbox approval browser cases passed (Chromium ${report.browser})`);
  } finally {
    report.executed = report.cases.length + Number(Boolean(report.current_case));
    report.failed = Number(Boolean(report.current_case));
    await browser?.close();
    await stopServer(child);
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
