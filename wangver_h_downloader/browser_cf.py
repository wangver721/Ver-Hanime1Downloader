"""
浏览器模拟与 CF 盾牌半自动化接管：
真实浏览器 + 默认隐藏 + CF 触发时自动弹窗 + 验证通过后再次隐藏。
"""
import asyncio
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable

from playwright.async_api import async_playwright, BrowserContext, Page, Response

from .config import (
    DEFAULT_USER_DATA_DIR,
    CF_FORBIDDEN_STATUS,
    CF_INDICATOR_TEXTS,
    CF_INDICATOR_SELECTORS,
)


# ── Windows 窗口控制 ───────────────────────────────────────

_OFFSCREEN = (-32000, -32000)         # 离屏隐藏位置
_NORMAL_SIZE = (1280, 820)            # 正常显示大小
_IS_WIN = sys.platform == "win32"


def _find_browser_window() -> Optional[int]:
    """枚举顶层窗口，找到当前进程关联的 Chromium 主窗口。

    判定条件：窗口类名为 Chrome_WidgetWin_1 且当前位置在屏幕外（即由我们启动）。
    """
    if not _IS_WIN:
        return None
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        # 我们启动时把窗口推到 -32000，这里只匹配在屏幕外的
        if rect.left < -10000 or rect.top < -10000:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def _show_window(hwnd: Optional[int]) -> None:
    """把窗口移到屏幕中央并置顶。"""
    if not _IS_WIN or not hwnd:
        return
    user32 = ctypes.windll.user32
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    w, h = _NORMAL_SIZE
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040)   # SWP_SHOWWINDOW
    user32.ShowWindow(hwnd, 9)                          # SW_RESTORE
    user32.SetForegroundWindow(hwnd)


def _hide_window(hwnd: Optional[int]) -> None:
    """把窗口推回屏幕外。"""
    if not _IS_WIN or not hwnd:
        return
    user32 = ctypes.windll.user32
    x, y = _OFFSCREEN
    w, h = _NORMAL_SIZE
    user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040)


# ── 数据 ──────────────────────────────────────────────────

@dataclass
class SessionCredentials:
    """验证通过后的会话凭证，供下载引擎使用。"""
    cookies: list
    user_agent: str


def _default_cf_alert_callback(message: str) -> None:
    print("\n" + "=" * 60)
    print("[CF] " + message)
    print("=" * 60 + "\n")


# ── 主类 ──────────────────────────────────────────────────

class BrowserCFHandler:
    """真实浏览器驱动 + CF 检测 + 自动显隐窗口 + 凭证提取。"""

    def __init__(
        self,
        user_data_dir: Optional[Path] = None,
        headless: bool = False,
        on_cf_triggered: Optional[Callable[[str], None]] = None,
    ):
        self.user_data_dir = Path(user_data_dir or DEFAULT_USER_DATA_DIR)
        self.headless = headless
        self.on_cf_triggered = on_cf_triggered or _default_cf_alert_callback

        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._cf_detected = asyncio.Event()
        self._cf_passed = asyncio.Event()
        self._last_response_status: Optional[int] = None
        self._hwnd: Optional[int] = None

    # ── 启动/关闭 ──

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.user_data_dir),
            headless=self.headless,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--window-position={_OFFSCREEN[0]},{_OFFSCREEN[1]}",
                f"--window-size={_NORMAL_SIZE[0]},{_NORMAL_SIZE[1]}",
            ],
            viewport={"width": 1280, "height": 720},
        )
        self._page = await self._context.new_page()

        async def on_response(response: Response):
            self._last_response_status = response.status
            if response.status == CF_FORBIDDEN_STATUS:
                self._cf_detected.set()

        self._page.on("response", on_response)

        # 启动后异步寻找窗口句柄（窗口创建有延迟）
        if _IS_WIN:
            asyncio.create_task(self._find_hwnd_async())

    async def _find_hwnd_async(self) -> None:
        for _ in range(40):
            hwnd = _find_browser_window()
            if hwnd:
                self._hwnd = hwnd
                return
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._playwright = None
        self._hwnd = None

    # ── 显隐 ──

    async def show_window(self) -> None:
        """弹出浏览器窗口（CF 验证时调用）。"""
        if not self._hwnd:
            await self._find_hwnd_async()
        _show_window(self._hwnd)

    def hide_window(self) -> None:
        """把浏览器窗口推回屏幕外。"""
        _hide_window(self._hwnd)

    # ── 导航与 CF 检测 ──

    async def goto_and_handle_cf(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        real_content_selector: Optional[str] = None,
        wait_for_enter: bool = False,
    ) -> SessionCredentials:
        """
        导航到目标页：检测到 CF 时弹窗让用户验证，验证通过后立即隐藏。
        """
        self._cf_detected.clear()
        self._cf_passed.clear()
        await self._page.goto(url, wait_until=wait_until, timeout=60000)

        # 检测当前页面是否有 CF 特征
        triggered = self._cf_detected.is_set()
        if not triggered:
            try:
                content = await self._page.content()
                if any(t in content for t in CF_INDICATOR_TEXTS):
                    triggered = True
                else:
                    for sel in CF_INDICATOR_SELECTORS:
                        try:
                            if await self._page.locator(sel).count() > 0:
                                triggered = True
                                break
                        except Exception:
                            pass
            except Exception:
                pass

        if not triggered:
            return await self._make_credentials()

        # ── 触发 CF：弹出窗口让用户验证 ──
        self._cf_detected.set()
        self.on_cf_triggered("触发 Cloudflare 拦截，请在弹出的浏览器窗口中手动完成验证！")
        await self.show_window()

        try:
            async def wait_real_content():
                while True:
                    await asyncio.sleep(1)
                    if real_content_selector:
                        try:
                            if await self._page.locator(real_content_selector).count() > 0:
                                self._cf_passed.set()
                                return
                        except Exception:
                            pass
                    if self._last_response_status != CF_FORBIDDEN_STATUS:
                        try:
                            content = await self._page.content()
                            if not any(t in content for t in CF_INDICATOR_TEXTS):
                                self._cf_passed.set()
                                return
                        except Exception:
                            pass

            if wait_for_enter:
                await asyncio.gather(
                    wait_real_content(),
                    asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("验证完成后请按 Enter 继续... ")
                    ),
                )
            else:
                await wait_real_content()
        finally:
            # 验证完成后再次隐藏窗口
            self.hide_window()

        return await self._make_credentials()

    async def _make_credentials(self) -> SessionCredentials:
        cookies = await self._context.cookies()
        ua = await self._page.evaluate("() => navigator.userAgent")
        return SessionCredentials(cookies=cookies, user_agent=ua)

    async def get_page_content(self) -> str:
        if self._page:
            return await self._page.content()
        return ""

    def get_page(self) -> Optional[Page]:
        return self._page
