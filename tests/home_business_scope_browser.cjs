// Real homepage, loopback FastAPI and SQLite. Delay delivery after the server has answered.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

const first = "business-first";
const second = "business-second";
const admin = "business-admin";
const password = "business-demo-password";
const business = ["/api/daily_reports", "/api/timeline", "/api/materials", "/api/decisions", "/api/alerts", "/api/logs"];
const loaders = ["loadTrend", "loadTimeline", "loadMaterials", "loadDecisions", "loadAlerts", "loadLogs"];
const privateViews = ["timeline", "materials", "decisions", "alerts", "logs"];
const expiredMessage = "登录已失效，请重新登录";
const empty = { costs: [], timeline: "", materials: "", decisions: "", directory: "", summary: "", alerts: "", logs: "" };

async function login(page, username, previousCaptcha) {
  await page.waitForFunction(previous => window._captchaId && window._captchaId !== previous, previousCaptcha);
  await page.locator("#loginUser").fill(username);
  await page.locator("#loginPass").fill(password);
  await page.locator("#loginCaptcha").fill("0726");
  await page.locator("#loginCaptcha").press("Enter");
  await page.locator("#navUsername").getByText(username, { exact: true }).waitFor();
  await page.locator('[data-page="dashboard"]').click();
  await page.waitForFunction(() => window.echarts?.getInstanceByDom(document.querySelector("#trendChart"))
    ?.getOption()?.series?.length > 0);
}

async function logout(page) {
  const captcha = await page.evaluate(() => window._captchaId);
  await page.locator(".logout-btn").click();
  await page.locator("#loginPage").waitFor();
  return captcha;
}

async function snapshot(page) {
  return page.evaluate(() => {
    const text = id => document.getElementById(id).textContent;
    const chart = window.echarts?.getInstanceByDom(document.querySelector("#trendChart"));
    return { costs: chart?.getOption()?.series?.[0]?.data || [], timeline: text("timelineWrap"),
      materials: text("materialsWrap"), decisions: text("decisionsWrap"), directory: text("usersTableWrap"),
      summary: text("dashStats"), alerts: text("alertsWrap"), logs: text("logsWrap") };
  });
}

async function loginState(page) {
  return page.evaluate(() => ({ username: USER?.username ?? null, role: USER?.role ?? null,
    authenticated: Boolean(TOKEN), loginVisible: getComputedStyle(byId("loginPage")).display !== "none",
    messageVisible: getComputedStyle(byId("loginErr")).display !== "none", message: byId("loginErr").textContent }));
}

// Track the real api() promises so assertions run after fetch and JSON handling complete.
async function startLoads(page, names, key) {
  await page.evaluate(({ names, key }) => {
    const original = api;
    const calls = [];
    api = (...args) => {
      const result = original(...args);
      calls.push(result);
      return result;
    };
    try { names.forEach(name => window[name]()); }
    finally { api = original; }
    window[key] = Promise.all(calls);
  }, { names, key });
}

async function loadViews(page) {
  await startLoads(page, loaders, "currentReads");
  await page.evaluate(() => window.currentReads);
}

async function holdReads(page, paths) {
  const release = Promise.withResolvers();
  const pending = new Map(paths.map(p => [p, Promise.withResolvers()]));
  const statuses = {};
  await page.route(url => paths.includes(url.pathname), async route => {
    const pathname = new URL(route.request().url()).pathname;
    const entry = pending.get(pathname);
    if (!entry) return route.fallback();
    pending.delete(pathname);
    try {
      const response = await route.fetch();
      statuses[pathname] = response.status();
      entry.resolve();
      await release.promise;
      await route.fulfill({ response });
    } catch (error) {
      entry.reject(error);
      await route.abort().catch(() => {});
    }
  });
  const all = Promise.all([...pending.values()].map(entry => entry.promise));
  return {
    statuses,
    release: release.resolve,
    async captured() {
      let timer;
      try {
        await Promise.race([all, new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(`held responses timed out: ${paths}`)), 10000);
        })]);
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

async function rendersOwnedDecisions(page, evidence, output) {
  await login(page, first);
  const received = page.waitForResponse(r => new URL(r.url()).pathname === "/api/decisions");
  await page.locator('[data-page="decisions"]').click();
  const response = await received;
  const decisionView = page.locator("#decisionsWrap");
  await decisionView.locator("table, .empty-state").waitFor();
  evidence.api = { status: response.status(), body: await response.json() };
  evidence.rendered = await decisionView.locator("tbody tr").allTextContents();
  evidence.view = await decisionView.textContent();
  await decisionView.screenshot({ path: path.join(output, "owned-decisions.png") });
  assert.deepEqual(evidence.rendered.map(row => row.match(/first-\w+/)?.[0]),
    ["first-verified", "first-executed", "first-pending"]);
  assert.match(evidence.rendered[0], /已验证/);
  assert.match(evidence.rendered[1], /已执行/);
  assert.match(evidence.rendered[2], /待执行/);
}

