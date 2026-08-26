/* ================= NovaAgent web client (v14-ux) ================= */

const $ = id => document.getElementById(id);
const chatEl = $("chat"), welcomeEl = $("welcome"), wrapEl = $("chatWrap");
const input = $("input"), sendBtn = $("sendBtn");
const pill = $("statusPill"), modelInfo = $("modelInfo"), tokenInfo = $("tokenInfo");
const convoList = $("convoList"), convTitle = $("convTitle");
const verInfo = $("verInfo"), runStatusEl = $("runStatus");
const UI_VERSION = "v14-ux";

let sessionId = null;
let busy = false;
let conversations = [];   // [{id, title, ts, messages:[{role, content, meta?}]}]
let serverVersion = "?";
let lastUserText = "";    // for 重新生成 / 重试
const STORE_KEY = "nova_convos_v2";

/* ---------------- theme ---------------- */

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("nova_theme", t); } catch (e) {}
  $("themeBtn").textContent = t === "dark" ? "☀️" : "🌙";
}
$("themeBtn").onclick = () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
applyTheme(document.documentElement.dataset.theme || "light");

/* ---------------- storage ---------------- */

function loadConvos() {
  try {
    conversations = JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
    return;
  } catch (e) { /* fall through to legacy migration */ }
  try {                                   // migrate pre-v12 list (no messages)
    conversations = JSON.parse(localStorage.getItem("nova_convos") || "[]")
      .map(c => ({ ...c, messages: [] }));
  } catch (e) { conversations = []; }
  saveConvos();
}
function saveConvos() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(conversations.slice(0, 50))); }
  catch (e) { /* storage full → keep UI alive */ }
}
function currentConvo() {
  return conversations.find(c => c.id === sessionId);
}
function recordUserMessage(text) {
  let c = currentConvo();
  if (!c) { c = { id: sessionId, title: text.slice(0, 24), ts: Date.now(), messages: [] };
            conversations.unshift(c); }
  else { c.title = text.slice(0, 24); c.ts = Date.now(); }
  c.messages.push({ role: "user", content: text });
  saveConvos();
}
function persistBotMessage(content, meta) {
  const c = currentConvo();
  if (!c) return;
  const last = c.messages[c.messages.length - 1];
  if (!content) {                          // nothing to store yet
    if (last && last.role === "bot") c.messages.pop();
    saveConvos(); return;
  }
  if (last && last.role === "bot") { last.content = content; if (meta) last.meta = meta; }
  else c.messages.push({ role: "bot", content, ...(meta ? { meta } : {}) });
  c.ts = Date.now();
  saveConvos();
}
/* ---------------- mini markdown renderer ---------------- */

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
          .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const KEYWORDS_PY = /\b(def|class|return|if|elif|else|for|while|import|from|as|with|try|except|finally|raise|in|not|and|or|None|True|False|lambda|pass|break|continue|global|yield|assert|async|await)\b/;
const KEYWORDS_JS = /\b(function|const|let|var|return|if|else|for|while|import|export|from|of|in|new|class|extends|try|catch|finally|throw|typeof|null|undefined|true|false|async|await)\b/;

function pickKeywords(lang) {
  const l = (lang || "").toLowerCase();
  if (["py", "python"].includes(l)) return KEYWORDS_PY;
  if (["js", "javascript", "ts", "typescript", "jsx", "tsx", "json"].includes(l)) return KEYWORDS_JS;
  return null;
}

