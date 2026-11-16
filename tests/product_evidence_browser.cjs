const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium, expect } = require('playwright/test');
const { browserOptions } = require('./browser_options.cjs');

async function main() {
  const [, , base, output, conversationId] = process.argv;
  const browser = await chromium.launch(browserOptions());
  try {
    const page = await browser.newPage();
    page.on('pageerror', error => console.error(error.message));
    const response = await page.request.post(base + '/api/login', {
      data: { username: 'product-evidence-owner', password: 'local-product-evidence-fixture' }
    });
    const login = (await response.json()).data;
    await page.addInitScript(session => {
      sessionStorage.setItem('ad.login', JSON.stringify(session));
      sessionStorage.setItem('ad.chatPage', 'groups');
    }, login);
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    await page.locator(`#chatGroupList [data-conversation-id="${conversationId}"]`).click();
    const input = page.locator('#chatInput');
    await expect(input).toBeEnabled();
    await input.fill('商品资料：evidence-main US METRO-500 接触饮料的内胆是什么材质？');
    await page.locator('#chatSend').click();
    const button = page.locator('#chatMessages button[data-product-chunk]').first();
    await button.waitFor({ state: 'visible' });
    const chunkId = await button.getAttribute('data-product-chunk');
    const [lookup] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/chunks/' + chunkId)), button.click(),
    ]);
    assert.equal(lookup.status(), 200);
    assert.equal(lookup.headers()['cache-control'], 'no-store');
    const source = await lookup.json();
    const excerpt = page.locator('#chatMessages [data-product-original]').first();
    await excerpt.waitFor({ state: 'visible' });
    assert.ok((await excerpt.textContent()).includes('page:4'));
    assert.ok((await excerpt.textContent()).includes('304 stainless steel'));
    await button.click();
    await excerpt.waitFor({ state: 'hidden' });
    assert.equal(await excerpt.textContent(), '');
    fs.writeFileSync(output, JSON.stringify({ browser: browser.version(), chunk_id: chunkId,
      version: source.version, location: source.locations[0].location, readback_http: lookup.status(),
      authorization_cache_control: lookup.headers()['cache-control'], collapse_clears_text: true }, null, 2) + '\n');
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
