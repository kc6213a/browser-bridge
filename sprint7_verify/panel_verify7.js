// Sprint 7 验收：注入真实 panel.js，确认“议题”区块出现且条数正确。
const { chromium } = require('playwright-core');

const PANEL_JS = 'C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge/extension/panel.js';
const PAGE = 'http://localhost:8791/cgpt7.html';
const OUT = 'C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge/sprint7_verify/panel/shots/panel_topics.png';

(async () => {
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

  await page.goto(PAGE, { waitUntil: 'load' });
  await page.addScriptTag({ path: PANEL_JS });
  await page.waitForTimeout(12000); // 2 个轮询周期 + 缓冲

  const panelText = await page.evaluate(() => {
    const p = document.querySelector('.__bb_panel__');
    return p ? p.innerText : null;
  });

  await page.screenshot({ path: OUT });

  const blocks = panelText ? panelText.split(/\n/) : [];
  const topicIdx = blocks.indexOf('议题');
  const conclIdx = blocks.indexOf('结论');
  const topicLines = (topicIdx >= 0 && conclIdx > topicIdx)
    ? blocks.slice(topicIdx + 1, conclIdx).filter((l) => l.trim())
    : [];
  const hasTopicBlock = topicIdx >= 0;
  const topicCount = topicLines.filter((l) => /\[turn \d+\]/.test(l)).length;
  const orderOk = (() => {
    if (!panelText) return false;
    const a = panelText.indexOf('假设'), t = panelText.indexOf('议题'), c = panelText.indexOf('结论');
    return a >= 0 && a < t && t < c;
  })();

  console.log(JSON.stringify({
    panelPresent: panelText !== null,
    hasTopicBlock,
    orderAssumptionBeforeTopicBeforeConclusion: orderOk,
    topicCount,
    topicLines,
    panelText,
    errors,
  }, null, 2));
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