async function lateBusinessResponses(page, evidence) {
  await login(page, first);
  await loadViews(page);
  evidence.first = await snapshot(page);
  const old = await holdReads(page, business);
  const oldMark = await holdReads(page, ["/api/alerts/read"]);
  let unauthorized;
  try {
    await startLoads(page, loaders, "oldReads");
    await old.captured();
    await startLoads(page, ["markAllRead"], "oldMarkRead");
    await oldMark.captured();
    const renewed = await page.request.post("/api/login", { data: { username: first, password } });
    assert.equal((await renewed.json()).code, 200);
    unauthorized = await holdReads(page, ["/api/timeline"]);
    await startLoads(page, ["loadTimeline"], "oldUnauthorized");
    await unauthorized.captured();

    const captcha = await logout(page);
    evidence.afterLogout = await snapshot(page);
    await login(page, second, captcha);
    evidence.beforeLoading = await snapshot(page);
    await loadViews(page);
    evidence.second = await snapshot(page);
    evidence.loginBeforeRelease = await loginState(page);
    await page.evaluate(() => {
      const original = loadAlerts;
      window.staleAlertRefreshes = 0;
      loadAlerts = () => {
        window.staleAlertRefreshes++;
        return original();
      };
    });
    old.release();
    oldMark.release();
    unauthorized.release();
    await page.evaluate(() => Promise.all([window.oldReads, window.oldUnauthorized, window.oldMarkRead]));
    evidence.afterRelease = await snapshot(page);
    evidence.loginAfterRelease = await loginState(page);
    evidence.delayedStatuses = { success: old.statuses, unauthorized: unauthorized.statuses, mark: oldMark.statuses };
    evidence.staleAlertRefreshes = await page.evaluate(() => window.staleAlertRefreshes);

    assert.deepEqual(evidence.delayedStatuses, {
      success: Object.fromEntries(business.map(route => [route, 200])),
      unauthorized: { "/api/timeline": 401 },
      mark: { "/api/alerts/read": 200 },
    });
    assert.deepEqual(evidence.afterLogout, empty);
    for (const view of [...privateViews, "directory"]) {
      assert.equal(evidence.beforeLoading[view], "");
    }
    assert.deepEqual(evidence.first.costs, [3, 120]);
    assert.deepEqual(evidence.second.costs, [60, 18000]);
    for (const view of privateViews) {
      assert.match(evidence.first[view], /first-/);
      assert.doesNotMatch(evidence.first[view], /second-|orphan-(plan|material|decision)/);
      assert.match(evidence.second[view], /second-/);
      assert.doesNotMatch(evidence.second[view], /first-|orphan-/);
    }
    assert.deepEqual(evidence.afterRelease, evidence.second);
    assert.equal(evidence.staleAlertRefreshes, 0);
    assert.deepEqual(evidence.loginAfterRelease, evidence.loginBeforeRelease);
    assert.deepEqual(evidence.loginAfterRelease, { username: second, role: "user", authenticated: true,
      loginVisible: false, messageVisible: false, message: "" });

    await page.locator('[data-page="alerts"]').click();
    const alerts = page.locator("#alertsWrap");
    const one = alerts.locator("tbody tr").filter({ hasText: "second-alert-one" });
    const two = alerts.locator("tbody tr").filter({ hasText: "second-alert-two" });
    const waitForMark = () => page.waitForResponse(r => new URL(r.url()).pathname === "/api/alerts/read");
    const single = waitForMark();
    await one.getByRole("button", { name: "标记已读" }).click();
    evidence.ownedSingleMark = (await single).status();
    await one.getByText("已读", { exact: true }).waitFor();
    assert.equal(await two.getByRole("button", { name: "标记已读" }).count(), 1);
    const all = waitForMark();
    await page.locator("#page-alerts").getByRole("button", { name: "全部已读" }).click();
    evidence.ownedAllMark = (await all).status();
    await two.getByText("已读", { exact: true }).waitFor();
    assert.equal(await alerts.getByRole("button", { name: "标记已读" }).count(), 0);
    assert.deepEqual([evidence.ownedSingleMark, evidence.ownedAllMark], [200, 200]);
  } finally {
    old.release();
    oldMark.release();
    unauthorized?.release();
  }
}

