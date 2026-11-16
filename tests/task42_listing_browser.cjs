const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");

const [base, output] = process.argv.slice(2);
const sku = process.env.AD_TASK42_SKU || "TASK42-BOTTLE";
const message = process.env.AD_TASK42_MESSAGE ||
  `看一下 SKU ${sku} 最近两周卖得怎么样，结合商品资料帮我重写英文五点。先给我看改动，我确认后再应用。`;
const username = process.env.AD_TASK42_USERNAME || "boss";
const password = process.env.AD_TASK42_PASSWORD;
const captcha = process.env.AD_TASK42_CAPTCHA || "0726";
const groups = "/api/chat/conversations";

function readProduct(database, target, actionId) {
  const key = [target.shop_id, target.marketplace_id, target.sku];
  const row = database.prepare(`SELECT * FROM listing_test_products
    WHERE shop_id=? AND marketplace_id=? AND sku=?`).get(...key);
  const otherProducts = database.prepare(`SELECT * FROM listing_test_products
    WHERE NOT (shop_id=? AND marketplace_id=? AND sku=?) ORDER BY shop_id, marketplace_id, sku`).all(...key);
  const receipts = database.prepare("SELECT * FROM listing_change_receipts WHERE action_id=?").all(actionId);
  return { row, otherProducts, receipts };
}

async function checkChart(page, metrics) {
  assert.deepEqual([metrics.baseline.units, metrics.current.units], [70, 49]);
  assert.equal(metrics.units_delta, -21);
  assert.equal(metrics.units_change_pct, -30);
  assert.equal(metrics.conversion_rate, null);
  const chart = page.locator(".group-listing-chart");
  await expect(chart).toBeVisible();
  await expect(chart).toContainText("转化率 未知");
  const dates = await chart.locator("[data-date]").evaluateAll(points => points.map(point => ({
    date: point.dataset.date, units: point.dataset.units === "unknown" ? null : Number(point.dataset.units),
  })));
  assert.deepEqual(dates, [...metrics.baseline.days, ...metrics.current.days]);
}

