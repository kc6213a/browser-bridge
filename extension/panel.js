/**
 * Browser Bridge side panel (Sprint 5 / v0.1)
 *
 * 作用：把本地接收端 received/runtime/state.json 渲染成一个固定在页面右侧的浮层，
 * 让用户第一次「用眼睛」看见系统在工作（假设 / 结论 / 依赖 / 最近变化）。
 *
 * 设计约束（来自 Sprint 5 任务书，红线不可破）：
 *  - 只在 chatgpt / openai / claude / deepseek 注入（manifest 已按 host 限定；
 *    此处再设一道 host 守卫作为兜底）。测试页可加 html[data-bridge-state-url]
 *    覆盖请求地址并放开 host 限制（仅供本地验证，真实站点无此属性）。
 *  - 每 5 秒轮询 fetch http://localhost:8787/state（够用，不引入 SSE/WebSocket）。
 *  - v0.1 只四块：假设 / 结论 / 依赖 / 最近变化。不做议题树、分支视图、节点折叠、
 *    点击跳转、编辑、样式动画、主题切换。
 *  - 降级不崩：state 为空显示「尚无数据」；server 挂了显示「未连接」。
 *  - 深浅色跟系统默认（prefers-color-scheme），不强制。
 *  - 可折叠（最小化到头部条）/ 可关闭（× 隐藏，刷新页面恢复，不持久化偏好）。
 */
