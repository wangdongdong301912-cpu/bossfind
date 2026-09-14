import asyncio
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from typing import Any

from app import database
from app.boss_adapter import adapter
from app.browser_bridge import ContactResult, existing_browser_adapter, _is_transient_cdp_error
from app.schemas import CampaignPayload, ReplyRunRequest, RunRequest


DEMO_JOBS = [
    {"id": "demo-1", "job_title": "AI 产品经理", "company": "云杉智能（演示）", "salary": "18-30K", "recruiter": "林女士", "tags": ["3-5年", "SaaS", "AIGC"], "base_score": 91},
    {"id": "demo-2", "job_title": "高级产品经理", "company": "禾城科技（演示）", "salary": "20-28K", "recruiter": "周先生", "tags": ["企业服务", "B端", "双休"], "base_score": 88},
    {"id": "demo-3", "job_title": "增长产品经理", "company": "潮汐网络（演示）", "salary": "15-25K", "recruiter": "陈女士", "tags": ["增长", "数据分析", "电商"], "base_score": 84},
    {"id": "demo-4", "job_title": "平台产品经理", "company": "经纬数字（演示）", "salary": "16-26K", "recruiter": "顾先生", "tags": ["中台", "平台", "3-5年"], "base_score": 80},
    {"id": "demo-5", "job_title": "产品运营", "company": "南湖互联（演示）", "salary": "12-20K", "recruiter": "沈女士", "tags": ["用户运营", "内容", "互联网"], "base_score": 65},
    {"id": "demo-6", "job_title": "项目经理", "company": "嘉创软件（演示）", "salary": "14-22K", "recruiter": "许先生", "tags": ["交付", "制造业", "软件"], "base_score": 58},
]

_ACTIVE_TASKS: dict[int, asyncio.Task] = {}
_ACTIVE_RADAR_TASK: asyncio.Task | None = None
_RADAR_PAUSE_REQUESTED = False


REVIEW_ONLY_WORDS = ["身份证", "身份证号", "手机号", "微信", "住址", "面试时间", "确认入职", "保证", "承诺"]
SENSITIVE_TEMPLATE_WORDS = ["身份证", "身份证号", "银行卡", "验证码", "密码", "住址", "详细地址", "微信", "手机号"]
COMMITMENT_TEMPLATE_WORDS = ["保证录用", "保证入职", "一定入职", "薪资承诺", "接受任何薪资"]
SAFETY_STOP_STATUSES = {"needs_human", "uncertain", "failed"}
TARGET_CANDIDATE_POOL = 300
ROLE_EXPANSIONS = {
    "数据": [
        "大数据开发",
        "数据开发工程师",
        "数据仓库",
        "数仓开发",
        "ETL工程师",
        "数据治理",
        "数据平台开发",
        "数据分析师",
        "数据分析",
        "商业分析",
        "SQL数据分析",
        "Python数据分析",
        "电商数据分析",
        "经营分析",
        "数据运营",
        "AI数据分析",
        "数据产品",
    ],
    "bi": ["BI工程师", "BI开发", "BI数据分析", "数据分析师", "商业分析", "SQL数据分析"],
    "产品": ["产品经理", "AI产品经理", "数据产品经理", "平台产品经理", "增长产品经理", "产品运营"],
}


def _contains_any(text: str, words: list[str]) -> list[str]:
    normalized = text or ""
    return [word for word in words if word and word in normalized]


