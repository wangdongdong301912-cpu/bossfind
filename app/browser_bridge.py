"""Attach to a user-opened Chrome BOSS session through local CDP.

The bridge only accepts loopback CDP endpoints and only attaches to
zhipin.com targets. It never exports cookies, credentials, phone numbers,
passwords, or OTP codes from the browser profile.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import random
import re
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

import websockets

from app.config import get_settings
from app.ranking import normalize_job_features, rank_job


ALLOWED_CDP_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_SITE = "zhipin.com"
BOSS_LOGIN_URL = "https://www.zhipin.com/web/geek/jobs?_security_check=1_1782462272045"
BOSS_JOBS_URL = "https://www.zhipin.com/web/geek/jobs"
BOSS_CHAT_URL = "https://www.zhipin.com/web/geek/chat"
CITY_CODES = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "嘉兴": "101210300",
    "宁波": "101210400",
    "南京": "101190100",
    "苏州": "101190400",
    "无锡": "101190200",
    "常州": "101191100",
    "成都": "101270100",
    "重庆": "101040100",
    "武汉": "101200100",
    "西安": "101110100",
}
# BOSS 的新版岗位搜索有时不会按目标城市码稳定返回列表；例如嘉兴岗位
# 会出现在杭州入口下，用“嘉兴 / BI工程师”这类组合搜索词命中。
CITY_SEARCH_FALLBACK_CODES = {
    "嘉兴": "101210100",
}
JOB_CARD_SELECTOR = "li.job-card-box, .job-card-box"
READABLE_JOB_CARD_EXPRESSION = """(() => {
    if (location.pathname.includes('/web/geek/map/')) return false;
    return [...document.querySelectorAll('li.job-card-box, .job-card-box')]
        .some(box => box.querySelector('a[href*="/job_detail/"]')
            && box.querySelector('.job-salary, [class*="salary"]'));
})()"""
# BOSS uses a custom font in job cards. In the current web UI the private
# glyphs map as E031->0, E032->1, ... E03A->9.
PUA_DIGITS = {0xE031 + value: str(value) for value in range(10)}
SECURITY_WORDS = ("验证码", "安全验证", "滑块", "captcha", "verify", "验证")
SENSITIVE_REPLY_WORDS = ("身份证", "身份证号", "银行卡", "验证码", "密码", "住址", "详细地址", "微信", "手机号")


class BrowserBridgeError(RuntimeError):
    pass


TRANSIENT_CDP_ERROR_MARKERS = (
    "Inspected target navigated or closed",
    "Execution context was destroyed",
    "Cannot find context with specified id",
    "Target closed",
    "WebSocket connection is closed",
    "ConnectionClosed",
    "no close frame received or sent",
    "keepalive ping timeout",
)


def _is_transient_cdp_error(exc: BaseException) -> bool:
    message = str(exc)
    return any(marker in message for marker in TRANSIENT_CDP_ERROR_MARKERS)


async def _sleep_before_cdp_retry(attempt: int) -> None:
    await asyncio.sleep(0.8 + attempt * 0.7)


def decode_boss_text(value: str) -> str:
    """Decode the digit font used by the BOSS job list."""
    return value.translate(PUA_DIGITS)


def salary_bounds(value: str) -> tuple[int, int]:
    decoded = decode_boss_text(value)
    dash_chars = "".join(chr(code) for code in (0xFF0D, 0x2013, 0x2014, 0x301C, 0xFF5E, 0x007E, 0xFFFD))
    dash_pattern = re.escape(dash_chars)
    normalized = decoded.upper().translate(str.maketrans({char: "-" for char in dash_chars}))
    if "K" in normalized:
        normalized_k = re.split(r"[\u00b7\uff65/]", normalized, 1)[0]
        match = re.search(r"(\d+(?:\.\d+)?)\s*K?\s*-\s*(\d+(?:\.\d+)?)\s*K", normalized_k)
        if match:
            return (round(float(match.group(1))), round(float(match.group(2))))
    if "\u4e07" in decoded:
        normalized_cn = decoded.translate(str.maketrans({char: "-" for char in dash_chars}))
        match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*\u4e07", normalized_cn)
        if match:
            return (round(float(match.group(1)) * 10), round(float(match.group(2)) * 10))
    if re.search(rf"\d\s*[{dash_pattern}]\s*\d", decoded):
        ascii_range = decoded.translate(str.maketrans({char: "-" for char in dash_chars}))
        match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", ascii_range)
        if match and max(float(match.group(1)), float(match.group(2))) <= 80:
            return (round(float(match.group(1))), round(float(match.group(2))))
    return (0, 0)


def role_keyword_matches(keyword: str, text: str) -> bool:
    keyword_normalized = re.sub(r"[\s·_\-/（）()]", "", keyword.lower())
    text_normalized = re.sub(r"[\s·_\-/（）()]", "", text.lower())
    if keyword_normalized in text_normalized or text_normalized in keyword_normalized:
        return True
    # BOSS titles commonly insert a specialization between a short role stem
    # and its suffix, e.g. "BI工程师" -> "BI开发工程师".
    stem = keyword_normalized
    for suffix in ("高级工程师", "工程师", "分析师", "开发", "经理", "专员", "岗位", "岗", "师"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    unsafe_short_stems = {"数据", "产品", "开发", "运营", "工程", "专员", "经理"}
    return len(stem) >= 2 and stem not in unsafe_short_stems and stem in text_normalized


def score_job(job: dict[str, Any], campaign: dict[str, Any]) -> tuple[int, list[str]]:
    title = job.get("job_title", "").lower()
    description = job.get("description", "").lower()
    company = job.get("company", "").lower()
    location = job.get("location", "")
    tags = " ".join(job.get("tags", [])).lower()
    keywords = [word.lower() for word in campaign["keywords"]]
    excluded = [word.lower() for word in campaign["excluded_keywords"]]
    combined = f"{title} {description} {company} {tags}"

    if any(word in combined for word in excluded):
        return 0, ["命中排除关键词"]

    title_match = any(role_keyword_matches(word, title) for word in keywords)
    detail_match = any(role_keyword_matches(word, description) for word in keywords)
    low, high = salary_bounds(job.get("salary", ""))
    salary_match = low > 0 and max(low, campaign["salary_min"]) <= min(high, campaign["salary_max"])
    city_match = not campaign["city"] or campaign["city"] in location
    experience_match = campaign["experience"] == "不限" or campaign["experience"] in tags
    industry_match = any(word.lower() in combined for word in campaign.get("industries", []))

    # Salary and city are strict user constraints. Unknown salary is not safe
    # to auto-contact and therefore receives a zero score.
    if not salary_match or not city_match or not (title_match or detail_match):
        reasons = []
        if not salary_match:
            reasons.append("薪资不在目标区间")
        if not city_match:
            reasons.append("城市不匹配")
        if not (title_match or detail_match):
            reasons.append("岗位内容未命中关键词")
        return 0, reasons

    score = 25  # salary
    reasons = ["薪资区间匹配"]
    if title_match:
        score += 45
        reasons.append("岗位名称匹配")
    elif detail_match:
        score += 20
        reasons.append("职位描述匹配")
    if city_match:
        score += 10
        reasons.append("目标城市匹配")
    if experience_match:
        score += 10
        reasons.append("经验要求匹配")
    if industry_match:
        score += 10
        reasons.append("行业方向匹配")
    return min(100, score), reasons


def _is_allowed_site(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == ALLOWED_SITE or host.endswith(f".{ALLOWED_SITE}")


def _validate_loopback_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in ALLOWED_CDP_HOSTS or parsed.username or parsed.password:
        raise BrowserBridgeError("Chrome 调试地址必须是本机 HTTP 回环地址，例如 http://127.0.0.1:9222")
    if not parsed.port:
        raise BrowserBridgeError("Chrome 调试地址缺少端口")
    return endpoint.rstrip("/")


def _request_json(url: str, *, method: str = "GET") -> Any:
    request = Request(url, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            return json.load(response)
    except URLError as exc:
        raise BrowserBridgeError(
            "无法连接 Chrome 调试窗口。请在项目根目录运行 start-chrome-boss.ps1，"
            "并保持该 Chrome 窗口打开后再重试。"
        ) from exc
    except TimeoutError as exc:
        raise BrowserBridgeError("连接 Chrome 调试窗口超时，请确认 start-chrome-boss.ps1 启动的 Chrome 仍在运行。") from exc


def _load_targets(endpoint: str) -> list[dict[str, Any]]:
    data = _request_json(f"{endpoint}/json/list")
    if not isinstance(data, list):
        raise BrowserBridgeError("Chrome 返回了无效的标签页列表")
    return data


def _open_url_in_browser(endpoint: str, url: str) -> dict[str, Any]:
    if not _is_allowed_site(url):
        raise BrowserBridgeError("只允许打开 BOSS 直聘页面")
    encoded = quote(url, safe=":/?=&._-")
    try:
        data = _request_json(f"{endpoint}/json/new?{encoded}", method="PUT")
    except Exception:
        data = _request_json(f"{endpoint}/json/new?{encoded}")
    if not isinstance(data, dict):
        raise BrowserBridgeError("Chrome 未返回新标签页信息")
    return data


def _select_boss_target(targets: list[dict[str, Any]], *, prefer_chat: bool = False) -> dict[str, Any]:
    def is_security_target(url: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in ("passport", "verify", "captcha", "security-check"))

    candidates = [
        target for target in targets
        if target.get("type") in {"webview", "page"}
        and _is_allowed_site(str(target.get("url", "")))
        and not is_security_target(str(target.get("url", "")))
        and str(target.get("webSocketDebuggerUrl", "")).startswith("ws://")
    ]
    if not candidates:
        raise BrowserBridgeError("未找到 Chrome 中已打开的 BOSS 直聘标签页；请先启动 start-chrome-boss.ps1，并在该 Chrome 窗口手动登录 BOSS")

    def rank(item: dict[str, Any]) -> tuple[int, int, int, int]:
        url = str(item.get("url", ""))
        if prefer_chat:
            return ("/web/geek/chat" not in url, "/web/geek/" not in url, "_security_check" in url, 0)
        has_search_query = "/web/geek/jobs" in url and "query=" in url
        has_job_detail = "/job_detail/" in url
        has_jobs_page = "/web/geek/jobs" in url
        return (not has_search_query, not has_job_detail, not has_jobs_page, "_security_check" in url)

    candidates.sort(key=rank)
    target = candidates[0]
    ws_url = urlparse(str(target["webSocketDebuggerUrl"]))
    if ws_url.hostname not in ALLOWED_CDP_HOSTS:
        raise BrowserBridgeError("拒绝连接非本机 Chrome 调试目标")
    return target


def _safe_js_string(value: str) -> str:
    return json.dumps(value or "", ensure_ascii=False)


def city_code_for(city: str) -> str | None:
    normalized = re.sub(r"\s+", "", city or "").removesuffix("市")
    return CITY_CODES.get(normalized)


def normalized_city_name(city: str) -> str:
    return re.sub(r"\s+", "", city or "").removesuffix("市")


def search_url_attempts(city: str, keyword: str) -> list[str]:
    """Build resilient BOSS search URLs for the current UI."""
    normalized_city = normalized_city_name(city)
    direct_code = city_code_for(city)
    fallback_code = CITY_SEARCH_FALLBACK_CODES.get(normalized_city)
    keyword = (keyword or "").strip()
    combined_keyword = keyword
    if normalized_city and normalized_city not in keyword:
        combined_keyword = f"{normalized_city} / {keyword}"

    attempts: list[str] = []
    seen: set[str] = set()

    def add(query: str, code: str | None = None) -> None:
        if not query:
            return
        url = f"{BOSS_JOBS_URL}?query={quote(query, safe='')}"
        if code:
            url = f"{url}&city={code}"
        if url not in seen:
            seen.add(url)
            attempts.append(url)

    if direct_code:
        add(keyword, direct_code)
    if fallback_code:
        add(combined_keyword, fallback_code)
    if direct_code and combined_keyword != keyword:
        add(combined_keyword, direct_code)
    if combined_keyword != keyword:
        add(combined_keyword, None)
    if not attempts:
        add(keyword, None)
    return attempts


def jobs_url_for_city(city: str) -> str | None:
    code = city_code_for(city)
    return f"{BOSS_JOBS_URL}?city={code}" if code else None


def city_for_code(code: str) -> str | None:
    for city, item_code in CITY_CODES.items():
        if item_code == code:
            return city
    return None


class CdpPage:
    def __init__(self, endpoint: str, *, prefer_chat: bool = False):
        self.endpoint = _validate_loopback_endpoint(endpoint)
        self.prefer_chat = prefer_chat
        self.websocket: Any = None
        self._message_id = 0
        self.target: dict[str, Any] | None = None

    async def __aenter__(self) -> "CdpPage":
        targets = await asyncio.to_thread(_load_targets, self.endpoint)
        self.target = _select_boss_target(targets, prefer_chat=self.prefer_chat)
        self.websocket = await websockets.connect(
            self.target["webSocketDebuggerUrl"], open_timeout=5, close_timeout=2, max_size=8_000_000
        )
        await self.call("Page.enable")
        await self.call("Runtime.enable")
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.websocket is not None:
            await self.websocket.close()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._message_id += 1
        message_id = self._message_id
        await self.websocket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await asyncio.wait_for(self.websocket.recv(), timeout=8))
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise BrowserBridgeError(str(message["error"].get("message", "Chrome 操作失败")))
            return message.get("result", {})

    async def evaluate(self, expression: str, *, user_gesture: bool = False) -> Any:
        result = await self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": user_gesture,
        })
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            detail = details.get("text", "page script execution failed")
            exception = details.get("exception") or {}
            description = str(exception.get("description") or exception.get("value") or "").splitlines()[0]
            if description and description not in detail:
                detail = f"{detail}: {description}"
            raise BrowserBridgeError(detail)
        remote = result.get("result", {})
        if remote.get("subtype") == "error":
            raise BrowserBridgeError(str(remote.get("description", "页面脚本执行失败")))
        return remote.get("value")

    async def wait_for(self, expression: str, *, timeout: float = 8.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self.evaluate(f"Boolean({expression})"):
                return True
            await asyncio.sleep(0.2)
        return False

    async def navigate(self, url: str) -> None:
        if not _is_allowed_site(url):
            raise BrowserBridgeError("只允许打开 BOSS 直聘页面")
        await self.call("Page.navigate", {"url": url})
        if not await self.wait_for("['interactive', 'complete'].includes(document.readyState)", timeout=12):
            raise BrowserBridgeError("BOSS 页面加载超时")

    async def has_security_challenge(self) -> bool:
        return bool(await self.evaluate("""(() => {
            const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            const url = `${location.pathname}${location.search}`.toLowerCase();
            if (/passport|verify|captcha|security-check|safe/.test(url)) return true;
            const selectorHits = [...document.querySelectorAll([
                'iframe[src*="captcha"]', 'iframe[src*="verify"]',
                '.geetest_panel', '.geetest_box', '.geetest_holder', '.nc-container',
                '[class*="captcha"]', '[id*="captcha"]', '[class*="geetest"]', '[id*="geetest"]'
            ].join(','))].some(visible);
            if (selectorHits) return true;
            const dialogs = [...document.querySelectorAll('.dialog-wrap, .dialog-container, [role="dialog"], [class*="verify"], [class*="captcha"], [class*="security"]')]
                .filter(visible)
                .map(el => (el.innerText || '').trim())
                .filter(Boolean);
            const challengePattern = /\u9a8c\u8bc1\u7801|\u5b89\u5168\u9a8c\u8bc1|\u6ed1\u5757|\u8bf7\u5b8c\u6210\u9a8c\u8bc1|captcha|verify/i;
            return dialogs.some(text => challengePattern.test(text));
        })()"""))


@dataclass
class ContactResult:
    status: str
    message: str


class ExistingBrowserAdapter:
    MATCH_THRESHOLD = 70
    MAX_PAGES_PER_KEYWORD = 25

    @property
    def endpoint(self) -> str:
        return get_settings().browser_cdp_url

    async def open_login_page(self) -> dict[str, Any]:
        endpoint = _validate_loopback_endpoint(self.endpoint)
        target = await asyncio.to_thread(_open_url_in_browser, endpoint, BOSS_LOGIN_URL)
        return {
            "state": "opened",
            "url": target.get("url", BOSS_LOGIN_URL),
            "message": "已在 Chrome 调试窗口打开 BOSS 页面；请在 Chrome 中手动完成验证码/安全验证，完成后回到 BossFind 点击“检查 Chrome 会话”。",
            "captcha_policy": "human_only",
        }

    async def status(self) -> dict[str, Any]:
        try:
            async with CdpPage(self.endpoint) as page:
                state = await page.evaluate("""(() => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    const body = document.body?.innerText || '';
                    const loggedIn = Boolean(
                        document.querySelector('a[href*="/web/geek/chat"], a[href*="/web/geek/resume"], .user-nav, .nav-figure, [ka="header-message"]')
                    ) || /\u6d88\u606f|\u7b80\u5386|\u6c9f\u901a\u8fc7|\u6211\u7684\u5728\u7ebf\u7b80\u5386/.test(body);
                    const url = `${location.pathname}${location.search}`.toLowerCase();
                    const securityPath = /passport|verify|captcha|security-check|safe/.test(url);
                    const selectorSecurity = [...document.querySelectorAll([
                        'iframe[src*="captcha"]', 'iframe[src*="verify"]',
                        '.geetest_panel', '.geetest_box', '.geetest_holder', '.nc-container',
                        '[class*="captcha"]', '[id*="captcha"]', '[class*="geetest"]', '[id*="geetest"]'
                    ].join(','))].some(visible);
                    const dialogs = [...document.querySelectorAll('.dialog-wrap, .dialog-container, [role="dialog"], [class*="verify"], [class*="captcha"], [class*="security"]')]
                        .filter(visible)
                        .map(el => (el.innerText || '').trim())
                        .filter(Boolean);
                    const challengePattern = /\u9a8c\u8bc1\u7801|\u5b89\u5168\u9a8c\u8bc1|\u6ed1\u5757|\u8bf7\u5b8c\u6210\u9a8c\u8bc1|captcha|verify/i;
                    const security = securityPath || selectorSecurity || dialogs.some(text => challengePattern.test(text));
                    return {title: document.title, url: location.href, loggedIn, security, jobPage: location.pathname.includes('/web/geek/')};
                })()""")
            if state.get("security"):
                session_state = "needs_human"
                message = "BOSS 正在要求人工验证码/安全验证，请先在 Chrome 中完成"
            elif state.get("loggedIn"):
                session_state = "ready"
                message = "已连接当前 Chrome 中的 BOSS 登录会话"
            else:
                session_state = "login_required"
                message = "已找到 BOSS 标签页，但未确认登录状态；请在 Chrome 中手动登录"
            return {
                "state": session_state,
                "connected": True,
                "title": state.get("title", ""),
                "url": state.get("url", ""),
                "message": message,
                "browser": "chrome",
                "endpoint": self.endpoint,
                "captcha_policy": "human_only",
            }
        except Exception as exc:
            return {
                "state": "blocked", "connected": False, "title": "", "url": "",
                "message": str(exc).splitlines()[0][:240], "browser": "chrome",
                "endpoint": self.endpoint, "captcha_policy": "human_only",
            }

    async def _ensure_jobs_page(self, page: CdpPage) -> None:
        on_jobs = await page.evaluate("location.pathname.includes('/web/geek/jobs')")
        on_detail = await page.evaluate("location.pathname.includes('/job_detail/')")
        if not on_jobs and not on_detail:
            await page.navigate(BOSS_JOBS_URL)
            await page.wait_for(
                f"document.querySelector('.job-search-form input.input, {JOB_CARD_SELECTOR}')",
                timeout=12,
            )

    async def _select_city(self, page: CdpPage, city: str) -> None:
        if not city:
            return

        city_url = jobs_url_for_city(city)
        if city_url:
            current_url = await page.evaluate("location.href")
            code = city_code_for(city)
            if f"city={code}" not in current_url:
                await page.navigate(city_url)
                if await page.has_security_challenge():
                    raise BrowserBridgeError("BOSS 要求人工安全验证，请先在 Chrome 中完成")
                if not await page.wait_for(
                    f"document.querySelector('.job-search-form input.input, {JOB_CARD_SELECTOR}, input[name=\"query\"]')",
                    timeout=12,
                ):
                    raise BrowserBridgeError(f"已切换到“{city}”，但没有读取到 BOSS 岗位搜索区域")
            return

        current = await page.evaluate("document.querySelector('.cur-city-label')?.textContent.trim() || document.querySelector('[class*=city]')?.textContent.trim() || ''")
        if not city or current == city:
            return
        opened = await page.evaluate("""(() => {
            const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            const selectors = [
                '.city-label.active',
                '.cur-city-label',
                '[ka="header-city-switch"]',
                '[ka="city-switch"]',
                '[class*="city"][class*="label"]',
                '[class*="city"][class*="switch"]'
            ];
            const button = selectors.map(selector => document.querySelector(selector)).find(visible);
            if (!button) return false;
            button.click(); return true;
        })()""", user_gesture=True)
        if not opened or not await page.wait_for("""(() => {
            const dialogs = [...document.querySelectorAll('.city-select-dialog, [class*="city"][class*="dialog"], [class*="city"][class*="panel"], [class*="city"][class*="pop"]')];
            return dialogs.some(el => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            });
        })()"""):
            raise BrowserBridgeError(f"无法打开 BOSS 城市选择器，且暂不支持城市“{city}”的直接跳转码")

        tabs = await page.evaluate("[...document.querySelectorAll('.city-char-list li')].map(e => e.textContent.trim())") or []
        for tab in [None, *tabs]:
            if tab is not None:
                tab_json = _safe_js_string(tab)
                await page.evaluate(f"""(() => {{
                    const tab = [...document.querySelectorAll('.city-char-list li')].find(e => e.textContent.trim() === {tab_json});
                    if (tab) tab.click(); return Boolean(tab);
                }})()""", user_gesture=True)
                await asyncio.sleep(0.1)
            city_json = _safe_js_string(city)
            clicked = await page.evaluate(f"""(() => {{
                const items = [...document.querySelectorAll('.city-select-dialog .dialog-body a, .city-select-dialog .dialog-body li')]
                    .filter(e => !e.closest('.city-char-list'));
                const item = items.find(e => e.textContent.trim() === {city_json});
                if (item) item.click(); return Boolean(item);
            }})()""", user_gesture=True)
            if clicked:
                await page.wait_for(f"document.querySelector('.cur-city-label')?.textContent.trim() === {city_json}", timeout=8)
                return
        await page.evaluate("document.querySelector('.city-select-dialog [ka=dialog_close]')?.click()", user_gesture=True)
        raise BrowserBridgeError(f"BOSS 城市列表中未找到“{city}”")

    async def _search(self, page: CdpPage, keyword: str, city: str = "") -> None:
        if city_code_for(city):
            last_url = ""
            for url in search_url_attempts(city, keyword):
                last_url = url
                await page.navigate(url)
                if await page.has_security_challenge():
                    raise BrowserBridgeError("BOSS 要求人工安全验证，请先在 Chrome 中完成")
                if await page.wait_for(READABLE_JOB_CARD_EXPRESSION, timeout=12):
                    await asyncio.sleep(0.5)
                    return
            raise BrowserBridgeError(
                f"搜索“{city} / {keyword}”后没有读取到岗位列表；已尝试 BOSS 直接城市入口和组合搜索入口，最后页面：{last_url}"
            )

        keyword_json = _safe_js_string(keyword)
        ok = await page.evaluate(f"""(() => {{
            const input = document.querySelector('.job-search-form input.input, input[name="query"], input[placeholder*="搜索"], input[placeholder*="职位"], input[placeholder*="岗位"]');
            const button = document.querySelector('.job-search-form .search-btn, button[type="submit"], [ka*="search"], .search-btn');
            if (input) {{
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                if (setter) setter.call(input, {keyword_json});
                else input.value = {keyword_json};
                input.dispatchEvent(new Event('input', {{bubbles:true}}));
                input.dispatchEvent(new Event('change', {{bubbles:true}}));
                if (button) button.click();
                else input.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true}}));
                return true;
            }}
            const url = new URL(location.href);
            url.pathname = '/web/geek/jobs';
            url.searchParams.set('query', {keyword_json});
            location.href = url.toString();
            return true;
        }})()""", user_gesture=True)
        if not ok:
            raise BrowserBridgeError("当前 BOSS 页面没有可用的岗位搜索框")
        if not await page.wait_for(READABLE_JOB_CARD_EXPRESSION, timeout=10):
            if await page.has_security_challenge():
                raise BrowserBridgeError("BOSS 要求人工安全验证，请先在 Chrome 中完成")
            raise BrowserBridgeError(f"搜索“{keyword}”后没有读取到岗位列表")
        await asyncio.sleep(0.5)

    async def _cards(self, page: CdpPage) -> list[dict[str, Any]]:
        cards = await page.evaluate("""[...document.querySelectorAll('li.job-card-box, .job-card-box')].map(box => {
            const link = box.querySelector('a.job-name[href*="/job_detail/"], a[href*="/job_detail/"]');
            const title = link?.textContent || box.querySelector('.job-name, [class*="job-name"], [class*="job-title"]')?.textContent || '';
            const tagText = [...box.querySelectorAll('.tag-list li, .job-card-footer li, .job-info li, [class*="tag"]')]
                .map(e => e.textContent.trim())
                .filter(Boolean);
            const infoText = [...box.querySelectorAll('.job-info li, .job-card-left li, .job-primary li')]
                .map(e => e.textContent.trim())
                .filter(Boolean);
            return {
                job_title: title.trim(),
                job_url: link?.href || '',
                salary: (box.querySelector('.job-salary, [class*="salary"]')?.textContent || '').trim(),
                company: (box.querySelector('.boss-name, .company-name, [class*="company"]')?.textContent || '').trim(),
                location: (box.querySelector('.company-location, .job-area, [class*="location"], [class*="area"]')?.textContent || '').trim(),
                experience: infoText.find(text => /经验|不限|\\d+-\\d+年|\\d+年以上/.test(text)) || '',
                education: infoText.find(text => /学历|大专|本科|硕士|博士/.test(text)) || '',
                company_size: (box.querySelector('.company-tag-list li:nth-child(2), .company-info li:nth-child(2)')?.textContent || '').trim(),
                company_industry: (box.querySelector('.company-tag-list li:first-child, .company-info li:first-child')?.textContent || '').trim(),
                tags: tagText
            };
        }).filter(job => job.job_url)""") or []
        for card in cards:
            card["salary"] = decode_boss_text(card.get("salary", ""))
            card["job_id"] = card["job_url"].rsplit("/", 1)[-1].split(".", 1)[0]
            card.update(normalize_job_features(card))
        return cards

    async def _advance_results_page(self, page: CdpPage, seen_card_ids: set[str]) -> bool:
        before_count = len(seen_card_ids)
        advanced = await page.evaluate("""(() => {
            const visible = (el) => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            const scrollers = [
                document.querySelector('.job-list-container'),
                document.querySelector('.job-list'),
                document.querySelector('[class*="job-list"]'),
                document.scrollingElement,
                document.documentElement,
                document.body,
            ].filter(Boolean);
            for (const el of scrollers) {
                try { el.scrollTop = el.scrollHeight; } catch (_) {}
            }
            window.scrollTo(0, document.body?.scrollHeight || document.documentElement?.scrollHeight || 0);
            const nextButtons = [...document.querySelectorAll([
                '.options-pages a', '.page a', '.pagination a', '.ui-pagination a',
                '[ka*="page-next"]', '[class*="next"]', 'a[rel="next"]'
            ].join(','))]
                .filter(visible)
                .filter(el => !/disabled|unable|inactive/.test(el.className || ''))
                .filter(el => /\u4e0b\u4e00\u9875|next|>/.test((el.innerText || el.getAttribute('aria-label') || el.getAttribute('ka') || '').trim().toLowerCase()));
            const button = nextButtons[nextButtons.length - 1];
            if (button) { button.click(); return 'clicked'; }
            return 'scrolled';
        })()""", user_gesture=True)
        for _ in range(12):
            await asyncio.sleep(0.35)
            cards = await self._cards(page)
            new_ids = {card.get("job_id", "") for card in cards if card.get("job_id")}
            if len(new_ids - seen_card_ids) > 0 or len(new_ids) > before_count:
                return True
        return advanced == "clicked"

    async def _read_detail(self, page: CdpPage, card: dict[str, Any]) -> dict[str, Any]:
        if card.get("job_url"):
            current_url = await page.evaluate("location.href")
            if card["job_url"] not in current_url:
                await page.navigate(card["job_url"])
        loaded = await page.wait_for(
            "document.querySelector('.job-detail-container, .job-detail, .job-sec-text, #main')",
            timeout=8,
        )
        if not loaded:
            return {**card, "description": "", "recruiter": ""}
        detail = await page.evaluate("""(() => {
            const root = document.querySelector('.job-detail-container, .job-detail, #main');
            if (!root) return {description:'', recruiter:''};
            const primaryBlocks = [...root.querySelectorAll('.job-sec-text, .job-detail-body')]
                .map(el => (el.innerText || '').trim())
                .filter(Boolean);
            const fallbackBlocks = [...root.querySelectorAll('.job-detail-section')]
                .map(el => (el.innerText || '').trim())
                .filter(Boolean);
            const body = ((primaryBlocks[0] || fallbackBlocks.join('\\n\\n')) || root.innerText || '').trim();
            const recruiter = (root.querySelector('.job-boss-info .name, .boss-info-attr .name, .job-boss-info, .boss-name')?.innerText || '').trim();
            const text = root.innerText || '';
            const tags = [...root.querySelectorAll('.job-tags span, .job-tags li, .tag-list span, .tag-list li, .job-keyword-list span, [class*="tag"]')]
                .map(el => (el.innerText || el.textContent || '').trim())
                .filter(Boolean);
            const companyItems = [...root.querySelectorAll('.company-info li, .sider-company p, .job-company-info li, [class*="company"] li')]
                .map(el => (el.innerText || el.textContent || '').trim())
                .filter(Boolean);
            const experience = (text.match(/(?:经验不限|不限|\\d+-\\d+年|\\d+年以上)/) || [''])[0];
            const education = (text.match(/(?:学历不限|大专|本科|硕士|博士)/) || [''])[0];
            return {
                description: body.slice(0, 12000),
                recruiter: recruiter.slice(0, 100),
                tags,
                experience,
                education,
                company_industry: companyItems.find(item => /互联网|人工智能|大数据|电商|企业服务|软件|金融|贸易|教育/.test(item)) || '',
                company_size: companyItems.find(item => /\\d+-\\d+人|\\d+人以上|少于\\d+人/.test(item)) || ''
            };
        })()""") or {}
        return normalize_job_features({**card, **detail})

    async def current_job_page_context(self) -> dict[str, Any]:
        """Read the currently visible BOSS jobs page without clicking external actions."""
        async with CdpPage(self.endpoint) as page:
            if await page.has_security_challenge():
                raise BrowserBridgeError("BOSS 要求人工安全验证，请先在 Chrome 中完成")
            if not await page.evaluate("location.pathname.includes('/web/geek/jobs') || location.pathname.includes('/web/geek/map/jobs')"):
                return {"available": False, "reason": "当前 Chrome 未停留在 BOSS 岗位搜索页"}
            raw = await page.evaluate("""(() => {
                const input = document.querySelector('.job-search-form input.input, input[name="query"], input[placeholder*="搜索"], input[placeholder*="职位"], input[placeholder*="岗位"]');
                const cards = [...document.querySelectorAll('li.job-card-box, .job-card-box')].slice(0, 30).map(box => ({
                    text: (box.innerText || box.textContent || '').replace(/\\s+/g, ' ').trim(),
                    salary: (box.querySelector('.job-salary, [class*="salary"]')?.textContent || '').trim(),
                    location: (box.querySelector('.company-location, .job-area, [class*="location"], [class*="area"]')?.textContent || '').trim(),
                    hasJobLink: Boolean(box.querySelector('a[href*="/job_detail/"]'))
                }));
                return {
                    available: true,
                    url: location.href,
                    pathname: location.pathname,
                    queryText: (input?.value || input?.getAttribute('value') || '').trim(),
                    visibleText: (document.body?.innerText || '').slice(0, 3000),
                    cards
                };
            })()""") or {}
        url = str(raw.get("url", ""))
        params = parse_qs(urlparse(url).query)
        query = unquote((params.get("query") or [""])[0]).strip()
        code = (params.get("city") or params.get("cityCode") or [""])[0]
        city = city_for_code(code) or ""
        query_text = str(raw.get("queryText") or query or "").strip()
        if "/" in query_text:
            left, right = [part.strip() for part in query_text.split("/", 1)]
            if left and city_code_for(left):
                city = normalized_city_name(left)
                query_text = right or query_text
        location_text = " ".join(str(card.get("location") or card.get("text") or "") for card in raw.get("cards", []))
        city_hits = [
            name for name in CITY_CODES
            if name in location_text and name not in {"杭州"} or (name == "杭州" and "杭州" in location_text)
        ]
        if city_hits:
            city = max(city_hits, key=lambda item: location_text.count(item))
        cards = raw.get("cards", [])
        readable_cards = [
            {**card, "salary": decode_boss_text(str(card.get("salary", "")))}
            for card in cards
            if card.get("hasJobLink")
        ]
        return {
            "available": bool(raw.get("available")),
            "url": url,
            "city": city,
            "keyword": query_text,
            "card_count": len(readable_cards),
            "is_map": "/web/geek/map/" in str(raw.get("pathname", "")),
            "sample_cards": readable_cards[:5],
        }

    async def search_jobs(self, campaign: dict[str, Any], max_results: int = 20) -> list[dict[str, Any]]:
        return (await self.search_jobs_with_diagnostics(campaign, max_results=max_results))["jobs"]

    async def search_jobs_with_diagnostics(self, campaign: dict[str, Any], max_results: int = 20) -> dict[str, Any]:
        last_error: BaseException | None = None
        for attempt in range(3):
            try:
                return await self._search_jobs_with_diagnostics_once(campaign, max_results=max_results)
            except Exception as exc:
                if not _is_transient_cdp_error(exc) or attempt == 2:
                    raise
                last_error = exc
                await _sleep_before_cdp_retry(attempt)
        raise BrowserBridgeError(f"BOSS 页面刚刚跳转或刷新，重新连接后仍未恢复：{last_error}")

    async def _search_jobs_with_diagnostics_once(self, campaign: dict[str, Any], max_results: int = 20) -> dict[str, Any]:
        results: dict[str, dict[str, Any]] = {}
        diagnostics: dict[str, Any] = {
            "searched_keywords": [],
            "cards_read": 0,
            "rejected_salary": 0,
            "rejected_city": 0,
            "rejected_excluded": 0,
            "rejected_score": 0,
            "duplicates": 0,
            "examples": [],
        }
        first_keyword = next((str(item).strip() for item in campaign.get("keywords", []) if str(item).strip()), "")
        if first_keyword:
            first_urls = search_url_attempts(campaign.get("city", ""), first_keyword)
            if first_urls:
                await asyncio.to_thread(_open_url_in_browser, _validate_loopback_endpoint(self.endpoint), first_urls[0])
                await asyncio.sleep(1)
        async with CdpPage(self.endpoint) as page:
            if await page.has_security_challenge():
                raise BrowserBridgeError("BOSS 要求人工安全验证，请先在 Chrome 中完成")
            await self._ensure_jobs_page(page)
            await self._select_city(page, campaign["city"])
            seen_card_ids: set[str] = set()
            max_pages_per_keyword = int(campaign.get("max_pages_per_keyword") or self.MAX_PAGES_PER_KEYWORD)
            for keyword in campaign["keywords"]:
                diagnostics["searched_keywords"].append(keyword)
                await self._search(page, keyword, campaign["city"])
                page_index = 0
                while len(results) < max_results and page_index < max_pages_per_keyword:
                    list_url = await page.evaluate("location.href")
                    page_cards = await self._cards(page)
                    if not page_cards:
                        break
                    for card in page_cards:
                        job_id = card.get("job_id", "")
                        if not job_id or job_id in seen_card_ids:
                            diagnostics["duplicates"] += 1
                            continue
                        seen_card_ids.add(job_id)
                        diagnostics["cards_read"] += 1
                        if job_id in results:
                            diagnostics["duplicates"] += 1
                            continue
                        low, high = salary_bounds(card["salary"])
                        if not low or max(low, campaign["salary_min"]) > min(high, campaign["salary_max"]):
                            diagnostics["rejected_salary"] += 1
                            if len(diagnostics["examples"]) < 5:
                                diagnostics["examples"].append(f"{card.get('job_title', '')} ({card.get('salary', '')}) salary mismatch")
                            continue
                        if campaign["city"] and campaign["city"] not in card["location"]:
                            diagnostics["rejected_city"] += 1
                            if len(diagnostics["examples"]) < 5:
                                diagnostics["examples"].append(f"{card.get('job_title', '')} ({card.get('location', '')}) city mismatch")
                            continue
                        title = card["job_title"].lower()
                        if any(word.lower() in title for word in campaign["excluded_keywords"]):
                            diagnostics["rejected_excluded"] += 1
                            continue
                        detail = await self._read_detail(page, card)
                        score, reasons = score_job(detail, campaign)
                        detail = rank_job(detail, campaign, score, reasons)
                        match_threshold = int(campaign.get("match_threshold") or self.MATCH_THRESHOLD)
                        if detail["match_score"] < match_threshold:
                            diagnostics["rejected_score"] += 1
                            if len(diagnostics["examples"]) < 5:
                                diagnostics["examples"].append(f"{card.get('job_title', '')} score {detail['match_score']}: {'; '.join(detail.get('match_reasons') or reasons)}")
                            continue
                        detail.update({
                            "match_score": detail["match_score"],
                            "reason": "; ".join(detail.get("match_reasons") or reasons),
                            "source_keyword": keyword,
                            "source": "boss_live",
                        })
                        results[job_id] = detail
                        if len(results) >= max_results:
                            break
                    if len(results) >= max_results:
                        break
                    current_path_is_jobs = await page.evaluate("location.pathname.includes('/web/geek/jobs') || location.pathname.includes('/web/geek/map/jobs')")
                    if not current_path_is_jobs:
                        await page.navigate(list_url)
                        await page.wait_for(READABLE_JOB_CARD_EXPRESSION, timeout=8)
                    if not await self._advance_results_page(page, seen_card_ids):
                        break
                    page_index += 1
                if len(results) >= max_results:
                    break
        return {"jobs": sorted(results.values(), key=lambda item: item["match_score"], reverse=True), "diagnostics": diagnostics}

    async def _send_chat_message(self, page: CdpPage, text: str) -> ContactResult:
        if not text.strip():
            return ContactResult("needs_human", "招呼/回复内容为空，已停止")
        if any(word in text for word in SENSITIVE_REPLY_WORDS):
            return ContactResult("needs_human", "待发送内容包含敏感词，已转人工")
        text_json = _safe_js_string(text.strip())
        result = await page.evaluate(f"""(() => {{
            const visible = (el) => {{
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            }};
            const editors = [...document.querySelectorAll([
                    '.chat-conversation [contenteditable="true"]',
                    '.chat-conversation textarea',
                    '.chat-input [contenteditable="true"]',
                    '.chat-input textarea',
                    '.message-input [contenteditable="true"]',
                    '.message-input textarea',
                    '[class*="editor"] [contenteditable="true"]',
                    '[class*="input"] [contenteditable="true"]',
                    '[contenteditable="true"]',
                    'textarea'
                ].join(','))]
                .filter(visible)
                .filter(el => !el.closest('[aria-hidden="true"]'))
                .filter(el => !el.closest('.boss-search-container, .job-search-form, header'))
                .filter(el => !/搜索|职位|公司/.test(el.getAttribute('placeholder') || ''));
            if (editors.length !== 1) return {{ok:false, reason:`找到 ${{editors.length}} 个可见输入框，未安全发送`}};
            const editor = editors[0];
            editor.focus();
            if (editor.isContentEditable) {{
                editor.innerText = {text_json};
                editor.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:{text_json}}}));
            }} else {{
                const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
                    || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                setter.call(editor, {text_json});
                editor.dispatchEvent(new Event('input', {{bubbles:true}}));
                editor.dispatchEvent(new Event('change', {{bubbles:true}}));
            }}
            const buttons = [...document.querySelectorAll([
                    '.chat-conversation button',
                    '.chat-conversation .btn',
                    '.chat-conversation [role="button"]',
                    '.chat-input button',
                    '.chat-input .btn',
                    '[class*="send"]',
                    'button',
                    '.btn',
                    '[role="button"]'
                ].join(','))]
                .filter(visible)
                .filter(el => !el.disabled && !el.classList.contains('disabled'))
                .filter(el => /发送|send/i.test((el.innerText || el.getAttribute('aria-label') || el.getAttribute('ka') || el.className || '').trim()));
            if (buttons.length !== 1) return {{ok:false, reason:`找到 ${{buttons.length}} 个发送按钮，未安全发送`}};
            buttons[0].click();
            return {{ok:true, reason:'已点击发送按钮'}};
        }})()""", user_gesture=True)
        await asyncio.sleep(1)
        if await page.has_security_challenge():
            return ContactResult("needs_human", "BOSS 要求人工安全验证，自动发送已暂停")
        if result and result.get("ok"):
            return ContactResult("sent", str(result.get("reason", "消息已发送")))
        return ContactResult("needs_human", str((result or {}).get("reason", "未检测到安全的聊天输入区")))

    async def contact_job(self, job: dict[str, Any], greeting: str | None = None) -> ContactResult:
        last_error: BaseException | None = None
        for attempt in range(3):
            try:
                return await self._contact_job_once(job, greeting)
            except Exception as exc:
                if not _is_transient_cdp_error(exc) or attempt == 2:
                    raise
                last_error = exc
                await _sleep_before_cdp_retry(attempt)
        return ContactResult("uncertain", f"BOSS 页面刚刚跳转或刷新，重新连接后仍未恢复：{last_error}")

    async def _contact_job_once(self, job: dict[str, Any], greeting: str | None = None) -> ContactResult:
        async with CdpPage(self.endpoint) as page:
            await page.navigate(job["job_url"])
            if await page.has_security_challenge():
                return ContactResult("needs_human", "BOSS 要求人工安全验证，自动投递已暂停")
            await page.wait_for("Boolean(document.querySelector('.op-btn-chat, .btn-startchat'))", timeout=8)
            state = await page.evaluate("""(() => {
                const button = document.querySelector('.op-btn-chat, .btn-startchat');
                return {text:(button?.innerText || '').trim(), found:Boolean(button)};
            })()""")
            if not state.get("found"):
                return ContactResult("failed", "职位页未找到沟通按钮")
            if "继续沟通" in state.get("text", ""):
                return ContactResult("already_contacted", "该岗位已经沟通过，已跳过")
            if "立即沟通" not in state.get("text", ""):
                return ContactResult("needs_human", f"沟通按钮状态异常：{state.get('text') or '未知'}")

            clicked = await page.evaluate("""(() => {
                const button = document.querySelector('.op-btn-chat, .btn-startchat');
                if (!button) return false; button.click(); return true;
            })()""", user_gesture=True)
            if not clicked:
                return ContactResult("failed", "沟通按钮点击失败")
            await asyncio.sleep(1.5)
            after = await page.evaluate("""(() => {
                const button = document.querySelector('.op-btn-chat, .btn-startchat');
                const dialogs = [...document.querySelectorAll('.dialog-wrap, .dialog-container, [role=dialog]')]
                    .filter(e => getComputedStyle(e).display !== 'none')
                    .map(e => (e.innerText || '').trim()).join(String.fromCharCode(10));
                const nl = String.fromCharCode(10);
                const combined = String(button?.innerText || '') + nl + String(dialogs || '') + nl + String(location.href || '');
                const sentPattern = new RegExp('\\u5df2\\u53d1\\u9001|\\u7ee7\\u7eed\\u6c9f\\u901a|\\u6c9f\\u901a\\u4e2d');
                const sent = sentPattern.test(combined) || location.pathname.includes('/web/geek/chat');
                return {url:location.href, text:(button?.innerText || '').trim(), dialogs:dialogs.slice(0,2000), sent};
            })()""")
            combined = f"{after.get('text', '')} {after.get('dialogs', '')} {after.get('url', '')}"
            if any(word in combined.lower() for word in [item.lower() for item in SECURITY_WORDS]):
                return ContactResult("needs_human", "BOSS 要求人工安全验证，自动投递已暂停")
            if after.get("sent"):
                return ContactResult("sent", "BOSS page shows the outreach message was sent")
            if after.get("dialogs"):
                return ContactResult("sent", "Clicked outreach; BOSS returned a non-security dialog after the action")
            if greeting and ("/chat" in after.get("url", "") or "继续沟通" in combined):
                sent = await self._send_chat_message(page, greeting)
                if sent.status == "sent":
                    return ContactResult("sent", "已发起沟通并发送招呼语")
                return ContactResult("sent", f"已发起沟通；{sent.message}")
            if "继续沟通" in combined or "/chat" in after.get("url", ""):
                return ContactResult("sent", "已通过 BOSS 的“立即沟通”发起岗位沟通")
            return ContactResult("sent", "Clicked outreach; no security or failure signal appeared")

    async def _ensure_chat_tab(self) -> None:
        endpoint = _validate_loopback_endpoint(self.endpoint)
        targets = await asyncio.to_thread(_load_targets, endpoint)
        has_chat = any(
            target.get("type") in {"webview", "page"}
            and _is_allowed_site(str(target.get("url", "")))
            and "/web/geek/chat" in str(target.get("url", ""))
            for target in targets
        )
        if not has_chat:
            await asyncio.to_thread(_open_url_in_browser, endpoint, BOSS_CHAT_URL)

    async def read_reply_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        await self._ensure_chat_tab()
        async with CdpPage(self.endpoint, prefer_chat=True) as page:
            if not await page.evaluate("location.pathname.includes('/web/geek/chat')"):
                await page.navigate(BOSS_CHAT_URL)
            if await page.has_security_challenge():
                raise BrowserBridgeError("BOSS 要求人工安全验证，请先在 Chrome 中完成")
            await page.wait_for("document.readyState === 'complete'", timeout=12)
            await asyncio.sleep(0.6)
            return await page.evaluate(f"""(() => {{
                const visible = (el) => {{
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                }};
                const selectors = [
                    '.chat-list li', '.chat-list-item', '.conversation-list li', '.session-list li',
                    '.friend-list li', '.user-list li', '.friend-content-warp', '.friend-content',
                    '[class*="chat-list"] li', '[class*="conversation"] li', '[class*="user-list"] li'
                ];
                const nodes = [];
                for (const selector of selectors) {{
                    for (const node of document.querySelectorAll(selector)) {{
                        if (!visible(node)) continue;
                        const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (text.length < 4 || /搜索|全部|未读|新招呼|仅沟通|更多|联系人/.test(text)) continue;
                        if (!nodes.includes(node)) nodes.push(node);
                    }}
                }}
                return nodes.slice(0, {int(limit)}).map((node, index) => {{
                    const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    return {{
                        index,
                        title: text.slice(0, 60),
                        last_message: text.slice(-240),
                        raw: text.slice(0, 500)
                    }};
                }});
            }})()""") or []

    async def send_reply_to_candidate(self, index: int, text: str) -> ContactResult:
        await self._ensure_chat_tab()
        async with CdpPage(self.endpoint, prefer_chat=True) as page:
            if not await page.evaluate("location.pathname.includes('/web/geek/chat')"):
                await page.navigate(BOSS_CHAT_URL)
            if await page.has_security_challenge():
                return ContactResult("needs_human", "BOSS 要求人工安全验证，自动回复已暂停")
            clicked = await page.evaluate(f"""(() => {{
                const visible = (el) => {{
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                }};
                const selectors = [
                    '.chat-list li', '.chat-list-item', '.conversation-list li', '.session-list li',
                    '.friend-list li', '.user-list li', '.friend-content-warp', '.friend-content',
                    '[class*="chat-list"] li', '[class*="conversation"] li', '[class*="user-list"] li'
                ];
                const nodes = [];
                for (const selector of selectors) {{
                    for (const node of document.querySelectorAll(selector)) {{
                        if (!visible(node)) continue;
                        const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (text.length < 4 || /搜索|全部|未读|新招呼|仅沟通|更多|联系人/.test(text)) continue;
                        if (!nodes.includes(node)) nodes.push(node);
                    }}
                }}
                const node = nodes[{int(index)}];
                if (!node) return false;
                node.click();
                return true;
            }})()""", user_gesture=True)
            if not clicked:
                return ContactResult("failed", "未找到对应聊天会话，可能列表已变化")
            await asyncio.sleep(0.8)
            return await self._send_chat_message(page, text)

    async def random_pause(self, minimum: int, maximum: int) -> None:
        await asyncio.sleep(random.randint(minimum, maximum))


existing_browser_adapter = ExistingBrowserAdapter()
