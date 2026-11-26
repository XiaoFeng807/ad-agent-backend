const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");

async function verify() {
  const [origin, folder] = process.argv.slice(2);
  const { scenarios } = JSON.parse(await fs.readFile(path.join(folder, "result.json"), "utf8"));
  const browser = await chromium.launch(browserOptions());
  try {
    const context = await browser.newContext({ baseURL: origin, viewport: { width: 1440, height: 1050 } });
    const response = await context.request.post("/api/login", {
      data: { username: "ad053-replay", password: process.env.AD053_PASSWORD },
    });
    const login = await response.json();
    assert.equal(login.code, 200);
    await context.addInitScript(data => {
      if (!sessionStorage.getItem("ad.login")) {
        sessionStorage.setItem("ad.login", JSON.stringify(data));
        sessionStorage.setItem("ad.chatPage", "groups");
      }
    }, login.data);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const outcomes = {};
    await page.goto("/");
    for (const name of ["complete", "missing"]) {
      const scenario = scenarios[name];
      const group = page.locator("#chatGroupList").getByRole("button", {name: scenario.group, exact: true});
      const card = page.locator('[data-result-kind="search_term"]')
        .filter({hasText: scenario.term}).filter({hasText: "诊断未完成"});
      const side = page.locator("#workbenchDetail");
      const label = name === "complete" ? "归属待补 · 广告组指标" : "广告组指标无法核验";
      const expected = JSON.parse(await fs.readFile(path.join(folder, name, "http.json"), "utf8")).get.body;
      const visits = [];
      for (const visit of ["detail", "refresh"]) {
        if (visit === "refresh") await page.reload();
        const groupId = (await group.getAttribute("data-conversation-id"));
        assert.ok(scenario.path.includes(groupId));
        await group.click();
        await expect(page.locator("#chatGroupTitle")).toHaveText(scenario.group);
        const get = page.waitForResponse(res => new URL(res.url()).pathname === scenario.path &&
          res.request().method() === "GET");
        await card.click();
        const detailResponse = await get;
        assert.equal(detailResponse.status(), 200);
        const body = await detailResponse.json();
        assert.deepEqual(body, expected);
        await expect(side.getByText(label, {exact: true})).toBeVisible();
        await expect(side.getByText(/^诊断未完成/).first()).toBeVisible();
        await expect(side.getByRole("button", {name: /编辑|复制|复核/})).toHaveCount(0);
        await expect(side.getByText(/复核待办/)).toHaveCount(0);
        if (name === "complete") {
          await side.getByText("报表原行与指标 · 2 行", {exact: true}).click();
          await expect(side.getByText(/曝光 .* · 点击 21 · 成交/)).toBeVisible();
          await expect(side.locator(".search-term-evidence strong")).toHaveText([/^第 8 行/, /^第 26 行/]);
        } else {
          await expect(side.getByText(/缺少报表原行.*第 26 行/)).toBeVisible();
          await expect(side.getByText(/报表原行与指标/)).toHaveCount(0);
          await side.getByText(/历史商品指标/).click();
          await expect(side.getByText(/曝光 .* · 点击 10 · 成交/)).toBeVisible();
          await expect(side.locator(".search-term-evidence strong")).toHaveText([/^第 8 行/]);
        }
        await page.screenshot({path: path.join(folder, name, `edge-${visit}.png`)});
        visits.push({visit, status: detailResponse.status(), label, groupId,
          clickedPath: scenario.path, sourceLines: body.rows.map(row => row.line),
          historicalLines: (body.historical_rows ?? []).map(row => row.line),
          clicks: body.item.metrics?.clicks ?? null});
      }
      outcomes[name] = visits;
    }
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(folder, "edge-result.json"), JSON.stringify({
      passed: true, browser: "Microsoft Edge", version: browser.version(), scenarios: outcomes,
    }, null, 2) + "\n");
  } finally {
    await browser.close();
  }
}

verify().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
