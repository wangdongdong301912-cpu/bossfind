import os

os.environ.setdefault("BOSSFIND_DATABASE", "data/test_bossfind.db")

from app import database, services
from app.config import get_settings
from app.schemas import RunRequest
from app.vault import vault


def setup_function():
    get_settings.cache_clear()
    path = get_settings().database_path
    if path.exists():
        path.unlink()
    database.init_db()


def teardown_function():
    path = get_settings().database_path
    if path.exists():
        path.unlink()


def test_default_campaign_and_preview():
    campaign = services.get_campaign()
    jobs = services.preview_jobs()
    assert campaign["city"] == "嘉兴"
    assert campaign["dry_run"] is True
    assert jobs[0]["source"] == "demo"
    assert jobs[0]["match_score"] >= jobs[-1]["match_score"]


def test_dry_run_creates_audit_records_without_sending():
    import asyncio

    result = asyncio.run(services.run_campaign(RunRequest(limit=3)))
    records = database.list_records()
    assert result["processed"] == 3
    assert result["sent"] == 0
    assert len(records) == 3
    assert all(record["status"] == "dry_run" for record in records)


def test_force_live_requires_explicit_confirmation_even_when_dry_run_is_on():
    import asyncio

    try:
        asyncio.run(services.run_campaign(RunRequest(limit=1, force_live=True)))
        assert False, "forced live run should require explicit confirmation"
    except ValueError as exc:
        assert "真实投递前必须确认" in str(exc)


def test_force_dry_stays_offline_even_when_campaign_is_live():
    import asyncio
    from app.schemas import CampaignPayload

    campaign = services.get_campaign()
    campaign["dry_run"] = False
    services.update_campaign(CampaignPayload(**campaign))

    result = asyncio.run(services.run_campaign(RunRequest(limit=2, force_dry=True)))
    assert result["mode"] == "dry_run"
    assert result["sent"] == 0
    assert all(record["status"] == "dry_run" for record in database.list_records())


def test_live_run_with_no_candidates_writes_actionable_audit_record(monkeypatch):
    import asyncio
    from app.schemas import CampaignPayload

    class EmptyBrowser:
        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def search_jobs(self, *_args, **_kwargs):
            return []

    campaign = services.get_campaign()
    campaign.update({"dry_run": True, "work_start": "00:00", "work_end": "23:59"})
    services.update_campaign(CampaignPayload(**campaign))
    monkeypatch.setattr(services, "existing_browser_adapter", EmptyBrowser())

    result = asyncio.run(services.run_campaign(RunRequest(limit=1, force_live=True, confirm_external_action=True)))
    records = database.list_records()
    assert result["mode"] == "live"
    assert result["sent"] == 0
    assert records[0]["job_title"] == "真实投递未执行"
    assert records[0]["status"] == "failed"
    assert "没有向 BOSS 发起真实沟通" in records[0]["reason"]


def test_live_run_with_no_candidates_pauses_batch(monkeypatch):
    import asyncio
    from app.schemas import CampaignPayload

    class EmptyBrowser:
        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def current_job_page_context(self):
            return {"available": False}

        async def search_jobs_with_diagnostics(self, *_args, **_kwargs):
            return {"jobs": [], "diagnostics": {"cards_read": 45, "rejected_salary": 45, "examples": []}}

    campaign = services.get_campaign()
    campaign.update({"dry_run": True, "daily_limit": 30, "work_start": "00:00", "work_end": "23:59"})
    services.update_campaign(CampaignPayload(**campaign))
    monkeypatch.setattr(services, "existing_browser_adapter", EmptyBrowser())

    run = database.create_run("live_workflow", 20, "queued")
    result = asyncio.run(services.run_campaign(RunRequest(limit=20, force_live=True, confirm_external_action=True), run_id=run["id"]))
    detail = services.get_run_detail(run["id"])

    assert result["sent"] == 0
    assert detail["status"] == "paused"
    assert detail["current_step"] == "manual review required"
    assert detail["security_events"][0]["action"] == "no_candidates"


def test_rule_answer_and_sensitive_question_boundary():
    matched = services.simulate_answer("大概什么时候可以到岗？")
    review = services.simulate_answer("把身份证号发给我")
    assert matched["matched"] is True
    assert matched["action"] == "suggest_reply"
    assert review["matched"] is False
    assert review["action"] == "needs_review"


