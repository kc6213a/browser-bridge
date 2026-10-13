/**
 * L4（#44/#46 方案 A）真浏览器回归测试
 *
 * 用 playwright-core + 本机 Edge headless，加载自建的「虚拟滚动模拟页」，
 * 把真实 extension/content.js 注入进去跑，验证方案 A 的机制。
 *
 * ⚠️ 边界（说清楚，别自欺）：
 *   本测试验的是**机制**——分步下滚、累积采集、去重、恢复 scrollTop、
 *   observer 暂停/恢复、非虚拟滚动站点 no-op。它用的是**模拟的**虚拟列表，
 *   **不等于真实 DeepSeek**。真机验收（滚到底是否抓到全部消息）仍需人工配合，
 *   见 docs/design_freeze_4446.md §6。
 *
 * 用法：node e2e_virtual_list/run.js
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const HERE = __dirname;
const CONTENT_JS = fs.readFileSync(path.join(HERE, '..', 'extension', 'content.js'), 'utf8');

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log((ok ? '  ✅ ' : '  ❌ ') + name + (detail ? '  — ' + detail : ''));
}

const CHROME_STUB = () => {
  window.__posts = [];
  window.chrome = {
    runtime: {
      id: 'e2e-test',
      sendMessage: function (payload, cb) {
        window.__posts.push(payload);
        if (cb) cb({ ok: true, server: { status: 200 } });
      },
    },
  };
};

async function openPage(browser, file) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.addInitScript(CHROME_STUB);
  await page.goto('file://' + path.join(HERE, file), { waitUntil: 'load' });
  await page.addScriptTag({ content: CONTENT_JS });
  return { ctx, page };
}

/** 等 boot 的自动 fullScan 真正结束。
 *  不能用「POST 静默」判断——diagnose 只在消息条数变化时才发，而虚拟列表的
 *  DOM 条数本来就恒定，大多数步骤不发 POST，静默检测会提前 break，
 *  导致手动 fullScan 和自动的撞车（实测踩过）。改用 console 的 done 日志。 */
function waitAutoFullScanDone(page, timeoutMs) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { cleanup(); resolve(false); }, timeoutMs || 30000);
    function onConsole(msg) {
      if (msg.text().indexOf('[browser-bridge] fullScan done') >= 0) {
        cleanup(); resolve(true);
      }
    }
    function cleanup() {
      clearTimeout(timer);
      page.off('console', onConsole);
    }
    page.on('console', onConsole);
  });
}

function turnsOf(page) {
  return page.evaluate(() => window.__posts.filter((p) => p.type === 'BRIDGE_TURN'));
}
function diagnosesOf(page) {
  return page.evaluate(() => window.__posts.filter((p) => p.type === 'BRIDGE_DIAGNOSE'));
}