async function lateDirectoryResponse(page, evidence) {
  await login(page, admin);
  await page.locator("#navUsers").click();
  await page.locator("#usersTableWrap").getByText(first, { exact: true }).waitFor();
  evidence.firstDirectory = (await snapshot(page)).directory;
  const old = await holdReads(page, ["/api/users"]);
  try {
    await startLoads(page, ["loadUsers"], "oldDirectory");
    await old.captured();
    const captcha = await logout(page);
    evidence.afterLogout = (await snapshot(page)).directory;
    await login(page, second, captcha);
    evidence.beforeRelease = (await snapshot(page)).directory;
    old.release();
    await page.evaluate(() => window.oldDirectory);
    evidence.afterRelease = (await snapshot(page)).directory;
    evidence.delayedStatus = old.statuses["/api/users"];
    evidence.directoryNavHidden = await page.locator("#navUsers").isHidden();
    evidence.login = await loginState(page);
    assert.equal(evidence.delayedStatus, 200);
    assert.deepEqual([evidence.afterLogout, evidence.beforeRelease, evidence.afterRelease], ["", "", ""]);
    assert.equal(evidence.directoryNavHidden, true);
    assert.deepEqual(evidence.login, { username: second, role: "user", authenticated: true,
      loginVisible: false, messageVisible: false, message: "" });
  } finally {
    old.release();
  }
}

async function current401ClearsViews(page, evidence) {
  await login(page, first);
  await loadViews(page);
  const renewed = await page.request.post("/api/login", { data: { username: first, password } });
  assert.equal((await renewed.json()).code, 200);
  const received = page.waitForResponse(r => new URL(r.url()).pathname === "/api/materials");
  await startLoads(page, ["loadMaterials"], "revokedRead");
  evidence.status = (await received).status();
  await page.evaluate(() => window.revokedRead);
  evidence.cleared = await snapshot(page);
  evidence.login = await loginState(page);
  assert.equal(evidence.status, 401);
  assert.deepEqual(evidence.cleared, empty);
  assert.deepEqual(evidence.login, { username: null, role: null, authenticated: false,
    loginVisible: true, messageVisible: true, message: expiredMessage });
}

async function run() {
  const output = path.resolve(process.argv[2] || "logs/task17-browser");
  await fs.mkdir(output, { recursive: true });
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "ad-task17-"));
  const child = spawn(process.env.PYTHON || "python", ["-B", "-X", "utf8", "-m", "tests.home_business_scope_server", directory], {
    windowsHide: true, stdio: ["ignore", "pipe", "inherit"],
    env: { ...process.env, AD_OFFLINE_ACCEPTANCE: "1", PYTHON_DOTENV_DISABLED: "1", LLM_PROVIDER: "mock",
      API_KEY: "", DEEPSEEK_API_KEY: "", OPENAI_API_KEY: "", JWT_SECRET: randomBytes(32).toString("hex") },
  });
  const stopped = new Promise(resolve => child.once("close", resolve));
  const report = { cases: [], evidence: {}, executed: 0, failed: 0, skipped: 0, issues: [], passed: false };
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
    for (const test of [rendersOwnedDecisions, lateBusinessResponses, lateDirectoryResponse, current401ClearsViews]) {
      report.executed++;
      const page = await browser.newPage({ baseURL: origin });
      page.setDefaultTimeout(10000);
      const evidence = report.evidence[test.name] = { dialogs: [], pageErrors: [] };
      page.on("dialog", async dialog => {
        evidence.dialogs.push(dialog.message());
        await dialog.dismiss();
      });
      page.on("pageerror", error => evidence.pageErrors.push(error.message));
      try {
        await page.goto("/");
        await test(page, evidence, output);
        assert.deepEqual(evidence.dialogs, []);
        assert.deepEqual(evidence.pageErrors, []);
        report.cases.push(test.name);
      } catch (error) {
        report.failed++;
        report.issues.push(`${test.name}: ${error.message}`);
      } finally {
        await page.close();
      }
    }
    report.passed = report.failed === 0;
  } catch (error) {
    report.failed++;
    report.issues.push(String(error));
  } finally {
    if (browser) await browser.close();
    child.kill();
    await stopped;
    await fs.rm(directory, { recursive: true, force: true });
    await fs.writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2) + "\n");
  }
  if (!report.passed) {
    console.error(report.issues.join("\n"));
    process.exitCode = 1;
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
