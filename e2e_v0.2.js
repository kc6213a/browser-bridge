// v0.2 端到端验证：以 --load-extension 真实加载 extension/ 目录（不走扩展页 UI），
// 分别在两套自建 DOM 上验证：
//   场景 A（模拟 ChatGPT，带 data-message-author-role） -> 应命中第一层
//   场景 B（只有普通 div，未知站点）                    -> 应命中第二层兜底
// 两个场景都要：content.js 注入 -> 消息 POST /turn -> 自诊断 POST /diagnose -> 服务端落盘。
const { chromium } = require('playwright-core');
const os = require('os');
const path = require('path');
const fs = require('fs');

const EXT = path.resolve(__dirname, 'extension');
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-profile-v02-'));
const URL_A = 'http://localhost:8787/test-page.html?scene=chatgpt';
const URL_B = 'http://localhost:8787/test-page.html?scene=generic';

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

  async function scenario(name, url, clicks) {
    console.log('=== SCENARIO ' + name + ' :: ' + url + ' ===');
    await page.goto(url, { waitUntil: 'load' });
    await page.waitForTimeout(2000);
    console.log('  ready=' + await page.evaluate(
      () => document.documentElement.getAttribute('data-bridge-ready')));
    console.log('  layer=' + await page.evaluate(
      () => document.documentElement.getAttribute('data-bridge-layer')));
    console.log('  site=' + await page.evaluate(
      () => document.documentElement.getAttribute('data-bridge-site')));

    for (const sel of clicks) {
      await page.click(sel);
      await page.waitForTimeout(1200);
    }
    await page.waitForTimeout(1500);
    console.log('  dom_count=' + await page.evaluate(
      () => document.querySelectorAll('#thread > div').length));
    console.log('  layer_after=' + await page.evaluate(
      () => document.documentElement.getAttribute('data-bridge-layer')));
  }

  await scenario('A / 第一层站点专用', URL_A, ['#add-user', '#add-assistant']);
  await scenario('B / 第二层通用启发式', URL_B, ['#add-user', '#add-assistant']);

  await ctx.close();
  console.log('E2E_DONE');
})().catch((e) => {
  console.log('E2E_FAIL ' + e.message);
  process.exit(1);
});
