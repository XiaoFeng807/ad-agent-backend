"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

async function main() {
  const origin = process.argv[2] || "http://127.0.0.1:5142";
  const directory = process.argv[3] || "logs/task42/olist-sales";
  const expected = JSON.parse(await fs.readFile(path.join(directory, "chart-reconciliation.json"), "utf8"));
  const browser = await chromium.launch(browserOptions());
  const evidence = { browser: browser.version(), scenarios: {}, passed: false };
  try {
    const page = await browser.newPage({ baseURL: origin, viewport: { width: 1440, height: 1050 } });
    const login = await page.request.post("/api/login", { data: { username: "boss", password: "admin123" } });
    const account = (await login.json()).data;
    assert.ok(account?.token);
    await page.goto("/");
    await page.evaluate((account) => sessionStorage.setItem("ad.login", JSON.stringify(account)), account);
    await page.goto("/orders");
    await page.locator("#sales-submit:not([disabled])").waitFor();
    const scope = "/api/orders/shops/olist-v2/" + expected.selected_seller_and_multi_item.shop_id + "/sales";
    for (const [name, target] of Object.entries(expected)) {
      const started = performance.now();
      await page.locator("#sales-product").selectOption(target.product_id);
      await page.locator("#sales-status").selectOption(target.included_statuses.length === 1 ? target.included_statuses[0] : "");
      for (const key of ["baseline", "current"]) {
        await page.locator("#" + key + "_start").fill(target[key].start_date);
        await page.locator("#" + key + "_end").fill(target[key].end_date);
      }
      const response = page.waitForResponse((event) => new URL(event.url()).pathname === scope);
      await page.locator("#sales-submit").click();
      const receipt = await response;
      assert.equal(receipt.status(), 200);
      const api = (await receipt.json()).data;
      await page.locator("#sales-chart canvas").first().waitFor({ state: "visible" });
      const rendered = await page.locator("#sales-chart").evaluate((node) => {
        const chart = window.echarts.getInstanceByDom(node).getOption();
        return { dates: chart.xAxis[0].data, units: chart.series[0].data, orders: chart.series[1].data };
      });
      const points = [...target.baseline.points, ...target.current.points];
      const expectedSeries = {
        dates: points.map(({ date }) => date), units: points.map(({ units }) => units), orders: points.map(({ orders }) => orders),
      };
      evidence.scenarios[name] = {
        points: points.length, baseline: target.baseline.totals, current: target.current.totals,
        rendered, expectedSeries, seconds: (performance.now() - started) / 1000,
      };
      assert.deepEqual(api, target);
      assert.deepEqual(rendered, expectedSeries);
      assert.match(await page.locator("#sales-source").textContent(), /Olist v2.*原始下单日期.*仅本卖家行项/);
      assert.equal(await page.locator("#sales-points tr").count(), points.length);
      if (target.change.units.relative_pct === null) {
        assert.match(await page.locator("#sales-totals").textContent(), /基准为 0/);
      }
      if (name === "selected_seller_and_multi_item") {
        await page.locator("#sales-panel").screenshot({ path: path.join(directory, "olist-sales-chart.png") });
      }
    }
    evidence.passed = true;
  } finally {
    await browser.close();
    await fs.writeFile(path.join(directory, "chart-browser.json"), JSON.stringify(evidence, null, 2) + "\n");
  }
  console.log(JSON.stringify(evidence));
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