(() => {
  'use strict';

  // ---------------------------------------------------------------
  // 注入守卫：避免重复注入；仅在允许的站点（或测试覆盖）运行
  // ---------------------------------------------------------------
  if (window.__bridgePanelLoaded) return;
  window.__bridgePanelLoaded = true;

  const ALLOWED_HOSTS = ['chatgpt.com', 'chat.openai.com', 'claude.ai', 'chat.deepseek.com'];
  const OVERRIDE = (() => {
    try { return document.documentElement.getAttribute('data-bridge-state-url'); }
    catch (e) { return null; }
  })();
  const TEST_MODE = !!OVERRIDE;

  const host = location.hostname || '';
  const hostOk = ALLOWED_HOSTS.some((h) => host === h || host.endsWith('.' + h));
  if (!TEST_MODE && !hostOk) return; // 真实站点且不在白名单 -> 不注入

  const BASE = (OVERRIDE || 'http://127.0.0.1:8787').replace(/\/+$/, '');
  // 按 hostname 分区：server 侧每个 host 一套 state，面板只拿自己所在站点那份。
  // 「host 是哪个」由浏览器给出，面板不做任何判断/过滤 —— 那是 server 的事。
  const STATE_URL = BASE + '/state?host=' + encodeURIComponent(location.hostname || '');
  const POLL_MS = 5000;

  // ---------------------------------------------------------------
  // 颜色（跟系统深浅色，无强制、无动画）
  // ---------------------------------------------------------------
  function palette() {
    const dark = window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (dark) {
      return {
        panelBg: '#1e1e22', headerBg: '#2a2a30', bodyBg: '#1e1e22',
        text: '#e6e6e6', sub: '#a0a0a8', border: '#3a3a42',
        stale: '#ffcc66', link: '#7aa7ff', accent: '#8ab4f8',
      };
    }
    return {
      panelBg: '#ffffff', headerBg: '#f2f3f5', bodyBg: '#ffffff',
      text: '#1f1f24', sub: '#6b6b73', border: '#e2e3e7',
      stale: '#b58900', link: '#1a66ff', accent: '#1a66ff',
    };
  }

  // ---------------------------------------------------------------
  // 构建 DOM（用 createElement + textContent，避免注入风险）
  // ---------------------------------------------------------------
  function el(tag, opts) {
    const n = document.createElement(tag);
    if (opts) {
      if (opts.cls) n.className = opts.cls;
      if (opts.text != null) n.textContent = opts.text;
      if (opts.style) Object.assign(n.style, opts.style);
    }
    return n;
  }

  function buildPanel() {
    const c = palette();

    // 外层 host：暴露在真实 DOM（document.body）仅用于定位，但内部用 Shadow DOM 物理隔离，
    // 让 content.js 的 document.querySelectorAll('div, article') 抓不到面板内容（断回环），
    // 同时隔离 ChatGPT 全局 CSS（样式全部内联进 shadow root）。
    const host = el('div', { cls: '__bb_panel__' });
    host.setAttribute('data-bridge-panel', '1'); // 防御性标记：即便 shadow 失效，content.js 也能据此排除
    Object.assign(host.style, {
      position: 'fixed', top: '0', right: '0', width: '280px',
      maxHeight: '100vh', overflowY: 'auto', zIndex: '2147483600',
      boxSizing: 'border-box',
    });

    const shadow = host.attachShadow({ mode: 'open' });

    // 面板样式全部内联进 shadow root：ChatGPT 的 CSS 无法穿透 shadow 边界。
    const style = document.createElement('style');
    style.textContent = [
      '* { box-sizing: border-box; font: 12px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }',
      '.__bb_panel__ { background: ' + c.panelBg + '; color: ' + c.text +
        '; border-left: 1px solid ' + c.border + '; box-shadow: -2px 0 8px rgba(0,0,0,0.12); }',
      '.__bb_head__ { display: flex; align-items: center; justify-content: space-between;',
      ' padding: 6px 8px; background: ' + c.headerBg + '; border-bottom: 1px solid ' + c.border +
        '; position: sticky; top: 0; }',
      '.__bb_body__ { padding: 8px; }',
      'button { cursor: pointer; border: 1px solid ' + c.border + '; background: ' + c.panelBg +
        '; color: ' + c.text + '; border-radius: 4px; width: 22px; height: 20px; line-height: 1; }',
      'button.__bb_x__ { font-size: 14px; }',
    ].join('\n');
    shadow.appendChild(style);

    // 内部根（位于 shadow 内）：承载 header + body
    const root = el('div', { cls: '__bb_panel__' });

    // 头部（样式来自 shadow <style>，不再用内联）
    const header = el('div', { cls: '__bb_head__' });
    const title = el('span', { text: '系统状态', style: { fontWeight: '600', color: c.text } });
    const btnRow = el('span', { style: { display: 'flex', gap: '4px' } });
    const collapseBtn = el('button', { text: '▾', cls: '__bb_icon__' }); // ▾
    const closeBtn = el('button', { text: '×', cls: '__bb_x__' }); // ×
    btnRow.appendChild(collapseBtn);
    btnRow.appendChild(closeBtn);
    header.appendChild(title);
    header.appendChild(btnRow);

    // 身体
    const body = el('div', { cls: '__bb_body__' });

    root.appendChild(header);
    root.appendChild(body);
    shadow.appendChild(root);

    // 交互：折叠（最小化到头部）/ 关闭（隐藏整个 host，刷新页面恢复）
    collapseBtn.addEventListener('click', () => {
      const collapsed = body.style.display === 'none';
      body.style.display = collapsed ? '' : 'none';
      collapseBtn.textContent = collapsed ? '▾' : '▸'; // ▸
      root.style.maxHeight = collapsed ? '100vh' : 'auto';
    });
    closeBtn.addEventListener('click', () => {
      host.style.display = 'none';
    });

    if (document.body) document.body.appendChild(host);
    return { root, body, host };
  }

  // ---------------------------------------------------------------
  // 渲染四块
  // ---------------------------------------------------------------
  function blockTitle(text, c) {
    const t = el('div', {
      text,
      style: {
        fontSize: '11px', fontWeight: '700', color: c.sub, textTransform: 'uppercase',
        letterSpacing: '0.04em', margin: '10px 0 4px', borderBottom: '1px dashed ' + c.border,
        paddingBottom: '2px',
      },
    });
    return t;
  }

  function row(text, c, extra) {
    const style = Object.assign({
      margin: '2px 0', color: c.text, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
    }, extra || {});
    return el('div', { text, style });
  }

  // 节点最后活跃时间（server 侧附加的 at_last，'YYYY-MM-DD HH:MM'）。
  // 面板横向只有 280px，省掉年份只留 'MM-DD HH:MM'。
  function shortAt(at) {
    return (typeof at === 'string' && at.length >= 16) ? at.slice(5, 16) : '';
  }

  // 在行尾追加灰色小字时间；没有时间戳则原样返回，布局零变化。
  function withAt(nodeEl, node, c) {
    const s = node && shortAt(node.at_last);
    if (!s) return nodeEl;
    nodeEl.appendChild(el('span', {
      text: ' ' + s,
      style: { color: c.sub, fontSize: '11px' },
    }));
    return nodeEl;
  }

  // 按最后活跃时间倒序（最新的在前）。at_last 缺失视为 ''，排到末尾。
  // Array.prototype.sort 在现代 JS 里稳定：时间相同的节点保持 state 原有顺序。
  function sortByLastActive(nodes) {
    return [...nodes].sort((a, b) =>
      (b.at_last || '').localeCompare(a.at_last || '')
    );
  }

  function renderAssumptions(state, body, c) {
    body.appendChild(blockTitle('假设', c));
    const nodes = sortByLastActive((state.nodes || []).filter((n) => n.type === 'assumption'));
    if (!nodes.length) { body.appendChild(row('（无）', c, { color: c.sub })); return; }
    for (const a of nodes) {
      const versions = a.versions || [];
      const active = versions.find((v) => v.status === 'active') || versions[versions.length - 1];
      const ver = active && active.version != null ? active.version : '?';
      const val = active && active.value != null ? String(active.value) : '—';
      body.appendChild(withAt(row(a.title + '  ' + val + '  (v' + ver + ')', c), a, c));
    }
  }

  // Sprint 7：议题区块。
  // Sprint 17（议题树 v0.1）：按 parent_id 缩进显示。
  //   - parent_id 为 null / 指向不存在节点 / 指向自己 -> 顶层，缩进 0
  //   - 有有效 parent_id -> 缩进一级
  //   - v0.1 只支持一级（不递归孙节点）、无折叠、无交互、无关联边。
  // 排序：纯按最后活跃时间倒序，直接按此顺序渲染 —— 不再把子节点挪到父节点旁边
  // （那样会让子议题跟着父议题的时间走，打断时间线）。父子关系只体现在缩进上。
  function renderTopics(state, body, c) {
    body.appendChild(blockTitle('议题', c));
    const all = sortByLastActive((state.nodes || []).filter((n) => n.type === 'topic'));
    if (!all.length) { body.appendChild(row('尚无议题', c, { color: c.sub })); return; }

    const byId = {};
    for (const t of all) { if (t && t.id) byId[t.id] = t; }

    // 与 state_updater 同一口径：父必须真实存在且不是自己，否则当顶层。
    const parentOf = (t) => {
      const pid = t && t.parent_id;
      if (typeof pid !== 'string' || !pid) return null;
      const p = byId[pid];
      return (p && p.id !== t.id) ? pid : null;
    };

    for (const t of all) {
      const isChild = parentOf(t) !== null;
      body.appendChild(
        withAt(row((isChild ? '└ ' : '') + (t.title || ''), c, {
          paddingLeft: isChild ? '14px' : '0px',
          color: isChild ? c.sub : c.text,
        }), t, c)
      );
    }
  }

  function renderConclusions(state, body, c) {
    body.appendChild(blockTitle('结论', c));
    const nodes = sortByLastActive((state.nodes || []).filter((n) => n.type === 'conclusion'));
    if (!nodes.length) { body.appendChild(row('（无）', c, { color: c.sub })); return; }
    for (const cn of nodes) {
      const health = cn.health || '';
      const isStale = /stale/i.test(health);
      const label = cn.title + (health ? '  ⚠ ' + health : ''); // ⚠
      body.appendChild(
        withAt(row(label, c, isStale ? { color: c.stale, fontWeight: '600' } : {}), cn, c)
      );
    }
  }

  function renderDependencies(state, body, c) {
    body.appendChild(blockTitle('依赖', c));
    const edges = (state.edges || []).filter((e) => e.type === 'depends_on');
    if (!edges.length) { body.appendChild(row('（无）', c, { color: c.sub })); return; }
    const titleOf = (id) => {
      const n = (state.nodes || []).find((x) => x.id === id);
      return (n && n.title) ? n.title : (id || '?');
    };
    const groups = {};
    for (const e of edges) {
      const from = e.from || '?';
      (groups[from] = groups[from] || []).push(e.to);
    }
    for (const from of Object.keys(groups)) {
      const deps = groups[from].map(titleOf).join(', ');
      body.appendChild(row(titleOf(from) + '  ←  ' + deps, c)); // ←
    }
  }

  // 过滤 rejected 噪音：judge_result == "rejected" 的行不显示。
  // 行格式：EVENT_TYPE:judge_result@turn=N（无 judge 时形如 EVENT_TYPE@turn=N）。
  function isRejectedNoise(line) {
    if (typeof line !== 'string') return false;
    const beforeAt = line.split('@')[0];
    const m = beforeAt.match(/^([^:]+):(.+)$/);
    if (!m) return false;                       // 没有 judge_result 段 -> 保留
    return m[2].trim().toLowerCase() === 'rejected';
  }

  function renderRecentChanges(state, body, c) {
    body.appendChild(blockTitle('最近变化', c));
    const changes = (state.working_state && state.working_state.recent_changes) || [];
    if (!changes.length) { body.appendChild(row('（无）', c, { color: c.sub })); return; }
    const visible = changes.filter((line) => !isRejectedNoise(line)); // 丢弃 rejected
    if (!visible.length) { body.appendChild(row('（无）', c, { color: c.sub })); return; }
    const last5 = visible.slice(-5).reverse(); // 倒序（过滤后的最新 5 条）
    for (const line of last5) body.appendChild(row(line, c, { color: c.sub }));
  }

  function renderState(state, body, c) {
    body.innerHTML = '';
    if (!state) {
      body.appendChild(row('尚无数据', c, { color: c.sub, fontStyle: 'italic' }));
      return;
    }
    renderAssumptions(state, body, c);
    renderTopics(state, body, c);
    renderConclusions(state, body, c);
    renderDependencies(state, body, c);
    renderRecentChanges(state, body, c);
  }

  function renderDisconnected(body, c) {
    body.innerHTML = '';
    body.appendChild(row('未连接', c, { color: c.sub, fontStyle: 'italic' }));
  }

  // ---------------------------------------------------------------
  // 轮询
  // ---------------------------------------------------------------
  let panel = null;

  function ensurePanel() {
    if (!panel) {
      if (!document.body) {
        document.addEventListener('DOMContentLoaded', () => { panel = buildPanel(); }, { once: true });
        return null;
      }
      panel = buildPanel();
    }
    return panel;
  }

  async function poll() {
    const p = ensurePanel();
    if (!p) return;
    const c = palette();
    try {
      const resp = await fetch(STATE_URL, { cache: 'no-store' });
      if (!resp.ok) { renderDisconnected(p.body, c); return; }
      const data = await resp.json();
      // 约定：缺失文件时 server 返回 {"ok": true, "state": null}
      const state = (data && data.state !== undefined) ? data.state : data;
      renderState(state, p.body, c);
    } catch (e) {
      renderDisconnected(p.body, c);
    }
  }

  function boot() {
    if (document.readyState === 'loading' && !document.body) {
      document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
      start();
    }
  }
  function start() {
    poll();
    setInterval(poll, POLL_MS);
  }

  boot();
})();
