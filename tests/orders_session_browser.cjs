"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const [origin = "http://127.0.0.1:5141", output = "logs/task41/ad049-ad050"] = process.argv.slice(2);
const replayOrigin = process.argv[4];
const otherShop = "/api/orders/shops/amazon-us-fbm/edge-session-shop";
const otherOrder = "edge-session-order";
const workspace = "#workspace";
const denied = "当前账号没有可访问的店铺。";

function snapshot() {
  const text = (id) => document.getElementById(id).textContent;
  return {
    workspace: !document.getElementById("workspace").hidden,
    detail: document.getElementById("detail").open,
    rows: document.getElementById("orders").children.length,
    runs: document.getElementById("runs").children.length,
    order: text("detail-title"),
    detailContent: text("detail-content"),
    shop: text("shop-id"),
    notice: text("notice"),
    syncNotice: text("sync-notice"),
  };
}

function assertCleared(state) {
  const { notice, ...data } = state;
  assert.deepEqual(data, {
    workspace: false, detail: false, rows: 0, runs: 0, order: "订单详情",
    detailContent: "", shop: "", syncNotice: "",
  });
}

async function login(page, username) {
  const response = await page.request.post("/api/login", {
    data: { username, password: username === "boss" ? "admin123" : "user123" },
  });
  const { code, data } = await response.json();
  assert.equal(code, 200);
  return data;
}

async function setLogin(page, data) {
  await page.evaluate((session) => {
    sessionStorage.setItem("ad.login", JSON.stringify(session));
    window.openSession(session);
  }, data);
}

