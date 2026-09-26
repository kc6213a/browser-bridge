// v0.3 端到端验证：真实 Edge + --load-extension 加载 extension/ 目录，打到本机 8787 接收端。
//
//   场景 A（?scene=chatgpt&seed=0）：3 条不同 data-message-id 的消息
//        -> received/turns.jsonl 应恰好 3 条；第 4 条复用 msg-1 的 id 应被去重；
//           服务端也要能挡住重复 POST；/diagnose 里 message_id_source = attr:data-message-id
//   场景 B（?scene=generic&seed=0）：DOM 上根本没有 data-message-id
//        -> /diagnose 里 message_id_source 必须如实报 fallback:hash(role+text)
//
// 用法： SCENE=A node e2e_v0.3.js   |   SCENE=B node e2e_v0.3.js
const { chromium } = require('playwright-core');
const os = require('os');
const path = require('path');
const fs = require('fs');

const EXT = path.resolve(__dirname, process.env.EXT_DIR || 'extension');
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-profile-v03-'));
const BASE = process.env.BASE || 'http://localhost:8787';
const RECV_DIR = process.env.RECV_DIR
  ? path.resolve(process.env.RECV_DIR)
  : path.join(__dirname, 'received');
const SCENE = (process.env.SCENE || 'A').toUpperCase();

const URL_A = BASE + '/test-page.html?scene=chatgpt&seed=0';
const URL_B = BASE + '/test-page.html?scene=generic&seed=0';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 扩展侧有 debounce（800ms，最长 3s 强制触发），消息不是点击即到。
// 断言必须「等条件达成」，不能抢跑。
async function waitFor(fn, timeoutMs = 10000, step = 250) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (fn()) return true;
    if (Date.now() > deadline) return false;
    await sleep(step);
  }
}

