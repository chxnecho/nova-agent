/* ================= NovaAgent web client ================= */

const $ = id => document.getElementById(id);
const chatEl = $("chat"), welcomeEl = $("welcome"), wrapEl = $("chatWrap");
const input = $("input"), sendBtn = $("sendBtn");
const pill = $("statusPill"), modelInfo = $("modelInfo"), tokenInfo = $("tokenInfo");
const convoList = $("convoList"), convTitle = $("convTitle");
const verInfo = $("verInfo");
const UI_VERSION = "v12-local";

let sessionId = null;
let busy = false;
let conversations = [];   // [{id, title, ts, messages:[{role, content}]}]
let serverVersion = "?";
const STORE_KEY = "nova_convos_v2";

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
function persistBotMessage(content) {
  const c = currentConvo();
  if (!c) return;
  const last = c.messages[c.messages.length - 1];
  if (!content) {                          // nothing to store yet
    if (last && last.role === "bot") c.messages.pop();
    saveConvos(); return;
  }
  if (last && last.role === "bot") last.content = content;
  else c.messages.push({ role: "bot", content });
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

/* ---------------- status & sessions ---------------- */

function setStatus(state, text) {
  pill.className = "pill" + (state === "ok" ? "" : " " + state);
  pill.textContent = text;
}

async function createSession(retries = 6) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const r = await fetch("/api/sessions", { method: "POST" });
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

function renderConvoList() {
  const label = '<div class="side-label">对话记录</div>';
  const items = conversations.map(c =>
    '<div class="convo' + (c.id === sessionId ? " active" : "") + '" data-id="' + c.id +
    '" title="' + escapeHtml(c.title) + '">' + escapeHtml(c.title) +
    '<button class="del" title="删除">✕</button></div>').join("");
  convoList.innerHTML = label + (items || "");
  convoList.querySelectorAll(".convo").forEach(el => {
    el.onclick = ev => {
      if (ev.target.classList.contains("del")) {
        deleteConversation(el.dataset.id);
        return;
      }
      switchConversation(el.dataset.id);
    };
  });
}

function deleteConversation(id) {
  // local removal + best-effort server-side sync
  fetch("/api/sessions/" + id, { method: "DELETE" }).catch(() => {});
  conversations = conversations.filter(c => c.id !== id);
  saveConvos();
  renderConvoList();
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
  createSession().then(renderConvoList);
  input.focus();
}

async function switchConversation(id) {
  sessionId = id;
  chatEl.innerHTML = "";
  renderConvoList();
  const c = conversations.find(c => c.id === id);
  convTitle.textContent = c ? c.title : "对话";
  showChat(true);

  // prefer the local copy (instant, survives server restarts)
  if (c && c.messages && c.messages.length) {
    for (const m of c.messages)
      appendMessage(m.role === "user" ? "user" : "bot", m.content, m.role !== "user");
    scrollBottom(true);
    input.focus();
    return;
  }

  // fall back to the server's in-memory history, then cache it locally
  try {
    const r = await fetch("/api/history/" + id);
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

function appendMessage(role, content, mdRender) {
  showChat(true);
  const isUser = role === "user";
  const row = document.createElement("div");
  row.className = "row " + (isUser ? "user" : "bot");
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (!isUser) {
    bubble.innerHTML = '<div class="steps"></div><div class="md body"></div>';
    setBody(bubble, content || "", mdRender);
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

/* ---------------- chat streaming ---------------- */

function setBusyUI(b) {
  busy = b;
  sendBtn.classList.toggle("stop", b);
  sendBtn.textContent = b ? "■" : "➤";
  sendBtn.title = b ? "停止生成" : "发送";
}

async function sendMessage() {
  if (busy) {                       // acting as STOP button
    requestStop();
    return;
  }
  const text = input.value.trim();
  if (!text || !sessionId) return;
  input.value = ""; autoGrow();
  setBusyUI(true);

  appendMessage("user", text);
  recordUserMessage(text);
  convTitle.textContent = text.slice(0, 24);

  // animated hint shown while the model is silently thinking
  const waitingEl = document.createElement("div");
  waitingEl.className = "waiting";
  waitingEl.textContent = "⏳ 正在思考与执行,请稍候…";
  chatEl.appendChild(waitingEl);
  scrollBottom(true);
  const clearWaiting = () => waitingEl.remove();

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
  function finish(note, color) {
    if (finished) return;
    finished = true;
    if (note) {
      bodyEl().insertAdjacentHTML("beforeend",
        '<p style="color:' + color + ';font-size:12.5px">' + note + "</p>");
    }
    const liveR = stepsEl.querySelector("details.reasoning[open]");
    if (liveR) liveR.open = false;           // collapse thinking panel
    bodyEl().classList.remove("cursor");
    clearWaiting();
    persistBotMessage(acc);                  // auto-save the final answer locally
    fetch("/api/stats/" + sessionId).then(r => r.json()).then(s => {
      tokenInfo.textContent = "tokens: " + s.total_tokens;
      modelInfo.textContent = "model: " + s.model;
    }).catch(() => {});
    setBusyUI(false);
    scrollBottom();
    input.focus();
  }

  // ---- step 1: start the run ---- //
  // Compatible with both backends:
  //   new server -> JSON {"run_id"} then poll /api/events/{sid}/{rid}
  //   old server -> the POST itself returns the SSE event stream
  let resp;
  const postChat = () => fetch("/api/chat/" + sessionId, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text }),
  });
  try {
    resp = await postChat();
    if (resp.status === 404) {                    // stale session → recreate & retry
      await createSession(2);
      resp = await postChat();
    }
  } catch (e) {
    finish("❌ 连接失败: " + escapeHtml(String(e)), "var(--red)");
    return;
  }
  if (resp.status === 409) {
    finish("⚠️ 上一个任务仍在运行,请先停止或等待完成", "var(--orange)");
    return;
  }
  if (!resp.ok) { finish("❌ HTTP " + resp.status, "var(--red)"); return; }

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
          try { handleEvent(d); }                 // 同样加保护
          catch (err) { console.error("handleEvent error:", err); }
          if (finished) return;
        }
      }
      persistBotMessage(acc);
      finish();
    } catch (e) {
      finish("❌ 连接中断: " + escapeHtml(String(e)), "var(--red)");
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
  // Plain request/response polling: works identically in every browser and
  // through every proxy, no streaming support required.
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const pollLoop = async () => {
    while (!finished) {
      let j;
      try {
        const r = await fetch(`/api/events/${sessionId}/${runId}?after=${idx}`);
        if (r.status === 404) throw new Error("run lost");
        if (!r.ok) throw new Error("HTTP " + r.status);
        j = await r.json();
      } catch (e) {
        fails++;
        if (fails >= 12) {          // ~10s of consecutive failures → give up cleanly
          finish("❌ 无法获取事件(" + escapeHtml(String(e.message || e)) +
                 "),请确认 nova serve 正在运行", "var(--red)");
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

  function handleEvent(d) {
    if (d.type === "delta") {
      acc += d.text;
      queueRender();
    } else if (d.type === "reasoning") {
      reasonText += d.text;
      const det = ensurePanel();
      det.querySelector(".rbody").textContent = reasonText.slice(-2000);
      det.querySelector(".rbody").scrollTop = 1e6;
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
      scrollBottom();
          acc = d.text || acc;
          setBody(bubble, acc, true);
          if (d.reason === "user_stopped")
            finish("⏹ 已按你的要求停止", "var(--dim)");
          else if (d.reason && d.reason !== "completed")
            finish("⚠️ 提前停止: " + escapeHtml(d.reason), "var(--orange)");
        } else if (d.type === "error") {
          stepsEl.insertAdjacentHTML("beforeend",
            '<div class="step" style="border-left-color:var(--red)">❌ ' +
            escapeHtml(d.message) + "</div>");
          scrollBottom();
        } else if (d.type === "done") {
          finish();                         // all events delivered
        }
  }

  pollLoop();
}

/* stop: tell the server to finish the run gracefully. The stream stays open
   until the server emits final+done, so the UI never misses the outcome. */
function requestStop() {
  fetch("/api/stop/" + sessionId, { method: "POST" }).catch(() => {});
}

/* ---------------- composer & init ---------------- */

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}

sendBtn.onclick = sendMessage;
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
input.addEventListener("input", autoGrow);
$("newChat").onclick = newConversation;
$("menuBtn").onclick = () => $("sidebar").classList.toggle("hidden");
document.querySelectorAll(".card").forEach(c =>
  c.addEventListener("click", () => { input.value = c.dataset.prompt; autoGrow(); input.focus(); }));

loadConvos();
if (window.innerWidth < 860) $("sidebar").classList.add("hidden");   // mobile: collapsed
(async function init() {
  const ok = await createSession();
  renderConvoList();
  if (ok && conversations.length) showChat(false);
})();
