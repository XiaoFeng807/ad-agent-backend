const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");

const [origin, folder, databasePath] = process.argv.slice(2);
const groups = "/api/chat/conversations";
const approvalLabel = "批准应用英文五点（本地测试）";

function listingSnapshot(db) {
  return {
    products: db.prepare("SELECT * FROM listing_test_products ORDER BY sku").all(),
    proposals: db.prepare("SELECT * FROM listing_change_proposals ORDER BY action_id").all(),
    receipts: db.prepare("SELECT * FROM listing_change_receipts ORDER BY receipt_id").all(),
  };
}

async function run() {
  const scenario = JSON.parse(await fs.readFile(path.join(folder, "scenario.json"), "utf8"));
  const report = { rounds: [], approvals: [], pageErrors: [] };
  let browser, page, db;
  try {
    browser = await chromium.launch(browserOptions());
    db = new DatabaseSync(databasePath, { readOnly: true });
    report.browser = browser.version();
    page = await browser.newPage({ baseURL: origin, viewport: { width: 1360, height: 1050 }, timezoneId: "Asia/Shanghai" });
    page.setDefaultTimeout(95000);
    page.on("pageerror", error => report.pageErrors.push(error.message));
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.task45Sse = [];
      window.fetch = async (url, options) => {
        const started = performance.now();
        const response = await fetch(url, options);
        if (url === "/api/chat/stream") {
          const item = {};
          window.task45Sse.push(item);
          const reader = response.clone().body.getReader();
          const decoder = new TextDecoder();
          const consume = async () => {
            let raw = "";
            let pending = "";
            while (true) {
              const { value, done } = await reader.read();
              const chunk = decoder.decode(value, { stream: !done });
              raw += chunk;
              pending += chunk;
              const lines = pending.split("\n");
              pending = lines.pop();
              for (const line of lines.filter(line => line.startsWith("data: "))) {
                const event = JSON.parse(line.slice(6));
                if (["done", "error", "stopped", "superseded"].includes(event.type)) {
                  item.terminalMs = performance.now() - started;
                }
              }
              if (done) break;
            }
            item.raw = raw;
          };
          consume().catch(error => { item.error = error.message; });
        }
        return response;
      };
    });
    const matches = (response, method, pathname) =>
      response.request().method() === method && new URL(response.url()).pathname === pathname;
    async function packet(response) {
      return { method: response.request().method(), path: new URL(response.url()).pathname,
        status: response.status(), body: await response.json() };
    }
    await page.goto(origin);
    await page.locator("#loginUser").fill("task45-operator");
    await page.locator("#loginPass").fill(process.env.TASK45_BROWSER_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    const loggedIn = page.waitForResponse(response => matches(response, "POST", "/api/login"));
    await page.locator("#loginPass").press("Enter");
    const login = await (await loggedIn).json();
    assert.equal(login.code, 200);
    await page.locator('[data-page="chat"]').click();
    const headers = { Authorization: "Bearer " + login.data.token };
    const created = page.waitForResponse(response => matches(response, "POST", groups));
    await submitGroupCreation(page, "TASK-045-T1 " + scenario.id);
    const conversation = (await (await created).json()).data.conversation_id;
    const groupPath = groups + "/" + conversation;
    assert.ok(conversation);
    report.conversation_id = conversation;

    async function submit(message) {
      const index = report.rounds.length;
      const round = { message };
      report.rounds.push(round);
      await page.locator("#chatInput").fill(message);
      const ready = page.waitForResponse(response => matches(response, "POST", "/api/chat/stream"));
      await page.locator("#chatSend").click();
      const response = await ready;
      round.request = response.request().postDataJSON();
      round.status = response.status();
      assert.deepEqual(round.request, { message, conversation_id: conversation });
      await page.waitForFunction(i => window.task45Sse[i]?.raw !== undefined || window.task45Sse[i]?.error, index);
      const sse = await page.evaluate(i => window.task45Sse[i], index);
      round.sse = sse;
      round.events = (sse.raw || "").split(/\r?\n/).filter(line => line.startsWith("data: "))
        .map(line => JSON.parse(line.slice(6)));
      const terminal = round.events.findLast(event => ["done", "error", "stopped", "superseded"].includes(event.type));
      round.terminal = terminal;
      round.history = await (await page.request.get(groupPath + "/history", { headers })).json();
      if (terminal?.run_id) {
        const response = await page.request.get(groupPath + "/runs/" + terminal.run_id + "/evidence", { headers });
        round.evidence = { status: response.status(), body: await response.json() };
      }
      return round;
    }
    if (scenario.context) await submit("选 SKU " + scenario.context);
    const round = await submit(scenario.message);
    const entry = round.history?.data?.entries?.findLast(row => row.kind === "assistant");
    if (entry) {
      const answer = page.locator(`.msg[data-entry-id="${entry.entry_id}"]`);
      await expect(answer).toBeVisible();
      report.answer = await answer.innerText();
      const card = answer.locator('[data-result-kind="listing"]');
      report.listingCards = await card.count();
      await answer.screenshot({ path: path.join(folder, "answer.png") });
      if (scenario.inspect_evidence) {
        assert.equal(report.listingCards, 0);
        await answer.locator('[data-result-kind="evidence"]').click();
        const detail = page.locator("#workbenchDetail");
        await expect(detail.getByText("逐款巡检依据", { exact: true })).toBeVisible();
        for (const sku of scenario.inspect_evidence) {
          await expect(detail.getByRole("heading", { name: new RegExp("SKU " + sku) })).toBeVisible();
        }
        await expect(detail.getByRole("button", { name: approvalLabel, exact: true })).toHaveCount(0);
        await detail.screenshot({ path: path.join(folder, "inspection-evidence.png") });
      }
      if (report.listingCards) {
        const actionPath = groupPath + "/runs/" + entry.run_id + "/listing-actions/" + entry.listing_actions[0].action_id;
        const pending = page.waitForResponse(response => matches(response, "GET", actionPath));
        await card.click();
        report.pending = await packet(await pending);
        const section = page.locator(".group-listing-action");
        await expect(section).toBeVisible();
        const button = section.getByRole("button", { name: approvalLabel, exact: true });
        if (report.pending.body.data?.state === "pending" && !report.pending.body.data.approval_blocked) {
          await expect(button).toBeVisible();
        }
        await page.locator("#workbenchDetail").screenshot({ path: path.join(folder, "pending.png") });
        report.approveButtons = await button.count();
        report.sqlBeforeApproval = listingSnapshot(db);
        if (scenario.force_confirm) {
          const confirmed = await page.request.post(actionPath + "/confirm", { headers });
          report.forcedConfirmation = { status: confirmed.status(), body: await confirmed.json() };
          report.sqlAfterForcedConfirmation = listingSnapshot(db);
        }
        if (!scenario.observe_pending && (scenario.approve || scenario.id === "base") && report.approveButtons) {
          const oracle = JSON.parse(await fs.readFile(path.join(folder, "sql-expected.json"), "utf8"));
          const source = JSON.parse(await fs.readFile(path.join(folder, "source.json"), "utf8"));
          const expectedSku = scenario.sku || oracle.comparison.find(row => row.selected)?.sku;
          const product = source.find(row => row.target.sku === expectedSku);
          const proposal = report.pending.body.data?.proposal;
          assert.deepEqual(proposal?.target, product?.target);
          assert.equal(proposal.original_version, product.listing_version);
          assert.equal(proposal.facts_version, product.facts_version);
          assert.equal(proposal.bullets.length, 5);
          assert.equal(new Set(proposal.bullets.map(bullet => bullet.fact_id)).size, 5);
          for (const bullet of proposal.bullets) {
            const fact = product.facts.find(row => row.fact_id === bullet.fact_id);
            assert.equal(bullet.statement, fact?.statement);
            assert.ok(fact?.options.includes(bullet.text));
          }
          page.on("request", request => {
            const requestPath = new URL(request.url()).pathname;
            if ([actionPath, actionPath + "/confirm"].includes(requestPath)) {
              report.approvals.push({ method: request.method(), path: requestPath, body: request.postData() });
            }
          });
          const post = page.waitForResponse(response => matches(response, "POST", actionPath + "/confirm"));
          const get = page.waitForResponse(response => matches(response, "GET", actionPath));
          await button.click();
          report.post = await packet(await post);
          report.get = await packet(await get);
          await expect(section).toHaveAttribute("data-state", "approved");
          report.sqlAfterApproval = listingSnapshot(db);
          await section.screenshot({ path: path.join(folder, "approved.png") });
          await page.reload();
          const refresh = page.waitForResponse(response => matches(response, "GET", actionPath));
          await card.click();
          report.refresh = await packet(await refresh);
          await expect(section).toHaveAttribute("data-state", "approved");
          report.refreshedAnswer = await answer.innerText();
          report.refreshedReceipt = await section.innerText();
          report.sqlAfterRefresh = listingSnapshot(db);
          await section.screenshot({ path: path.join(folder, "refresh.png") });
        }
      } else {
        report.approveButtons = await page.getByRole("button", { name: approvalLabel, exact: true }).count();
      }
    }
  } catch (error) {
    report.failure = error.stack;
    if (page) await page.screenshot({ path: path.join(folder, "failure.png"), timeout: 3000 }).catch(() => {});
    process.exitCode = 1;
  } finally {
    db?.close();
    await browser?.close();
    await fs.writeFile(path.join(folder, "browser.json"), JSON.stringify(report, null, 2) + "\n");
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
