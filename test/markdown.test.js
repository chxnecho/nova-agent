/* Unit tests for nova/web/static/markdown.js (pure content helpers).
   Run with:  node --test test/markdown.test.js   (from repo root)
   Uses Node's built-in test runner so no npm toolchain is required. */

"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const m = require(path.resolve(__dirname, "../nova/web/static/markdown.js"));

test("escapeHtml escapes HTML metacharacters", () => {
  assert.equal(m.escapeHtml("<script>alert('x')</script>"),
               "&lt;script&gt;alert('x')&lt;/script&gt;");
});

test("renderMarkdown produces headings, paragraphs, inline styles and code", () => {
  const html = m.renderMarkdown("# Title\n\nhello **bold** and `code`");
  assert.ok(html.includes("<h1>Title</h1>"));
  assert.ok(html.includes("<strong>bold</strong>"));
  assert.ok(html.includes('<code class="ic">code</code>'));
  assert.ok(html.includes("<p>"));
});

test("renderMarkdown escapes raw HTML instead of injecting it (XSS guard)", () => {
  const html = m.renderMarkdown("<img src=x onerror=alert(1)>\n\n[ok](https://a.b)");
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img"));
});

test("renderMarkdown renders fenced code blocks with language header", () => {
  const html = m.renderMarkdown("```python\n# hi\nx = 1\ny = 2\n```");
  assert.ok(html.includes('class="codeblock"'));
  assert.ok(html.includes("python"));
  assert.ok(html.includes("<pre><code>"));
  assert.ok(html.includes("tok-c"));
});

test("renderMarkdown renders lists and blockquotes", () => {
  const html = m.renderMarkdown("- a\n- b\n\n> quoted");
  assert.ok(html.includes("<ul><li>a</li><li>b</li></ul>"));
  assert.ok(html.includes("<blockquote>quoted</blockquote>"));
});

test("humanizeError maps common failures to friendly messages", () => {
  assert.match(m.humanizeError("HTTP 401 unauthorized"), /令牌|访问令牌/);
  assert.match(m.humanizeError("rate limit exceeded 429"), /太频繁|额度/);
  assert.match(m.humanizeError("connection refused"), /网络连接失败/);
});

test("humanizeError fallback escapes the raw message (XSS guard)", () => {
  const out = m.humanizeError("<script>alert(1)</script>");
  assert.ok(out.includes("&lt;script&gt;"));
  assert.ok(!out.includes("<script>"));
});
