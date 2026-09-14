import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from typing import Any

from app.config import get_settings
from app.vault import vault


LOGIN_URL = "https://www.zhipin.com/web/user/"


def _is_authenticated_url(url: str) -> bool:
    return "/web/geek/" in url and "/web/passport/" not in url


async def _is_authenticated_page(page: Any) -> bool:
    if _is_authenticated_url(page.url):
        return True
    # Boss may render the authenticated job page while keeping /web/user/ in
    # the address bar.  Require both account-only navigation links so a piece
    # of public page text cannot be mistaken for a logged-in session.
    chat_link = await _first_visible(page, ['a[href*="/web/geek/chat"]'])
    resume_link = await _first_visible(page, ['a[href*="/web/geek/resume"]'])
    return chat_link is not None and resume_link is not None


CODE_INPUT_SELECTORS = [
    '[ka="signup-sms"]',
    'input[placeholder*="短信验证码"]',
    'input[placeholder*="验证码"]',
    'input[autocomplete="one-time-code"]',
    'input[name*="code" i]',
    '.sms-input-wrapper input',
    'input[maxlength="6"]',
]

LOGIN_SUBMIT_SELECTORS = [
    'button[ka="signup_submit_button_click"]',
    'button[ka*="submit"]',
    '.sms-form-btn button',
    'button.sure-btn',
    'button[type="submit"]',
]


async def _first_visible(page: Any, selectors: list[str]) -> Any | None:
    frames = getattr(page, "frames", None) or [page]
    for frame in frames:
        for selector in selectors:
            try:
                locator = frame.locator(selector)
                count = min(await locator.count(), 5)
                for index in range(count):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        return candidate
            except Exception:
                continue
    return None


async def _body_text(page: Any) -> str:
    try:
        return (await page.locator("body").inner_text()).strip()
    except Exception:
        return ""


async def _bring_to_front(page: Any) -> None:
    bring_to_front = getattr(page, "bring_to_front", None)
    if bring_to_front is None:
        return
    try:
        await bring_to_front()
    except Exception:
        # Focusing is a usability aid; it must not turn a valid login page
        # into a server error on window managers that reject activation.
        pass


async def _open_login_page(page: Any) -> str:
    """Open the login page without leaving the user on an about:blank window.

    Boss may keep loading subresources long enough for ``domcontentloaded`` to
    time out even though navigation has started.  A second navigation that
    waits only for the initial commit is enough to get the browser onto a real
    page, where a captcha or other verification can be completed manually.
    """
    last_error: Exception | None = None
    for wait_until, timeout in (("domcontentloaded", 30000), ("commit", 20000)):
        try:
            await page.goto(LOGIN_URL, wait_until=wait_until, timeout=timeout)
        except Exception as exc:
            last_error = exc

        if page.is_closed():
            raise ValueError("Boss 登录窗口已关闭，请重新发送验证码")

        if page.url != "about:blank":
            await _bring_to_front(page)
            await page.wait_for_timeout(800)
            return await _body_text(page)

    detail = type(last_error).__name__ if last_error is not None else "NavigationError"
    raise ValueError(f"Boss 登录页未能开始加载（{detail}），已关闭空白窗口；请检查网络后重试")


@dataclass
class SmsLoginFlow:
    playwright: Any
    browser: Any
    page: Any
    state: str
    updated_at: datetime
    message: str
    profile_dir: Any | None = None


