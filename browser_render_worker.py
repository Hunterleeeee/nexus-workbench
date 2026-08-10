#!/usr/bin/env python3
"""独立截图 worker：由 app.py 以子进程方式调用，超时由父进程 kill 进程组兜底。

用法: python browser_render_worker.py <url> <输出png路径>
成功退出码 0；失败退出码 1（stderr 写原因）。
"""
import glob
import sys


def find_chromium() -> str | None:
    candidates = [
        "/www/workbench/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/www/workbench/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
    ]
    for pattern in candidates:
        for path in sorted(glob.glob(pattern)):
            return path
    return None


def main() -> int:
    if len(sys.argv) < 3:
        sys.stderr.write("usage: browser_render_worker.py <url> <out.png>\n")
        return 1
    url, out_path = sys.argv[1], sys.argv[2]
    try:
        from patchright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"patchright 不可用: {exc}\n")
        return 1

    executable_path = find_chromium()
    try:
        with sync_playwright() as p:
            launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            if executable_path:
                launch_kwargs["executable_path"] = executable_path
            browser = p.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1500)
                page.screenshot(path=out_path, type="png")
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
        return 0
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"渲染失败: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
