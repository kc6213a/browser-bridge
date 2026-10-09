// background service worker (v0.2)：把 content.js 的消息转发到本地接收端。
// 放在 background 而不是 content.js 直接 fetch 的原因：
// ChatGPT / Claude 是 https 页面，content script 里 fetch("http://127.0.0.1:8787")
// 会被浏览器按 mixed-content 拦截；service worker 发起的请求不受该限制。
// 注意：这里用 127.0.0.1 而非 localhost —— server 只监听 IPv4，Chrome 在 Windows 上
// 常把 localhost 解析成 ::1(IPv6) 导致 "Failed to fetch"，显式写 IPv4 绕过。
const BASE = 'http://127.0.0.1:8787';
const ROUTES = {
  BRIDGE_TURN: '/turn',
  BRIDGE_DIAGNOSE: '/diagnose',
};

function forward(route, body) {
  return fetch(BASE + route, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
    .then((r) => r.json().catch(() => ({ ok: false, error: 'non-json response' })))
    .catch((e) => ({ ok: false, error: String(e) }));
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const route = msg && ROUTES[msg.type];
  if (!route) return false;

  const body = msg.type === 'BRIDGE_DIAGNOSE'
    ? msg.report
    : {
        source: msg.source || (sender.tab && sender.tab.url) || 'unknown',
        site_key: msg.site_key || null,
        layer: msg.layer,
        selector_used: msg.selector_used || null,
        role: msg.role || '?',
        text: msg.text || '',
        message_id: msg.message_id || null,
        message_id_source: msg.message_id_source || null,
        at: msg.at || new Date().toISOString(),
        project_id: msg.project_id || null,
      };

  forward(route, body).then((server) => sendResponse({ ok: !!(server && server.ok), server }));
  return true; // 保持消息通道，异步 sendResponse
});

chrome.runtime.onInstalled.addListener(() => {
  console.log('[browser-bridge] installed, receiver = ' + BASE + ' (/turn, /diagnose)');
});

console.log('[browser-bridge] background service worker ready -> ' + BASE);
