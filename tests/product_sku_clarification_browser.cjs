const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium, expect } = require('playwright/test');
const { browserOptions } = require('./browser_options.cjs');

async function main() {
  const [, , base, output, conversationId] = process.argv;
  const browser = await chromium.launch(browserOptions());
  try {
    const page = await browser.newPage();
    const loginResponse = await page.request.post(new URL('/api/login', base).href, {
      data: { username: 'product-evidence-owner', password: 'local-product-evidence-fixture' },
    });
    assert.equal(loginResponse.status(), 200);
    const login = (await loginResponse.json()).data;
    await page.addInitScript(session => {
      sessionStorage.setItem('ad.login', JSON.stringify(session));
      sessionStorage.setItem('ad.chatPage', 'groups');
    }, login);

    const scope = '商品资料：t1-harbor US';
    const question = 'What is the beam angle at half peak intensity?';
    const input = page.locator('#chatInput');
    const group = page.locator(`#chatGroupList [data-conversation-id="${conversationId}"]`);
    const prompt = page.locator('#chatMessages .msg.assistant').filter({ hasText: '请选择一个 SKU' }).last();
    const results = {};
    for (const mode of ['workgroup', 'chat']) {
      if (mode === 'chat') {
        // The same composer serves the legacy chat when the group API is unavailable.
        await page.route('**/api/chat/conversations', route => route.fulfill({ status: 404, json: {} }));
      }
      await page.goto(base, { waitUntil: 'domcontentloaded' });
      if (mode === 'workgroup') await group.click();
      await expect(input).toBeEnabled();
      await input.fill(scope);
      await input.press('Shift+Enter');
      await input.pressSequentially(question);
      await expect(input).toHaveValue(scope + '\n' + question);
      const [request] = await Promise.all([
        page.waitForRequest(r => new URL(r.url()).pathname === '/api/chat/stream'),
        input.press('Enter'),
      ]);
      const body = request.postDataJSON();
      assert.equal(body.message, scope + '\n' + question);
      assert.equal(body.conversation_id, mode === 'workgroup' ? conversationId : undefined);
      await expect(prompt).toHaveAttribute('data-reply-status', '回复已保存');
      await expect(prompt).toContainText('ArcTrail 折叠工作灯 · ARC-220');
      await expect(prompt).toContainText('ArcTrail 折叠工作灯 · ARC-480');
      await expect(prompt.locator('button[data-product-chunk]')).toHaveCount(0);
      const before = await prompt.innerText();
      await page.reload({ waitUntil: 'domcontentloaded' });
      if (mode === 'workgroup') await group.click();
      await expect(prompt).toHaveText(before, { useInnerText: true });
      results[mode] = { request: body, before_reload: before, after_reload: await prompt.innerText() };
    }
    fs.writeFileSync(output, JSON.stringify({ browser: browser.version(), ...results }, null, 2) + '\n');
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
