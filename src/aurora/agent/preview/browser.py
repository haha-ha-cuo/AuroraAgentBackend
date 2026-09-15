"""在受限预览生命周期内生成真实浏览器截图。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

DEFAULT_VIEWPORTS = [{"width": 1440, "height": 900}, {"width": 390, "height": 844}]


def validate_preview(config, sandbox):
    """验证本机预览 URL、页面、视口及工作目录。"""
    if not isinstance(config, dict) or not config.get("url"):
        raise ValueError("请配置预览 URL、启动命令、工作目录及页面路径")
    parsed = urlsplit(config["url"])
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname
        not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        or parsed.username
        or parsed.password
    ):
        raise ValueError("预览 URL 必须是本机 HTTP(S) 地址")
    sandbox.resolve(config.get("cwd", "."))
    pages = config.get("pages", ["/"])
    if not isinstance(pages, list) or not pages or len(pages) > 5:
        raise ValueError("预览页面数量应为 1 到 5")
    for path in pages:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("预览页面必须是站内绝对路径")
    for viewport in config.get("viewports", DEFAULT_VIEWPORTS):
        if set(viewport) != {"width", "height"} or any(
            not isinstance(value, int) or not 200 <= value <= 3000 for value in viewport.values()
        ):
            raise ValueError("视口宽高必须为 200 到 3000 的整数")
    if (
        not config.get("viewports", DEFAULT_VIEWPORTS)
        or len(config.get("viewports", DEFAULT_VIEWPORTS)) > 4
    ):
        raise ValueError("视口数量应为 1 到 4")
    return config


class BrowserCapture:
    """通过 Playwright 截图并在有限时间内释放预览进程。"""

    def capture(self, config, sandbox):
        """返回截图字节、页面元数据和预览日志。"""
        from playwright.sync_api import sync_playwright

        config = validate_preview(config, sandbox)
        cancelled = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = (
                executor.submit(
                    sandbox.run_in, config.get("cwd", "."), config["command"], 90, cancelled
                )
                if config.get("command")
                else None
            )
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urlopen(config["url"], timeout=1) as response:
                            if response.status >= 400:
                                raise ValueError("预览服务返回错误")
                        break
                    except OSError:
                        if future and future.done():
                            raise ValueError("预览命令提前退出，请检查项目预览配置") from None
                        if time.monotonic() >= deadline:
                            raise ValueError("预览服务启动超时") from None
                        time.sleep(0.2)
                shots = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    try:
                        for viewport in config.get("viewports", DEFAULT_VIEWPORTS):
                            context = browser.new_context(viewport=viewport, device_scale_factor=1)
                            try:
                                page = context.new_page()
                                page.set_default_timeout(10000)
                                for path in config.get("pages", ["/"]):
                                    url = urljoin(config["url"], path)
                                    response = page.goto(url, wait_until="networkidle")
                                    if not response or response.status >= 400:
                                        raise ValueError("预览页面加载失败")
                                    if urlsplit(page.url).netloc != urlsplit(url).netloc:
                                        raise ValueError("预览页面跳转到了其他站点")
                                    page.evaluate("document.fonts.ready")
                                    shots.append(
                                        (
                                            page.screenshot(animations="disabled"),
                                            {"url": page.url, "viewport": viewport},
                                        )
                                    )
                            finally:
                                context.close()
                    finally:
                        browser.close()
                cancelled.set()
                log = future.result().render() if future else "使用已有预览服务"
                return shots, log
            finally:
                cancelled.set()
                if future:
                    future.result()
