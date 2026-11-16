// Real homepage and SQLite-backed API, with deterministic delivery of late responses.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

const first = "scope-first";
const second = "scope-second";
const password = "scope-demo-password";
const plans = "#plansTableWrap";
const accounts = "#budgetTableWrap";

async function login(page, username, previousCaptcha) {
  await page.waitForFunction(previous => window._captchaId && window._captchaId !== previous, previousCaptcha);
  await page.locator("#loginUser").fill(username);
  await page.locator("#loginPass").fill(password);
  await page.locator("#loginCaptcha").fill("0726");
  await page.locator("#loginCaptcha").press("Enter");
  await page.locator("#navUsername").getByText(username, { exact: true }).waitFor();
}

async function openLists(page, plan, account) {
  await page.locator('[data-page="plans"]').click();
  await page.locator(plans).getByText(plan, { exact: true }).waitFor();
  await page.locator('[data-page="budget"]').click();
  await page.locator(accounts).getByText(account, { exact: true }).waitFor();
}

async function listRows(page) {
  return { plans: await page.locator(`${plans} tbody tr`).allTextContents(),
    accounts: await page.locator(`${accounts} tbody tr`).allTextContents() };
}

async function planActions(page, dialogs, output) {
  await page.locator('[data-page="plans"]').click();
  await page.locator(plans).getByText("计划甲", { exact: true }).waitFor();
  await page.getByRole("button", { name: "+ 新建计划" }).click();
  await page.locator("#planName").fill("浏览器自有计划");
  await page.locator("#planModal").getByRole("button", { name: "创建", exact: true }).click();
  const row = page.locator(`${plans} tbody tr`).filter({ hasText: "浏览器自有计划" });
  await row.getByText("投放中", { exact: true }).waitFor();
  await row.getByRole("button", { name: "暂停", exact: true }).click();
  await row.getByText("已暂停", { exact: true }).waitFor();
  await row.screenshot({ path: path.join(output, "owned-plan-paused.png") });
  await row.getByRole("button", { name: "启用", exact: true }).click();
  await row.getByText("投放中", { exact: true }).waitFor();
  await row.getByRole("button", { name: "删除", exact: true }).click();
  await row.waitFor({ state: "detached" });
  await page.evaluate(() => togglePlan(502));
  await page.evaluate(() => deletePlan(999999));
  assert.deepEqual(dialogs, ["创建成功", "计划已暂停", "计划已启用", "已删除", "计划不存在", "计划不存在"]);
  const rows = (await listRows(page)).plans;
  assert.equal(rows.length, 1);
  assert.match(rows[0], /计划甲/);
  return { dialogs: [...dialogs], rows };
}

async function sessionSwitch(page, dialogs, output) {
  await openLists(page, "计划甲", "账户甲");
  const firstRows = await listRows(page);
  const readDraft = () => page.evaluate(() => ({
    name: document.getElementById("planName").value,
    platform: document.getElementById("planPlatform").value,
    budget: document.getElementById("planBudget").value,
  }));
  await page.evaluate(() => showModal("planModal"));
  await page.locator("#planName").fill("甲的计划草稿");
  await page.locator("#planPlatform").selectOption("Meta Ads");
  await page.locator("#planBudget").fill("9746");
  const firstDraft = await readDraft();
  assert.deepEqual(firstDraft, { name: "甲的计划草稿", platform: "Meta Ads", budget: "9746" });
  await page.evaluate(() => closeModal("planModal"));
  const previousCaptcha = await page.evaluate(() => window._captchaId);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let hold = true;
  const held = {};
  const captured = Promise.withResolvers();
  const matcher = /\/api\/(plans|accounts|plans\/501\/toggle)$/;
  await page.route(matcher, async route => {
    if (!hold) return route.continue();
    const url = new URL(route.request().url());
    const response = await route.fetch();
    const body = await response.json();
    held[url.pathname] = { status: response.status(), data: body.data };
    if (Object.keys(held).length === 3) captured.resolve();
    await gate;
    await route.fulfill({ response });
  });
  let timer;
  try {
    await page.evaluate(() => { window.oldRequests = Promise.all([loadPlans(), loadBudget(), togglePlan(501)]); });
    await Promise.race([captured.promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("old responses were not captured")), 10000);
    })]);
    clearTimeout(timer);
    assert.deepEqual(Object.keys(held).sort(), ["/api/accounts", "/api/plans", "/api/plans/501/toggle"]);
    assert.deepEqual(held["/api/plans"].data.map(row => row.user_id), [17]);
    assert.deepEqual(held["/api/accounts"].data.map(row => row.user_id), [17]);
    assert.equal(held["/api/plans/501/toggle"].status, 200);
    hold = false;
    await page.locator(".logout-btn").click();
    const cleared = await listRows(page);
    assert.deepEqual(cleared, { plans: [], accounts: [] });
    assert.deepEqual(await page.locator(`${plans}, ${accounts}`).allTextContents(), ["", ""]);
    await login(page, second, previousCaptcha);
    assert.deepEqual(await listRows(page), cleared);
    await page.evaluate(() => showModal("planModal"));
    const secondDraft = await readDraft();
    assert.deepEqual(secondDraft, { name: "", platform: "Google Ads", budget: "300" });
    await page.evaluate(() => closeModal("planModal"));
    await openLists(page, "计划乙", "账户乙");
    const secondRows = await listRows(page);
    assert.equal(secondRows.plans.length, 2);
    assert.equal(secondRows.accounts.length, 1);
    const refreshes = [];
    page.on("request", request => { if (matcher.test(request.url())) refreshes.push(request.method()); });
    release();
    await page.evaluate(() => window.oldRequests);
    assert.deepEqual(await listRows(page), secondRows);
    assert.deepEqual(dialogs, []);
    assert.deepEqual(refreshes, []);
    await page.locator(accounts).screenshot({ path: path.join(output, "second-account-after-late-response.png") });
    // Reveal the already-rendered plan table without making a fresh request.
    await page.evaluate(() => {
      document.querySelector("#page-budget").classList.remove("active");
      document.querySelector("#page-plans").classList.add("active");
    });
    await page.locator(plans).screenshot({ path: path.join(output, "second-plans-after-late-response.png") });
    return { firstRows, cleared, firstDraft, secondDraft, secondRows, afterRelease: await listRows(page),
      delayedResponses: Object.fromEntries(Object.entries(held).map(([url, value]) => [url, value.status])),
      staleDialogs: dialogs.length, staleRefreshes: refreshes.length };
  } finally {
    clearTimeout(timer);
    release();
  }
}