(async () => {
  const browser = await chromium.launch({
    executablePath: fs.existsSync(EDGE) ? EDGE : undefined,
    headless: true,
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });

  // ============================================================
  // 场景 1：虚拟滚动页（DeepSeek 形态）—— 方案 A 主路径
  // ============================================================
  console.log('\n=== 场景 1：虚拟滚动页（40 条消息，视口仅 ~3 条）===');
  {
    const { ctx, page } = await openPage(browser, 'page.html');

    // 等 boot 的自动 fullScan 真正结束（console done 日志，非 POST 静默）
    const autoDone = await waitAutoFullScanDone(page, 30000);
    check('A0 boot 后自动 fullScan 被触发并跑完', autoDone === true);

    const beforeManual = await turnsOf(page);
    const uniqueTexts = new Set(beforeManual.map((t) => t.text));
    const ids = beforeManual.map((t) => t.message_id);
    const uniqueIds = new Set(ids);

    check('A1 全量捕获：boot 自动 fullScan 后 40 条消息全部入库',
      uniqueTexts.size === 40, 'unique=' + uniqueTexts.size + '/40');

    // 防呆：必须走 L1（站点专用层），不能悄悄退化到 L2 通用层。
    // 踩过：data-bridge-site 写在 <body> 上而 siteKey() 读 <html>，
    // 导致整个用例跑的是 L2（角色按索引奇偶交替），翻转角色+结果不稳定全是 L2 产物。
    const diagsEarly = await diagnosesOf(page);
    const layers = new Set(diagsEarly.map((d) => d.report && d.report.layer));
    check('A1b 走的是 L1 站点专用层（非 L2 通用层）',
      layers.size === 1 && layers.has(1), 'layers=' + JSON.stringify([...layers]));

    check('A2 无重复：turn 的 message_id 无重复',
      uniqueIds.size === ids.length, 'ids=' + ids.length + ' unique=' + uniqueIds.size);

    // 防呆 2：同一条消息不能被翻转角色采集两次（role+text 都该唯一）。
    // L2 通用层按索引奇偶分角色，DOM 一变角色就翻——这条会抓住它。
    const pairs = beforeManual.map((t) => t.role + '|' + t.text);
    const uniquePairs = new Set(pairs);
    check('A2b 无「同文本翻转角色」重复采集',
      uniquePairs.size === pairs.length,
      'pairs=' + pairs.length + ' unique=' + uniquePairs.size);

    // 手动再跑一次：应 ran=true 且不新增任何 turn（去重生效）
    const manual = await page.evaluate(() => window.__bridgeFullScan());
    const afterManual = await turnsOf(page);
    check('A3 二次 fullScan 不产生新 turn（sentIds 去重生效）',
      afterManual.length === beforeManual.length,
      'before=' + beforeManual.length + ' after=' + afterManual.length);
    check('A4 手动 fullScan 正确识别虚拟列表容器',
      manual && manual.ran === true, 'ran=' + (manual && manual.ran) +
      ' steps=' + (manual && manual.steps));

    // scrollTop 恢复
    const scrollTop = await page.evaluate(() => {
      const s = document.getElementById('scroller');
      return s.scrollTop;
    });
    check('A5 fullScan 结束后 scrollTop 恢复原值(0)', scrollTop === 0, 'scrollTop=' + scrollTop);

    // observer 已恢复：新增消息应被采到
    const postsBeforeMutate = (await turnsOf(page)).length;
    await page.evaluate(() => {
      const d = document.createElement('div');
      d.className = 'ds-message';
      d.textContent = 'MSG-NEW-after-fullscan unique-zzz';
      document.getElementById('spacer').appendChild(d);
    });
    await page.waitForTimeout(1500);
    const postsAfterMutate = await turnsOf(page);
    check('A6 observer 已恢复：fullScan 后新增消息仍被采集',
      postsAfterMutate.length > postsBeforeMutate,
      'before=' + postsBeforeMutate + ' after=' + postsAfterMutate.length);

    // diagnose 报 virtual_list（缺口可见）
    const diags = await diagnosesOf(page);
    const withVL = diags.filter((d) => d.report && d.report.virtual_list);
    check('A7 diagnose 上报 virtual_list（缺口可见）',
      withVL.length > 0, withVL.length + '/' + diags.length + ' 条含 virtual_list');

    await ctx.close();
  }

  // ============================================================
  // 场景 2：非虚拟滚动页（ChatGPT 形态）—— 必须零影响
  // ============================================================
  console.log('\n=== 场景 2：非虚拟滚动页（ChatGPT 形态）—— 回归闸门：零影响 ===');
  {
    const { ctx, page } = await openPage(browser, 'page_no_vl.html');
    await page.waitForTimeout(2500);   // 等 boot + 自动 fullScan(no-op)

    const turns = await turnsOf(page);
    const texts = new Set(turns.map((t) => t.text));
    check('B1 全部消息正常采集（不受 L4 影响）', texts.size === 4,
      'unique=' + texts.size + '/4');

    const manual = await page.evaluate(() => window.__bridgeFullScan());
    check('B2 无虚拟列表标记 → fullScan no-op（ran=false）',
      manual && manual.ran === false, JSON.stringify(manual));

    const turns2 = await turnsOf(page);
    check('B3 no-op 后无新增 turn（不重复采集）',
      turns2.length === turns.length, 'before=' + turns.length + ' after=' + turns2.length);

    const diags = await diagnosesOf(page);
    const withVL = diags.filter((d) => d.report && d.report.virtual_list);
    check('B4 diagnose 的 virtual_list 为 null（非虚拟滚动站点）',
      withVL.length === 0 && diags.length > 0,
      diags.length + ' 条 diagnose，含 virtual_list ' + withVL.length + ' 条');

    // observer 仍工作
    const before = (await turnsOf(page)).length;
    await page.evaluate(() => {
      const d = document.createElement('div');
      d.className = 'msg';
      d.setAttribute('data-message-author-role', 'user');
      d.textContent = 'CG-NEW unique-qqq';
      document.getElementById('list').appendChild(d);
    });
    await page.waitForTimeout(1200);
    const after = await turnsOf(page);
    check('B5 observer 仍工作：新增消息被采集', after.length > before,
      'before=' + before + ' after=' + after.length);

    await ctx.close();
  }

  await browser.close();

  // ============================================================
  const failed = results.filter((r) => !r.ok);
  console.log('\n=== 汇总：' + (results.length - failed.length) + '/' + results.length + ' 通过 ===');
  if (failed.length) {
    console.log('失败项：');
    failed.forEach((f) => console.log('  ❌ ' + f.name + ' — ' + f.detail));
    process.exit(1);
  }
  console.log('全部通过。');
})().catch((e) => {
  console.error('FATAL', e);
  process.exit(1);
});