async function main() {
  const report = JSON.parse(await fs.readFile(path.join(output, "olist-reconciliation.json"), "utf8"));
  const scope = "/api/orders/shops/olist-v2/" + report.shop_id;
  const route = "/orders?" + new URLSearchParams({ source: report.source, shop: report.shop_id });
  const browser = await chromium.launch({
    channel: "msedge",
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
    args: ["--enable-features=BackForwardCache"],
  });
  const evidence = { browser: browser.version(), cases: {}, pageErrors: [], passed: false };

  async function run(name, check, baseURL = origin) {
    const page = await browser.newPage({ baseURL });
    page.on("pageerror", (error) => evidence.pageErrors.push(error.message));
    const record = evidence.cases[name] = {};
    try {
      await page.addInitScript(`
        const snapshot = ${snapshot};
        window.restores = [];
        window.parsed = [];
        const read = Response.prototype.json;
        Response.prototype.json = async function () {
          const body = await read.call(this);
          if (this.url.includes("/api/orders/")) {
            window.parsed.push({ path: new URL(this.url).pathname, status: this.status });
          }
          return body;
        };
        addEventListener("pageshow", (event) => {
          if (location.pathname === "/orders") window.restores.push({ persisted: event.persisted, ...snapshot() });
        });
      `);
      await page.goto("/");
      const boss = await login(page, "boss");
      await setLogin(page, boss);
      await check(page, record, boss);
      record.passed = true;
    } finally {
      if (new URL(page.url()).pathname === "/orders") record.restores = await page.evaluate(() => window.restores);
      await page.close();
    }
  }

  async function showDetail(page, url) {
    await page.goto(url);
    await page.locator(workspace).waitFor({ state: "visible" });
    await page.locator("#detail-content table").waitFor({ state: "visible" });
    assert.ok((await page.locator("#runs tr").count()) > 0);
  }

  async function restore(page, record) {
    const authorization = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/orders/shops");
    await page.goBack({ waitUntil: "commit" });
    await page.waitForFunction(() => window.restores?.some((event) => event.persisted));
    const immediate = await page.evaluate(() => window.restores.at(-1));
    const { persisted, ...state } = immediate;
    assert.equal(persisted, true);
    assertCleared(state);
    assert.equal(state.notice, "");
    record.firstRestore = immediate;
    return authorization;
  }

  try {
    const sample = Object.keys(report.mixed_seller_expected)[0];
    await run("different_account_back", async (page, record) => {
      await showDetail(page, route + "&order=" + sample);
      record.before = await page.evaluate(snapshot);
      // Navigate with the modal still open, preserving the order deep link.
      await page.locator("a.back").evaluate((link) => link.click());
      await page.locator("#navUsername").waitFor({ state: "visible" });
      await page.locator(".logout-btn").click();
      const other = await login(page, "lisi");
      await setLogin(page, other);
      record.otherAccountApi = await Promise.all([scope, scope + "/orders/" + sample].map(async (url) =>
        (await page.request.get(url, { headers: { Authorization: "Bearer " + other.token } })).status()));
      assert.deepEqual(record.otherAccountApi, [404, 404]);
      const request = await restore(page, record);
      assert.equal(request.headers().authorization, "Bearer " + other.token);
      await page.getByText(denied).waitFor({ state: "visible" });
      record.afterAuthorization = await page.evaluate(snapshot);
      assertCleared(record.afterAuthorization);
    });

    await run("same_account_back", async (page, record, boss) => {
      const filters = "status=delivered&search=a&offset=25";
      const response = await page.request.get(scope + "?" + filters, { headers: { Authorization: "Bearer " + boss.token } });
      const expected = (await response.json()).data;
      assert.equal(expected.orders.length, 25);
      const order = expected.orders[0].order_id;
      const url = route + "&" + filters + "&order=" + order;
      await showDetail(page, url);
      record.before = await page.evaluate(snapshot);
      await page.locator("a.back").evaluate((link) => link.click());
      await page.locator("#navUsername").waitFor({ state: "visible" });
      await restore(page, record);
      await page.locator("#detail-content table").waitFor({ state: "visible" });
      await page.locator(workspace).waitFor({ state: "visible" });
      assert.equal(new URL(page.url()).searchParams.toString(), new URL(url, origin).searchParams.toString());
      assert.equal(await page.locator("#status").inputValue(), "delivered");
      assert.equal(await page.locator("#search").inputValue(), "a");
      assert.match(await page.locator("#range").textContent(), /26–50/);
      assert.equal(await page.locator("#orders tr:first-child a").textContent(), order);
      assert.equal(await page.locator("#detail-title").textContent(), order);
      record.afterAuthorization = await page.evaluate(snapshot);
      record.filters = new URL(page.url()).search;
    });

    for (const [name, endpoint, status] of [
      ["late_list_200", scope, 200],
      ["late_detail_200", scope + "/orders/" + sample, 200],
      ["late_list_401", scope, 401],
    ]) {
      await run(name, async (page, record, boss) => {
        const other = await login(page, "zhangsan");
      const { promise: held, resolve: markHeld, reject: failHold } = Promise.withResolvers();
        const { promise: released, resolve: release } = Promise.withResolvers();
        await page.route((url) => url.pathname === endpoint, async (interception) => {
          const isOld = (await interception.request().headerValue("authorization")) === "Bearer " + boss.token;
          if (!isOld) return interception.continue();
        try {
          const upstream = status === 200 ? await interception.fetch() : null;
          assert.equal(upstream?.status() ?? status, status);
          markHeld();
          await released;
          await interception.fulfill(upstream ? { response: upstream } : { status, json: { detail: "expired fixture" } });
        } catch (error) {
          failHold(error);
          evidence.pageErrors.push(error.message);
        }
        });
        try {
          await page.goto(route + (name === "late_detail_200" ? "&order=" + sample : ""));
          await held;
          // Same-tab sessionStorage changes from a same-origin frame emit a storage event.
          await page.evaluate((session) => {
            const frame = document.createElement("iframe");
            frame.hidden = true;
            document.body.append(frame);
            frame.contentWindow.sessionStorage.setItem("ad.login", JSON.stringify(session));
            frame.remove();
          }, other);
          await page.waitForFunction((id) => document.querySelector("#orders tr a")?.textContent === id, otherOrder);
          const currentUrl = new URL(page.url());
          assert.equal(currentUrl.searchParams.get("shop"), "edge-session-shop");
          record.beforeRelease = await page.evaluate(snapshot);
          await page.evaluate(({ id, oldShop }) => {
            window.staleMutations = 0;
            const observer = new MutationObserver((mutations) => {
              const stale = (node) => node.textContent?.includes(id) || node.textContent?.includes(oldShop);
            if (document.getElementById("detail").open || !document.getElementById("login").hidden ||
                mutations.some((mutation) => [...mutation.addedNodes].some(stale))) window.staleMutations++;
            });
            observer.observe(document.querySelector("main"), { childList: true, subtree: true, attributes: true });
            observer.observe(document.getElementById("detail"), { childList: true, subtree: true, attributes: true });
          }, { id: sample, oldShop: report.shop_id });
          release();
          await page.waitForFunction(({ endpoint, status }) => window.parsed.some((entry) =>
            entry.path === endpoint && entry.status === status), { endpoint, status });
          record.afterRelease = await page.evaluate(snapshot);
          record.received = await page.evaluate(() => window.parsed);
          record.staleMutations = await page.evaluate(() => window.staleMutations);
          assert.deepEqual(record.afterRelease, record.beforeRelease);
          assert.equal(record.staleMutations, 0);
          assert.equal(await page.locator("#login").isVisible(), false);
          assert.equal(await page.evaluate((token) => JSON.parse(sessionStorage.getItem("ad.login")).token === token,
            other.token), true);
          const authorized = await page.request.get(otherShop, { headers: { Authorization: "Bearer " + other.token } });
          assert.equal(authorized.status(), 200);
        } finally {
          release();
        }
      });
    }
    if (replayOrigin) {
      await run("amazon_optional_deadline", async (page, record, boss) => {
        const replay = JSON.parse(await fs.readFile(path.join(output, "amazon-incomplete-replay.json"), "utf8"));
        const endpoint = "/api/orders/shops/amazon-us-fbm/fixture-us";
        for (const expected of replay.optional_deadline_get) {
          const id = expected.order_id;
          const response = await page.request.get(endpoint + "/orders/" + id, {
            headers: { Authorization: "Bearer " + boss.token },
          });
          assert.equal(response.status(), 200);
          assert.deepEqual((await response.json()).data, expected);
          await showDetail(page, "/orders?source=amazon-us-fbm&shop=fixture-us&order=" + id);
          const listDeadline = await page.locator("#orders tr").filter({ hasText: id }).locator("td").last().textContent();
          const detailDeadline = await page.locator("#detail-content dl div").filter({ hasText: "发货截止（整单）" })
            .locator("dd").textContent();
          assert.equal(listDeadline, "未知");
          assert.equal(detailDeadline, "未知");
          (record.deadlines ??= []).push({ order: id, api: expected.ship_by, list: listDeadline, detail: detailDeadline });
        }
        const latestRun = await page.locator("#runs tr").first().locator("td").allTextContents();
        assert.match(latestRun[2], /缺少有效源更新时间 · 已读取 2 页/);
        record.latestRun = latestRun;
        await page.screenshot({ path: path.join(output, "amazon-unknown-deadline.png") });
      }, replayOrigin);
    }
    assert.deepEqual(evidence.pageErrors, []);
    evidence.passed = true;
  } catch (error) {
    evidence.failure = error.stack;
    throw error;
  } finally {
    await browser.close();
    await fs.writeFile(path.join(output, "browser-session-report.json"), JSON.stringify(evidence, null, 2) + "\n");
  }
  console.log(JSON.stringify({ browser: evidence.browser, cases: Object.keys(evidence.cases), passed: evidence.passed }));
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