def _campaign_safety_issues(campaign: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    greeting_hits = _contains_any(campaign.get("greeting_template", ""), SENSITIVE_TEMPLATE_WORDS + COMMITMENT_TEMPLATE_WORDS)
    if greeting_hits:
        issues.append("Greeting template contains blocked safety terms: " + ", ".join(greeting_hits))
    for rule in campaign.get("answer_rules", []):
        answer_hits = _contains_any(str(rule.get("answer", "")), SENSITIVE_TEMPLATE_WORDS + COMMITMENT_TEMPLATE_WORDS)
        if answer_hits:
            issues.append(f"Answer rule '{rule.get('question', 'untitled')}' contains blocked safety terms: " + ", ".join(answer_hits))
    return issues


def _preflight_safety_warnings(campaign: dict[str, Any], request: RunRequest) -> list[str]:
    warnings: list[str] = []
    if campaign.get("daily_limit", 0) > 30:
        warnings.append("Daily limit is above the conservative safety threshold of 30.")
    if campaign.get("interval_min", 0) < 30:
        warnings.append("Minimum interval is below the conservative safety threshold of 30 seconds.")
    if request.limit > campaign.get("daily_limit", request.limit):
        warnings.append("Requested limit exceeds today's configured limit and will be capped.")
    return warnings


def _log_security_event(run_id: int | None, severity: str, action: str, message: str) -> None:
    try:
        database.add_security_event(run_id, severity, action, message)
    except Exception:
        pass


def _dedupe_text(items: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        value = str(item or "").strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            cleaned.append(value)
    return cleaned


def _expanded_keywords(base_keywords: list[str]) -> list[str]:
    expanded = list(base_keywords)
    normalized = " ".join(base_keywords).lower()
    for trigger, additions in ROLE_EXPANSIONS.items():
        if trigger in normalized:
            expanded.extend(additions)
    return _dedupe_text(expanded)


def _search_attempts(campaign: dict[str, Any], target_success: int) -> list[dict[str, Any]]:
    expanded_keywords = _expanded_keywords(campaign.get("keywords", []))
    broad_campaign = deepcopy(campaign)
    broad_campaign["keywords"] = expanded_keywords
    broad_campaign["max_pages_per_keyword"] = 25

    relaxed_campaign = deepcopy(broad_campaign)
    relaxed_campaign["match_threshold"] = 60

    strict_campaign = deepcopy(campaign)
    strict_campaign["max_pages_per_keyword"] = 25
    strict_campaign["match_threshold"] = 70

    return [
        {"label": "严格策略", "campaign": strict_campaign, "max_results": max(TARGET_CANDIDATE_POOL, target_success * 10)},
        {"label": "扩展关键词", "campaign": broad_campaign, "max_results": max(TARGET_CANDIDATE_POOL, target_success * 12)},
        {"label": "补量模式", "campaign": relaxed_campaign, "max_results": max(TARGET_CANDIDATE_POOL, target_success * 15)},
    ]


def _merge_diagnostics(current: dict[str, Any], incoming: dict[str, Any], label: str) -> dict[str, Any]:
    merged = deepcopy(current)
    for key in ("cards_read", "rejected_salary", "rejected_city", "rejected_excluded", "rejected_score", "duplicates"):
        merged[key] = int(merged.get(key) or 0) + int(incoming.get(key) or 0)
    searched = list(merged.get("searched_keywords") or [])
    searched.extend(incoming.get("searched_keywords") or [])
    merged["searched_keywords"] = _dedupe_text(searched)
    examples = list(merged.get("examples") or [])
    examples.extend(incoming.get("examples") or [])
    merged["examples"] = examples[:10]
    attempts = list(merged.get("attempts") or [])
    attempts.append({
        "stage": label,
        "cards_read": incoming.get("cards_read", 0),
        "accepted": incoming.get("accepted", 0),
        "searched_keywords": incoming.get("searched_keywords", []),
    })
    merged["attempts"] = attempts
    return merged


def _save_job_snapshots(jobs: list[dict[str, Any]]) -> None:
    for job in jobs:
        try:
            database.upsert_job_snapshot(job)
        except Exception:
            continue


def _pause_run_for_manual_review(run_id: int | None, action: str, message: str) -> None:
    _log_security_event(run_id, "warning", action, message)
    if run_id is not None:
        database.update_run(run_id, status="paused", current_step="manual review required", stop_reason=message[:240])


def _run_terminal_status(run_id: int | None) -> bool:
    return _control_status(run_id) in {"paused", "canceled", "failed"}

def get_campaign() -> dict[str, Any]:
    return database.get_campaign()


def update_campaign(payload: CampaignPayload) -> dict[str, Any]:
    campaign = payload.model_dump()
    issues = _campaign_safety_issues(campaign)
    if issues:
        raise ValueError("; ".join(issues))
    return database.save_campaign(campaign)


def _salary_bounds(value: str) -> tuple[int, int]:
    clean = value.upper().replace("K", "").split("·", 1)[0]
    try:
        low, high = clean.split("-", 1)
        return int(low), int(high)
    except (ValueError, TypeError):
        return 0, 0


def preview_jobs() -> list[dict[str, Any]]:
    campaign = get_campaign()
    wanted = [item.lower() for item in campaign["keywords"]]
    excluded = [item.lower() for item in campaign["excluded_keywords"]]
    results = []
    for job in DEMO_JOBS:
        title = job["job_title"].lower()
        if any(word in title for word in excluded):
            continue
        low, high = _salary_bounds(job["salary"])
        title_match = any(word in title or title in word for word in wanted)
        salary_overlap = max(low, campaign["salary_min"]) <= min(high, campaign["salary_max"])
        score = job["base_score"] + (5 if title_match else 0) + (3 if salary_overlap else -8)
        reasons = []
        if title_match:
            reasons.append("岗位关键词匹配")
        if salary_overlap:
            reasons.append("薪资区间重合")
        if not reasons:
            reasons.append("相关岗位，建议人工复核")
        results.append({**job, "match_score": min(99, max(0, score)), "reason": "；".join(reasons), "source": "demo"})
    return sorted(results, key=lambda item: item["match_score"], reverse=True)


def render_greeting(job: dict[str, Any], campaign: dict[str, Any]) -> str:
    return (
        campaign["greeting_template"]
        .replace("{job_title}", job["job_title"])
        .replace("{company}", job["company"])
        .replace("{experience}", campaign["experience"])
    )


def _diagnostic_message(campaign: dict[str, Any], diagnostics: dict[str, Any], deduped: int = 0) -> str:
    parts = [
        f"没有找到新的高匹配岗位。本次策略：{campaign['city']} / {','.join(campaign['keywords'])} / {campaign['salary_min']}-{campaign['salary_max']}K / {campaign['experience']}。",
        f"已读取 {diagnostics.get('cards_read', 0)} 张岗位卡片",
    ]
    rejected = []
    if diagnostics.get("rejected_salary"):
        rejected.append(f"薪资过滤 {diagnostics['rejected_salary']} 个")
    if diagnostics.get("rejected_city"):
        rejected.append(f"城市过滤 {diagnostics['rejected_city']} 个")
    if diagnostics.get("rejected_excluded"):
        rejected.append(f"排除词过滤 {diagnostics['rejected_excluded']} 个")
    if diagnostics.get("rejected_score"):
        rejected.append(f"匹配分不足 {diagnostics['rejected_score']} 个")
    if deduped:
        rejected.append(f"已沟通过去重 {deduped} 个")
    if rejected:
        parts.append("；".join(rejected))
    examples = diagnostics.get("examples") or []
    if examples:
        parts.append("样例：" + "；".join(examples[:3]))
    return "；".join(parts) + "；本次没有向 BOSS 发起真实沟通。"


async def _campaign_for_current_boss_page(campaign: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Temporarily align live execution with the currently visible BOSS search page."""
    live_campaign = deepcopy(campaign)
    try:
        context = await existing_browser_adapter.current_job_page_context()
    except Exception:
        return live_campaign, ""
    if not context.get("available") or not context.get("card_count"):
        return live_campaign, ""
    changes = []
    city = str(context.get("city") or "").strip()
    keyword = str(context.get("keyword") or "").strip()
    if city and city != live_campaign.get("city"):
        changes.append(f"城市由 {live_campaign.get('city')} 临时切换为 {city}")
        live_campaign["city"] = city
    if keyword and not any(keyword.lower() == str(item).lower() for item in live_campaign.get("keywords", [])):
        changes.append(f"优先搜索词临时加入 {keyword}")
        live_campaign["keywords"] = [keyword, *live_campaign.get("keywords", [])]
    if changes:
        return live_campaign, "已按当前 Chrome BOSS 页面同步：" + "；".join(changes)
    return live_campaign, ""


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    return database.list_runs(limit)


def get_run_detail(run_id: int, records_limit: int = 120, security_limit: int = 50) -> dict[str, Any]:
    run = database.get_run(run_id)
    run["records"] = database.list_records_for_run(run_id, min(max(records_limit, 1), 200))
    run["security_events"] = database.list_security_events(run_id, min(max(security_limit, 1), 100))
    run["active"] = run_id in _ACTIVE_TASKS and not _ACTIVE_TASKS[run_id].done()
    return run


def list_job_snapshots(limit: int = 100, priority: str | None = None) -> list[dict[str, Any]]:
    return database.list_job_snapshots(min(max(limit, 1), 300), priority)


def _clean_selected_job_ids(job_ids: list[str] | None) -> list[str]:
    return list(dict.fromkeys(str(job_id).strip() for job_id in (job_ids or []) if str(job_id).strip()))


def _selected_snapshot_candidates(job_ids: list[str]) -> list[dict[str, Any]]:
    selected = _clean_selected_job_ids(job_ids)
    if not selected:
        return []
    snapshots_by_id = {job["job_id"]: job for job in list_job_snapshots(limit=300)}
    candidates: list[dict[str, Any]] = []
    missing: list[str] = []
    for job_id in selected:
        snapshot = snapshots_by_id.get(job_id)
        if not snapshot:
            missing.append(job_id)
            continue
        job = dict(snapshot)
        job["reason"] = "用户在岗位雷达中确认选择"
        job["source"] = "radar_selected"
        job["job_url"] = job.get("job_url") or ""
        job["salary"] = job.get("salary") or ""
        job["recruiter"] = job.get("recruiter") or ""
        job["description"] = job.get("description") or ""
        candidates.append(job)
    if missing:
        raise ValueError(f"以下岗位快照不存在或已过期：{', '.join(missing[:5])}")
    return candidates


def pause_run(run_id: int) -> dict[str, Any]:
    return database.mark_run_control(run_id, "pause")


def cancel_run(run_id: int) -> dict[str, Any]:
    task = _ACTIVE_TASKS.get(run_id)
    run = database.mark_run_control(run_id, "cancel")
    if task and task.done():
        database.update_run(run_id, status="canceled", current_step="canceled", stop_reason="user requested cancel")
    return database.get_run(run_id)


def _control_status(run_id: int | None) -> str:
    if run_id is None:
        return ""
    try:
        return database.get_run_status(run_id)
    except ValueError:
        return ""


def _control_requested(run_id: int | None) -> str:
    status = _control_status(run_id)
    if status in {"pause_requested", "cancel_requested"}:
        return status
    return ""


def _finalize_controlled_run(run_id: int, default_status: str, step: str, reason: str = "") -> dict[str, Any]:
    status = database.get_run_status(run_id)
    if status == "pause_requested":
        return database.update_run(run_id, status="paused", current_step="paused", stop_reason="user requested pause")
    if status == "cancel_requested":
        return database.update_run(run_id, status="canceled", current_step="canceled", stop_reason="user requested cancel")
    return database.update_run(run_id, status=default_status, current_step=step, stop_reason=reason)


async def start_live_workflow(request: RunRequest) -> dict[str, Any]:
    campaign = get_campaign()
    if not request.confirm_external_action:
        _log_security_event(None, "warning", "confirmation_missing", "Live outreach was blocked because external action confirmation was missing.")
        raise ValueError("Live workflow requires explicit external-action confirmation.")
    issues = _campaign_safety_issues(campaign)
    if issues:
        raise ValueError("; ".join(issues))
    selected_count = len(_clean_selected_job_ids(request.selected_job_ids))
    total_limit = selected_count if selected_count else max(20, request.min_successful_contacts)
    mode = "radar_selected_workflow" if selected_count else "live_workflow"
    run = database.create_run(mode, total_limit, "queued")
    _log_security_event(run["id"], "info", "preflight", "Safety preflight passed.")
    for warning in _preflight_safety_warnings(campaign, request):
        _log_security_event(run["id"], "warning", "preflight", warning)
    task = asyncio.create_task(_run_live_workflow(run["id"], request))
    _ACTIVE_TASKS[run["id"]] = task
    return get_run_detail(run["id"])


async def _run_live_workflow(run_id: int, request: RunRequest) -> None:
    try:
        database.update_run(run_id, status="running", current_step="running outreach")
        await run_campaign(request, run_id=run_id)
        if _control_requested(run_id):
            _finalize_controlled_run(run_id, "completed", "completed")
            return
        if _run_terminal_status(run_id):
            return
        database.update_run(run_id, current_step="running auto replies")
        await run_auto_replies(ReplyRunRequest(limit=5, confirm_external_action=True), run_id=run_id)
        if _run_terminal_status(run_id):
            return
        _finalize_controlled_run(run_id, "completed", "completed")
    except Exception as exc:
        database.update_run(
            run_id,
            status="failed",
            current_step="failed",
            stop_reason=(str(exc).splitlines()[0] or type(exc).__name__)[:240],
        )
        _log_security_event(run_id, "error", "workflow_failed", (str(exc).splitlines()[0] or type(exc).__name__)[:240])
    finally:
        database.refresh_run_progress(run_id)
        _ACTIVE_TASKS.pop(run_id, None)


async def preview_live_jobs(limit: int = 20) -> list[dict[str, Any]]:
    campaign = get_campaign()
    status = await existing_browser_adapter.status()
    if status["state"] != "ready":
        _log_security_event(None, "warning", "browser_not_ready", status["message"])
        raise ValueError(status["message"])
    jobs = await existing_browser_adapter.search_jobs(campaign, max_results=limit)
    _save_job_snapshots(jobs)
    return jobs


async def collect_radar_jobs(limit: int = 80) -> dict[str, Any]:
    global _ACTIVE_RADAR_TASK, _RADAR_PAUSE_REQUESTED
    current_task = asyncio.current_task()
    if _ACTIVE_RADAR_TASK is not None and not _ACTIVE_RADAR_TASK.done() and _ACTIVE_RADAR_TASK is not current_task:
        raise ValueError("岗位采集正在进行中，请先暂停当前采集后再重试。")
    _ACTIVE_RADAR_TASK = current_task
    _RADAR_PAUSE_REQUESTED = False
    campaign = get_campaign()
    max_results = min(max(limit, 1), TARGET_CANDIDATE_POOL)
    try:
        status = await existing_browser_adapter.status()
        if status["state"] != "ready":
            _log_security_event(None, "warning", "browser_not_ready", status["message"])
            raise ValueError(status["message"])
        if hasattr(existing_browser_adapter, "search_jobs_with_diagnostics"):
            result = await existing_browser_adapter.search_jobs_with_diagnostics(campaign, max_results=max_results)
            jobs = result.get("jobs", [])
            diagnostics = result.get("diagnostics", {})
        else:
            jobs = await existing_browser_adapter.search_jobs(campaign, max_results=max_results)
            diagnostics = {"cards_read": len(jobs), "examples": []}
        _save_job_snapshots(jobs)
        snapshots = list_job_snapshots(limit=max_results)
        return {
            "mode": "radar_collect",
            "status": "completed",
            "collected": len(jobs),
            "snapshots": snapshots,
            "diagnostics": diagnostics,
            "message": f"已采集并排序 {len(jobs)} 个岗位，不会自动投递。",
        }
    except asyncio.CancelledError:
        snapshots = list_job_snapshots(limit=max_results)
        return {
            "mode": "radar_collect",
            "status": "paused",
            "collected": 0,
            "snapshots": snapshots,
            "diagnostics": {"paused": True},
            "message": "岗位采集已暂停，已保留当前岗位快照。",
        }
    finally:
        if _ACTIVE_RADAR_TASK is current_task:
            _ACTIVE_RADAR_TASK = None
            _RADAR_PAUSE_REQUESTED = False


def pause_radar_collection() -> dict[str, Any]:
    global _RADAR_PAUSE_REQUESTED
    task = _ACTIVE_RADAR_TASK
    if task is None or task.done():
        return {"status": "idle", "message": "当前没有正在进行的岗位采集。"}
    _RADAR_PAUSE_REQUESTED = True
    task.cancel()
    return {"status": "pause_requested", "message": "已请求暂停岗位采集。"}


def _inside_work_window(campaign: dict[str, Any]) -> bool:
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
    start, end = campaign["work_start"], campaign["work_end"]
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


async def run_campaign(request: RunRequest, run_id: int | None = None) -> dict[str, Any]:
    campaign = get_campaign()
    if (campaign["dry_run"] or request.force_dry) and not request.force_live:
        jobs = preview_jobs()[: request.limit]
        records = []
        for job in jobs:
            record = {
                "job_title": job["job_title"], "company": job["company"], "salary": job["salary"],
                "recruiter": job["recruiter"], "match_score": job["match_score"], "status": "dry_run",
                "message": render_greeting(job, campaign), "reason": "演练模式：已生成消息，未访问或发送到 Boss 直聘", "source": "demo",
                "run_id": run_id,
            }
            record["id"] = database.add_record(record)
            records.append(record)
            if run_id is not None:
                database.refresh_run_progress(run_id, f"dry run {len(records)}")
        return {"mode": "dry_run", "processed": len(records), "sent": 0, "records": records, "message": "演练完成，未产生站外操作。"}

    if not request.confirm_external_action:
        _log_security_event(run_id, "warning", "confirmation_missing", "Live outreach was blocked because external action confirmation was missing.")
        raise ValueError("真实投递前必须确认将通过当前 BOSS 账号发起沟通")
    if not _inside_work_window(campaign):
        _log_security_event(run_id, "warning", "work_window", "Live outreach was blocked outside the configured work window.")
        raise ValueError(f"当前不在允许执行时段 {campaign['work_start']}-{campaign['work_end']}")

    if run_id is not None:
        database.update_run(run_id, current_step="checking browser")
    status = await existing_browser_adapter.status()
    if status["state"] != "ready":
        _log_security_event(run_id, "warning", "browser_not_ready", status["message"])
        raise ValueError(status["message"])
    if _control_requested(run_id):
        return {"mode": "live", "processed": 0, "sent": 0, "records": [], "message": "Run was paused or canceled."}
    campaign, sync_note = await _campaign_for_current_boss_page(campaign)
    sent_today = database.count_sent_today()
    remaining = max(0, campaign["daily_limit"] - sent_today)
    selected_job_ids = _clean_selected_job_ids(request.selected_job_ids)
    target_success = len(selected_job_ids) if selected_job_ids else max(20, request.min_successful_contacts)
    if remaining < target_success:
        message = f"今日剩余额度不足：还剩 {remaining} 个，至少需要成功投递 {target_success} 个。请把每日上限调到不低于 20，或明天再执行。"
        _log_security_event(run_id, "warning", "quota_below_target", message)
        raise ValueError(message)

    if run_id is not None:
        step = "loading selected radar jobs" if selected_job_ids else f"building candidate pool for {target_success} successful contacts"
        database.update_run(run_id, total_limit=target_success, current_step=step)

    desired_candidate_pool = max(target_success * 2, target_success + 10)
    candidates_by_job: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, Any] = {"cards_read": 0, "examples": [], "searched_keywords": [], "attempts": []}
    deduped = 0
    fresh_candidates = []
    seen_companies: set[str] = set()
    if selected_job_ids:
        selected_candidates = _selected_snapshot_candidates(selected_job_ids)
        for job in selected_candidates:
            job_id = str(job.get("job_id") or "").strip()
            company = str(job.get("company", "")).strip()
            if not job_id or not company:
                deduped += 1
                continue
            if database.was_contacted(job_id) or database.was_company_contacted(company):
                deduped += 1
                continue
            fresh_candidates.append(job)
        diagnostics.update({"manual_selected": len(selected_job_ids), "fresh_candidates": len(fresh_candidates), "deduped_existing_or_company": deduped})
    else:
        for attempt in _search_attempts(campaign, target_success):
            attempt_label = attempt["label"]
            attempt_campaign = attempt["campaign"]
            if run_id is not None:
                database.update_run(run_id, current_step=f"searching jobs: {attempt_label} ({len(fresh_candidates)}/{desired_candidate_pool} candidates)")
            try:
                if hasattr(existing_browser_adapter, "search_jobs_with_diagnostics"):
                    search_result = await existing_browser_adapter.search_jobs_with_diagnostics(attempt_campaign, max_results=attempt["max_results"])
                    attempt_candidates = search_result["jobs"]
                    attempt_diagnostics = search_result.get("diagnostics", {})
                else:
                    attempt_candidates = await existing_browser_adapter.search_jobs(attempt_campaign, max_results=attempt["max_results"])
                    attempt_diagnostics = {"cards_read": len(attempt_candidates), "examples": []}
            except Exception as exc:
                if not _is_transient_cdp_error(exc):
                    raise
                message = f"搜索阶段 Chrome 瞬时断开：{str(exc).splitlines()[0][:160]}；已保留 {len(fresh_candidates)} 个候选继续处理。"
                _log_security_event(run_id, "warning", "search_transient_disconnect", message)
                if len(fresh_candidates) >= target_success:
                    break
                continue
            _save_job_snapshots(attempt_candidates)
            attempt_diagnostics["accepted"] = len(attempt_candidates)
            diagnostics = _merge_diagnostics(diagnostics, attempt_diagnostics, attempt_label)
            for job in attempt_candidates:
                job_id = str(job.get("job_id") or "").strip()
                company = str(job.get("company", "")).strip()
                company_key = company.lower()
                if not job_id or not company:
                    deduped += 1
                    continue
                if job_id in candidates_by_job:
                    deduped += 1
                    continue
                if database.was_contacted(job_id) or database.was_company_contacted(company) or company_key in seen_companies:
                    deduped += 1
                    continue
                candidates_by_job[job_id] = job
                seen_companies.add(company_key)
                fresh_candidates.append(job)
                if len(fresh_candidates) >= desired_candidate_pool:
                    break
            if len(fresh_candidates) >= desired_candidate_pool:
                break
        diagnostics["fresh_candidates"] = len(fresh_candidates)
        diagnostics["deduped_existing_or_company"] = deduped
    candidates = fresh_candidates
    records: list[dict[str, Any]] = []
    sent = 0
    if not selected_job_ids and len(candidates) < target_success:
        message = _diagnostic_message(campaign, diagnostics, deduped)
        shortfall_message = f"候选池不足：三阶段搜索后只有 {len(candidates)} 个符合条件且未沟通过的公司，未达到 {target_success} 个；本批未开始真实投递。"
        message = f"{message} {shortfall_message}"
        if sync_note:
            message = f"{sync_note}。{message}"
        record = {
            "job_title": "真实投递未执行",
            "company": "BOSS 直聘",
            "salary": f"{campaign['salary_min']}-{campaign['salary_max']}K",
            "recruiter": "",
            "description": str(diagnostics)[:12000],
            "match_score": 0,
            "status": "failed",
            "message": "未发送",
            "reason": message,
            "source": "boss_live",
            "run_id": run_id,
        }
        record["id"] = database.add_record(record)
        records.append(record)
        if run_id is not None:
            database.refresh_run_progress(run_id, "candidate pool below target")
            _pause_run_for_manual_review(run_id, "no_candidates" if not candidates else "target_not_reached", shortfall_message)
        return {"mode": "live", "processed": len(records), "sent": 0, "target_success": target_success, "records": records, "message": message}

    for index, job in enumerate(candidates):
        if sent >= target_success:
            break
        control = _control_requested(run_id)
        if control:
            break
        if run_id is not None:
            database.update_run(run_id, current_step=f"contacting {job.get('job_title', 'job')} ({sent}/{target_success} sent)")
        greeting = render_greeting(job, campaign)
        try:
            outcome = await existing_browser_adapter.contact_job(job, greeting)
        except Exception as exc:
            if not _is_transient_cdp_error(exc):
                raise
            outcome = ContactResult("failed", f"browser transient disconnect; skipped this job: {str(exc).splitlines()[0][:160]}")
        record = {
            "job_id": job["job_id"], "job_url": job["job_url"], "job_title": job["job_title"],
            "company": job["company"], "salary": job["salary"], "recruiter": job.get("recruiter", ""),
            "description": job.get("description", ""), "match_score": job["match_score"],
            "status": outcome.status, "message": greeting, "reason": f"{job.get('reason', '岗位雷达匹配')}；{outcome.message}",
            "source": "boss_live",
            "run_id": run_id,
        }
        record["id"] = database.add_record(record)
        records.append(record)
        if outcome.status == "sent":
            sent += 1
        if run_id is not None:
            database.refresh_run_progress(run_id, f"sent {sent}/{target_success}; processed {len(records)} outreach records")
        if outcome.status in {"needs_human", "uncertain"}:
            _pause_run_for_manual_review(run_id, "outreach_manual_review", outcome.message)
            break
        if sent >= target_success:
            break
        if index < len(candidates) - 1:
            await existing_browser_adapter.random_pause(campaign["interval_min"], campaign["interval_max"])

    message = f"本次处理 {len(records)} 个岗位，成功发起沟通 {sent}/{target_success} 个。"
    if sync_note:
        message = f"{sync_note}。{message}"
    if not candidates:
        message = _diagnostic_message(campaign, diagnostics, deduped)
        if sync_note:
            message = f"{sync_note}。{message}"
        record = {
            "job_title": "真实投递未执行",
            "company": "BOSS 直聘",
            "salary": f"{campaign['salary_min']}-{campaign['salary_max']}K",
            "recruiter": "",
            "description": str(diagnostics)[:12000],
            "match_score": 0,
            "status": "failed",
            "message": "未发送",
            "reason": message,
            "source": "boss_live", "run_id": run_id,
        }
        record["id"] = database.add_record(record)
        records.append(record)
        if run_id is not None:
            database.refresh_run_progress(run_id, "no candidates")
            _pause_run_for_manual_review(run_id, "no_candidates", message)
    elif sent < target_success and not _run_terminal_status(run_id):
        shortfall_message = f"符合条件且未沟通过的候选公司不足，已成功 {sent}/{target_success} 个；未达到至少 20 个成功投递目标，批次暂停。"
        message = f"{message} {shortfall_message}"
        _pause_run_for_manual_review(run_id, "target_not_reached", shortfall_message)
    return {"mode": "live", "processed": len(records), "sent": sent, "target_success": target_success, "records": records, "message": message}


def simulate_answer(message: str) -> dict[str, Any]:
    if any(word in message for word in REVIEW_ONLY_WORDS):
        return {"matched": False, "answer": None, "confidence": 0, "action": "needs_review", "message": "问题涉及隐私、面试或承诺，需要人工处理。"}
    campaign = get_campaign()
    normalized = message.lower()
    for rule in campaign["answer_rules"]:
        if rule.get("enabled") and any(str(word).lower() in normalized for word in rule.get("keywords", [])):
            return {"matched": True, "rule_id": rule["id"], "question": rule["question"], "answer": rule["answer"], "confidence": 0.92, "action": "suggest_reply"}
    return {"matched": False, "answer": None, "confidence": 0, "action": "needs_review", "message": "未命中规则，建议转人工处理。"}


async def run_auto_replies(request: ReplyRunRequest, run_id: int | None = None) -> dict[str, Any]:
    if not request.confirm_external_action:
        raise ValueError("自动回复会向 BOSS 聊天发送消息，执行前必须明确确认")

    campaign = get_campaign()
    if run_id is not None:
        database.update_run(run_id, current_step="checking chat")
    status = await existing_browser_adapter.status()
    if status["state"] != "ready":
        raise ValueError(status["message"])

    records: list[dict[str, Any]] = []
    sent = 0
    try:
        candidates = await existing_browser_adapter.read_reply_candidates(limit=request.limit)
    except Exception as exc:
        message = f"BOSS 聊天读取失败：{str(exc).splitlines()[0][:200] or type(exc).__name__}"
        record = {
            "job_title": "BOSS 聊天自动回复",
            "company": "BOSS 直聘",
            "salary": "",
            "recruiter": "",
            "description": "",
            "match_score": 0,
            "status": "failed",
            "message": "未发送",
            "reason": message,
            "source": "boss_chat", "run_id": run_id,
        }
        record["id"] = database.add_record(record)
        records.append(record)
        if run_id is not None:
            database.refresh_run_progress(run_id, "chat read failed")
        return {"mode": "auto_reply", "processed": 1, "sent": 0, "records": records, "message": message}

    for candidate in candidates:
        control = _control_requested(run_id)
        if control:
            break
        if run_id is not None:
            database.update_run(run_id, current_step=f"replying {candidate.get('title', 'conversation')}")
        decision = simulate_answer(candidate.get("last_message") or candidate.get("raw") or "")
        if decision.get("action") != "suggest_reply" or not decision.get("answer"):
            record = {
                "job_title": "BOSS 聊天自动回复",
                "company": candidate.get("title", "未知会话"),
                "salary": "",
                "recruiter": "",
                "description": candidate.get("raw", ""),
                "match_score": int(float(decision.get("confidence", 0)) * 100),
                "status": "needs_human",
                "message": decision.get("message", "未命中自动回复规则"),
                "reason": f"待人工处理：{candidate.get('last_message', '')}",
                "source": "boss_chat",
                "run_id": run_id,
            }
            record["id"] = database.add_record(record)
            records.append(record)
            if run_id is not None:
                database.refresh_run_progress(run_id, f"processed {len(records)} reply records")
            incoming = candidate.get("last_message") or candidate.get("raw") or ""
            if _contains_any(incoming, REVIEW_ONLY_WORDS):
                _pause_run_for_manual_review(run_id, "reply_sensitive_question", decision.get("message", "Sensitive reply needs manual review."))
                break
            continue

        try:
            outcome = await existing_browser_adapter.send_reply_to_candidate(int(candidate["index"]), str(decision["answer"]))
        except Exception as exc:
            from app.browser_bridge import ContactResult
            outcome = ContactResult("failed", f"BOSS 聊天发送前检查失败：{str(exc).splitlines()[0][:200] or type(exc).__name__}")
        record = {
            "job_title": "BOSS 聊天自动回复",
            "company": candidate.get("title", "未知会话"),
            "salary": "",
            "recruiter": "",
            "description": candidate.get("raw", ""),
            "match_score": int(float(decision.get("confidence", 0)) * 100),
            "status": "auto_replied" if outcome.status == "sent" else outcome.status,
            "message": decision["answer"],
            "reason": f"{outcome.message}；触发规则：{decision.get('question', '未命名规则')}",
            "source": "boss_chat",
            "run_id": run_id,
        }
        record["id"] = database.add_record(record)
        records.append(record)
        if run_id is not None:
            database.refresh_run_progress(run_id, f"processed {len(records)} reply records")
        if outcome.status == "sent":
            sent += 1
        if outcome.status in {"needs_human", "uncertain", "failed"}:
            _pause_run_for_manual_review(run_id, "reply_manual_review", outcome.message)
            break
        await existing_browser_adapter.random_pause(
            max(15, min(campaign["interval_min"], campaign["interval_max"])),
            max(campaign["interval_min"], campaign["interval_max"]),
        )

    if not candidates:
        message = "没有读取到可自动回复的 BOSS 会话；请确认 Chrome 已停留在 BOSS 聊天页或账号已登录。"
    else:
        message = f"本次检查 {len(records)} 个会话，自动回复 {sent} 条。"
    return {"mode": "auto_reply", "processed": len(records), "sent": sent, "records": records, "message": message}


def dashboard() -> dict[str, Any]:
    campaign = get_campaign()
    sent_today = database.count_sent_today()
    return {
        "campaign": campaign,
        "stats": {"matched": len(preview_jobs()), "processed": database.count_records(), "sent": sent_today, "daily_limit": campaign["daily_limit"]},
        "recent_records": database.list_records(8),
        "recent_runs": database.list_runs(5),
        "live_adapter": adapter.live_status(),
    }