def test_job_snapshot_upsert_and_priority_listing():
    database.upsert_job_snapshot({
        "job_id": "radar-1",
        "job_url": "https://www.zhipin.com/job_detail/radar-1.html",
        "job_title": "数据分析师",
        "company": "雷达科技",
        "salary": "12-18K",
        "city": "杭州",
        "experience": "1-3年",
        "education": "本科",
        "welfare_tags": ["双休", "五险一金"],
        "work_time": "09:00-18:00",
        "weekend_policy": "双休",
        "match_score": 91,
        "priority_level": "S",
        "match_reasons": ["岗位名称匹配", "双休匹配"],
        "source_keyword": "数据分析师",
    })

    snapshots = services.list_job_snapshots(limit=10)

    assert snapshots[0]["job_id"] == "radar-1"
    assert snapshots[0]["priority_level"] == "S"
    assert snapshots[0]["welfare_tags"] == ["双休", "五险一金"]
    assert snapshots[0]["work_time"] == "09:00-18:00"


def test_collect_radar_jobs_persists_ranked_snapshots(monkeypatch):
    import asyncio
    from app.schemas import CampaignPayload

    class RadarBrowser:
        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def search_jobs_with_diagnostics(self, *_args, **_kwargs):
            return {
                "jobs": [
                    {
                        "job_id": "radar-high",
                        "job_url": "https://www.zhipin.com/job_detail/radar-high.html",
                        "job_title": "数据分析师",
                        "company": "高分科技",
                        "salary": "12-18K",
                        "city": "杭州",
                        "experience": "1-3年",
                        "education": "本科",
                        "welfare_tags": ["周末双休", "五险一金"],
                        "work_time": "朝九晚六",
                        "weekend_policy": "双休",
                        "match_score": 92,
                        "priority_level": "S",
                        "match_reasons": ["岗位名称匹配"],
                        "source_keyword": "数据分析师",
                    },
                    {
                        "job_id": "radar-low",
                        "job_url": "https://www.zhipin.com/job_detail/radar-low.html",
                        "job_title": "数据运营",
                        "company": "低分科技",
                        "salary": "10-12K",
                        "city": "杭州",
                        "match_score": 62,
                        "priority_level": "B",
                        "match_reasons": ["职位描述匹配"],
                    },
                ],
                "diagnostics": {"cards_read": 2, "examples": []},
            }

    campaign = services.get_campaign()
    campaign.update({"dry_run": True, "work_start": "00:00", "work_end": "23:59"})
    services.update_campaign(CampaignPayload(**campaign))
    monkeypatch.setattr(services, "existing_browser_adapter", RadarBrowser())

    result = asyncio.run(services.collect_radar_jobs(limit=20))

    assert result["collected"] == 2
    assert result["snapshots"][0]["job_id"] == "radar-high"
    assert result["snapshots"][0]["priority_level"] == "S"
    assert len(services.list_job_snapshots(limit=10)) == 2


def test_ranking_extracts_work_time_weekend_and_priority():
    from app.ranking import rank_job

    ranked = rank_job(
        {
            "job_title": "数据分析师",
            "company": "示例科技",
            "salary": "12-18K",
            "location": "杭州",
            "description": "负责 SQL 数据分析，周末双休，朝九晚六，五险一金。",
            "tags": ["1-3年", "本科"],
        },
        {"city": "杭州"},
        78,
        ["岗位名称匹配", "薪资区间匹配"],
    )

    assert ranked["priority_level"] == "S"
    assert ranked["weekend_policy"] == "双休"
    assert ranked["work_time"] == "朝九晚六"
    assert any("双休" in tag for tag in ranked["welfare_tags"])


def test_password_lease_is_ephemeral_and_zeroed():
    token, lease = vault.create("13800138000", "only-for-test")
    assert vault.get(token) is lease
    assert lease.reveal_password() == "only-for-test"
    vault.release(token)
    assert vault.get(token) is None
    assert set(lease.password_bytes) == {0}


def test_sms_code_schema_and_idle_status():
    from pydantic import ValidationError
    from app.schemas import SmsLoginVerify
    from app.sms_login import sms_login_manager

    assert SmsLoginVerify(session_token="x" * 16, code="123456").code == "123456"
    try:
        SmsLoginVerify(session_token="x" * 16, code="12345a")
        assert False, "non-numeric code should fail"
    except ValidationError:
        pass
    assert sms_login_manager.status("missing")["state"] == "not_started"


def test_sms_login_requires_explicit_policy_before_browser_action():
    import asyncio
    from app.sms_login import sms_login_manager

    token, _ = vault.create("13800138000", "")
    try:
        try:
            asyncio.run(sms_login_manager.start(token, False))
            assert False, "policy guard should stop the flow"
        except ValueError as exc:
            assert "需要同意" in str(exc)
        assert sms_login_manager.status(token)["state"] == "not_started"
    finally:
        vault.release(token)


