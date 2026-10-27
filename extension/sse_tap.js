/* SSE tap —— MAIN world / document_start
 *
 * 职责单一：patch window.fetch，抓 ChatGPT `/backend-api/f/conversation` 的 SSE 响应，
 * 交给 sse_parse.js 解析，结果 postMessage 给 isolated 侧的 content.js 转发上报。
 *
 * 约束（见 docs/design_sse_passive_tap.md）：
 *   1. 全扩展唯一一处 fetch patch（content.js 永远不 patch）
 *   2. 只 clone 响应读，原 Response 原样返回，不消费页面自己的 body
 *   3. 读完被 abort 但已有数据 → 照常上报并标 aborted，不当失败
 *   4. 幂等：window.__BB_SSE_TAP 防重复注入
 */
(function () {
  'use strict';
  if (window.__BB_SSE_TAP) return;
  window.__BB_SSE_TAP = 1;

  var SSE_ENDPOINT = /\/backend-api\/f\/conversation/i;
  var PREPARE = /prepare/i; // 排除 /prepare 预检（非流式，读出来是垃圾会污染 turn）
  var MAX = 2097152;
  var IDLE_MS = 150000;

  // [#22 前提2a] 进入会话页自动拉历史，按 message.id 补差（server 侧去重）。
  // 闸门 = 同会话 5 分钟内不重复拉（不是"今日一次"——否则当天漏的新消息永远缺）。
  var AUTO_FETCH_COOLDOWN_MS = 5 * 60 * 1000;
  var lastAutoFetch = {};        // cid -> 上次自动拉的时间戳
  var currentConvId = null;      // URL 监听：当前会话 id
  // [#22 前提3] 嗅探 ChatGPT 切 WebSocket 的信号（全局只报一次，避免刷）
  var wsFlagReported = false;

  // S4 历史腿：从页面自己发出的 /backend-api/ 请求里偷 Authorization（Bearer），
  // 供 fetchHistory 主动调 conversations/{id} 复用。isolated world 拿不到这个 token。
  var AUTH_TOKEN = null;
  function cacheAuth(init) {
    if (!init || !init.headers) return;
    var h = init.headers, v = null;
    try {
      if (typeof h.get === 'function') v = h.get('authorization');
      else if (h && typeof h === 'object') {
        for (var k in h) { if (/^authorization$/i.test(k)) { v = h[k]; break; } }
      }
    } catch (e) {}
    if (v) AUTH_TOKEN = String(v).replace(/^Bearer\s+/i, '');
  }

  function post(kind, data) {
    try { window.postMessage({ __bbSse: 1, kind: kind, data: data }, '*'); } catch (e) {}
  }

  post('install', {
    url: location.href,
    readyState: document.readyState,
    at: Date.now(),
    hasParser: !!window.BBSseParse
  });

  // content.js 是 document_idle 注入，可能晚于上面那条 install。
  // 页面就绪后再补一条，确保 isolated 侧的 listener 一定收得到。
  function reInstall() {
    post('install', {
      url: location.href,
      readyState: document.readyState,
      at: Date.now(),
      late: true,
      hasParser: !!window.BBSseParse
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      setTimeout(reInstall, 1200);
    }, { once: true });
  } else {
    setTimeout(reInstall, 1200);
  }

  function report(raw, meta) {
    // [#22 前提3] 嗅探响应体里的 resume_with_websockets 信号：ChatGPT 一旦把
    // /backend-api/f/conversation 流换成 WebSocket，SSE hook 会静默失效。
    // 该信号一般在流前部（readStream 截断保留头半），扫描一次即可。
    if (!wsFlagReported && raw && raw.indexOf('resume_with_websockets') >= 0) {
      wsFlagReported = true;
      post('ws_flag', { url: meta.url, detected: true });
    }
    if (!window.BBSseParse) {
      post('error', { url: meta.url, error: 'BBSseParse missing', bytes: raw.length });
      return;
    }
    var r = window.BBSseParse.parseChatGPTStream(raw, { aborted: !!meta.aborted });
    post('turn', {
      url: meta.url,
      method: meta.method,
      status: meta.status,
      bytes: raw.length,
      aborted: !!meta.aborted,
      abortError: meta.abortError || null,
      completed: r.meta.completed,
      conversationId: r.meta.conversationId,
      model: r.meta.model,
      ignored: r.meta.ignored,
      messages: r.messages,
      attachments: r.meta.attachments,
      attachmentRaw: r.meta.attachmentRaw
    });
  }

  function readStream(res, meta) {
    try {
      var clone = res.clone();
      if (!clone.body || !clone.body.getReader) {
        clone.text().then(function (t) { report(t, meta); }).catch(function () {});
        return;
      }
      var reader = clone.body.getReader();
      var dec = new TextDecoder();
      var acc = '';
      var t0 = Date.now();

      function pump() {
        reader.read().then(function (r) {
          if (r.done) { report(acc, meta); return; }
          acc += dec.decode(r.value, { stream: true });
          if (acc.length > MAX) {
            // 只保留头+尾：input_message（含附件元数据+用户正文）在流开头，
            // [DONE]/最终正文在尾部，中间长正文可丢，但不能丢附件。
            var keep = Math.floor(MAX / 2);
            acc = acc.slice(0, keep) + '\n__BB_TRUNC__\n' + acc.slice(-keep);
          }
          if (Date.now() - t0 > IDLE_MS) { report(acc, meta); return; }
          pump();
        }).catch(function (e) {
          // 页面读完流会主动 abort，clone 的 reader 跟着抛错，
          // 但此时数据通常已完整（[DONE] 都到了）——必须照常上报。
          if (acc.length > 0) {
            meta.aborted = true;
            meta.abortError = String(e);
            report(acc, meta);
          } else {
            post('error', { url: meta.url, error: String(e), bytes: 0 });
          }
        });
      }
      pump();
    } catch (e) {
      post('error', { url: meta.url, error: 'readStream:' + String(e) });
    }
  }

  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var p = origFetch.apply(this, arguments);

    // 任何 /backend-api/ 请求都缓存其 Authorization，供 S4 历史腿复用（不拦截 body）
    if (/backend-api/i.test(url)) cacheAuth(init);

    // 只拦截真实对话 SSE 端点；其他 backend-api（bazaar/settings/gizmos/ads/sync-token 等）
    // 一律放行，避免把它们被拦截器 block 或 abort 的异常当 error 落盘（噪声）。
    if (SSE_ENDPOINT.test(url) && !PREPARE.test(url)) {
      var meta = { url: url, method: (init && init.method) || 'GET' };
      p.then(function (res) {
        meta.status = res.status;
        if (!res.ok) {
          post('skip', { url: url.slice(0, 200), status: res.status });
          return;
        }
        readStream(res, meta);
      }).catch(function (e) {
        post('error', { url: url.slice(0, 200), error: String(e) });
      });
    }
    return p;
  };

  // ---- S4 历史腿（主动抓取，不走被动监听） ----

  function getConvIdFromUrl() {
    var m = location.pathname.match(/\/c\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/);
    return m ? m[1] : null;
  }

  // 主动拉 conversations/{id} 扁平 messages[]，翻页补齐旧消息。
  // 必须在 MAIN world 执行（isolated world 无 Authorization，会被 404）。
  function fetchHistory() {
    var cid = getConvIdFromUrl();
    if (!cid) {
      post('error', { kind: 'history', error: 'no conversation id in url', url: location.href });
      return;
    }
    if (!AUTH_TOKEN) {
      post('error', {
        kind: 'history',
        error: 'no auth cached — open or reload the conversation first (let it issue a /backend-api/ request)'
      });
      return;
    }
    // 翻页用游标 before=<最旧消息id>，不能用 offset——服务端忽略 offset，
    // 会每次都返回同一批最新消息，导致 5550 条复制品（S4 初版实测踩过）。
    var NUM = 100, MAXP = 30, pages = 0;
    var all = [];
    var seenIds = {};        // 防重复兜底：即使游标失效也不落复制品
    var lastPageInfo = null;
    var before = null;       // 游标：上一页最旧消息 id
    function step() {
      var u = 'https://chatgpt.com/backend-api/conversations/' + cid + '?num_turns=' + NUM;
      if (before) u += '&before=' + encodeURIComponent(before);
      return origFetch(u, {
        method: 'GET',
        headers: { 'Authorization': 'Bearer ' + AUTH_TOKEN, 'Content-Type': 'application/json' }
      }).then(function (res) {
        if (!res.ok) { post('error', { kind: 'history', error: 'HTTP ' + res.status, url: u }); return null; }
        return res.json();
      }).then(function (data) {
        if (!data) return null;
        var msgs = Array.isArray(data.messages) ? data.messages : [];
        var added = 0;
        for (var i = 0; i < msgs.length; i++) {
          var mid = msgs[i] && msgs[i].id;
          if (mid && !seenIds[mid]) { seenIds[mid] = 1; all.push(msgs[i]); added++; }
        }
        lastPageInfo = data.page_info || null;
        pages++;
        // before 游标 = 本页数组最后一条（最旧）消息 id
        var oldestId = null;
        for (var k = msgs.length - 1; k >= 0; k--) {
          if (msgs[k] && msgs[k].id) { oldestId = msgs[k].id; break; }
        }
        if (oldestId) before = oldestId;
        var more = !!(lastPageInfo && (lastPageInfo.has_previous_page || lastPageInfo.has_more));
        // 安全闸：游标未推进(added=0) 或 达上限 或 无可翻 → 停，避免重复刷同页
        if (more && added > 0 && pages < MAXP) { return step(); }
        return true;
      });
    }
    step().then(function () {
      if (!window.BBSseParse || !window.BBSseParse.parseConversationMessages) {
        post('error', { kind: 'history', error: 'parser missing' });
        return;
      }
      var r = window.BBSseParse.parseConversationMessages(all, { conversationId: cid });
      post('history', {
        conversationId: cid,
        url: location.href,
        capturedMessages: all.length,
        pageCount: pages,
        rawPageInfo: lastPageInfo,
        completed: true,
        messages: r.messages,
        attachments: r.meta.attachments
      });
      // 核对样本：前 3 条完整，供验收看真实字段结构（避免传全部正文过大）
      post('history_raw', {
        conversationId: cid,
        pageInfo: lastPageInfo,
        capturedMessages: all.length,
        sample: all.slice(0, 3)
      });
    }).catch(function (e) {
      post('error', { kind: 'history', error: String(e) });
    });
  }

  // ---- [#22 前提2a + 前提3] 进入会话页自动补历史 + 发 session_open ----
  function emitSessionOpen(cid) {
    post('session_open', { conversationId: cid, url: location.href });
  }

  function maybeAutoFetchHistory() {
    var cid = getConvIdFromUrl();
    if (!cid) return;
    var now = Date.now();
    if (lastAutoFetch[cid] && (now - lastAutoFetch[cid] < AUTO_FETCH_COOLDOWN_MS)) return;
    lastAutoFetch[cid] = now;
    if (!AUTH_TOKEN) {
      // 刚进入会话页，Authorization 可能还没被 cacheAuth 抓到（页面尚未发 /backend-api 请求）。
      // 延迟 2s 重试一次；仍无则放弃本次（5 分钟闸门已设，避免刷）。
      setTimeout(function () {
        lastAutoFetch[cid] = Date.now();
        if (AUTH_TOKEN) fetchHistory();
      }, 2000);
      return;
    }
    fetchHistory();
  }

  function onUrlMaybeConversation() {
    var cid = getConvIdFromUrl();
    // 只在遇到「不同的非空 cid」时触发一次；cid 为 null 或与当前相同都不变更。
    // 注意：SPA 加载期 getConvIdFromUrl() 会瞬态返回 null（过渡 URL），
    // 若在此重置 currentConvId，cid 再现时会误判为「新会话」重复发 session_open / 拉 history。
    if (cid && cid !== currentConvId) {
      currentConvId = cid;
      emitSessionOpen(cid);
      maybeAutoFetchHistory();
    }
  }

  // SPA 路由变化监听（ChatGPT 是单页应用，切换会话不刷新页面）。
  (function hookHistory() {
    var _origPush = history.pushState;
    if (typeof _origPush === 'function') {
      history.pushState = function () {
        var r = _origPush.apply(this, arguments);
        onUrlMaybeConversation();
        return r;
      };
    }
    window.addEventListener('popstate', onUrlMaybeConversation);
    window.addEventListener('hashchange', onUrlMaybeConversation);
    // 兜底：轮询 location 变化（防页面覆写 pushState 导致 hook 失效），靠 cid 去重不重复触发
    setInterval(onUrlMaybeConversation, 1500);
  })();

  window.addEventListener('message', function (e) {
    if (e.source !== window) return;
    var d = e.data;
    if (!d || d.__bbSseCmd !== 'fetchHistory') return;
    fetchHistory();
  });
})();
