// Sprint 6 Goal B 验证：注入真实 panel.js，确认“最近变化”里不再有 rejected。
const { chromium } = require('playwright-core');
const path = require('path');

const PANEL_JS = 'C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge/extension/panel.js';
const PAGE = 'http://localhost:8791/cgpt6.html';
const OUT = 'C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge/sprint6_verify/panel/shots/panel_filtered.png';

(async () => {
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

  await page.goto(PAGE, { waitUntil: 'load' });
  await page.addScriptTag({ path: PANEL_JS });
  // 等 2 个轮询周期（5s）+ 缓冲
  await page.waitForTimeout(12000);

  const panelText = await page.evaluate(() => {
    const p = document.querySelector('.__bb_panel__');
    return p ? p.innerText : null;
  });

  await page.screenshot({ path: OUT });
  const hasRejected = panelText ? /rejected/i.test(panelText) : null;
  const hasAccepted = panelText ? /ASSUMPTION_CHANGED:accepted|DEPENDENCY_DECLARED:accepted|CONCLUSION_STALE:derived|NEW_TOPIC@turn/i.test(panelText) : null;

  console.log(JSON.stringify({
    panelPresent: panelText !== null,
    hasRejected,
    hasAccepted,
    panelText,
    errors,
  }, null, 2));
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
