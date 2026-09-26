// Sprint 5 隔离验证：在 8791 上加载 ChatGPT 外观代理页，注入真实 panel.js，
// 轮询 8790(/state)，截图「初态」与「状态更新后」。不触碰 8787（Sprint 4）。
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BR = path.resolve(__dirname, '..');
const PANEL = fs.readFileSync(path.join(BR, 'extension', 'panel.js'), 'utf8');
const OUT = path.resolve(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch({
    channel: 'msedge', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

  await page.goto('http://localhost:8791/cgpt.html', { waitUntil: 'load' });
  await page.addScriptTag({ content: PANEL }); // 模拟 manifest 注入 panel.js
  await page.waitForTimeout(1500); // 等首轮轮询渲染
  await page.screenshot({ path: path.join(OUT, 'panel_v1.png') });

  // 模拟一次 turn 把 state 改写为 v2
  fs.copyFileSync(path.join(__dirname, 'runtime', 'state_v2.json'),
                  path.join(__dirname, 'runtime', 'state.json'));
  await page.waitForTimeout(6500); // > 5s 轮询周期
  await page.screenshot({ path: path.join(OUT, 'panel_v2.png') });

  // 验证面板里确实出现了更新后的「测试字段 2 (v2)」
  const found = await page.evaluate(() => {
    const txt = document.body.innerText || '';
    return {
      hasV2: /测试字段\s+2\s+\(v2\)/.test(txt),
      hasStale: txt.includes('第一年收入') && txt.includes('stale'),
      hasDep: txt.includes('第一年收入') && txt.includes('←'),
    };
  });

  await browser.close();
  console.log('CONSOLE_ERRORS:', JSON.stringify(errors));
  console.log('PANEL_CHECK:', JSON.stringify(found));
  process.exit(found.hasV2 ? 0 : 2);
})().catch((e) => { console.error('VERIFY_ERR', e); process.exit(1); });
