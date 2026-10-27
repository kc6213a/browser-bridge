/* ChatGPT SSE 解析器 —— 纯函数，无 DOM / 无 chrome.* / 无网络依赖
 *
 * 依据：docs/_diag_22_chatgpt_sse.md（2026-10-27 真机 15 帧实测破译）
 * 用法：浏览器里挂到 window.BBSseParse；node 里 require('...') 用于单测
 *
 * 设计原则：
 *   1. 只做解析，不做采集（采集在 content script / tap 层）
 *   2. 未知帧一律忽略并计数，绝不抛错（防止站点加新帧类型就整条流报废）
 *   3. type 事件与 p/o/v 补丁分开处理，互不干扰
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.BBSseParse = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {

  function safeParse(s) {
    try { return JSON.parse(s); } catch (e) { return undefined; }
  }

  // 从 content.parts 的单条目里抽文本。ChatGPT 两种形态都要覆盖：
  //   1) 字符串 parts（SSE 流 / 较新历史消息）
  //   2) 对象 parts {content_type:'text', text:'...'}（#22 旧历史消息实测为此形态，
  //      只认字符串会漏掉 assistant 正文 → 50 条空文本翻车）
  function partText(part) {
    if (typeof part === 'string') return part;
    if (part && typeof part === 'object') {
      if (typeof part.text === 'string') return part.text;
      if (typeof part.content === 'string') return part.content;
      if (typeof part.value === 'string') return part.value;
    }
    return '';
  }

  /**
   * 把原始 SSE 文本拆成 data 负载数组（只取 `data: ` 行，跳过 event:/注释/空行）
   */
  function splitFrames(raw) {
    var out = [];
    if (!raw) return out;
    var lines = String(raw).split('\n');
    for (var i = 0; i < lines.length; i++) {
      var s = lines[i].trim();
      if (!s || s.charAt(0) === ':') continue;
      if (s.indexOf('data: ') !== 0) continue;
      out.push(s.slice(6));
    }
    return out;
  }

  // [#51 / S3] 从一条 message 对象里抽取附件元数据。
  // ChatGPT 的附件可能出现在两处：
  //   1) content.parts 里的对象型条目（content_type 含 input_file/input_image/file/...，带 file_id）
  //   2) metadata.attachments[]（含 id/name/size/mime_type）
  // 返回 [{source, content_type, file_id, name, size, mime_type}]
  function collectAttachments(msg) {
    var out = [];
    var c = msg && msg.content;
    if (c && Array.isArray(c.parts)) {
      for (var i = 0; i < c.parts.length; i++) {
        var p = c.parts[i];
        if (!p || typeof p !== 'object') continue;
        var ct = p.content_type || p.type || '';
        var isAtt = /(input_file|input_image|input_audio|input_pdf|file|image|audio|document|attachment)/i.test(ct) || !!p.file_id;
        if (isAtt) {
          out.push({
            source: 'part',
            content_type: ct || null,
            file_id: p.file_id || p.id || null,
            name: p.name || null,
            size: p.size || null
          });
        }
      }
    }
    var md = msg && msg.metadata;
    if (md && Array.isArray(md.attachments)) {
      for (var j = 0; j < md.attachments.length; j++) {
        var a = md.attachments[j];
        if (!a || typeof a !== 'object') continue;
        out.push({
          source: 'metadata',
          file_id: a.id || a.file_id || null,
          name: a.name || null,
          size: a.size || null,
          mime_type: a.mime_type || a.mimeType || null
        });
      }
    }
    return out;
  }

  function attKey(a) {
    return a.file_id || ((a.name || '') + '|' + (a.size || ''));
  }

  function mergeAttachments(rec, list) {
    for (var i = 0; i < list.length; i++) {
      var k = attKey(list[i]);
      var existing = null;
      for (var m = 0; m < rec.attachments.length; m++) {
        if (attKey(rec.attachments[m]) === k) { existing = rec.attachments[m]; break; }
      }
      if (!existing) {
        rec.attachments.push(list[i]);
      } else {
        // 同 file_id 出现多次（part 与 metadata 各一次）：合并非空字段，
        // 不要整条跳过——否则会丢掉 metadata 里的 name/size。
        if (!existing.name && list[i].name) existing.name = list[i].name;
        if (!existing.size && list[i].size) existing.size = list[i].size;
        if (!existing.content_type && list[i].content_type) existing.content_type = list[i].content_type;
        if (!existing.mime_type && list[i].mime_type) existing.mime_type = list[i].mime_type;
        if (!existing.file_id && list[i].file_id) existing.file_id = list[i].file_id;
      }
    }
  }

  // [#51 / S3] 兜底发现：遍历每个 frame，凡含 file_id / signed_path 的对象都记一条
  // 原始样本（截断字段名），防止上面的结构化猜测不准时仍能从真实数据看清形状。
  function scanForAttachments(node, meta, depth, budget) {
    if (!node || typeof node !== 'object' || depth > 3) return;
    if (budget.count > 20000) return;
    budget.count++;
    if (Array.isArray(node)) {
      for (var i = 0; i < node.length; i++) scanForAttachments(node[i], meta, depth + 1, budget);
      return;
    }
    if ('file_id' in node || 'signed_path' in node) {
      var fid = node.file_id || null;
      var dup = false;
      for (var d = 0; d < meta.attachmentRaw.length; d++) {
        if (meta.attachmentRaw[d].file_id === fid) { dup = true; break; }
      }
      if (!dup && meta.attachmentRaw.length < 40) {
        meta.attachmentRaw.push({
          file_id: fid,
          signed_path: node.signed_path || null,
          name: node.name || null,
          size: node.size || null,
          mime_type: node.mime_type || node.mimeType || null,
          keys: Object.keys(node).slice(0, 30)
        });
      }
    }
    for (var key in node) {
      if (!node.hasOwnProperty(key)) continue;
      var val = node[key];
      if (val && typeof val === 'object') scanForAttachments(val, meta, depth + 1, budget);
    }
  }

  /**
   * 解析 ChatGPT `/backend-api/f/conversation` 的 SSE 流
   *
   * @param {string} raw  完整或已中断的 SSE 文本
   * @param {object} [opts] { aborted:boolean } 由调用方标记「读完被 abort 但数据完整」
   * @returns {{
   *   messages: Array<{id,role,text,status,createTime}>,
   *   meta: { conversationId, model, completed, frameCount, parsed, ignored },
   *   ok: boolean
   * }}
   */
  function parseChatGPTStream(raw, opts) {
    opts = opts || {};
    var frames = splitFrames(raw);
    var byId = {};
    var order = [];
    var currentId = null;
    var meta = {
      conversationId: null,
      model: null,
      completed: false,
      frameCount: frames.length,
      parsed: 0,
      ignored: {},
      aborted: !!opts.aborted,
      attachmentRaw: []
    };

    function bump(k) { meta.ignored[k] = (meta.ignored[k] || 0) + 1; }

    function touch(msg) {
      if (!msg || !msg.id) return null;
      var id = msg.id;
      if (!byId[id]) {
        byId[id] = {
          id: id,
          role: (msg.author && msg.author.role) || 'unknown',
          text: '',
          status: msg.status || null,
          createTime: msg.create_time || null,
          attachments: []
        };
        order.push(id);
      } else {
        if (!byId[id].status && msg.status) byId[id].status = msg.status;
        if (!byId[id].createTime && msg.create_time) byId[id].createTime = msg.create_time;
        if (byId[id].role === 'unknown' && msg.author && msg.author.role) {
          byId[id].role = msg.author.role;
        }
      }
      currentId = id;
      return byId[id];
    }

    function seedText(msg) {
      // 先建记录再判正文：system 消息 parts=[""] 正文为空，
      // 但它承载 role 信息，不能因为"没有文字"就整条丢掉。
      var rec = touch(msg);
      if (!rec) return;
      // [#51 / S3] 顺带抽附件元数据（user 的 input_message / assistant 的 message 都过这里）
      mergeAttachments(rec, collectAttachments(msg));
      var c = msg && msg.content;
      if (!c || !c.parts || !c.parts.length) return;
      var t = '';
      for (var i = 0; i < c.parts.length; i++) {
        t += partText(c.parts[i]);
      }
      if (t && rec.text.length < t.length) rec.text = t;
    }

    function applyPatch(item) {
      if (!item || typeof item !== 'object') return;
      var p = String(item.p || '');
      var o = item.o || null;
      var v = item.v;
      var rec = currentId ? byId[currentId] : null;

      if (p.indexOf('/message/content/parts/') >= 0 && (o === null || o === 'append')) {
        if (rec && typeof v === 'string') rec.text += v;
        return;
      }
      if (p.indexOf('/message/status') >= 0 && (o === null || o === 'replace')) {
        if (rec && typeof v === 'string') rec.status = v;
        return;
      }
      if (p.indexOf('/message/metadata') >= 0) {
        // 消息级 model_slug 不可信（实测是 gpt-6-mini 这类内部路由名），
        // 真实模型以 server_ste_metadata 为准，这里只记录不覆盖。
        if (v && typeof v === 'object' && v.model_slug) {
          if (!meta.messageModel) meta.messageModel = v.model_slug;
        }
        return;
      }
      bump('patch:' + (p || '<root>'));
    }

    for (var i = 0; i < frames.length; i++) {
      var f = frames[i];
      if (f === '[DONE]') { meta.completed = true; continue; }

      var j = safeParse(f);
      if (j === undefined) { bump('unparsable'); continue; }
      if (j === null || typeof j !== 'object') { bump('scalar'); continue; }
      meta.parsed++;

      // [#51 / S3] 兜底发现真实附件形状（结构化猜测万一漏了也能从原始样本看清）
      scanForAttachments(j, meta, 0, { count: 0 });

      var t = j.type;

      if (t === 'input_message') {
        if (j.conversation_id) meta.conversationId = j.conversation_id;
        seedText(j.input_message);
        continue;
      }
      if (t === 'message_stream_complete') {
        if (j.conversation_id) meta.conversationId = j.conversation_id;
        meta.completed = true;
        continue;
      }
      if (t === 'server_ste_metadata') {
        if (j.conversation_id) meta.conversationId = j.conversation_id;
        // 服务端元数据是模型的权威来源，无条件覆盖
        if (j.metadata && j.metadata.model_slug) meta.model = j.metadata.model_slug;
        continue;
      }
      if (t) { bump('type:' + t); continue; }

      var v = j.v;

      if (v && typeof v === 'object' && v.message) {
        seedText(v.message);
        continue;
      }
      if (Object.prototype.toString.call(v) === '[object Array]') {
        for (var k = 0; k < v.length; k++) applyPatch(v[k]);
        continue;
      }
      bump('shape:unknown');
    }

    var messages = [];
    for (var m = 0; m < order.length; m++) messages.push(byId[order[m]]);

    // 汇总全量附件（去重）到 meta，便于服务端/验收直接看一条 turn 的附件清单
    var consolidated = [];
    for (var ci = 0; ci < messages.length; ci++) {
      var atts = messages[ci].attachments || [];
      for (var ai = 0; ai < atts.length; ai++) {
        var k = attKey(atts[ai]);
        var dup = false;
        for (var di = 0; di < consolidated.length; di++) {
          if (attKey(consolidated[di]) === k) { dup = true; break; }
        }
        if (!dup) consolidated.push(atts[ai]);
      }
    }
    meta.attachments = consolidated;

    return {
      ok: messages.length > 0,
      messages: messages,
      meta: meta
    };
  }

  /**
   * 解析 `GET /backend-api/conversations/{id}` 返回的扁平 messages[]（历史腿 S4）
   * 与 SSE 流不同：这是已完成消息的快照，无流式 patch，直接逐条提取。
   * 顺序使用服务端数组顺序（禁止 create_time 排序，见 design_sse_passive_tap.md §7）。
   *
   * @param {Array|string|object} raw  messages 数组，或含 {messages, page_info} 的对象 / JSON 字符串
   * @param {object} [opts] { conversationId }
   * @returns {{ok, messages, meta}}
   */
  function parseConversationMessages(raw, opts) {
    opts = opts || {};
    var data = raw;
    if (typeof raw === 'string') data = safeParse(raw);
    if (!data || typeof data !== 'object') {
      return {
        ok: false, messages: [],
        meta: { conversationId: opts.conversationId || null, error: 'non-object payload', messageCount: 0, hasPreviousPage: false }
      };
    }
    var conversationId = opts.conversationId || data.conversation_id || data.id || null;
    var list = Array.isArray(data.messages)
      ? data.messages
      : (Array.isArray(data) ? data : []);
    var pageInfo = data.page_info || {};
    var byId = {};
    var order = [];
    var meta = {
      conversationId: conversationId,
      messageCount: list.length,
      hasPreviousPage: !!(pageInfo.has_previous_page || pageInfo.has_more),
      pageInfoKeys: Object.keys(pageInfo).slice(0, 20)
    };

    for (var i = 0; i < list.length; i++) {
      var m = list[i];
      if (!m || !m.id) continue;
      var rec = {
        id: m.id,
        role: (m.author && m.author.role) || m.role || 'unknown',
        text: '',
        status: m.status || null,
        createTime: m.create_time || null,
        attachments: []
      };
      var c = m.content;
      if (c && Array.isArray(c.parts)) {
        var t = '';
        for (var j = 0; j < c.parts.length; j++) {
          t += partText(c.parts[j]);
        }
        rec.text = t;
      }
      mergeAttachments(rec, collectAttachments(m));
      byId[rec.id] = rec;
      order.push(rec.id);
    }

    var messages = [];
    for (var k = 0; k < order.length; k++) messages.push(byId[order[k]]);

    // 汇总全量附件（去重）到 meta
    var consolidated = [];
    for (var ci = 0; ci < messages.length; ci++) {
      var atts = messages[ci].attachments || [];
      for (var ai = 0; ai < atts.length; ai++) {
        var key = attKey(atts[ai]);
        var dup = false;
        for (var di = 0; di < consolidated.length; di++) {
          if (attKey(consolidated[di]) === key) { dup = true; break; }
        }
        if (!dup) consolidated.push(atts[ai]);
      }
    }
    meta.attachments = consolidated;

    return { ok: messages.length > 0, messages: messages, meta: meta };
  }

  return {
    parseChatGPTStream: parseChatGPTStream,
    parseConversationMessages: parseConversationMessages,
    splitFrames: splitFrames
  };
});