def test_missing_login_form_becomes_human_verification_not_server_error():
    import asyncio
    from datetime import datetime, timezone
    from app.sms_login import SmsLoginFlow, SmsLoginManager

    class MissingLocator:
        async def inner_text(self):
            return "安全验证"
        async def wait_for(self, **_kwargs):
            raise RuntimeError("form not rendered")

    class ChallengePage:
        url = "https://www.zhipin.com/web/user/?intent=0"
        def is_closed(self):
            return False
        def locator(self, _selector):
            return MissingLocator()

    token, _ = vault.create("13800138000", "")
    manager = SmsLoginManager()
    manager._flows[token] = SmsLoginFlow(None, None, ChallengePage(), "opening", datetime.now(timezone.utc), "")
    try:
        result = asyncio.run(manager.start(token, True))
        assert result["state"] == "human_verification_required"
        assert "人工验证" in result["message"]
    finally:
        manager._flows.clear()
        vault.release(token)



def test_login_url_uses_non_blank_boss_route():
    from app.sms_login import LOGIN_URL
    assert LOGIN_URL == "https://www.zhipin.com/web/user/"
    assert "intent=" not in LOGIN_URL


def test_blank_login_page_retries_with_commit_navigation():
    import asyncio
    from app.sms_login import LOGIN_URL, _open_login_page

    class BodyLocator:
        async def inner_text(self):
            return "手机号登录"

    class RetryPage:
        url = "about:blank"

        def __init__(self):
            self.wait_modes = []

        def is_closed(self):
            return False

        async def goto(self, url, *, wait_until, timeout):
            assert url == LOGIN_URL
            self.wait_modes.append((wait_until, timeout))
            if wait_until == "domcontentloaded":
                raise TimeoutError("page kept loading")
            self.url = LOGIN_URL

        async def wait_for_timeout(self, _timeout):
            return None

        def locator(self, selector):
            assert selector == "body"
            return BodyLocator()

    page = RetryPage()
    text = asyncio.run(_open_login_page(page))

    assert text == "手机号登录"
    assert [mode for mode, _ in page.wait_modes] == ["domcontentloaded", "commit"]


def test_persistent_blank_login_page_returns_actionable_error():
    import asyncio
    from app.sms_login import _open_login_page

    class BlankPage:
        url = "about:blank"

        def is_closed(self):
            return False

        async def goto(self, *_args, **_kwargs):
            raise TimeoutError("network unavailable")

    try:
        asyncio.run(_open_login_page(BlankPage()))
        assert False, "persistent blank page should fail"
    except ValueError as exc:
        assert "已关闭空白窗口" in str(exc)


def test_verify_on_security_page_returns_human_state_instead_of_structure_error():
    import asyncio
    from datetime import datetime, timezone
    from app.sms_login import SmsLoginFlow, SmsLoginManager

    class EmptyLocator:
        async def count(self):
            return 0
    class BodyLocator:
        async def inner_text(self):
            return "安全验证：请完成验证"
    class EmptyFrame:
        def locator(self, _selector):
            return EmptyLocator()
    class SecurityPage:
        url = "https://www.zhipin.com/web/passport/zp/verify.html"
        frames = [EmptyFrame()]
        def is_closed(self):
            return False
        def locator(self, selector):
            return BodyLocator() if selector == "body" else EmptyLocator()

    token, _ = vault.create("13800138000", "")
    manager = SmsLoginManager()
    manager._flows[token] = SmsLoginFlow(None, None, SecurityPage(), "code_sent", datetime.now(timezone.utc), "")
    try:
        result = asyncio.run(manager.verify(token, "123456"))
        assert result["state"] == "human_verification_required"
        assert "安全验证" in result["message"]
    finally:
        manager._flows.clear()
        vault.release(token)


def test_authenticated_page_detects_account_navigation_on_user_route():
    import asyncio
    from app.sms_login import _is_authenticated_page

    class Locator:
        def __init__(self, found):
            self.found = found
        async def count(self):
            return 1 if self.found else 0
        def nth(self, _index):
            return self
        async def is_visible(self):
            return self.found

    class Frame:
        def locator(self, selector):
            return Locator("/web/geek/chat" in selector or "/web/geek/resume" in selector)

    class AuthenticatedUserPage:
        url = "https://www.zhipin.com/web/user/"
        frames = [Frame()]

    assert asyncio.run(_is_authenticated_page(AuthenticatedUserPage())) is True