class SmsLoginManager:
    def __init__(self) -> None:
        self._flows: dict[str, SmsLoginFlow] = {}
        self._lock = asyncio.Lock()

    async def start(self, token: str, accept_policy: bool) -> dict[str, Any]:
        if not accept_policy:
            raise ValueError("发送验证码前需要同意 Boss 直聘用户协议和隐私政策")
        lease = vault.get(token)
        if lease is None:
            raise ValueError("临时账号会话不存在或已过期")
        phone = "".join(character for character in lease.account if character.isdigit())
        if len(phone) != 11:
            raise ValueError("短信登录目前仅支持 11 位中国大陆手机号")

        async with self._lock:
            flow = self._flows.get(token)
            if flow is not None and flow.page.is_closed():
                await self.close(token)
                flow = None

            if flow is None:
                from playwright.async_api import async_playwright

                playwright = await async_playwright().start()
                profile_dir = TemporaryDirectory(prefix="bossfind-login-")
                try:
                    browser = await playwright.chromium.launch_persistent_context(
                        profile_dir.name,
                        headless=get_settings().headless,
                        viewport={"width": 1280, "height": 820},
                    )
                    pages = browser.pages
                    page = pages[0] if pages else await browser.new_page()
                except Exception as exc:
                    profile_dir.cleanup()
                    await playwright.stop()
                    detail = str(exc).splitlines()[0][:240] or type(exc).__name__
                    if "Executable doesn't exist" in str(exc):
                        detail = "Chromium 未安装，请运行：.\\.venv\\Scripts\\python.exe -m playwright install chromium"
                    raise ValueError(f"启动登录浏览器失败：{detail}") from exc
                flow = SmsLoginFlow(
                    playwright,
                    browser,
                    page,
                    "opening",
                    datetime.now(timezone.utc),
                    "正在打开 Boss 登录页",
                    profile_dir,
                )
                self._flows[token] = flow
                try:
                    body_snapshot = await _open_login_page(page)
                except ValueError:
                    await self.close(token)
                    raise
            else:
                body_snapshot = await _body_text(flow.page)

            page = flow.page
            await _bring_to_front(page)
            if page.url == "about:blank" or not body_snapshot:
                try:
                    body_snapshot = await _open_login_page(page)
                except ValueError as exc:
                    await self.close(token)
                    raise exc
            if not body_snapshot:
                flow.state = "human_verification_required"
                flow.message = "Boss 页面已打开但内容仍在加载，请在浏览器中刷新或完成安全验证，然后点击重新发送验证码。"
                flow.updated_at = datetime.now(timezone.utc)
                return self.status(token)
            if await _is_authenticated_page(page):
                flow.state = "authenticated"
                flow.message = "已检测到 Boss 登录成功。"
                flow.updated_at = datetime.now(timezone.utc)
                return self.status(token)
            phone_input = await _first_visible(page, ['input[type="tel"]', 'input[placeholder*="手机号"]'])
            if phone_input is None:
                flow.state = "human_verification_required"
                flow.message = "未检测到短信登录表单。请在打开的浏览器中完成人工验证或进入验证码登录页，再点击重新发送验证码。"
                flow.updated_at = datetime.now(timezone.utc)
                return self.status(token)

            try:
                await phone_input.fill(phone)

                agreement = await _first_visible(page, ["input.agree-policy", 'input[type="checkbox"]'])
                if agreement is not None and not await agreement.is_checked():
                    await agreement.check(force=True)

                send_button = await _first_visible(
                    page,
                    ['[ka="send_sms_code_click"]', '[ka*="send_sms"]', 'button:has-text("发送验证码")', 'button:has-text("获取验证码")'],
                )
                if send_button is None:
                    raise ValueError("Boss 登录页结构已变化：未找到发送验证码按钮")
                await send_button.click()
                await page.wait_for_timeout(1200)

                if page.url == "about:blank":
                    await self.close(token)
                    raise ValueError("Boss 阻止了自动登录页，无法安全确认短信请求；请使用已登录的正常浏览器会话")

                body_text = (await page.locator("body").inner_text()).lower()
                verification_words = ["拖动滑块", "安全验证", "访问异常", "captcha", "verify"]
                needs_human = any(word in body_text for word in verification_words)
                code_input = await _first_visible(page, CODE_INPUT_SELECTORS)
                if await _is_authenticated_page(page):
                    flow.state = "authenticated"
                    flow.message = "已检测到 Boss 登录成功。"
                elif needs_human:
                    flow.state = "human_verification_required"
                    flow.message = "浏览器中出现安全验证，请先在可见浏览器里手动完成；完成后再检查登录状态。"
                elif code_input is not None:
                    flow.state = "code_sent"
                    flow.message = "Boss 已显示验证码输入框，请查看手机并输入验证码。"
                else:
                    flow.state = "send_unconfirmed"
                    flow.message = "已点击发送按钮，但 Boss 未显示验证码输入框，无法确认短信已发送。请在打开的浏览器中手动发码或登录。"
                flow.updated_at = datetime.now(timezone.utc)
                return self.status(token)
            except ValueError:
                raise
            except Exception as exc:
                if page.url == "about:blank":
                    await self.close(token)
                    raise ValueError("Boss 阻止了自动登录页，无法安全确认短信请求；请使用已登录的正常浏览器会话") from exc
                flow.state = "human_verification_required"
                flow.message = "Boss 阻止了自动请求。请在打开的浏览器中手动点击发送验证码，然后回到 BossFind 输入验证码。"
                flow.updated_at = datetime.now(timezone.utc)
                return self.status(token)
    async def verify(self, token: str, code: str) -> dict[str, Any]:
        flow = self._flows.get(token)
        if flow is None:
            raise ValueError("没有待验证的短信登录流程，请先发送验证码")
        if flow.page.is_closed():
            await self.close(token)
            raise ValueError("登录浏览器已关闭，请重新发送验证码")
        if await _is_authenticated_page(flow.page):
            flow.state = "authenticated"
            flow.message = "已检测到 Boss 登录成功。"
            flow.updated_at = datetime.now(timezone.utc)
            return self.status(token)

        body_text = (await _body_text(flow.page)).lower()
        security_page = "/web/passport/" in flow.page.url or any(
            word in body_text for word in ["安全验证", "拖动滑块", "异常访问行为", "captcha"]
        )
        code_input = await _first_visible(
            flow.page,
            CODE_INPUT_SELECTORS,
        )
        submit = await _first_visible(
            flow.page,
            LOGIN_SUBMIT_SELECTORS,
        )
        if code_input is None:
            flow.state = "human_verification_required"
            flow.message = (
                "当前仍是 Boss 安全验证页，请先在打开的浏览器中完成人工验证；回到验证码登录表单后再点击验证。"
                if security_page
                else "未检测到 Boss 验证码输入框。请在打开的浏览器中进入验证码登录页并确认短信已发送，然后再次点击验证。"
            )
            flow.updated_at = datetime.now(timezone.utc)
            return self.status(token)

        try:
            await _bring_to_front(flow.page)
            await code_input.fill(code)
            if submit is not None:
                await submit.click()
            else:
                await code_input.press("Enter")
            await flow.page.wait_for_timeout(1800)
        except Exception:
            flow.state = "human_verification_required"
            flow.message = "自动填写被 Boss 阻止，请在打开的浏览器中手动输入验证码并登录，然后回到 BossFind 点击验证。"
            flow.updated_at = datetime.now(timezone.utc)
            return self.status(token)

        if await _is_authenticated_page(flow.page):
            flow.state = "authenticated"
            flow.message = "验证码验证成功，Boss 登录会话已建立。"
        else:
            body_text = (await _body_text(flow.page)).lower()
            if "/web/passport/" in flow.page.url or "安全验证" in body_text:
                flow.state = "human_verification_required"
                flow.message = "验证码已提交，但 Boss 仍要求安全验证，请在打开的浏览器中手动完成。"
            else:
                error_node = await _first_visible(flow.page, [".tip-error", ".error-message", ".form-error", ".sms-form-wrapper .error"])
                error_text = (await error_node.inner_text()).strip() if error_node is not None else ""
                flow.state = "verification_failed"
                flow.message = error_text or "验证码未通过，请检查验证码是否正确或是否已过期。"
        flow.updated_at = datetime.now(timezone.utc)
        return self.status(token)

    async def refresh_status(self, token: str) -> dict[str, Any]:
        flow = self._flows.get(token)
        if flow is None:
            return self.status(token)
        if flow.page.is_closed():
            await self.close(token)
            return {"state": "browser_closed", "message": "登录浏览器已关闭，请重新连接账号"}
        await _bring_to_front(flow.page)
        if await _is_authenticated_page(flow.page):
            flow.state = "authenticated"
            flow.message = "已检测到 Boss 登录成功。"
        else:
            body_text = (await _body_text(flow.page)).lower()
            verification_words = ["拖动滑块", "安全验证", "访问异常", "captcha", "verify"]
            if any(word in body_text for word in verification_words):
                flow.state = "human_verification_required"
                flow.message = "Boss 仍在等待人工安全验证。"
            elif await _first_visible(flow.page, CODE_INPUT_SELECTORS) is not None:
                flow.state = "code_sent"
                flow.message = "Boss 验证码输入框已就绪。"
            else:
                flow.state = "manual_login_required"
                flow.message = "尚未检测到登录成功。请在打开的 Boss 浏览器中手动完成登录。"
        flow.updated_at = datetime.now(timezone.utc)
        return self.status(token)
    def status(self, token: str) -> dict[str, Any]:
        flow = self._flows.get(token)
        if flow is None:
            return {"state": "not_started", "message": "尚未启动短信登录"}
        return {
            "state": flow.state,
            "message": flow.message,
            "updated_at": flow.updated_at,
            "page_url": flow.page.url,
            "browser_visible": not get_settings().headless,
        }

    async def close(self, token: str) -> None:
        flow = self._flows.pop(token, None)
        if flow is None:
            return
        try:
            if flow.browser is not None:
                await flow.browser.close()
        finally:
            try:
                if flow.playwright is not None:
                    await flow.playwright.stop()
            finally:
                if flow.profile_dir is not None:
                    flow.profile_dir.cleanup()

    async def shutdown(self) -> None:
        for token in list(self._flows):
            await self.close(token)


sms_login_manager = SmsLoginManager()









