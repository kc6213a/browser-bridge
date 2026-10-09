/**
 * 会话/项目标识（2026-10-09）
 *
 * 从 URL 取「当前会话所属的容器」——ChatGPT 的项目 / DeepSeek 的会话。
 * 取不到返回 null（server 侧兜底为 `_no_project`）：不猜、不编造。
 *
 * ⚠️ 为什么单独一个文件：content.js（采集）和 panel.js（展示）都需要这个值，
 *    两处各写一份正则一定会漂移 —— 一旦漂移，面板就会去请求另一个桶，
 *    看起来像「串台」。所以放在这里，两个脚本共用同一个实现。
 *    manifest 里必须排在 content.js / panel.js 之前加载。
 *
 * ⚠️ 实测 URL（2026-10-09 turn 718 探针）：
 *   chatgpt   /g/g-p-6ab279ccd1988191b6d08b22a7f31b41-zhi-biao-kan-ban-gai-ban/c/6ac7b708-...
 *             hex 后面跟的是中文拼音 slug，**不是 '/'** ——
 *             所以 /g/g-p-([a-f0-9]+)/ 匹配不到，必须停在 '-' 上。
 *   deepseek  /a/chat/s/d5d48e45-aa33-475c-90d3-2569380c2506
 *             DeepSeek 没有项目概念，「一个会话 = 一个容器」，前缀 s- 标明来源。
 *
 * 必须在每次发消息 / 每次轮询时重新调用：这些都是 SPA，切会话只改 URL 不重载页面。
 */
(() => {
  'use strict';

  const PROJECT_PATTERNS = [
    { host: 'chatgpt.com', re: /\/g\/(g-p-[0-9a-f]{8,})/i },
    { host: 'chat.openai.com', re: /\/g\/(g-p-[0-9a-f]{8,})/i },
    {
      host: 'chat.deepseek.com',
      re: /\/a\/chat\/s\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i,
      prefix: 's-',
    },
  ];

  function bridgeProjectId() {
    try {
      const host = String(location.hostname || '').toLowerCase();
      const path = String(location.pathname || '');
      for (const p of PROJECT_PATTERNS) {
        if (host === p.host || host.endsWith('.' + p.host)) {
          const m = path.match(p.re);
          // 用完整 id，不做截断：截断有撞号风险，撞号会把两个会话并成一个桶（假合并）。
          if (m && m[1]) return (p.prefix || '') + m[1].toLowerCase();
        }
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  // 面板显示用：把完整 id 压成短标签，只用于「这是哪个会话」的肉眼标识。
  // 注意：这是**展示层**截断，路由键永远用完整 id。
  function bridgeProjectLabel(pid) {
    if (!pid) return null;
    if (pid.indexOf('g-p-') === 0) return pid.slice(0, 12);        // g-p-6ab279cc
    if (pid.indexOf('s-') === 0) return 's-' + pid.slice(2, 10);   // s-d5d48e45
    return pid.slice(0, 14);
  }

  window.__BRIDGE_PROJECT_ID__ = bridgeProjectId;
  window.__BRIDGE_PROJECT_LABEL__ = bridgeProjectLabel;
})();