def test_boss_salary_font_and_matching_are_decoded_strictly():
    from app.browser_bridge import decode_boss_text, role_keyword_matches, salary_bounds, score_job

    encoded = "\ue032\ue031-\ue032\ue036K"
    assert decode_boss_text(encoded) == "10-15K"
    assert salary_bounds(encoded) == (10, 15)
    assert decode_boss_text("\ue037-\ue03aK·\ue032\ue034薪") == "6-9K·13薪"
    assert salary_bounds("6-36元/时") == (0, 0)
    assert role_keyword_matches("BI工程师", "BI开发工程师(J11124)") is True
    assert role_keyword_matches("数据分析师", "AI数据采集") is False
    assert role_keyword_matches("数据分析师", "高级数据分析师") is True
    campaign = services.get_campaign()
    campaign.update({"city": "嘉兴", "keywords": ["数据分析"], "salary_min": 15, "salary_max": 25, "experience": "1-3年"})
    job = {
        "job_title": "数据分析师", "company": "示例科技", "salary": encoded, "location": "嘉兴",
        "tags": ["1-3年", "本科"], "description": "负责业务数据分析和报表建设",
    }
    score, reasons = score_job(job, campaign)
    assert score >= 70
    assert "岗位名称匹配" in reasons


def test_known_boss_city_uses_direct_jobs_url_instead_of_city_dialog():
    import asyncio
    from app.browser_bridge import existing_browser_adapter, city_code_for, jobs_url_for_city

    assert city_code_for("嘉兴市") == "101210300"
    assert jobs_url_for_city("杭州") == "https://www.zhipin.com/web/geek/jobs?city=101210100"

    class FakePage:
        def __init__(self):
            self.navigated = []
            self.waited = []

        async def evaluate(self, expression, **_kwargs):
            if expression == "location.href":
                return "https://www.zhipin.com/web/geek/jobs"
            raise AssertionError(f"city dialog should not be queried for known city: {expression}")

        async def navigate(self, url):
            self.navigated.append(url)

        async def has_security_challenge(self):
            return False

        async def wait_for(self, expression, **_kwargs):
            self.waited.append(expression)
            return True

    page = FakePage()
    asyncio.run(existing_browser_adapter._select_city(page, "嘉兴"))
    assert page.navigated == ["https://www.zhipin.com/web/geek/jobs?city=101210300"]
    assert page.waited


def test_boss_target_selection_keeps_jobs_page_with_security_check_query():
    from app.browser_bridge import _select_boss_target

    target = _select_boss_target([
        {
            "type": "page",
            "url": "http://127.0.0.1:8000/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/local",
        },
        {
            "type": "page",
            "url": "https://www.zhipin.com/web/geek/jobs?_security_check=1_1782462272045",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/boss",
        },
    ])

    assert target["webSocketDebuggerUrl"].endswith("/boss")


def test_known_city_search_uses_direct_city_query_url_when_it_has_cards():
    import asyncio
    from app.browser_bridge import existing_browser_adapter

    class FakePage:
        def __init__(self):
            self.navigated = []

        async def navigate(self, url):
            self.navigated.append(url)

        async def has_security_challenge(self):
            return False

        async def wait_for(self, *_args, **_kwargs):
            return True

    page = FakePage()
    asyncio.run(existing_browser_adapter._search(page, "BI工程师", "嘉兴"))
    assert page.navigated == ["https://www.zhipin.com/web/geek/jobs?query=BI%E5%B7%A5%E7%A8%8B%E5%B8%88&city=101210300"]


def test_jiaxing_search_falls_back_to_combined_query_under_hangzhou_entry():
    import asyncio
    from app.browser_bridge import existing_browser_adapter, search_url_attempts

    attempts = search_url_attempts("嘉兴", "BI工程师")
    assert attempts[:2] == [
        "https://www.zhipin.com/web/geek/jobs?query=BI%E5%B7%A5%E7%A8%8B%E5%B8%88&city=101210300",
        "https://www.zhipin.com/web/geek/jobs?query=%E5%98%89%E5%85%B4%20%2F%20BI%E5%B7%A5%E7%A8%8B%E5%B8%88&city=101210100",
    ]

    class FakePage:
        def __init__(self):
            self.navigated = []

        async def navigate(self, url):
            self.navigated.append(url)

        async def has_security_challenge(self):
            return False

        async def wait_for(self, *_args, **_kwargs):
            return len(self.navigated) == 2

    page = FakePage()
    asyncio.run(existing_browser_adapter._search(page, "BI工程师", "嘉兴"))
    assert page.navigated == attempts[:2]


