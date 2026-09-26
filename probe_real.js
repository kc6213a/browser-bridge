// 验证：带扩展的浏览器访问真实站点 chatgpt.com / claude.ai 时，
// content.js 是否真的被注入（不依赖登录态，只看注入是否发生）。
const { chromium } = require('playwright-core');
const os = require('os');
const path = require('path');
const fs = require('fs');

const EXT = path.resolve(__dirname, 'extension');

(async () => {
  for (const url of ['https://chatgpt.com/', 'https://claude.ai/']) {
    const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-rp-'));
    const ctx = await chromium.launchPersistentContext(PROFILE, {
      channel: 'msedge',
      headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage', `--load-extension=${EXT}`],
    });
    const page = await ctx.newPage();
    let injected = false;
    page.on('console', (m) => {
      if (m.text().includes('[browser-bridge] content script ready')) injected = true;
    });
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    } catch (e) {
      console.log(url + ' GOTO_ERR ' + e.message.split('\n')[0]);
    }
    await page.waitForTimeout(8000);
    try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 }); } catch (e) {}
    await page.waitForTimeout(12000);
    console.log(url + '  final_url=' + page.url().slice(0, 80));
    console.log(url + '  title=' + (await page.title()));
    console.log(url + '  MAIN_DOM_ATTR_ready=' + await page.evaluate(
      () => document.documentElement.getAttribute('data-bridge-ready')));
    const n = await page.evaluate(() =>
      document.querySelectorAll('[data-message-id], [data-testid*="message"], article').length);
    console.log(url + '  MSG_CANDIDATES=' + n);
    console.log(url + '  INJECTED_LOGLINE=' + injected);
    await ctx.close();
  }
})().catch((e) => console.log('PROBE_REAL_FAIL ' + e.message));