function highlight(code, lang) {
  const kw = pickKeywords(lang);
  const tokens = [];
  const stash = html => "\x00" + (tokens.push(html) - 1) + "\x00";
  let out = code
    .replace(/(&quot;|"|')(?:\\.|(?!\1)[^\\\n])*\1/g,
             m => stash('<span class="tok-s">' + m + "</span>"))
    .replace(/(^|\n)(\s*#[^\n]*)/g,
             (m, a, b) => a + stash('<span class="tok-c">' + b + "</span>"))
    .replace(/(\/\/[^\n]*)/g, m => stash('<span class="tok-c">' + m + "</span>"));
  if (kw) out = out.replace(kw, m => '<span class="tok-k">' + m + "</span>");
  out = out.replace(/\b(\d+(?:\.\d+)?)\b/g, m => '<span class="tok-n">' + m + "</span>");
  return out.replace(/\x00(\d+)\x00/g, (m, i) => tokens[+i]);
}

function renderInline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code class="ic">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function renderMarkdown(src) {
  const lines = escapeHtml(String(src || "")).split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const lang = line.slice(3).trim();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push('<div class="codeblock"><div class="codehead"><span>' +
        (escapeHtml(lang) || "text") +
        '</span><button data-copy>复制</button></div>' +
        "<pre><code>" + highlight(buf.join("\n"), lang) + "</code></pre></div>");
      continue;
    }
    if (/^\s*$/.test(line)) { i++; continue; }
    if (/^(#{1,4})\s+/.test(line)) {
      const lvl = line.match(/^#+/)[0].length;
      out.push("<h" + lvl + ">" + renderInline(line.replace(/^#+\s*/, "")) + "</h" + lvl + ">");
      i++; continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^\s*&gt;\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i]))
        buf.push(lines[i++].replace(/^\s*&gt;\s?/, ""));
      out.push("<blockquote>" + renderInline(buf.join(" ")) + "</blockquote>");
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length &&
        /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|")
        .map(c => renderInline(c.trim()));
      const head = cells(lines[i]); i += 2;
      let rows = "";
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]))
        rows += "<tr>" + cells(lines[i++]).map(c => "<td>" + c + "</td>").join("") + "</tr>";
      out.push("<table><thead><tr>" +
        head.map(c => "<th>" + c + "</th>").join("") +
        "</tr></thead><tbody>" + rows + "</tbody></table>");
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i]))
        items.push("<li>" + renderInline(lines[i++].replace(/^\s*[-*+]\s+/, "")) + "</li>");
      out.push("<ul>" + items.join("") + "</ul>");
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i]))
        items.push("<li>" + renderInline(lines[i++].replace(/^\s*\d+\.\s+/, "")) + "</li>");
      out.push("<ol>" + items.join("") + "</ol>");
      continue;
    }
    const buf = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^```/.test(lines[i]) &&
           !/^\s*(#{1,4}\s|[-*+]\s|\d+\.\s|&gt;)/.test(lines[i]))
      buf.push(lines[i++]);
    out.push("<p>" + renderInline(buf.join("\n")).replace(/\n/g, "<br>") + "</p>");
  }
  return out.join("");
}
/* ---------------- status, auth & sessions ---------------- */

function setStatus(state, text) {
  pill.className = "pill" + (state === "ok" ? "" : " " + state);
  pill.textContent = text;
}

// Optional bearer-token auth: if the server enables NOVA_WEB_TOKEN, every
// /api call needs "Authorization: Bearer <token>". On 401 we prompt once,
// cache the token in localStorage and retry.
let authToken = localStorage.getItem("nova_token") || "";

async function apiFetch(path, opts = {}, retried = false) {
  const headers = Object.assign({"Content-Type": "application/json"}, opts.headers);
  if (authToken) headers["Authorization"] = "Bearer " + authToken;
  const r = await fetch(path, Object.assign({}, opts, {headers}));
  if (r.status === 401 && !retried) {
    const t = prompt("此服务已开启访问控制,请输入访问令牌 (NOVA_WEB_TOKEN):");
    if (t === null) return r;
    authToken = t.trim();
    localStorage.setItem("nova_token", authToken);
    return apiFetch(path, opts, true);
  }
  return r;
}

async function createSession(retries = 6) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const r = await apiFetch("/api/sessions", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      sessionId = j.session_id;
      modelInfo.textContent = "model: " + j.model;
      serverVersion = j.server_version || "(旧版,未知)";
      verInfo.textContent = "ui: " + UI_VERSION + " · srv: " + serverVersion;
      setStatus("ok", "已连接");
      return true;
    } catch (e) {
      setStatus("wait", "连接中…(" + attempt + "/" + retries + ")");
      if (attempt < retries) await new Promise(r => setTimeout(r, attempt * 700));
    }
  }
  setStatus("err", "连接失败 · 点击重试");
  return false;
}

pill.onclick = async () => {
  if (pill.classList.contains("err")) {
    setStatus("wait", "重连中…");
    await createSession();
  }
};

function renderConvoList(filter) {
  const label = '<div class="side-label">对话记录</div>';
  const q = (filter || "").trim().toLowerCase();
  const shown = q ? conversations.filter(c => c.title.toLowerCase().includes(q))
                  : conversations;
  const items = shown.map(c =>
    '<div class="convo' + (c.id === sessionId ? " active" : "") + '" data-id="' + c.id +
    '" title="' + escapeHtml(c.title) + '">' + escapeHtml(c.title) +
    '<button class="del" title="删除">✕</button></div>').join("");
  convoList.innerHTML = label + items;
  convoList.querySelectorAll(".convo").forEach(el => {
    el.onclick = ev => {
      if (ev.target.classList.contains("del")) {
        deleteConversation(el.dataset.id);
        return;
      }
      switchConversation(el.dataset.id);
      $("sidebar").classList.add("hidden");     // mobile: close drawer
      $("backdrop").classList.remove("show");
    };
  });
}
$("convoSearch").addEventListener("input", e => renderConvoList(e.target.value));

function deleteConversation(id) {
  // local removal + best-effort server-side sync
  apiFetch("/api/sessions/" + id, { method: "DELETE" }).catch(() => {});
  conversations = conversations.filter(c => c.id !== id);
  saveConvos();
  renderConvoList($("convoSearch").value);
  if (id === sessionId) newConversation();
}

function showChat(show) {
  welcomeEl.style.display = show ? "none" : "";
  chatEl.style.display = show ? "" : "none";
}

function newConversation() {
  chatEl.innerHTML = "";
  convTitle.textContent = "新对话";
  showChat(false);
  createSession().then(() => renderConvoList($("convoSearch").value));
  input.focus();
}

async function switchConversation(id) {
  sessionId = id;
  chatEl.innerHTML = "";
  renderConvoList($("convoSearch").value);
  const c = conversations.find(c => c.id === id);
  convTitle.textContent = c ? c.title : "对话";
  showChat(true);

  // prefer the local copy (instant, survives server restarts)
  if (c && c.messages && c.messages.length) {
    for (const m of c.messages)
      appendMessage(m.role === "user" ? "user" : "bot", m.content,
                    m.role !== "user", m.meta);
    scrollBottom(true);
    input.focus();
    return;
  }

  // fall back to the server's in-memory history, then cache it locally
  try {
    const r = await apiFetch("/api/history/" + id);
    if (r.status === 404) {           // server restarted; session gone
      conversations = conversations.filter(x => x.id !== id);
      saveConvos();
      return newConversation();
    }
    const j = await r.json();
    for (const m of j.messages)
      appendMessage(m.role === "user" ? "user" : "bot", m.content, m.role !== "user");
    if (c) { c.messages = j.messages; saveConvos(); }
    scrollBottom(true);
  } catch (e) { /* keep empty view */ }
  input.focus();
}
/* ---------------- message rendering ---------------- */

function nearBottom() {
  return wrapEl.scrollHeight - wrapEl.scrollTop - wrapEl.clientHeight < 120;
}
function scrollBottom(force) {
  if (force || nearBottom()) wrapEl.scrollTop = wrapEl.scrollHeight;
}

function appendMessage(role, content, mdRender, meta) {
  showChat(true);
  const isUser = role === "user";
  const row = document.createElement("div");
  row.className = "row " + (isUser ? "user" : "bot");
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (!isUser) {
    bubble.innerHTML = '<div class="steps"></div><div class="md body"></div>';
    setBody(bubble, content || "", mdRender);
    addBotExtras(bubble, content, meta, false);
  } else {
    bubble.textContent = content || "";
  }
  row.appendChild(bubble);
  chatEl.appendChild(row);
  scrollBottom(isUser);
  return bubble;
}

function setBody(bubble, text, asMarkdown) {
  const body = bubble.querySelector(".body");
  if (!body) return;
  if (asMarkdown) {
    try { body.innerHTML = renderMarkdown(text); }
    catch (e) { body.textContent = text; }   // never let a render bug freeze updates
  } else {
    body.textContent = text;
  }
  body.classList.add("md");
  scrollBottom();
}

/* meta line + hover action bar under a finished bot message */
function addBotExtras(bubble, content, meta, allowRegen) {
  const body = bubble.querySelector(".body");
  if (!body) return;
  body.querySelectorAll(".meta,.msg-actions").forEach(e => e.remove());
  if (meta && meta.duration_s != null) {
    let html = '<div class="meta">✅ 用时 ' + meta.duration_s + ' 秒 · ' +
               (meta.steps || "?") + " 步";
    if (typeof meta.cost_usd === "number") html += " · 约 $" + meta.cost_usd.toFixed(4);
    html += "</div>";
    body.insertAdjacentHTML("beforeend", html);
  }
  let bar = '<button data-copy title="复制回答">📋 复制</button>';
  if (allowRegen) bar += '<button data-regen title="重新回答">🔄 重新生成</button>';
  body.insertAdjacentHTML("beforeend", '<div class="msg-actions">' + bar + "</div>");
  body.querySelector("[data-copy]").onclick = () => {
    navigator.clipboard.writeText(content).then(() => setStatus("ok", "已复制 ✓"), () => {});
  };
  const regenBtn = body.querySelector("[data-regen]");
  if (regenBtn) regenBtn.onclick = () => { if (!busy && lastUserText) sendMessage(lastUserText); };
}

/* human-friendly error mapping for non-technical users */
function humanizeError(raw) {
  const s = String(raw || "");
  if (/401|unauthorized|令牌/i.test(s)) return "访问令牌不正确或已失效,请刷新页面重新输入。";
  if (/api.?key|authentication|invalid.*key/i.test(s)) return "API 密钥无效或未配置,请联系服务管理员检查配置。";
  if (/quota|insufficient|429|rate.?limit|too many/i.test(s)) return "请求太频繁或模型额度不足,请稍等几秒再试。";
  if (/timeout|timed out/i.test(s)) return "响应超时了,模型可能正忙,请点击重试。";
  if (/network|connect|fetch|failed/i.test(s)) return "网络连接失败,请确认服务正在运行后重试。";
  return "出了点小问题:" + s.slice(0, 200);
}
/* ---------------- chat streaming ---------------- */

function setBusyUI(b) {
  busy = b;
  sendBtn.classList.toggle("stop", b);
  sendBtn.textContent = b ? "■" : "➤";
  sendBtn.title = b ? "停止生成" : "发送";
}

/* friendly progress strip: "🔧 正在查看文件 · 第 2 步 · 已用 8 秒" */
const TOOL_LABELS = {
  run_shell: "执行命令", python_repl: "运行 Python 代码", read_file: "查看文件",
  write_file: "写入文件", edit_file: "修改文件", list_dir: "浏览目录",
  web_fetch: "访问网页", remember: "保存记忆", recall: "回忆信息",
  ingest_document: "索引文档", search_knowledge: "搜索知识库",
};
let phaseBase = "", phaseTimer = null, runStart = 0;
function setPhase(kind, toolName, stepNo) {
  if (kind === "thinking") phaseBase = "🤔 正在思考";
  else if (kind === "tool") phaseBase = "🔧 正在" +
    (TOOL_LABELS[toolName] || ("调用 " + toolName)) +
    (stepNo ? " · 第 " + stepNo + " 步" : "");
  else if (kind === "writing") phaseBase = "✍️ 正在撰写回答";
  else if (kind === "wait") phaseBase = "⏸ 等待你的确认";
  runStatusEl.textContent = phaseBase;
}
function startPhases() {
  runStart = Date.now();
  setPhase("thinking");
  runStatusEl.style.display = "block";
  clearInterval(phaseTimer);
  phaseTimer = setInterval(() => {
    const s = Math.round((Date.now() - runStart) / 1000);
    runStatusEl.textContent = phaseBase + " · 已用 " + s + " 秒";
  }, 500);
}
function stopPhases() { clearInterval(phaseTimer); runStatusEl.style.display = "none"; }

/* subtle completion nudge when the tab is in the background */
function notifyDone() {
  if (!document.hidden) return;
  document.title = "✅ 任务完成 — NovaAgent";
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.setValueAtTime(880, ctx.currentTime);
    o.frequency.setValueAtTime(660, ctx.currentTime + .13);
    g.gain.value = .06;
    o.start(); o.stop(ctx.currentTime + .28);
  } catch (e) {}
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) document.title = "NovaAgent";
});

async function sendMessage(forcedText) {
  if (busy) {                       // acting as STOP button
    requestStop();
    return;
  }
  const text = (forcedText != null ? forcedText : input.value).trim();
  if (!text || !sessionId) return;
  if (forcedText == null) { input.value = ""; }
  lastUserText = text;
  autoGrow();
  setBusyUI(true);

  appendMessage("user", text);
  recordUserMessage(text);
  convTitle.textContent = text.slice(0, 24);

  // animated hint shown while the model is silently thinking
  const waitingEl = document.createElement("div");
  waitingEl.className = "waiting";
  waitingEl.textContent = "🤔 正在思考,请稍候…";
  chatEl.appendChild(waitingEl);
  scrollBottom(true);
  const clearWaiting = () => waitingEl.remove();
  startPhases();

  const bubble = appendMessage("bot", "");
  const stepsEl = bubble.querySelector(".steps");
  const bodyEl = () => bubble.querySelector(".body");
  let acc = "";
  let reasonText = "";
  let renderQueued = false;

  const queueRender = () => {
    if (renderQueued) return;
    renderQueued = true;
    setTimeout(() => { renderQueued = false; setBody(bubble, acc, true); }, 60);
  };

  // single exit point: unlocks UI exactly once
  let finished = false;
  function finish(note, color, meta) {
    if (finished) return;
    finished = true;
    if (note) {
      bodyEl().insertAdjacentHTML("beforeend",
        '<p style="color:' + color + ';font-size:12.5px">' + note + "</p>");
    }
    const liveR = stepsEl.querySelector("details.reasoning[open]");
    if (liveR) liveR.open = false;           // collapse thinking panel
    stepsEl.querySelectorAll(".approve-card:not(.done) button").forEach(b => b.disabled = true);
    bodyEl().classList.remove("cursor");
    clearWaiting();
    stopPhases();
    persistBotMessage(acc, meta);            // auto-save the final answer locally
    addBotExtras(bubble, acc, meta, true);   // meta line + 复制 / 重新生成
    apiFetch("/api/stats/" + sessionId).then(r => r.json()).then(s => {
      tokenInfo.textContent = "tokens: " + s.total_tokens;
      modelInfo.textContent = "model: " + s.model;
    }).catch(() => {});
    setBusyUI(false);
    scrollBottom();
    input.focus();
    notifyDone();
  }

  // ---- step 1: start the run ---- //
  let resp;
  const postChat = () => apiFetch("/api/chat/" + sessionId, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text,
                           confirm_dangerous: getConfirmMode() }),
  });
  try {
    resp = await postChat();
    if (resp.status === 404) {                    // stale session → recreate & retry
      await createSession(2);
      resp = await postChat();
    }
  } catch (e) {
    finish("❌ " + humanizeError(e) +
           ' <button class="retry" style="border:none;background:none;color:var(--accent);cursor:pointer;font-size:12.5px">🔄 重试</button>',
           "var(--red)");
    wireRetry(bubble);
    return;
  }
  if (resp.status === 409) {
    finish("⚠️ 上一个任务仍在运行,请先停止或等待完成", "var(--orange)");
    return;
  }
  if (!resp.ok) { finish("❌ " + humanizeError("HTTP " + resp.status), "var(--red)"); return; }

  function wireRetry(b) {
    const btn = b.querySelector(".retry");
    if (btn) btn.onclick = () => { if (!busy && lastUserText) sendMessage(lastUserText); };
  }

  const ctype = (resp.headers.get("content-type") || "").split(";")[0].trim();

  // ---- legacy backend: the POST response body IS the event stream ---- //
  const legacyRead = async sseResp => {
    const reader = sseResp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop();
        for (const part of parts) {
          if (!part.startsWith("data: ")) continue;
          let d; try { d = JSON.parse(part.slice(6)); } catch (e) { continue; }
          try { handleEvent(d); }
          catch (err) { console.error("handleEvent error:", err); }
          if (finished) return;
        }
      }
      persistBotMessage(acc);
      finish();
    } catch (e) {
      finish("❌ " + humanizeError(String(e)), "var(--red)");
    }
  };

  if (!ctype.includes("json")) {           // old server process detected
    verInfo.textContent = "ui: " + UI_VERSION + " · srv: " + serverVersion + " (旧)";
    stepsEl.insertAdjacentHTML("beforeend",
      '<div class="step" style="border-left-color:var(--red)">⚠️ <b>检测到旧版服务进程</b>:'
      + "它不支持新的事件接口,实时更新和停止可能失效。"
      + "请完全停止当前的 nova serve 进程并重新运行,然后刷新本页面。</div>");
    scrollBottom();
    await legacyRead(resp);
    return;
  }

  // ---- step 2 (new backend): poll the run's event buffer ---- //
  let runId;
  try { runId = (await resp.json()).run_id; }
  catch (e) { finish("❌ 响应解析失败", "var(--red)"); return; }

  let idx = 0;
  let fails = 0;
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const pollLoop = async () => {
    while (!finished) {
      let j;
      try {
        const r = await apiFetch(`/api/events/${sessionId}/${runId}?after=${idx}`);
        if (r.status === 404) throw new Error("run lost");
        if (!r.ok) throw new Error("HTTP " + r.status);
        j = await r.json();
      } catch (e) {
        fails++;
        if (fails >= 12) {          // ~10s of consecutive failures → give up cleanly
          finish("❌ 无法获取任务进度,请确认服务正在运行后重试", "var(--red)");
          return;
        }
        await sleep(800);
        continue;                     // transient network hiccup → just retry
      }
      fails = 0;
      for (const d of j.events) {
        try { handleEvent(d); }                   // 单个事件出错不拖垮整个轮询
        catch (err) { console.error("handleEvent error:", err); }
        if (finished) return;
      }
      idx += j.events.length;
      persistBotMessage(acc);            // auto-save progress locally
      if (j.done) { finish(); return; }
      await sleep(400);
    }
  };

  const ensurePanel = () => {
    let det = stepsEl.querySelector("details.reasoning");
    if (!det) {
      stepsEl.insertAdjacentHTML("afterbegin",
        '<details class="reasoning" open><summary>🤔 思考过程与工具调用</summary>' +
        '<div class="rbody"></div><div class="rtlist"></div></details>');
      det = stepsEl.querySelector("details.reasoning");
    }
    return det;
  };

  let pendingMeta = null;

  function handleEvent(d) {
    if (d.type === "delta") {
      acc += d.text;
      queueRender();
      setPhase("writing");
    } else if (d.type === "reasoning") {
      reasonText += d.text;
      const det = ensurePanel();
      det.querySelector(".rbody").textContent = reasonText.slice(-2000);
      det.querySelector(".rbody").scrollTop = 1e6;
      setPhase("thinking");
      scrollBottom();
    } else if (d.type === "step") {
      if (d.kind !== "act") return;           // think/final stay out of the panel
      const det = ensurePanel();
      const args = Object.entries(d.args || {})
        .map(([k, v]) => k + "=" + String(v).slice(0, 60)).join(", ");
      det.querySelector(".rtlist").insertAdjacentHTML("beforeend",
        '<div class="rtool">🔧 <b>' + escapeHtml(d.tool || "") + "</b>(" +
        escapeHtml(args) + ")\n" +
        escapeHtml((d.observation || "").slice(0, 400)) + "</div>");
      det.querySelector(".rtlist").scrollTop = 1e6;
      det.open = true;                        // keep live progress visible
      setPhase("tool", d.tool, d.step);
      scrollBottom();
    } else if (d.type === "approval_request") {
      // inline consent card for dangerous tools (novice-friendly safety gate)
      const label = TOOL_LABELS[d.tool] || d.tool;
      const argSummary = Object.entries(d.args || {})
        .map(([k, v]) => k + ": " + String(v).slice(0, 300)).join("\n");
      stepsEl.insertAdjacentHTML("beforeend",
        '<div class="approve-card"><div class="at">⚠️ NovaAgent 想要「' +
        escapeHtml(label) + '」,需要你的确认</div>' +
        '<div class="ac">' + escapeHtml(argSummary) + "</div>" +
        '<div class="abtns"><button class="ok">✓ 允许执行</button>' +
        '<button class="no">✕ 跳过这步</button></div></div>');
      const card = stepsEl.lastElementChild;
      card.querySelectorAll(".abtns button").forEach(btn => {
        btn.onclick = async () => {
          if (card.classList.contains("done")) return;
          card.classList.add("done");
          card.querySelectorAll("button").forEach(b => b.disabled = true);
          const yes = btn.classList.contains("ok");
          card.querySelector(".abtns").insertAdjacentHTML("afterend",
            '<div class="verdict">' + (yes ? "✓ 已允许执行" : "✕ 已跳过这一步") + "</div>");
          try {
            await apiFetch(`/api/approve/${sessionId}/${runId}`, {
              method: "POST", body: JSON.stringify({ approved: yes }) });
          } catch (e) { console.error("approve failed:", e); }
        };
      });
      setPhase("wait");
      scrollBottom(true);
    } else if (d.type === "final") {
      acc = d.text || acc;                    // authoritative answer text
      setBody(bubble, acc, true);
      pendingMeta = { steps: d.steps, tokens: d.tokens,
                      cost_usd: d.cost_usd, duration_s: d.duration_s };
      if (d.reason === "user_stopped")
        finish("⏹ 已按你的要求停止", "var(--dim)", pendingMeta);
      else if (d.reason && d.reason !== "completed")
        finish("⚠️ 提前停止: " + escapeHtml(d.reason), "var(--orange)", pendingMeta);
    } else if (d.type === "error") {
      finish("❌ " + humanizeError(d.message), "var(--red)");
    } else if (d.type === "done") {
      finish(undefined, undefined, pendingMeta);   // all events delivered
    }
  }

  pollLoop();
}

/* stop: tell the server to finish the run gracefully. The stream stays open
   until the server emits final+done, so the UI never misses the outcome. */
function requestStop() {
  apiFetch("/api/stop/" + sessionId, { method: "POST" }).catch(() => {});
}

/* ---------------- composer & init ---------------- */

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}

/* confirmation mode: ask before dangerous tools execute (default on) */
function getConfirmMode() {
  try { return localStorage.getItem("nova_confirm") !== "0"; } catch (e) { return true; }
}
const confirmToggle = $("confirmToggle");
confirmToggle.checked = getConfirmMode();
confirmToggle.onchange = () => {
  try { localStorage.setItem("nova_confirm", confirmToggle.checked ? "1" : "0"); }
  catch (e) {}
};

sendBtn.onclick = () => sendMessage();
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
input.addEventListener("input", autoGrow);
$("newChat").onclick = newConversation;
$("menuBtn").onclick = () => {
  const sb = $("sidebar");
  sb.classList.toggle("hidden");
  $("backdrop").classList.toggle("show", !sb.classList.contains("hidden"));
};
$("backdrop").onclick = () => {
  $("sidebar").classList.add("hidden");
  $("backdrop").classList.remove("show");
};
document.querySelectorAll(".card").forEach(c =>
  c.addEventListener("click", () => sendMessage(c.dataset.prompt)));   // 一键体验

loadConvos();
if (window.innerWidth < 860) $("sidebar").classList.add("hidden");   // mobile: collapsed
(async function init() {
  const ok = await createSession();
  renderConvoList($("convoSearch").value);
  if (ok && conversations.length) showChat(false);
})();
