from dataclasses import asdict, dataclass
from enum import Enum

from app.config import get_settings


class AdapterState(str, Enum):
    NOT_CHECKED = "not_checked"
    PUBLIC_PAGE_READY = "public_page_ready"
    HUMAN_VERIFICATION_REQUIRED = "human_verification_required"
    LOGIN_REQUIRED = "login_required"
    PAGE_CHANGED = "page_changed"
    READY = "ready"
    BLOCKED = "blocked"


@dataclass
class AdapterReport:
    state: AdapterState
    title: str = ""
    url: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        return data


class BossAdapter:
    """Conservative boundary for Boss site automation.

    This adapter may inspect the public page. It deliberately does not solve
    CAPTCHAs, hide automation, or send messages until selectors are validated
    with a dedicated test account.
    """

    async def probe_public_page(self) -> AdapterReport:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return AdapterReport(AdapterState.BLOCKED, message="Playwright 尚未安装")

        settings = get_settings()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=settings.headless)
                page = await browser.new_page()
                await page.goto(settings.target_url, wait_until="domcontentloaded", timeout=30000)
                title = await page.title()
                text = (await page.locator("body").inner_text())[:3000]
                url = page.url
                await browser.close()
        except Exception as exc:
            return AdapterReport(AdapterState.BLOCKED, url=settings.target_url, message=f"公开页探测失败：{type(exc).__name__}")

        normalized = f"{title} {text}".lower()
        verification_words = ["验证码", "安全验证", "访问异常", "captcha", "verify", "滑块"]
        if any(word in normalized for word in verification_words):
            return AdapterReport(AdapterState.HUMAN_VERIFICATION_REQUIRED, title=title, url=url, message="检测到站点验证，需要用户人工处理")
        if "登录" in text or "login" in normalized:
            return AdapterReport(AdapterState.LOGIN_REQUIRED, title=title, url=url, message="公开页可访问，真实搜索前需要登录联调")
        return AdapterReport(AdapterState.PUBLIC_PAGE_READY, title=title, url=url, message="公开页可访问；登录后页面仍需测试账号验证")

    def live_status(self) -> dict:
        return {
            "state": AdapterState.NOT_CHECKED.value,
            "dry_run_only": False,
            "reason": "可复用当前 Codex 浏览器中的 BOSS 登录会话；验证码和滑块只允许人工处理",
            "captcha_policy": "human_only",
        }


adapter = BossAdapter()