def test_boss_detail_reader_supports_current_detail_page_layout():
    import asyncio
    from app.browser_bridge import existing_browser_adapter

    class FakePage:
        def __init__(self):
            self.navigated = []

        async def evaluate(self, expression, **_kwargs):
            if expression == "location.href":
                return "https://www.zhipin.com/web/geek/jobs?query=BI&city=101210300"
            if "job-sec-text" in expression and "description" in expression:
                return {"description": "职位描述\n负责 BI 数据分析", "recruiter": "赵女士 在线"}
            raise AssertionError(f"unexpected expression: {expression}")

        async def navigate(self, url):
            self.navigated.append(url)

        async def wait_for(self, expression, **_kwargs):
            assert ".job-detail" in expression
            return True

    card = {
        "job_id": "job-1",
        "job_url": "https://www.zhipin.com/job_detail/job-1.html",
        "job_title": "BI工程师",
        "company": "示例科技",
        "salary": "12-15K",
        "location": "嘉兴",
        "tags": ["1-3年"],
    }
    detail = asyncio.run(existing_browser_adapter._read_detail(FakePage(), card))
    assert detail["description"] == "职位描述\n负责 BI 数据分析"
    assert detail["recruiter"] == "赵女士 在线"


def test_browser_bridge_rejects_remote_debug_endpoint():
    from app.browser_bridge import BrowserBridgeError, _validate_loopback_endpoint

    assert get_settings().browser_cdp_url == "http://127.0.0.1:9222"
    assert _validate_loopback_endpoint("http://127.0.0.1:9222") == "http://127.0.0.1:9222"
    try:
        _validate_loopback_endpoint("http://example.com:9229")
        assert False, "remote endpoint must be rejected"
    except BrowserBridgeError:
        pass


