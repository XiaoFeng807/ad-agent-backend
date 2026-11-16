"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

async function main() {
  const [, , origin, output] = process.argv;
  const scenarios = JSON.parse(await fs.readFile(path.join(output, "browser-samples.json"), "utf8"));
  const browser = await chromium.launch(browserOptions());
  const evidence = { browser: browser.version(), cases: [] };
  try {
    const page = await browser.newPage({ baseURL: origin, viewport: { width: 1360, height: 1000 } });
    const login = await page.request.post("/api/login", { data: { username: "boss", password: "admin123" } });
    assert.equal(login.status(), 200);
    await page.goto("/");
    await page.evaluate((session) => sessionStorage.setItem("ad.login", JSON.stringify(session)), (await login.json()).data);
    await page.goto("/orders");
    await page.locator("#sales-submit:not([disabled])").waitFor();
    for (const [name, target] of Object.entries(scenarios)) {
      await page.locator("#sales-product").selectOption(target.product_id);
      for (const key of ["baseline", "current"]) {
        await page.locator(`#${key}_start`).fill(target[key].start_date);
        await page.locator(`#${key}_end`).fill(target[key].end_date);
      }
      const response = page.waitForResponse((event) => new URL(event.url()).pathname.endsWith("/sales"));
      await page.locator("#sales-submit").click();
      const receipt = await response;
      assert.equal(receipt.status(), 200);
      assert.deepEqual((await receipt.json()).data, target);
      await page.locator("#sales-chart canvas").first().waitFor({ state: "visible" });
      const rendered = await page.locator("#sales-chart").evaluate((node) => {
        const option = window.echarts.getInstanceByDom(node).getOption();
        return { dates: option.xAxis[0].data, units: option.series[0].data,
          orders: option.series[1].data, connectNulls: option.series[1].connectNulls };
      });
      const points = [...target.baseline.points, ...target.current.points];
      const distance = (Date.parse(target.current.start_date) - Date.parse(target.baseline.end_date)) / 86_400_000;
      const separated = distance > 1;
      const expectedChart = points.slice();
      if (separated) {
        expectedChart.splice(target.baseline.points.length, 0, { date: "日期间隔", units: null, orders: null });
      }
      const result = {
        name, apiMatched: true, points: points.length, distanceDays: distance, separated,
        baselineEnd: target.baseline.end_date, currentStart: target.current.start_date,
        renderedAtBoundary: rendered.dates.slice(target.baseline.points.length - 1, target.baseline.points.length + 2),
        renderedUnitsAtBoundary: rendered.units.slice(target.baseline.points.length - 1, target.baseline.points.length + 2),
        renderedOrdersAtBoundary: rendered.orders.slice(target.baseline.points.length - 1, target.baseline.points.length + 2),
        tableRows: await page.locator("#sales-points tr").count(),
      };
      evidence.cases.push(result);
      await page.locator("#sales-panel").screenshot({ path: path.join(output, name + ".png") });
      assert.deepEqual(rendered.dates, expectedChart.map((point) => point.date));
      assert.deepEqual(rendered.units, expectedChart.map((point) => point.units));
      assert.deepEqual(rendered.orders, expectedChart.map((point) => point.orders));
      if (separated) assert.equal(rendered.connectNulls, false);
      assert.equal(result.tableRows, points.length);
      result.passed = true;
    }
  } finally {
    await fs.writeFile(path.join(output, "browser.json"), JSON.stringify(evidence, null, 2) + "\n");
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