async function browserRun() {
  assert.ok(base && output && password, "Base URL, output directory and AD_TASK42_PASSWORD are required");
  await fs.mkdir(output, { recursive: true });
  const report = { passed: false, message };
  const browser = await chromium.launch(browserOptions());
  const database = process.env.AD_TASK42_SQLITE ? new DatabaseSync(process.env.AD_TASK42_SQLITE, { readOnly: true }) : null;
  try {
    report.browser = browser.version();
    const page = await browser.newPage({ baseURL: base, viewport: { width: 1360, height: 1050 }, timezoneId: "Asia/Shanghai" });
    page.setDefaultTimeout(25000);
    const pageErrors = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.task42Streams = [];
      window.fetch = async (url, options) => {
        const response = await fetch(url, options);
        if (url === "/api/chat/stream") {
          const stream = { status: response.status };
          window.task42Streams.push(stream);
          response.clone().text().then(text => {
            stream.events = text.split(/\r?\n/).filter(line => line.startsWith("data: "))
              .map(line => JSON.parse(line.slice(6)));
          }).catch(error => { stream.error = error.message; });
        }
        return response;
      };
    });
    const exactPath = (response, requested, method) => new URL(response.url()).pathname === requested &&
      response.request().method() === method;
    await page.goto(base);
    await page.locator("#loginUser").fill(username);
    await page.locator("#loginPass").fill(password);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill(captcha);
    const loggedIn = page.waitForResponse(response => exactPath(response, "/api/login", "POST"));
    await page.locator("#loginPass").press("Enter");
    const login = await (await loggedIn).json();
    assert.equal(login.code, 200);
    await page.locator('[data-page="chat"]').click();
    const headers = { Authorization: "Bearer " + login.data.token };
    const groupCreated = page.waitForResponse(response => exactPath(response, groups, "POST"));
    await submitGroupCreation(page, "TASK-042 商品运营");
    const conversation = (await (await groupCreated).json()).data.conversation_id;
    const mode = page.locator("#chatGroupListing");
    await expect(mode).toBeEnabled();
    const natural = process.env.AD_TASK42_NATURAL === "1";
    if (!natural) {
      await mode.click();
      await expect(mode).toHaveAttribute("aria-pressed", "true");
    }
    await page.locator("#chatInput").fill(message);
    const streamResponse = page.waitForResponse(response => exactPath(response, "/api/chat/stream", "POST"));
    await page.locator("#chatSend").click();
    const stream = await streamResponse;
    assert.equal(stream.status(), 200);
    report.request = stream.request().postDataJSON();
    assert.deepEqual(report.request, { message, conversation_id: conversation, ...(natural ? {} : { listing_operations: true }) });
    await page.waitForFunction(() => window.task42Streams[0]?.events || window.task42Streams[0]?.error);
    const captured = await page.evaluate(() => window.task42Streams[0]);
    assert.equal(captured.error, undefined);
    report.events = captured.events;
    const terminals = report.events.filter(event => ["done", "error", "stopped", "superseded"].includes(event.type));
    assert.deepEqual(terminals.map(event => event.type), ["done"]);
    const run = terminals[0].run_id;
    assert.equal(terminals[0].conversation_id, conversation);
    const evidencePath = `${groups}/${conversation}/runs/${run}/evidence`;
    const evidenceResponse = await page.request.get(evidencePath, { headers });
    assert.equal(evidenceResponse.status(), 200);
    const evidence = (await evidenceResponse.json()).data;
    report.evidence = evidence;
    assert.equal(evidence.kind, "listing_operations_v1");
    assert.equal(evidence.target.sku, sku);
    assert.equal(evidence.proposal.bullets.length, 5);
    const updated = evidence.proposal.bullets.map(bullet => bullet.text);
    const initial = evidence.listing.bullets;
    assert.notDeepEqual(updated, initial);
    const facts = new Map(evidence.facts.facts.map(fact => [fact.fact_id, fact]));
    assert.equal(new Set(evidence.proposal.bullets.map(bullet => bullet.fact_id)).size, 5);
    for (const bullet of evidence.proposal.bullets) assert.ok(facts.get(bullet.fact_id).options.includes(bullet.text));
    const answer = page.locator(`.msg[data-entry-id="${evidence.entry_id}"]`);
    const salesCard = answer.locator('[data-result-kind="sales"]');
    const changeCard = answer.locator('[data-result-kind="listing"]');
    const detail = page.locator("#workbenchDetail");
    await expect(detail).not.toBeVisible();
    await expect(salesCard).toContainText("两窗合计 119 件 · 变化 -21 件（-30%）");
    await salesCard.click();
    await checkChart(page, evidence.metrics);
    await page.locator(".group-listing-chart").screenshot({ path: path.join(output, "listing-chart.png") });
    await page.keyboard.press("Escape");
    await expect(salesCard).toBeFocused();
    await changeCard.click();
    const visible = detail.locator(".group-listing-action");
    await expect(visible).toHaveCount(1);
    await expect(visible).toBeVisible();
    await expect(page.locator(".group-pause")).toHaveCount(0);
    await expect(page.locator('[data-bullet-version="before"] > li')).toHaveText(initial);
    assert.deepEqual(await page.locator('[data-bullet-version="proposed"] > li').evaluateAll(items =>
      items.map(item => item.firstChild.textContent)), updated);
    assert.deepEqual(await page.locator('[data-bullet-version="proposed"] [data-fact-id]').evaluateAll(items =>
      items.map(item => item.dataset.factId)), evidence.proposal.bullets.map(bullet => bullet.fact_id));
    const actionId = await visible.getAttribute("data-action-id");
    assert.ok(actionId);
    const actionPath = `${groups}/${conversation}/runs/${run}/listing-actions/${actionId}`;
    const pendingResponse = await page.request.get(actionPath, { headers });
    assert.equal(pendingResponse.status(), 200);
    const pending = (await pendingResponse.json()).data;
    report.before = pending;
    assert.equal(pending.state, "pending");
    assert.equal(pending.receipt, null);
    assert.deepEqual(pending.listing, evidence.listing);
    assert.deepEqual(pending.proposal, evidence.proposal);
    if (database) {
      report.sqlBefore = readProduct(database, evidence.target, actionId);
      assert.equal(report.sqlBefore.row.version, evidence.listing.version);
      assert.deepEqual(JSON.parse(report.sqlBefore.row.bullets), initial);
      assert.deepEqual(report.sqlBefore.receipts, []);
    }
    await page.locator(".group-listing-diff").screenshot({ path: path.join(output, "listing-proposal-diff.png") });
    await visible.screenshot({ path: path.join(output, "listing-pending.png") });
    if (process.env.AD_TASK42_PENDING_ONLY === "1") {
      assert.ok(database, "Independent SQLite verification requires AD_TASK42_SQLITE");
      await page.reload();
      const [refreshedSales] = await Promise.all([
        page.waitForResponse(response => exactPath(response, evidencePath, "GET")), salesCard.click(),
      ]);
      assert.equal(refreshedSales.status(), 200);
      report.refreshEvidence = (await refreshedSales.json()).data;
      assert.deepEqual(report.refreshEvidence, evidence);
      await checkChart(page, report.refreshEvidence.metrics);
      await page.keyboard.press("Escape");
      const [refreshedAction] = await Promise.all([
        page.waitForResponse(response => exactPath(response, actionPath, "GET")), changeCard.click(),
      ]);
      assert.equal(refreshedAction.status(), 200);
      report.refresh = (await refreshedAction.json()).data;
      assert.deepEqual(report.refresh, pending);
      await expect(visible).toContainText("待审批");
      report.sqlAfter = readProduct(database, evidence.target, actionId);
      assert.deepEqual(report.sqlAfter, report.sqlBefore);
      await visible.screenshot({ path: path.join(output, "listing-refresh.png") });
      assert.deepEqual(pageErrors, []);
      report.passed = true;
      return;
    }
    const sequence = [];
    page.on("request", request => {
      const requestPath = new URL(request.url()).pathname;
      if (requestPath === actionPath || requestPath === actionPath + "/confirm") sequence.push(request.method());
    });
    report.confirmation = await detail.innerText();
    const approvedResponse = page.waitForResponse(response => exactPath(response, actionPath + "/confirm", "POST"));
    const readResponse = page.waitForResponse(response => exactPath(response, actionPath, "GET"));
    await visible.getByRole("button", { name: "批准应用英文五点（本地测试）" }).click();
    const approved = await approvedResponse;
    const independent = await readResponse;
    assert.equal(approved.status(), 200);
    assert.equal(independent.status(), 200);
    report.approval = (await approved.json()).data;
    report.readback = (await independent.json()).data;
    assert.deepEqual(report.approval, report.readback);
    assert.equal(report.readback.receipt.source_trace_id, terminals[0].trace_id);
    assert.ok(report.readback.receipt.execution_trace_id);
    assert.equal(report.readback.listing.version, pending.listing.version + 1);
    assert.deepEqual(report.readback.listing.bullets, updated);
    assert.deepEqual(report.readback.receipt.committed_bullets, updated);
    assert.equal(report.readback.receipt.committed_version, report.readback.listing.version);
    for (const line of updated) assert.ok(report.confirmation.includes(line));
    assert.ok(report.confirmation.includes(sku));
    await expect(visible.locator(".group-listing-receipt")).toContainText("当前实际版本 " + report.readback.listing.version);
    await expect(visible.locator('[data-bullet-version="actual"] > li')).toHaveText(updated);
    if (database) {
      report.sqlAfter = readProduct(database, evidence.target, actionId);
      const { row, otherProducts, receipts } = report.sqlAfter;
      assert.deepEqual(otherProducts, report.sqlBefore.otherProducts);
      assert.deepEqual({ ...row, bullets: undefined, version: undefined },
        { ...report.sqlBefore.row, bullets: undefined, version: undefined });
      assert.deepEqual(JSON.parse(row.bullets), updated);
      assert.equal(row.version, report.readback.listing.version);
      assert.equal(receipts.length, 1);
      assert.equal(receipts[0].receipt_id, report.readback.receipt.receipt_id);
    }
    await visible.screenshot({ path: path.join(output, "listing-approved.png") });
    const refreshed = page.waitForResponse(response => exactPath(response, actionPath, "GET"));
    await page.reload();
    await changeCard.click();
    assert.equal((await refreshed).status(), 200);
    report.refresh = (await (await refreshed).json()).data;
    assert.deepEqual(report.refresh, report.readback);
    await expect(visible.locator(".group-listing-receipt")).toContainText("当前实际版本 " + report.readback.listing.version);
    await expect(visible.locator('[data-bullet-version="actual"] > li')).toHaveText(updated);
    assert.deepEqual(sequence, ["POST", "GET", "GET"]);
    assert.deepEqual(pageErrors, []);
    report.sequence = sequence;
    await visible.screenshot({ path: path.join(output, "listing-refresh.png") });
    report.passed = true;
  } catch (error) {
    report.failure = error.stack;
    throw error;
  } finally {
    database?.close();
    await browser.close();
    await fs.writeFile(path.join(output, "listing-browser.json"), JSON.stringify(report, null, 2) + "\n");
  }
  console.log(JSON.stringify({ passed: report.passed, browser: report.browser, sequence: report.sequence }));
}

browserRun().catch(error => { console.error(error); process.exitCode = 1; });