def test_browser_bridge_connection_refused_is_actionable(monkeypatch):
    from urllib.error import URLError
    from app import browser_bridge
    from app.browser_bridge import BrowserBridgeError

    def refused(*_args, **_kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(browser_bridge, "urlopen", refused)
    try:
        browser_bridge._load_targets("http://127.0.0.1:9222")
        assert False, "connection failure should be converted to an actionable bridge error"
    except BrowserBridgeError as exc:
        assert "start-chrome-boss.ps1" in str(exc)


def test_browser_bridge_retries_transient_search_target_closure():
    import asyncio
    from app.browser_bridge import BrowserBridgeError, ExistingBrowserAdapter, _is_transient_cdp_error

    class FlakySearchBrowser(ExistingBrowserAdapter):
        def __init__(self):
            self.calls = 0

        async def _search_jobs_with_diagnostics_once(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise BrowserBridgeError("Inspected target navigated or closed")
            return {"jobs": [{"job_id": "ok"}], "diagnostics": {"cards_read": 1}}

    browser = FlakySearchBrowser()
    result = asyncio.run(browser.search_jobs_with_diagnostics({"keywords": []}))

    assert _is_transient_cdp_error(BrowserBridgeError("Inspected target navigated or closed"))
    assert result["jobs"][0]["job_id"] == "ok"
    assert browser.calls == 2


def test_browser_bridge_retries_transient_contact_target_closure():
    import asyncio
    from app.browser_bridge import BrowserBridgeError, ContactResult, ExistingBrowserAdapter

    class FlakyContactBrowser(ExistingBrowserAdapter):
        def __init__(self):
            self.calls = 0

        async def _contact_job_once(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise BrowserBridgeError("Inspected target navigated or closed")
            return ContactResult("sent", "sent")

    browser = FlakyContactBrowser()
    result = asyncio.run(browser.contact_job({"job_url": "https://www.zhipin.com/job_detail/ok.html"}))

    assert result.status == "sent"
    assert browser.calls == 2


def test_browser_bridge_injected_javascript_is_valid():
    import ast
    import subprocess
    from pathlib import Path

    source = Path("app/browser_bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    snippets = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"evaluate", "wait_for"} and node.args:
                arg = node.args[0]
                expression = None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    expression = arg.value
                elif isinstance(arg, ast.JoinedStr):
                    parts = []
                    for part in arg.values:
                        parts.append(str(part.value) if isinstance(part, ast.Constant) else '"__TEST__"')
                    expression = "".join(parts)
                if expression and any(token in expression for token in ("=>", "document.", "location.", "Boolean(", "querySelector")):
                    snippets.append((node.lineno, node.func.attr, expression))
            self.generic_visit(node)

    Visitor().visit(tree)
    assert snippets
    for lineno, kind, expression in snippets:
        script = expression
        if kind == "wait_for" and not script.lstrip().startswith("(()"):
            script = f"Boolean({script})"
        if not script.lstrip().startswith(("(()", "Boolean(", "[")):
            script = f"(()=>{{ return ({script}); }})()"
        result = subprocess.run(["node", "--check"], input=script, text=True, capture_output=True, encoding="utf-8")
        assert result.returncode == 0, (
            f"JS syntax error at browser_bridge.py:{lineno}\n{result.stderr}\n{script[:600]}"
        )


def test_boss_salary_parser_accepts_common_live_formats():
    from app.browser_bridge import salary_bounds

    assert salary_bounds("10-15K") == (10, 15)
    assert salary_bounds("10－15K") == (10, 15)
    assert salary_bounds("10–15K·13薪") == (10, 15)
    assert salary_bounds("1.5-2万") == (15, 20)
    assert salary_bounds("200-300元/天") == (0, 0)


def test_live_records_are_deduplicated_and_daily_sent_is_counted():
    record = {
        "job_id": "live-1", "job_url": "https://www.zhipin.com/job_detail/live-1.html",
        "job_title": "数据分析师", "company": "示例科技", "salary": "20-25K", "recruiter": "招聘者",
        "description": "数据分析", "match_score": 90, "status": "sent", "message": "你好",
        "reason": "匹配", "source": "boss_live",
    }
    database.add_record(record)
    assert database.was_contacted("live-1") is True
    assert database.was_contacted("live-2") is False
    assert database.count_sent_today() == 1


def test_auto_reply_requires_explicit_confirmation():
    import asyncio
    from app.schemas import ReplyRunRequest

    try:
        asyncio.run(services.run_auto_replies(ReplyRunRequest(limit=1)))
        assert False, "auto reply should require explicit confirmation"
    except ValueError as exc:
        assert "明确确认" in str(exc)


def test_auto_reply_bridge_error_becomes_audit_record(monkeypatch):
    import asyncio
    from app.schemas import ReplyRunRequest

    class BrokenBrowser:
        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def read_reply_candidates(self, *_args, **_kwargs):
            raise RuntimeError("chat page crashed")

    monkeypatch.setattr(services, "existing_browser_adapter", BrokenBrowser())
    result = asyncio.run(services.run_auto_replies(ReplyRunRequest(limit=1, confirm_external_action=True)))
    records = database.list_records()
    assert result["sent"] == 0
    assert result["records"][0]["status"] == "failed"
    assert "BOSS 聊天读取失败" in result["message"]
    assert records[0]["source"] == "boss_chat"


def test_run_batch_tracks_records_and_control_state():
    import asyncio

    run = database.create_run("dry_run", 2, "queued")
    result = asyncio.run(services.run_campaign(RunRequest(limit=2), run_id=run["id"]))
    detail = services.get_run_detail(run["id"])

    assert result["processed"] == 2
    assert detail["processed"] == 2
    assert len(detail["records"]) == 2
    assert all(record["run_id"] == run["id"] for record in detail["records"])

    paused = services.pause_run(run["id"])
    assert paused["status"] == "pause_requested"
    canceled = services.cancel_run(run["id"])
    assert canceled["status"] in {"cancel_requested", "canceled"}


def test_campaign_safety_rejects_sensitive_templates():
    from app.schemas import CampaignPayload

    campaign = services.get_campaign()
    campaign["greeting_template"] = campaign["greeting_template"] + " 身份证"
    try:
        services.update_campaign(CampaignPayload(**campaign))
        assert False, "sensitive greeting template should be rejected"
    except ValueError as exc:
        assert "blocked safety terms" in str(exc)


def test_manual_review_outcome_pauses_run_and_logs_security_event(monkeypatch):
    import asyncio
    from app.browser_bridge import ContactResult
    from app.schemas import CampaignPayload

    class ManualReviewBrowser:
        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def current_job_page_context(self):
            return {"available": False}

        async def search_jobs(self, *_args, **_kwargs):
            return [{
                "job_id": f"manual-{index}",
                "job_url": f"https://www.zhipin.com/job_detail/manual-{index}.html",
                "job_title": "产品经理",
                "company": f"示例科技{index}",
                "salary": "20-25K",
                "recruiter": "招聘者",
                "description": "产品经理",
                "match_score": 90,
                "reason": "匹配",
            } for index in range(20)]

        async def contact_job(self, *_args, **_kwargs):
            return ContactResult("needs_human", "captcha required")

        async def random_pause(self, *_args, **_kwargs):
            return None

    campaign = services.get_campaign()
    campaign.update({"dry_run": True, "work_start": "00:00", "work_end": "23:59"})
    services.update_campaign(CampaignPayload(**campaign))
    monkeypatch.setattr(services, "existing_browser_adapter", ManualReviewBrowser())

    run = database.create_run("live_workflow", 20, "queued")
    result = asyncio.run(services.run_campaign(RunRequest(limit=20, force_live=True, confirm_external_action=True), run_id=run["id"]))
    detail = services.get_run_detail(run["id"])

    assert result["processed"] == 1
    assert detail["status"] == "paused"
    assert detail["stop_reason"] == "captcha required"
    assert detail["security_events"][0]["action"] == "outreach_manual_review"

def test_run_listing_includes_security_count_and_detail_limits():
    import asyncio

    run = database.create_run("dry_run", 3, "queued")
    asyncio.run(services.run_campaign(RunRequest(limit=3), run_id=run["id"]))
    database.add_security_event(run["id"], "warning", "perf-test", "event 1")
    database.add_security_event(run["id"], "warning", "perf-test", "event 2")

    runs = services.list_runs(5)
    detail = services.get_run_detail(run["id"], records_limit=2, security_limit=1)

    assert runs[0]["security_event_count"] == 2
    assert len(detail["records"]) == 2
    assert len(detail["security_events"]) == 1

def test_live_run_stops_after_twenty_successful_companies(monkeypatch):
    import asyncio
    from app.browser_bridge import ContactResult
    from app.schemas import CampaignPayload

    class TwentyFiveCompanyBrowser:
        def __init__(self):
            self.contacted = []

        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def current_job_page_context(self):
            return {"available": False}

        async def search_jobs_with_diagnostics(self, *_args, **_kwargs):
            jobs = []
            for index in range(25):
                jobs.append({
                    "job_id": f"success-{index}",
                    "job_url": f"https://www.zhipin.com/job_detail/success-{index}.html",
                    "job_title": "产品经理",
                    "company": f"目标公司{index}",
                    "salary": "20-25K",
                    "recruiter": "招聘者",
                    "description": "产品经理",
                    "match_score": 90,
                    "reason": "匹配",
                })
            return {"jobs": jobs, "diagnostics": {"cards_read": len(jobs), "examples": []}}

        async def contact_job(self, job, *_args, **_kwargs):
            self.contacted.append(job["company"])
            return ContactResult("sent", "sent")

        async def random_pause(self, *_args, **_kwargs):
            return None

    campaign = services.get_campaign()
    campaign.update({"dry_run": True, "daily_limit": 30, "work_start": "00:00", "work_end": "23:59"})
    services.update_campaign(CampaignPayload(**campaign))
    browser = TwentyFiveCompanyBrowser()
    monkeypatch.setattr(services, "existing_browser_adapter", browser)

    result = asyncio.run(services.run_campaign(RunRequest(limit=30, force_live=True, confirm_external_action=True), run_id=None))

    assert result["sent"] == 20
    assert result["target_success"] == 20
    assert result["processed"] == 20
    assert len(browser.contacted) == 20
    assert len(services.list_job_snapshots(limit=30)) >= 20


def test_live_run_expands_candidate_pool_before_contacting(monkeypatch):
    import asyncio
    from app.browser_bridge import ContactResult
    from app.schemas import CampaignPayload

    class ExpandingBrowser:
        def __init__(self):
            self.calls = []
            self.contacted = []

        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def current_job_page_context(self):
            return {"available": False}

        async def search_jobs_with_diagnostics(self, campaign, *_args, **_kwargs):
            self.calls.append(list(campaign["keywords"]))
            start = 0 if len(self.calls) == 1 else 8
            count = 8 if len(self.calls) == 1 else 32
            jobs = []
            for index in range(start, start + count):
                jobs.append({
                    "job_id": f"expanded-{index}",
                    "job_url": f"https://www.zhipin.com/job_detail/expanded-{index}.html",
                    "job_title": "数据分析师",
                    "company": f"扩展公司{index}",
                    "salary": "12-18K",
                    "recruiter": "招聘者",
                    "description": "数据分析 SQL Python",
                    "match_score": 90,
                    "reason": "匹配",
                })
            return {"jobs": jobs, "diagnostics": {"cards_read": len(jobs), "examples": [], "searched_keywords": campaign["keywords"]}}

        async def contact_job(self, job, *_args, **_kwargs):
            self.contacted.append(job["company"])
            return ContactResult("sent", "sent")

        async def random_pause(self, *_args, **_kwargs):
            return None

    campaign = services.get_campaign()
    campaign.update({
        "dry_run": True,
        "daily_limit": 30,
        "work_start": "00:00",
        "work_end": "23:59",
        "keywords": ["数据分析师"],
        "salary_min": 9,
        "salary_max": 20,
    })
    services.update_campaign(CampaignPayload(**campaign))
    browser = ExpandingBrowser()
    monkeypatch.setattr(services, "existing_browser_adapter", browser)

    result = asyncio.run(services.run_campaign(RunRequest(limit=30, force_live=True, confirm_external_action=True), run_id=None))

    assert result["sent"] == 20
    assert result["processed"] == 20
    assert len(browser.calls) == 2
    assert len(browser.contacted) == 20


def test_live_run_uses_backup_candidates_when_some_contacts_do_not_send(monkeypatch):
    import asyncio
    from app.browser_bridge import ContactResult
    from app.schemas import CampaignPayload

    class BackupCandidateBrowser:
        def __init__(self):
            self.attempted = []

        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def current_job_page_context(self):
            return {"available": False}

        async def search_jobs_with_diagnostics(self, *_args, **_kwargs):
            jobs = []
            for index in range(45):
                jobs.append({
                    "job_id": f"backup-{index}",
                    "job_url": f"https://www.zhipin.com/job_detail/backup-{index}.html",
                    "job_title": "数据分析师",
                    "company": f"备用公司{index}",
                    "salary": "12-18K",
                    "recruiter": "招聘者",
                    "description": "数据分析 SQL Python",
                    "match_score": 90,
                    "reason": "匹配",
                })
            return {"jobs": jobs, "diagnostics": {"cards_read": len(jobs), "examples": []}}

        async def contact_job(self, job, *_args, **_kwargs):
            self.attempted.append(job["company"])
            if len(self.attempted) in {5, 12, 20}:
                return ContactResult("failed", "button click did not produce a sent state")
            return ContactResult("sent", "sent")

        async def random_pause(self, *_args, **_kwargs):
            return None

    campaign = services.get_campaign()
    campaign.update({
        "dry_run": True,
        "daily_limit": 30,
        "work_start": "00:00",
        "work_end": "23:59",
        "keywords": ["数据分析师"],
        "salary_min": 9,
        "salary_max": 20,
    })
    services.update_campaign(CampaignPayload(**campaign))
    browser = BackupCandidateBrowser()
    monkeypatch.setattr(services, "existing_browser_adapter", browser)

    result = asyncio.run(services.run_campaign(RunRequest(limit=30, force_live=True, confirm_external_action=True), run_id=None))

    assert result["sent"] == 20
    assert result["processed"] == 23
    assert len(browser.attempted) == 23


def test_live_run_deduplicates_companies_before_contact(monkeypatch):
    import asyncio
    from app.browser_bridge import ContactResult
    from app.schemas import CampaignPayload

    class DuplicateCompanyBrowser:
        def __init__(self):
            self.contacted = []

        async def status(self):
            return {"state": "ready", "message": "ready"}

        async def current_job_page_context(self):
            return {"available": False}

        async def search_jobs_with_diagnostics(self, *_args, **_kwargs):
            jobs = []
            for index in range(25):
                company = "重复公司" if index < 2 else f"唯一公司{index}"
                jobs.append({
                    "job_id": f"company-{index}",
                    "job_url": f"https://www.zhipin.com/job_detail/company-{index}.html",
                    "job_title": "产品经理",
                    "company": company,
                    "salary": "20-25K",
                    "recruiter": "招聘者",
                    "description": "产品经理",
                    "match_score": 90,
                    "reason": "匹配",
                })
            return {"jobs": jobs, "diagnostics": {"cards_read": len(jobs), "examples": []}}

        async def contact_job(self, job, *_args, **_kwargs):
            self.contacted.append(job["company"])
            return ContactResult("sent", "sent")

        async def random_pause(self, *_args, **_kwargs):
            return None

    campaign = services.get_campaign()
    campaign.update({"dry_run": True, "daily_limit": 30, "work_start": "00:00", "work_end": "23:59"})
    services.update_campaign(CampaignPayload(**campaign))
    browser = DuplicateCompanyBrowser()
    monkeypatch.setattr(services, "existing_browser_adapter", browser)

    result = asyncio.run(services.run_campaign(RunRequest(limit=30, force_live=True, confirm_external_action=True), run_id=None))

    assert result["sent"] == 20
    assert browser.contacted.count("重复公司") == 1
    assert len(browser.contacted) == 20
