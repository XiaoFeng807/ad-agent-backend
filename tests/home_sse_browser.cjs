// Run from the repository with Node.js, Playwright, and Chrome installed.
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

const origin = "http://ad016.test";
const html = process.argv[2] || path.join(__dirname, "../backend/static/index.html");
const json = (route, data) => route.fulfill({ json: { code: 200, data } });
const pathname = request => new URL(request.url()).pathname;
const historyResponse = response => pathname(response) === "/api/chat/history";

async function withChat(browser, test) {
  const page = await browser.newPage();
  page.setDefaultTimeout(3000);
  const saved = [];
  const replyGates = new Map();
  let historyGets = 0;
  let historyRoute = (route, rows) => json(route, rows());

  try {
    await page.route("**/*", async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== origin) {
        return route.fulfill({ status: 404 });
      }
      switch (url.pathname) {
        case "/":
          return route.fulfill({ path: html });
        case "/static/workbench.js":
        case "/static/workbench-sources.js":
        case "/static/search-terms.js":
        case "/static/product-shell.css":
        case "/static/workbench.css":
        case "/static/workbench-shell.css":
        case "/static/listing-review.css":
        case "/static/search-terms.css":
        case "/static/workbench-sources.css":
          return route.fulfill({ path: path.join(path.dirname(html), path.basename(url.pathname)) });
        case "/static/echarts.min.js":
          return route.fulfill({ contentType: "application/javascript", body:
            "window.echarts = {init: () => ({resize() {}, setOption() {}})};" });
        case "/api/captcha":
          return route.fulfill({ json: { code: 200, captcha_id: "fixture", image: "" } });
        case "/api/login":
          return json(route, { token: "fixture", user: { username: "fixture", role: "admin" } });
        case "/api/chat/history":
          historyGets++;
          return historyRoute(route, () => {
            const exclude = url.searchParams.get("exclude_chat_view_id");
            return saved.filter(row => !exclude || row.chatViewId !== exclude)
              .map(({ role, content }) => ({ role, content }));
          });
        case "/api/dashboard":
          return json(route, { total_cost: 25 });
        case "/api/daily_reports":
          return json(route, []);
        case "/api/chat/stream": {
          const { message, chat_view_id: chatViewId } = request.postDataJSON();
          saved.push({ role: "user", content: message, chatViewId });
          await replyGates.get(message);
          const parts = message === "Split HTML fixture" ? [
            "**正常回复**\n<img src=\"data:image/png,invalid\" ",
            "onerror=\"this.dataset.securityProbe='executed'\">\n[危险链接](javascript:alert(27))",
          ] : ["Reply " + message];
          const reply = parts.join("");
          saved.push({ role: "assistant", content: reply, chatViewId });
          const events = [
            { type: "thinking", content: "Thinking " + message },
            ...parts.map(content => ({ type: "text", content })),
            { type: "done" },
          ];
          return route.fulfill({
            contentType: "text/event-stream",
            body: events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""),
          });
        }
        default:
          return route.fulfill({ status: 404 });
      }
    });

    // Begin on the dashboard so each case can install its history gate before the first chat visit.
    await page.addInitScript(() => sessionStorage.setItem("ad.login", JSON.stringify({
      token: "fixture", user: { username: "fixture", role: "admin" },
    })));
    await page.goto(origin + "/?from=orders&page=dashboard");
    await page.locator("#dashStats").getByText("¥25", { exact: true }).waitFor();

    const chatLink = page.locator('[data-page="chat"]');
    const dashboardLink = page.locator('[data-page="dashboard"]');
    const input = page.locator("#chatInput");
    const bubbles = page.locator("#chatMessages .msg");
    const thinking = page.locator("#chatThinking");
    await test({
      page, chatLink, dashboardLink, bubbles, thinking, saved,
      historyGets: () => historyGets,
      handleHistory: handler => { historyRoute = handler; },
      holdHistory: () => {
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        historyRoute = async (route, rows) => {
          await gate;
          return json(route, rows());
        };
        return release;
      },
      holdReply: message => {
        let release;
        replyGates.set(message, new Promise(resolve => { release = resolve; }));
        return release;
      },
      send: async message => {
        await input.fill(message);
        await Promise.all([
          page.waitForRequest(request => pathname(request) === "/api/chat/stream" &&
            request.postDataJSON().message === message),
          input.press("Enter"),
        ]);
      },
    });
  } finally {
    await page.close();
  }
}

