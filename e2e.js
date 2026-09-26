// 端到端验证：以 --load-extension 真实加载已解压扩展，注入 content.js，
// 在自建测试页触发 3 条消息，验证「网页 DOM -> background -> 本地接收端」全链路。
const { chromium } = require('playwright-core');
const os = require('os');
const path = require('path');
const fs = require('fs');

const EXT = path.resolve(__dirname, 'extension');
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-profile-'));

(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    channel: 'msedge',
    headless: true,
    args: [
      '--no-sandbox',
      '--disable-dev-shm-usage',
      `--load-extension=${EXT}`,
      `--disable-extensions-except=${EXT}`,
    ],
  });

  const page = await ctx.newPage();
  page.on('console', (m) => {
    const t = m.text();
    if (t.includes('browser-bridge')) console.log('  [page] ' + t);
  });

  await page.goto('http://localhost:8788/test-page.html', { waitUntil: 'load' });
  await page.waitForTimeout(1500);
  console.log('BRIDGE_READY=' + await page.evaluate(
    () => document.documentElement.getAttribute('data-bridge-ready')));

  for (let i = 1; i <= 3; i++) {
    await page.click('#send');
    await page.waitForTimeout(600);
  }
  await page.waitForTimeout(1500);

  const domCount = await page.evaluate(() => document.querySelectorAll('.msg').length);
  console.log('DOM_MSG_COUNT=' + domCount);

  // 顺带验证 edge://extensions 内部页是否可导航
  try {
    await page.goto('edge://extensions', { timeout: 8000 });
    console.log('EDGE_EXTENSIONS_NAV=OK');
  } catch (e) {
    console.log('EDGE_EXTENSIONS_NAV=FAIL ' + e.message.split('\n')[0]);
  }

  await ctx.close();
})().catch((e) => {
  console.log('E2E_FAIL ' + e.message);
  process.exit(1);
});
