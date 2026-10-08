/**
 * Browser Bridge content script (v0.2)
 *
 * 三层结构：
 *   [L1] 站点专用选择器  —— 已知稳定属性（data-message-author-role / data-testid 等）
 *   [L2] 通用启发式      —— 站点专用全部落空时的兜底：div/article + 长度过滤 + 交替角色
 *   [L3] 自诊断          —— 每次扫描都把「命中了哪层、用了哪个选择器、抓到什么」POST 回本地
 *
 * 设计原则：不猜未知站点的 DOM，改版失效时让扩展自己把新 DOM 结构报回来（第三层）。
 * 用户全程不需要 F12。
 */
(() => {
  'use strict';

  // ---------------------------------------------------------------
  // [L1] 站点专用选择器
  // ---------------------------------------------------------------
  const SITE_SELECTORS = {
    "chatgpt.com": {
      roots: ["[data-message-author-role]"],
      roleAttr: "data-message-author-role",
      textSelector: ".markdown, .whitespace-pre-wrap, [data-message-content]",
    },
    "chat.openai.com": {
      roots: ["[data-message-author-role]"],
      roleAttr: "data-message-author-role",
      textSelector: ".markdown, .whitespace-pre-wrap, [data-message-content]",
    },
    "claude.ai": {
      roots: [
        "[data-testid='user-message']",
        "[data-testid='assistant-message']",
        ".font-claude-message, .font-user-message",
      ],
      roleFromSelector: true,
      textSelector: ".prose, [data-testid='message-content']",
    },
    "chat.deepseek.com": {
      roots: [".ds-message"],
      roleFromContent: true,
      assistantSelector: ".ds-markdown, .ds-think-content",
      textSelector: ".ds-markdown, .ds-think-content",
      // 注意：.ds-markdown 在 thinking 容器里也有一份（假 markdown），DOM 序在正式回答之前。
      // 正式回答的 markdown 类名带 ds-assistant-message-* 后缀，用类名特征排除 thinking。
      assistantPrimary: '[class*="ds-assistant-message"]',
      noFallbackToGeneric: true,
    },
  };

  const SOURCE = location.hostname || location.href;

  // 测试页钩子：自建 test-page.html 跑在 localhost 上，用它声明「我模拟的是哪个站点」，
  // 才能在本地验证第一层逻辑。真实站点不使用此钩子。
  function siteKey() {
    try {
      const override = document.documentElement.getAttribute('data-bridge-site');
      if (override) return override;
    } catch (e) { /* ignore */ }
    return location.hostname || '';
  }

  function lookupSiteConfig(hostname) {
    const host = String(hostname || '').toLowerCase();
    for (const key of Object.keys(SITE_SELECTORS)) {
      if (host === key || host.endsWith('.' + key)) return SITE_SELECTORS[key];
    }
    return null;
  }

  // ---------------------------------------------------------------
  // [L1] 采集
  // ---------------------------------------------------------------
  function roleFromSelectorText(sel) {
    const s = String(sel || '').toLowerCase();
    if (s.indexOf('user') >= 0) return 'user';
    if (s.indexOf('assistant') >= 0 || s.indexOf('claude') >= 0) return 'assistant';
    return 'unknown';
  }

  function collectSiteLayer(cfg) {
    if (!cfg || !cfg.roots) return null;
    for (const rawSel of cfg.roots) {
      let nodes;
      try {
        nodes = Array.prototype.slice.call(document.querySelectorAll(rawSel));
      } catch (e) {
        continue; // 非法选择器，换下一个
      }
      if (!nodes.length) continue;

      const items = [];
      for (const el of nodes) {
        let role = 'unknown';
        if (cfg.roleAttr) {
          role = el.getAttribute(cfg.roleAttr) || 'unknown';
        } else if (cfg.roleFromSelector) {
          // role 从命中的选择器推断
          role = roleFromSelectorText(rawSel);
          if (role === 'unknown' && el.className) {
            role = roleFromSelectorText(
              typeof el.className === 'string' ? el.className : ''
            );
          }
        } else if (cfg.roleFromContent) {
          // DeepSeek 无稳定 role 属性：assistant 消息含 .ds-markdown/.ds-think-content，user 不含
          role = el.querySelector(cfg.assistantSelector) ? 'assistant' : 'user';
        }
        let node = el;
        // DeepSeek：优先取正式回答块，thinking 不进正文
        if (cfg.assistantPrimary) {
          node = el.querySelector(cfg.assistantPrimary) || node;
        } else {
          const t = el.querySelector(cfg.textSelector);
          if (t) node = t;
        }
        const text = (node.innerText || node.textContent || '').trim();
        if (!text) continue;
        items.push({ el, role, text });
      }
      if (items.length) return { selectorUsed: rawSel, items };
    }
    return null;
  }

  // ---------------------------------------------------------------
  // [L2] 通用启发式（站点专用全部落空时启用）
  // ---------------------------------------------------------------
  const NAV_FILTER = 'nav, header, footer, aside, .sidebar, [role="navigation"], ' +
    '[role="banner"], [role="complementary"], [role="contentinfo"], script, style';
  const MIN_LEN = 4;
  const MAX_LEN = 5000;

  // ChatGPT / 通用 UI chrome 特征词：命中即视为非消息（顶栏 / 侧边栏 / 建议条 / 登录区等）。
  // 真实对话文本几乎不会包含这些词；即便个别包含，最坏只是漏抓一条兜底候选，影响极小。
  const CHROME_NOISE = [
    '新聊天', '定时任务', '插件', '升级', '资料库',
    '登录', '注册', '订阅', 'Upgrade', 'Log in', 'Sign up',
    '今天有什么计划？', '你今天在想些什么？',   // #14：ChatGPT 输入框占位文案
  ];

  function isChromeNoise(text) {
    for (const kw of CHROME_NOISE) {
      if (text.indexOf(kw) >= 0) return true;
    }
    return false;
  }

  function collectGenericLayer() {
    let all;
    try {
      all = Array.prototype.slice.call(document.querySelectorAll('div, article'));
    } catch (e) {
      return { selectorUsed: null, items: [] };
    }

    // 过滤 1：长度 + 非导航区 + 非 bridge 面板
    let cands = all.filter((el) => {
      if (el.closest && el.closest(NAV_FILTER)) return false;       // 导航/侧边/页脚等
      if (el.closest && el.closest('[data-bridge-panel]')) return false; // bridge 自己的面板（防御性；Shadow DOM 已物理隔离）
      const t = (el.innerText || el.textContent || '').trim();
      if (!t) return false;
      if (t.length < MIN_LEN || t.length > MAX_LEN) return false;   // 太短 / 太长
      return true;
    });
    if (!cands.length) return { selectorUsed: null, items: [] };

    // 过滤 2：只保留疑似消息（排除 UI 控件 + chrome 特征词）
    cands = cands.filter((el) => {
      if (el.getAttribute && (
        el.getAttribute('role') === 'button' || el.hasAttribute('tabindex')
      )) return false;                                              // 按钮 / 可聚焦控件不是消息
      // #14：排除输入框 / contenteditable 内的文本（ChatGPT 输入框占位文案）
      if (el.closest && el.closest('[contenteditable="true"], [role="textbox"]')) return false;
      const t = (el.innerText || el.textContent || '').trim();
      if (isChromeNoise(t)) return false;                          // UI chrome 文本
      return true;
    });
    if (!cands.length) return { selectorUsed: null, items: [] };

    // 过滤 3：去掉「祖先包着另一个候选」的外层容器，只保留最内层文本块
    const set = new Set(cands);
    cands = cands.filter((el) => {
      for (const child of el.querySelectorAll('div, article')) {
        if (set.has(child)) return false; // 外层容器，丢弃
      }
      return true;
    });

    // querySelectorAll 已经按 DOM 顺序返回，交替推测 user / assistant
    const items = cands.map((el, i) => ({
      el,
      role: i % 2 === 0 ? 'user' : 'assistant',
      text: (el.innerText || el.textContent || '').trim(),
    }));

    return { selectorUsed: 'generic:div,article', items };
  }

  // ---------------------------------------------------------------
  // 汇总：L1 -> L2
  // ---------------------------------------------------------------
  function collect() {
    const host = siteKey();
    const cfg = lookupSiteConfig(host);
    const site = cfg ? collectSiteLayer(cfg) : null;
    if (site) {
      return { layer: 1, siteHit: true, selectorUsed: site.selectorUsed, items: site.items };
    }
    // Sprint 20：配 noFallbackToGeneric 的站点（DeepSeek）L1 失败不掉 L2，避免重演 Sprint 18 的 137 条失控
    if (cfg && cfg.noFallbackToGeneric) {
      return { layer: 0, siteHit: false, selectorUsed: null, items: [] };
    }
    const generic = collectGenericLayer();
    if (generic.items.length) {
      return { layer: 2, siteHit: false, selectorUsed: generic.selectorUsed, items: generic.items };
    }
    return { layer: 0, siteHit: false, selectorUsed: null, items: [] };
  }

  // ---------------------------------------------------------------
  // [L3] 自诊断报告
  // ---------------------------------------------------------------
  function domSignature(items) {
    if (items.length && items[0].el && items[0].el.outerHTML) {
      return items[0].el.outerHTML.slice(0, 300);
    }
    const first = document.querySelector('main div, #main div, body > div');
    if (first && first.outerHTML) return first.outerHTML.slice(0, 300);
    return '';
  }

  function buildReport(result) {
    // 如实上报这一屏的消息 id 用的是哪种来源（可能混用：部分节点有属性、部分没有）
    const srcCount = {};
    for (const it of result.items) {
      const s = it.message_id_source || 'unknown';
      srcCount[s] = (srcCount[s] || 0) + 1;
    }
    const srcKeys = Object.keys(srcCount).sort();
    return {
      url: location.href,
      hostname: location.hostname,
      site_key: siteKey(),
      site_selector_hit: result.siteHit,
      layer: result.layer,
      selector_used: result.selectorUsed,
      message_count: result.items.length,
      message_id_source: srcKeys.length ? srcKeys.join('+') : 'none',
      message_id_sources: srcCount,
      sample_message_ids: result.items.slice(0, 5).map((x) => x.message_id || null),
      sample_roles: result.items.slice(0, 5).map((x) => x.role),
      sample_texts: result.items.slice(0, 2).map((x) => x.text.slice(0, 50)),
      dom_signature: domSignature(result.items),
      timestamp: new Date().toISOString(),
    };
  }

  function post(type, payload) {
    // 防 context invalidated：扩展重载后旧脚本仍挂在已打开的页面上
    if (typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.id) {
      return;  // 静默退出，不再刷红字
    }
    try {
      chrome.runtime.sendMessage(payload, (resp) => {
        if (!resp || !resp.ok) {
          console.warn('[browser-bridge] ' + type + ' failed: ' + JSON.stringify(resp));
        } else {
          console.log('[browser-bridge] ' + type + ' ok -> ' + JSON.stringify(resp.server));
        }
      });
    } catch (e) {
      const msg = String(e && e.message || e);
      if (msg.indexOf('Extension context invalidated') >= 0) return;  // 静默
      console.warn('[browser-bridge] sendMessage error: ' + e);
    }
  }

  // ---------------------------------------------------------------
  // 稳定去重键：data-message-id（v0.3）
  // 之前用 DOM 位置/序号当 turn 号，页面滚动或重渲染就会错位。
  // 真实 ChatGPT 上 data-message-id 是否存在，我们没有真实 DOM 可观测，
  // 因此：属性存在就用它（source=attr:data-message-id），
  //      不存在则退化为 hash(role+text)（source=fallback:hash(role+text)），
  // 并在 diagnose 里如实上报用的是哪一种，不猜、不谎报。
  // ---------------------------------------------------------------
  function hashText(s) { // FNV-1a 32bit
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return ('00000000' + h.toString(16)).slice(-8);
  }

  function messageIdFor(el, role, text) {
    let id = '';
    try {
      if (el && el.getAttribute) {
        const own = el.getAttribute('data-message-id');
        if (own && own.trim()) id = own.trim();
      }
      if (!id && el && el.closest) {
        const anc = el.closest('[data-message-id]');
        if (anc) {
          const v = anc.getAttribute('data-message-id');
          if (v && v.trim()) id = v.trim();
        }
      }
    } catch (e) { /* ignore */ }

    if (id) return { message_id: id, message_id_source: 'attr:data-message-id' };
    return {
      message_id: 'h:' + hashText(String(role || '?') + '|' + String(text || '')),
      message_id_source: 'fallback:hash(role+text)',
    };
  }

  const seen = new WeakSet();
  const sentIds = new Set();   // 稳定键去重：重渲染换了 DOM 节点但 id 不变 -> 不再重复发
  let lastCount = -1;

  function run() {
    const result = collect();

    // 新消息 -> /turn
    for (const it of result.items) {
      const ident = messageIdFor(it.el, it.role, it.text);
      it.message_id = ident.message_id;
      it.message_id_source = ident.message_id_source;
      if (sentIds.has(it.message_id)) continue;   // 同一条消息只发一次
      if (seen.has(it.el)) continue;
      seen.add(it.el);
      sentIds.add(it.message_id);
      post('turn', {
        type: 'BRIDGE_TURN',
        source: SOURCE,
        site_key: siteKey(),
        layer: result.layer,
        selector_used: result.selectorUsed,
        role: it.role,
        text: it.text,
        message_id: it.message_id,
        message_id_source: it.message_id_source,
        at: new Date().toISOString(),
      });
    }

    // 自诊断 -> /diagnose（消息数变化或首次运行才发，避免刷屏）
    if (result.items.length !== lastCount) {
      lastCount = result.items.length;
      post('diagnose', { type: 'BRIDGE_DIAGNOSE', report: buildReport(result) });
    }
    return result;
  }

  // ---------------------------------------------------------------
  // 观察层：DOM 变化后 debounce 重扫
  // ---------------------------------------------------------------
  // debounce 800ms，但设 3s 上限：真实页面上时钟/动画/流式输出会不断改动 DOM，
  // 纯 debounce 会被无限重置导致再也发不出消息，因此到点强制执行一次。
  const DEBOUNCE_MS = 800;
  const MAX_WAIT_MS = 3000;
  let timer = null;
  let firstScheduledAt = 0;

  function schedule() {
    const now = Date.now();
    if (!timer) firstScheduledAt = now;
    else clearTimeout(timer);
    const wait = Math.max(0, Math.min(DEBOUNCE_MS, firstScheduledAt + MAX_WAIT_MS - now));
    timer = setTimeout(() => { timer = null; firstScheduledAt = 0; run(); }, wait);
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.addedNodes && m.addedNodes.length) { schedule(); return; }
      if (m.type === 'characterData') { schedule(); return; }
    }
  });

  function boot() {
    try {
      observer.observe(document.documentElement || document.body, {
        childList: true,
        subtree: true,
        characterData: true,
      });
    } catch (e) {
      console.warn('[browser-bridge] observe failed: ' + e);
    }
    const r = run();
    document.documentElement.setAttribute('data-bridge-ready', '1');
    document.documentElement.setAttribute('data-bridge-layer', String(r.layer));
    console.log('[browser-bridge] ready on ' + SOURCE +
      ' layer=' + r.layer + ' count=' + r.items.length +
      ' selector=' + r.selectorUsed);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
