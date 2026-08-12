#!/usr/bin/env python3
"""长驻浏览器会话 worker：由 app.py 以子进程方式拉起，用 JSON-lines 收发指令。

与 browser_render_worker.py 的区别是它不退出——AI 要在同一个页面上连续点击、
输入、翻页，每次重开浏览器就丢掉了登录态和滚动位置。

协议：stdin 每行一个 JSON 指令，stdout 每行一个 JSON 结果。
    {"action": "goto", "url": "https://..."}
    {"action": "snapshot"}                      -> 截图 + 可交互元素清单
    {"action": "click", "index": 3}
    {"action": "type", "index": 5, "text": "..."}
    {"action": "scroll", "delta": 600}
    {"action": "upload", "index": 2, "paths": ["/abs/path"]}
    {"action": "close"}

放在子进程里跑的理由和截图 worker 一样：Chromium 卡死时父进程可以直接 kill
整个进程组，绝不把渲染进程泄漏在服务器上。
"""
from __future__ import annotations

import base64
import glob
import json
import sys
from typing import Any

VIEWPORT = {"width": 1280, "height": 860}

# 只允许这些标签成为可点目标，避免把整页的 div 都塞给模型。
INTERACTIVE_SELECTOR = (
    "a[href], button, input:not([type=hidden]), select, textarea, "
    "[role=button], [role=link], [role=tab], [role=checkbox], [role=menuitem], [onclick]"
)

# 抽取可交互元素：给每个元素一个稳定序号和一句人能看懂的说明，
# 模型按序号操作，不需要（也不应该）自己写 CSS 选择器。
COLLECT_SCRIPT = """
(selector) => {
  const out = [];
  const nodes = document.querySelectorAll(selector);
  for (const node of nodes) {
    const rect = node.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    const style = window.getComputedStyle(node);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    // 只收视口附近的元素：模型看到的是当前这一屏的截图，
    // 给它一个屏幕外的按钮序号只会让它点了没反应。
    if (rect.bottom < -200 || rect.top > window.innerHeight + 200) continue;
    const label = (
      node.getAttribute('aria-label') ||
      node.innerText ||
      node.value ||
      node.getAttribute('placeholder') ||
      node.getAttribute('title') ||
      node.getAttribute('alt') || ''
    ).replace(/\\s+/g, ' ').trim().slice(0, 120);
    const tag = node.tagName.toLowerCase();
    const type = (node.getAttribute('type') || '').toLowerCase();
    // 直接把序号写进 DOM：动作路径按这个属性精确定位，
    // 而不是在 Python 侧重新跑一遍过滤——两套过滤规则一旦有差异，
    // 序号就会错位，表现为「AI 点了另一个按钮」这种最难查的问题。
    node.setAttribute('data-wb-idx', String(out.length));
    out.push({
      tag,
      type,
      label,
      href: tag === 'a' ? (node.getAttribute('href') || '') : '',
      editable: tag === 'textarea' || (tag === 'input' && !['button','submit','checkbox','radio','file','hidden'].includes(type)),
      file_input: tag === 'input' && type === 'file',
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    });
  }
  return out;
}
"""


def find_chromium() -> str | None:
    candidates = [
        "/www/workbench/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/www/workbench/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium",
    ]
    for pattern in candidates:
        for path in sorted(glob.glob(pattern)):
            return path
    return None


def load_playwright():
    """优先 patchright（反检测更好），没有就退回标准 playwright。"""
    try:
        from patchright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        from playwright.sync_api import sync_playwright
        return sync_playwright


def reply(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def collect(page, include_shot: bool) -> dict[str, Any]:
    # 先清掉上一轮的标记：页面局部刷新后旧标记会残留，导致序号指向已经不在
    # 当前清单里的元素。
    page.evaluate("() => document.querySelectorAll('[data-wb-idx]').forEach((n) => n.removeAttribute('data-wb-idx'))")
    elements = page.evaluate(COLLECT_SCRIPT, INTERACTIVE_SELECTOR)
    for index, item in enumerate(elements):
        item["index"] = index
    text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 12000) : ''")
    payload: dict[str, Any] = {
        "ok": True,
        "url": page.url,
        "title": page.title(),
        "elements": elements[:120],
        "text": text,
        "scroll": page.evaluate("() => ({ y: Math.round(window.scrollY), height: Math.round(document.body ? document.body.scrollHeight : 0) })"),
    }
    if include_shot:
        payload["screenshot"] = base64.b64encode(page.screenshot(type="jpeg", quality=62)).decode("ascii")
    return payload


def main() -> int:
    sync_playwright = load_playwright()
    executable_path = find_chromium()
    launch_kwargs: dict[str, Any] = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if executable_path:
        launch_kwargs["executable_path"] = executable_path

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport=VIEWPORT, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(20000)
        reply({"ok": True, "ready": True})

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                reply({"ok": False, "error": "指令不是合法 JSON"})
                continue
            action = str(command.get("action") or "")
            try:
                if action == "close":
                    reply({"ok": True, "closed": True})
                    break
                if action == "goto":
                    page.goto(str(command.get("url") or ""), wait_until="domcontentloaded", timeout=25000)
                    page.wait_for_timeout(800)
                    reply(collect(page, True))
                    continue
                if action == "snapshot":
                    reply(collect(page, bool(command.get("screenshot", True))))
                    continue

                # 以下动作都按序号定位。序号来自上一次 snapshot，页面变化后会失效，
                # 所以每个动作执行完都回一份新的 snapshot 让调用方重新对齐。
                if action in {"click", "type", "upload"}:
                    index = int(command.get("index", -1))
                    target = page.query_selector(f'[data-wb-idx="{index}"]') if index >= 0 else None
                    if target is None:
                        marked = len(page.query_selector_all("[data-wb-idx]"))
                        reply({"ok": False, "error": f"序号 {index} 不存在或页面已变化，请重新 snapshot（当前标记了 {marked} 个可操作元素）"})
                        continue
                    if action == "click":
                        target.scroll_into_view_if_needed(timeout=5000)
                        target.click(timeout=8000)
                        page.wait_for_timeout(900)
                    elif action == "type":
                        target.scroll_into_view_if_needed(timeout=5000)
                        target.fill(str(command.get("text") or ""), timeout=8000)
                        if command.get("submit"):
                            target.press("Enter")
                            page.wait_for_timeout(900)
                    else:
                        paths = [str(item) for item in (command.get("paths") or []) if str(item)]
                        if not paths:
                            reply({"ok": False, "error": "没有提供要上传的文件"})
                            continue
                        target.set_input_files(paths, timeout=15000)
                        page.wait_for_timeout(500)
                    reply(collect(page, True))
                    continue
                if action == "scroll":
                    page.evaluate("(delta) => window.scrollBy(0, delta)", int(command.get("delta") or 600))
                    page.wait_for_timeout(500)
                    reply(collect(page, True))
                    continue
                if action == "back":
                    page.go_back(wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(600)
                    reply(collect(page, True))
                    continue
                reply({"ok": False, "error": f"不支持的动作：{action}"})
            except Exception as exc:  # noqa: BLE001 - 单个动作失败不能拖垮整个会话
                reply({"ok": False, "error": f"{action} 执行失败：{str(exc)[:300]}"})

        try:
            context.close()
            browser.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