const cases = [
  ["POST and reply while first history GET is pending", async chat => {
    const { page, chatLink, bubbles, thinking, send, holdHistory, historyGets } = chat;
    const release = holdHistory();
    let historyResponses = 0;
    page.on("response", response => { if (historyResponse(response)) historyResponses++; });
    await Promise.all([
      page.waitForRequest(request => pathname(request) === "/api/chat/history"),
      chatLink.click(),
    ]);
    try {
      await send("Pending");
      await bubbles.getByText("Reply Pending", { exact: true }).waitFor();
      await thinking.waitFor({ state: "hidden" });
      assert.deepEqual(await bubbles.allTextContents(), ["Pending", "Reply Pending"]);
      assert.equal(historyGets(), 1);
      assert.equal(historyResponses, 0);
      console.log("POST and reply rendered before the history response (GET=1, responses=0)");
    } finally {
      await Promise.all([page.waitForResponse(historyResponse), release()]);
    }
  }],
  ["503 history GET retries on returning to chat", async chat => {
    const { page, chatLink, dashboardLink, bubbles, saved, handleHistory, historyGets } = chat;
    saved.push({ role: "assistant", content: "Recovered history" });
    handleHistory((route, rows) => historyGets() === 1
      ? route.fulfill({ status: 503, json: { code: 503 } })
      : json(route, rows()));
    const [first] = await Promise.all([page.waitForResponse(historyResponse), chatLink.click()]);
    assert.equal(first.status(), 503);
    await first.finished();
    await dashboardLink.evaluate(link => link.click());
    await chatLink.click();
    await bubbles.getByText("Recovered history", { exact: true }).waitFor()
      .catch(error => { throw new Error(`History GET count after 503 and reentry: ${historyGets()}`, { cause: error }); });
    assert.deepEqual(await bubbles.allTextContents(), ["Recovered history"]);
    assert.equal(historyGets(), 2);
    console.log("History GET 503 → GET 200 (2 requests), recovered history rendered once");
  }],
  ["late history preserves local bubbles and distinct identical history", async chat => {
    const { page, chatLink, dashboardLink, bubbles, saved, send, holdHistory, historyGets } = chat;
    saved.push(
      { role: "user", content: "Repeat" },
      { role: "assistant", content: "Reply Repeat" },
      { role: "assistant", content: "Older history marker" },
    );
    const release = holdHistory();
    await Promise.all([
      page.waitForRequest(request => pathname(request) === "/api/chat/history"),
      chatLink.click(),
    ]);
    try {
      await send("Repeat");
      await bubbles.getByText("Reply Repeat", { exact: true }).waitFor();
      const localReply = await bubbles.nth(1).elementHandle();
      assert.equal(saved.length, 5); // The late snapshot would otherwise include both local rows.
      await dashboardLink.evaluate(link => link.click());
      await chatLink.click();
      await page.waitForTimeout(100); // The navigation loader is scheduled after 50 ms.
      assert.equal(historyGets(), 1);
      await Promise.all([page.waitForResponse(historyResponse), release()]);
      await bubbles.getByText("Older history marker", { exact: true }).waitFor();
      const afterHistory = await bubbles.allTextContents();
      console.log(JSON.stringify({ afterHistory, historyGets: historyGets() }));
      assert.deepEqual(afterHistory, ["Repeat", "Reply Repeat", "Older history marker", "Repeat", "Reply Repeat"]);
      assert.ok(await localReply.evaluate(node => node === document.querySelector("#chatMessages").lastElementChild));
      assert.equal(historyGets(), 1);
    } finally {
      release();
    }
  }],
  ["out-of-order replies stay paired through dashboard navigation", async chat => {
    const { page, chatLink, dashboardLink, bubbles, thinking, send, holdReply, historyGets } = chat;
    const releaseA = holdReply("A");
    const releaseB = holdReply("B");
    await Promise.all([page.waitForResponse(historyResponse), chatLink.click()]);
    await send("A");
    await send("B");
    await page.locator("#dashStats").evaluate(node => node.replaceChildren());
    const [dashboard] = await Promise.all([
      page.waitForResponse(origin + "/api/dashboard"), dashboardLink.evaluate(link => link.click()),
    ]);
    await page.locator("#dashStats").getByText("¥25", { exact: true }).waitFor();
    assert.equal(dashboard.status(), 200);
    console.log(`Dashboard HTTP ${dashboard.status()} and rendered while A and B were pending`);

    releaseB();
    await bubbles.getByText("Reply B", { exact: true }).waitFor({ state: "attached" });
    const afterB = await bubbles.allTextContents();
    const stillThinking = await thinking.evaluate(node => node.style.display !== "none");
    releaseA();
    await page.waitForFunction(() => document.querySelector("#chatThinking").style.display === "none");
    const afterA = await bubbles.allTextContents();
    console.log(JSON.stringify({ afterB, stillThinking, afterA }));
    assert.deepEqual(afterB, ["A", "", "B", "Reply B"]);
    assert.equal(stillThinking, true);
    assert.deepEqual(afterA, ["A", "Reply A", "B", "Reply B"]);

    // Saved rows are in commit order. Returning to chat must preserve the live bubbles.
    await chatLink.click();
    await bubbles.nth(1).waitFor({ state: "visible" });
    await page.waitForTimeout(100); // The page starts its loader after 50 ms.
    assert.deepEqual(await bubbles.allTextContents(), afterA);
    assert.equal(historyGets(), 1);

    await send("Split HTML fixture");
    const answer = bubbles.filter({ has: page.locator("strong", { hasText: "正常回复" }) });
    await answer.waitFor();
    await thinking.waitFor({ state: "hidden" });
    assert.match(await answer.textContent(), /securityProbe='executed'/);
    assert.equal(await answer.locator("img, [onerror], [data-security-probe], a").count(), 0);
    console.log("AD-038 split legacy SSE stayed inert and retained bold Markdown");
  }],
];

async function run() {
  const browser = await chromium.launch(browserOptions());
  console.log("AD_BROWSER " + browser.version());
  let passed = 0;
  try {
    for (const [name, test] of cases) {
      let failure = null;
      try {
        await withChat(browser, test);
        passed++;
        console.log(`PASS ${name}`);
      } catch (error) {
        failure = error.message;
        console.error(`FAIL ${name}: ${error.message}`);
      }
      if (process.env.AD_EVAL_RESULTS) {
        console.log("AD_EVAL_CASE " + JSON.stringify({ name, failure, trace_ids: [] }));
      }
    }
  } finally {
    await browser.close();
  }
  console.log(`${passed}/${cases.length} browser cases passed`);
  if (passed !== cases.length) process.exitCode = 1;
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
