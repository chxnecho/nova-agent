#!/usr/bin/env python3
"""Real-browser reproduction test: does the assistant bubble grow incrementally?

Usage: .venv/bin/python scripts/browser_repro.py [port]
"""
import sys, time, asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.async_api import async_playwright  # noqa: E402


async def main(port):
    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
        try:
            browser = await p.firefox.launch(**launch_kwargs)
        except Exception:
            browser = await p.firefox.launch(
                executable_path="/usr/bin/firefox", **launch_kwargs)
        page = await browser.new_page()
        logs = []
        page.on("console", lambda m: logs.append(f"[console.{m.type}] {m.text}"))
        def on_pageerror(e):
            logs.append(f"[PAGEERROR] {e}")
            stack = getattr(e, "stack", None)
            if stack:
                logs.append("[STACK]\n" + str(stack))
        page.on("pageerror", on_pageerror)

        await page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        await page.wait_for_function(
            "document.getElementById('verInfo').textContent.includes('v9')",
            timeout=15000)
        print("版本标识:", await page.inner_text("#verInfo"))

        await page.fill("#input", "不要调用工具。用三句话介绍你自己。")
        await page.click("#sendBtn")

        t0 = time.monotonic()
        samples = []
        last_len, last_busy = -1, None
        while time.monotonic() - t0 < 90:
            state = await page.evaluate("""() => ({
                len: (document.querySelector('.row.bot .body') || {textContent:''}).textContent.length,
                busy: (typeof busy !== 'undefined') ? busy : null,
                tools: document.querySelectorAll('.rtlist .rtool').length,
                reasoning: (document.querySelector('.rbody')||{textContent:''}).textContent.length,
                pill: document.getElementById('statusPill').textContent,
            })""")
            t = round(time.monotonic() - t0, 1)
            if state["len"] != last_len or state["busy"] != last_busy:
                samples.append((t, state["len"], state["tools"], state["reasoning"], state["busy"]))
                print(f"t={t:6.1f}s  正文={state['len']:4d}字  工具={state['tools']}  "
                      f"思考={state['reasoning']:4d}字  busy={state['busy']}")
                last_len, last_busy = state["len"], state["busy"]
            if not state["busy"]:
                break
            await page.wait_for_timeout(300)

        incremental = sum(1 for i in range(1, len(samples)) if samples[i][1] > samples[i-1][1] > 0)
        print(f"\n采样点: {len(samples)} | 正文长度变化次数: {incremental}")
        print("结论:", "✅ 实时逐字更新" if incremental >= 3 else
              ("❌ 全部内容最后一次性出现" if incremental <= 1 else "⚠️ 更新不连贯"))

        # local persistence check
        saved = await page.evaluate(
            "() => JSON.parse(localStorage.getItem('nova_convos_v2') || '[]')")
        target = next((c for c in saved if c.get("messages")), None)
        if target and any(m["role"] == "bot" for m in target["messages"]):
            roles = [m["role"] for m in target["messages"]]
            print("本地持久化: ✅", roles)
        else:
            print("本地持久化: ❌ 未找到已保存消息")

        # collapsed panel check
        collapsed = await page.evaluate(
            "() => { const d = document.querySelector('details.reasoning');"
            " return d ? !d.open : null; }")
        print("完成后思考面板折叠:", "✅" if collapsed else ("(无面板)" if collapsed is None else "❌"))

        if logs:
            print("\n浏览器控制台:")
            for m in logs[:15]:
                print(" ", m)
        await browser.close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8321
    asyncio.run(main(port))
