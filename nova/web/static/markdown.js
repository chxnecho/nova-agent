/* ================= NovaAgent markdown/content helpers (pure) =================
   UMD module: exposes `window.NovaMarkdown` in the browser and
   `module.exports` under Node (used by `node --test` for unit tests). */

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.NovaMarkdown = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

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
    /* single pass: skip stash placeholders (\x00<idx>\x00), highlight digits elsewhere
       — otherwise the number regex corrupts placeholder indices and restore breaks */
    out = out.replace(/\x00(\d+)\x00|\b(\d+(?:\.\d+)?)\b/g,
      (m, ph, num) => (ph !== undefined) ? m : '<span class="tok-n">' + num + "</span>");
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

  function humanizeError(raw) {
    const s = String(raw || "");
    if (/401|unauthorized|令牌/i.test(s)) return "访问令牌不正确或已失效,请刷新页面重新输入。";
    if (/api.?key|authentication|invalid.*key/i.test(s)) return "API 密钥无效或未配置,请联系服务管理员检查配置。";
    if (/quota|insufficient|429|rate.?limit|too many/i.test(s)) return "请求太频繁或模型额度不足,请稍等几秒再试。";
    if (/timeout|timed out/i.test(s)) return "响应超时了,模型可能正忙,请点击重试。";
    if (/network|connect|fetch|failed/i.test(s)) return "网络连接失败,请确认服务正在运行后重试。";
    return "出了点小问题:" + escapeHtml(s.slice(0, 200));
  }

  return { escapeHtml, pickKeywords, highlight, renderInline,
           renderMarkdown, humanizeError };
});
