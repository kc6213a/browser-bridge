// 浏览器控制能力探测：用本机 Edge 启动，访问目标页并抓取 DOM 片段
const { chromium } = require('playwright-core');
const fs = require('fs');

const TARGET = process.argv[2] || 'https://chatgpt.com';

(async () => {
  let browser;
  try {
    browser = await chromium.launch({
      channel: 'msedge',
      headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
    console.log('LAUNCH_OK version=' + browser.version());
  } catch (e) {
    console.log('LAUNCH_FAIL ' + e.message);
    process.exit(1);
  }

  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  try {
    const resp = await page.goto(TARGET, { waitUntil: 'domcontentloaded', timeout: 45000 });
    console.log('GOTO_OK status=' + (resp ? resp.status() : 'null') + ' url=' + page.url());
  } catch (e) {
    console.log('GOTO_FAIL ' + e.message);
  }
  await page.waitForTimeout(6000);
  console.log('FINAL_URL ' + page.url());
  console.log('TITLE ' + (await page.title()));

  const dump = await page.evaluate(() => {
    const bodyText = (document.body && document.body.innerText || '').slice(0, 400);
    const el = document.querySelector('main') || document.body;
    return {
      bodyText,
      outerHTML500: el ? el.outerHTML.slice(0, 500) : '',
      msgCandidates: Array.from(document.querySelectorAll('[data-message-id], [data-testid*="message"], article'))
        .length,
    };
  });
  console.log('---- BODY TEXT (400) ----');
  console.log(dump.bodyText);
  console.log('---- OUTER HTML (500) ----');
  console.log(dump.outerHTML500);
  console.log('MSG_CANDIDATES ' + dump.msgCandidates);

  fs.writeFileSync('probe_out.txt', JSON.stringify(dump, null, 2), 'utf-8');
  await browser.close();
})();
