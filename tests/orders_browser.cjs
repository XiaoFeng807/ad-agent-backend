"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

async function main() {
  const origin = process.argv[2] || "http://127.0.0.1:5041";
  const output = process.argv[3] || "logs/task41";
  const report = JSON.parse(await fs.readFile(path.join(output, "olist-reconciliation.json"), "utf8"));
  const scope = "/api/orders/shops/olist-v2/" + report.shop_id;
  const route = "/orders?" + new URLSearchParams({ source: report.source, shop: report.shop_id });
  const browser = await chromium.launch(browserOptions());
  const evidence = { browser: browser.version(), cases: [], pageErrors: [], mixedOrders: [], passed: false };
  try {
    const page = await browser.newPage({ baseURL: origin, viewport: { width: 1440, height: 1050 } });
    page.on("pageerror", (error) => evidence.pageErrors.push(error.message));
    await page.goto("/orders");
    await page.locator("#login").waitFor({ state: "visible" });
    evidence.cases.push("signed_out_page");
    const login = await page.request.post("/api/login", { data: { username: "boss", password: "admin123" } });
    const loginPayload = await login.json();
    assert.equal(loginPayload.code, 200);
    const auth = { Authorization: "Bearer " + loginPayload.data.token };
    await page.goto("/");
    await page.evaluate((data) => {
      sessionStorage.setItem("ad.login", JSON.stringify(data));
      window.openSession(data);
    }, loginPayload.data);
    await page.locator('nav a[href="/orders"]').click();
    await page.locator("#workspace").waitFor({ state: "visible" });
    const api = await page.request.get(scope, { headers: auth });
    const listing = (await api.json()).data;
    assert.deepEqual(listing.totals, { orders: report.summary.actual_orders, items: report.summary.actual_items });
    assert.equal(await page.locator("#total-orders").textContent(), "1,854");
    assert.equal(await page.locator("#total-items").textContent(), "2,033");
    assert.equal(await page.locator("#source-label").textContent(), "Olist v2 · 公开历史订单");
    assert.equal(await page.locator("#shop-id").textContent(), report.shop_id);
    assert.match(await page.locator("#source-note").textContent(), /时区未知/);
    assert.equal(await page.locator("#orders tr").count(), 25);
    evidence.cases.push("home_navigation_and_real_totals");
    await page.screenshot({ path: path.join(output, "orders-list.png") });
    await page.locator("#next").click();
    await page.waitForFunction(() => document.getElementById("range").textContent.includes("26–50"));
    assert.equal((await page.locator("#orders tr:first-child a").textContent()).length, 32);
    evidence.cases.push("next_page");

    async function checkDetail(expected) {
      const received = await page.request.get(scope + "/orders/" + expected.order_id, { headers: auth });
      assert.equal(received.status(), 200);
      const order = (await received.json()).data;
      for (const [key, value] of Object.entries(expected)) assert.deepEqual(order[key], value);
      await page.locator("#detail-content table").waitFor({ state: "visible" });
      assert.equal(await page.locator("#detail-title").textContent(), order.order_id);
      assert.equal(await page.locator("#detail-source").textContent(), "Olist v2 · 公开历史订单");
      const fields = await page.locator("#detail-content dd").allTextContents();
      assert.deepEqual(fields, [order.status, order.ordered_at, "未知", order.synced_at, order.carrier_at ?? "未知", "是，仅展示本店行项"]);
      const items = await page.locator("#detail-content tbody tr").evaluateAll((rows) =>
        rows.map((row) => Array.from(row.cells, (cell) => cell.textContent))
      );
      assert.deepEqual(items, expected.items.map((item) => [item.item_id, item.product_id, String(item.quantity), item.ship_by ?? "未知"]));
      return items.length;
    }

    const samples = Object.values(report.mixed_seller_expected);
    const sample = samples.find((order) => order.items.length >= 2) || samples[0];
    await page.locator("#status").selectOption(sample.status);
    await page.locator("#search").fill(sample.order_id);
    await page.getByRole("button", { name: "筛选" }).click();
    await page.waitForFunction(() => document.getElementById("range").textContent.includes("共 1 单"));
    const row = page.locator("#orders tr");
    assert.equal(await row.count(), 1);
    assert.deepEqual(await row.locator("td").allTextContents(),
      [sample.order_id, sample.status, sample.ordered_at, String(sample.items.length), "未知"]);
    await page.getByRole("link", { name: sample.order_id, exact: true }).click();
    await checkDetail(sample);
    await page.reload();
    await checkDetail(sample);
    assert.equal(await page.locator("#status").inputValue(), sample.status);
    assert.equal(await page.locator("#search").inputValue(), sample.order_id);
    await page.screenshot({ path: path.join(output, "orders-mixed-detail.png") });
    await page.keyboard.press("Escape");
    await page.locator("#detail").waitFor({ state: "hidden" });
    assert.equal(new URL(page.url()).searchParams.has("order"), false);
    evidence.cases.push("filter_detail_reload_and_close");

    for (const order of samples) {
      await page.goto(route + "&order=" + order.order_id);
      evidence.mixedOrders.push({ order_id: order.order_id, ownItems: await checkDetail(order) });
    }
    assert.equal(evidence.mixedOrders.length, 31);
    evidence.cases.push("all_31_mixed_orders_match_source_sql_and_api");
    await page.keyboard.press("Escape");
    await page.getByRole("link", { name: "返回工作台" }).click();
    await page.locator("#navUsername").waitFor({ state: "visible" });
    assert.equal(await page.locator("#navUsername").textContent(), "boss");
    await page.locator('nav a[href="/orders"]').click();
    await page.locator("#workspace").waitFor({ state: "visible" });
    evidence.cases.push("return_to_home_keeps_session");
    const other = await page.request.post("/api/login", { data: { username: "lisi", password: "user123" } });
    const otherSession = (await other.json()).data;
    const otherHeaders = { Authorization: "Bearer " + otherSession.token };
    assert.equal((await page.request.get(scope, { headers: otherHeaders })).status(), 404);
    assert.equal((await page.request.get(scope + "/orders/" + sample.order_id, { headers: otherHeaders })).status(), 404);
    await page.evaluate((data) => sessionStorage.setItem("ad.login", JSON.stringify(data)), otherSession);
    await page.goto(route + "&order=" + sample.order_id);
    await page.getByText("当前账号没有可访问的店铺。").waitFor({ state: "visible" });
    assert.equal(await page.locator("#workspace").isVisible(), false);
    assert.equal(await page.locator("#detail").isVisible(), false);
    evidence.cases.push("unauthorized_page_list_and_detail");
    assert.deepEqual(evidence.pageErrors, []);
    evidence.passed = true;
  } catch (error) {
    evidence.failure = error.stack;
    throw error;
  } finally {
    await browser.close();
    await fs.writeFile(path.join(output, "browser-report.json"), JSON.stringify(evidence, null, 2) + "\n");
  }
  console.log(JSON.stringify({ ...evidence, mixedOrders: evidence.mixedOrders.length }));
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