async function jget(u) {
  const r = await fetch(u);
  return r.json();
}
async function jpost(u, body) {
  const r = await fetch(u, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

function latestDiagnose() {
  const files = fs.readdirSync(RECV_DIR)
    .filter((f) => f.startsWith('diagnose_') && f.endsWith('.json'))
    .sort();
  if (!files.length) return null;
  const p = path.join(RECV_DIR, files[files.length - 1]);
  return { file: files[files.length - 1], json: JSON.parse(fs.readFileSync(p, 'utf8')) };
}

function turns() {
  const p = path.join(RECV_DIR, 'turns.jsonl');
  if (!fs.existsSync(p)) return [];
  return fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

(async () => {
  const health = await jget(BASE + '/health');
  console.log('HEALTH ' + JSON.stringify(health));

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

  let failures = 0;
  function check(name, cond, extra) {
    console.log((cond ? '  PASS ' : '  FAIL ') + name + (extra ? ' :: ' + extra : ''));
    if (!cond) failures++;
  }

  if (SCENE === 'A') {
    const before = turns().length;
    console.log('=== SCENE A :: ' + URL_A + ' ===');
    await page.goto(URL_A, { waitUntil: 'load' });
    await sleep(2000);
    console.log('  ready=' + await page.evaluate(() => document.documentElement.getAttribute('data-bridge-ready')));
    console.log('  layer=' + await page.evaluate(() => document.documentElement.getAttribute('data-bridge-layer')));

    // 1) user: 预算是200万
    await page.fill('#custom-text', '预算是200万，我们按这个假设继续测算。');
    await page.click('#add-custom');
    await sleep(1600);
    // 2) assistant: 普通消息
    await page.click('#add-assistant');
    await sleep(1600);
    // 3) user: 假设改成100万 + 引用过去（应触发真实事件判定）
    await page.fill('#custom-text', '假设预算改成100万，之前我们讨论过预算是200万。');
    await page.click('#add-custom');
    await sleep(2600);

    // 等第 3 条真正抵达（debounce 上限 3s），再断言
    const arrived = await waitFor(() => turns().length >= before + 3, 12000);
    console.log('  waitFor 3 turns -> ' + arrived);

    console.log('  dom_count=' + await page.evaluate(() => document.querySelectorAll('#thread > div').length));

    let recs = turns();
    let added = recs.slice(before);
    console.log('  turns.jsonl total=' + recs.length + ' new=' + added.length);
    for (const r of added) {
      console.log('    #' + r._turn + ' mid=' + r.message_id + ' src=' + r.message_id_source +
        ' role=' + r.role + ' runtime=' + r._runtime.status +
        ' records=' + r._runtime.records.length + ' decisions=' + r._runtime.decisions.length);
      for (const d of r._runtime.decisions) {
        console.log('       decision: ' + JSON.stringify({
          type: d.type, judge: d.judge_result, conf: d.confidence,
          rule: d.rule_id, level: d.level, actions: d.actions,
        }));
        if (d.reason) console.log('       reason: ' + String(d.reason).slice(0, 90));
      }
    }

    check('3 条不同 message_id -> 新增恰好 3 条', added.length === 3, 'got ' + added.length);
    check('message_id 互不相同', new Set(added.map((r) => r.message_id)).size === added.length);
    check('message_id_source = attr:data-message-id',
      added.every((r) => r.message_id_source === 'attr:data-message-id'),
      JSON.stringify(added.map((r) => r.message_id_source)));
    check('Runtime 真的被调用（无 not-called / 无 error）',
      added.every((r) => r._runtime && r._runtime.status !== 'not-called'));
    check('第 3 条产出真实事件记录（真模型判定）',
      added.some((r) => r._runtime.records.length > 0),
      'records=' + added.reduce((n, r) => n + r._runtime.records.length, 0));

    // 4) 页面内复用 msg-1 的 id -> 扩展侧去重
    await page.click('#add-dup');
    await sleep(2600);
    console.log('  after #add-dup dom_count=' + await page.evaluate(() => document.querySelectorAll('#thread > div').length));
    let recs2 = turns();
    check('页面内重复 id 不产生第 4 条', recs2.length === before + 3, 'total=' + recs2.length);

    // 5) 直接重复 POST（绕过扩展）-> 服务端去重
    const dupResp = await jpost(BASE + '/turn', {
      type: 'BRIDGE_TURN', source: 'e2e://direct', role: 'user',
      text: '预算是200万，我们按这个假设继续测算。',
      message_id: 'msg-1', message_id_source: 'attr:data-message-id',
    });
    console.log('  direct repost -> ' + JSON.stringify(dupResp));
    check('服务端对同一 message_id 重复 POST 返回 duplicate=true', dupResp.duplicate === true);
    check('重复 POST 后 turns.jsonl 仍为 ' + (before + 3) + ' 条',
      turns().length === before + 3, 'total=' + turns().length);

    const diag = latestDiagnose();
    console.log('  latest diagnose = ' + (diag && diag.file));
    if (diag) {
      console.log('    message_id_source = ' + diag.json.message_id_source);
      console.log('    message_id_sources = ' + JSON.stringify(diag.json.message_id_sources));
      console.log('    sample_message_ids = ' + JSON.stringify(diag.json.sample_message_ids));
    }
    check('/diagnose 上报 message_id_source',
      !!diag && String(diag.json.message_id_source).indexOf('attr:data-message-id') >= 0);
  } else {
    console.log('=== SCENE B :: ' + URL_B + ' ===');
    await page.goto(URL_B, { waitUntil: 'load' });
    await sleep(2000);
    console.log('  ready=' + await page.evaluate(() => document.documentElement.getAttribute('data-bridge-ready')));
    console.log('  layer=' + await page.evaluate(() => document.documentElement.getAttribute('data-bridge-layer')));

    await page.click('#add-user');
    await sleep(2600);
    await page.click('#add-assistant');
    await sleep(2600);

    const recs = turns();
    const last2 = recs.slice(-2);
    for (const r of last2) {
      console.log('    #' + r._turn + ' mid=' + r.message_id + ' src=' + r.message_id_source +
        ' layer=' + r.layer + ' runtime=' + r._runtime.status);
    }
    check('无 data-message-id 时退化为 fallback:hash(role+text)',
      last2.length > 0 && last2.every((r) => r.message_id_source === 'fallback:hash(role+text)'),
      JSON.stringify(last2.map((r) => r.message_id_source)));

    const diag = latestDiagnose();
    console.log('  latest diagnose = ' + (diag && diag.file));
    if (diag) {
      console.log('    message_id_source = ' + diag.json.message_id_source);
      console.log('    message_id_sources = ' + JSON.stringify(diag.json.message_id_sources));
      console.log('    sample_message_ids = ' + JSON.stringify(diag.json.sample_message_ids));
    }
    check('/diagnose 如实上报 fallback 来源',
      !!diag && String(diag.json.message_id_source).indexOf('fallback:hash(role+text)') >= 0);
  }

  await ctx.close();
  console.log(failures === 0 ? 'E2E_DONE (all checks passed)' : 'E2E_DONE with ' + failures + ' FAILED checks');
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.log('E2E_FAIL ' + e.message);
  process.exit(1);
});
