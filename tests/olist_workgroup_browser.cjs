"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");

async function main() {
  const [origin, directory] = process.argv.slice(2);
  const account = JSON.parse(process.env.AD_OLIST_GROUP_SESSION);
  const saved = JSON.parse(await fs.readFile(path.join(directory, "cases-result.json"), "utf8"));
  const disjoint = process.env.AD_OLIST_DISJOINT_SAMPLE === "1";
  const row = disjoint ? saved.separated_weeks : saved.selected_seller_and_multi_item;
  const browser = await chromium.launch(browserOptions());
  try {
    const page = await browser.newPage({ baseURL: origin, viewport: { width: 1340, height: 1000 } });
    await page.goto("/");
    await page.evaluate((value) => {
      sessionStorage.setItem("ad.login", JSON.stringify(value));
      sessionStorage.setItem("ad.chatPage", "groups");
    }, account);
    await page.reload();
    await page.locator('[data-page="chat"]').click();
    const chart = page.locator(".group-listing-chart");
    const points = [...row.evidence.metrics.baseline.points, ...row.evidence.metrics.current.points];
    const windows = ["baseline", "current"].map(key => row.evidence.metrics[key].points);
    const evidencePath = `/api/chat/conversations/${row.conversation_id}/runs/${row.events.at(-1).run_id}/evidence`;
    const waitEvidence = () => page.waitForResponse(response => new URL(response.url()).pathname === evidencePath);
    const card = page.locator(`.msg[data-entry-id="${row.evidence.entry_id}"] [data-result-kind="sales"]`);
    await page.locator(`#chatGroupList [data-conversation-id="${row.conversation_id}"]`).click();
    await expect(card).toContainText("Olist · 公开历史");
    const [actualEvidence] = await Promise.all([waitEvidence(), card.click()]);
    const assertEvidence = async response => {
      assert.equal(response.status(), 200);
      assert.deepEqual((await response.json()).data, row.evidence);
    };
    async function checkChart() {
      await expect(chart).toBeVisible();
      const rendered = await chart.locator("li[data-date]").evaluateAll(elements => elements.map(element => ({
        date: element.dataset.date, units: Number(element.dataset.units), orders: Number(element.dataset.orders),
      })));
      assert.deepEqual(rendered, points);
      assert.deepEqual(await chart.locator("ol").evaluateAll(lists => lists.map(list =>
        [...list.querySelectorAll("li[data-date]")].map(element => element.dataset.date))),
      windows.map(window => window.map(point => point.date)));
      await expect(page.locator(`[data-runtime-call-id="${row.evidence.runtime_call_id}"]`)).toBeVisible();
      await expect(page.locator(".group-listing-action, .group-pause")).toHaveCount(0);
    }
    await checkChart();
    await assertEvidence(actualEvidence);
    await page.reload();
    const [refreshedEvidence] = await Promise.all([waitEvidence(), card.click()]);
    await checkChart();
    await assertEvidence(refreshedEvidence);
    await chart.screenshot({ path: path.join(directory, "workgroup-chart.png") });
    await fs.writeFile(path.join(directory, "browser.json"), JSON.stringify({
      passed: true, browser: browser.version(), points: points.length, runtime_call_id: row.evidence.runtime_call_id,
      conversation_id: row.conversation_id, disjoint, windows, refresh_evidence_equal: true,
    }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