async function revokedSession(page, dialogs, output) {
  await openLists(page, "计划甲", "账户甲");
  const response = await page.request.post(new URL("/api/login", page.url()).href,
    { data: { username: first, password } });
  assert.equal((await response.json()).code, 200);
  const revoked = page.waitForResponse(r => new URL(r.url()).pathname === "/api/accounts");
  await page.evaluate(() => loadBudget());
  assert.equal((await revoked).status(), 401);
  const message = page.locator("#loginErr").getByText("登录已失效，请重新登录", { exact: true });
  await message.waitFor();
  assert.deepEqual(await listRows(page), { plans: [], accounts: [] });
  assert.deepEqual(dialogs, []);
  await message.screenshot({ path: path.join(output, "revoked-session.png") });
  return { status: 401, loginMessage: await message.textContent(), cleared: await listRows(page) };
}

async function run() {
  const output = path.resolve(process.argv[2] || "logs/task16-browser");
  await fs.mkdir(output, { recursive: true });
  const directory = await fs.mkdtemp(path.join(output, "demo-"));
  const child = spawn(process.env.PYTHON || "python", ["-B", "-X", "utf8", "-m", "tests.home_plan_scope_server", directory], {
    windowsHide: true, stdio: ["ignore", "pipe", "inherit"],
    env: { ...process.env, AD_OFFLINE_ACCEPTANCE: "1", PYTHON_DOTENV_DISABLED: "1", LLM_PROVIDER: "mock",
      API_KEY: "", DEEPSEEK_API_KEY: "", OPENAI_API_KEY: "", JWT_SECRET: randomBytes(32).toString("hex") },
  });
  const report = { cases: [], evidence: {}, executed: 0, failed: 0, skipped: 0, passed: false };
  let browser;
  try {
    const origin = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("server startup timeout")), 60000);
      readline.createInterface({ input: child.stdout }).on("line", line => {
        if (line.startsWith("DEMO_URL=")) { clearTimeout(timer); resolve(line.slice(9)); }
      });
      child.once("exit", code => { clearTimeout(timer); reject(new Error(`server exit ${code}`)); });
      child.once("error", error => { clearTimeout(timer); reject(error); });
    });
    browser = await chromium.launch(browserOptions());
    report.browser = browser.version();
    for (const [name, test] of [["plan_actions", planActions], ["session_switch", sessionSwitch],
      ["revoked_session", revokedSession]]) {
      report.executed++;
      const page = await browser.newPage();
      page.setDefaultTimeout(10000);
      const dialogs = [];
      page.on("dialog", async dialog => {
        if (dialog.type() === "alert") dialogs.push(dialog.message());
        await dialog.accept();
      });
      try {
        await page.goto(origin);
        await login(page, first);
        report.evidence[name] = await test(page, dialogs, output);
        report.cases.push(name);
      } finally {
        await page.close();
      }
    }
    report.passed = true;
  } catch (error) {
    report.failed = 1;
    report.issues = [String(error)];
    console.error(error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    child.kill();
    await fs.writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2) + "\n");
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
