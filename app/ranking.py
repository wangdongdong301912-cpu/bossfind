import re
from typing import Any


WEEKEND_PATTERNS = [
    ("双休", ("双休", "周末双休", "做五休二", "大小周否")),
    ("大小周", ("大小周", "单双休")),
    ("单休", ("单休", "做六休一", "月休4天")),
]

WORK_TIME_PATTERNS = [
    r"\d{1,2}[:：]\d{2}\s*[-~至到]\s*\d{1,2}[:：]\d{2}",
    r"\d{1,2}\s*点\s*[-~至到]\s*\d{1,2}\s*点",
    r"朝九晚六",
    r"朝九晚五",
    r"九点[到至]六点",
    r"九点[到至]五点",
    r"弹性工作",
]


def unique_text(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = str(item or "").strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def infer_weekend_policy(text: str, tags: list[str] | None = None) -> str:
    combined = f"{text or ''} {' '.join(tags or [])}"
    for policy, patterns in WEEKEND_PATTERNS:
        if any(pattern in combined for pattern in patterns):
            return policy
    return ""


def infer_work_time(text: str, tags: list[str] | None = None) -> str:
    combined = f"{text or ''} {' '.join(tags or [])}"
    for pattern in WORK_TIME_PATTERNS:
        match = re.search(pattern, combined)
        if match:
            return match.group(0).replace("：", ":").strip()
    return ""


def normalize_job_features(job: dict[str, Any]) -> dict[str, Any]:
    tags = unique_text([str(item) for item in job.get("tags") or []])
    description = str(job.get("description") or "")
    raw_text = " ".join([
        str(job.get("job_title") or ""),
        str(job.get("company") or ""),
        str(job.get("salary") or ""),
        str(job.get("location") or job.get("city") or ""),
        description,
        " ".join(tags),
    ])
    welfare_tags = unique_text([*tags, *re.findall(r"(?:周末)?双休|大小周|单休|五险一金|年终奖|带薪年假|弹性工作|节日福利|餐补|交通补助|补充医疗|定期体检", raw_text)])
    return {
        **job,
        "city": job.get("city") or job.get("location") or "",
        "experience": job.get("experience") or _first_match(tags, ("不限", "1-3年", "3-5年", "5-10年", "10年以上", "经验不限")),
        "education": job.get("education") or _first_match(tags, ("学历不限", "大专", "本科", "硕士", "博士")),
        "company_size": job.get("company_size") or "",
        "company_industry": job.get("company_industry") or "",
        "welfare_tags": welfare_tags,
        "work_time": job.get("work_time") or infer_work_time(description, welfare_tags),
        "weekend_policy": job.get("weekend_policy") or infer_weekend_policy(description, welfare_tags),
    }


def priority_from_score(score: int, reject_reasons: list[str] | None = None) -> str:
    if reject_reasons:
        return "D"
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    return "C"


def rank_job(job: dict[str, Any], campaign: dict[str, Any], base_score: int, reasons: list[str]) -> dict[str, Any]:
    normalized = normalize_job_features(job)
    score = int(base_score or 0)
    ranking_reasons = list(reasons)
    reject_reasons: list[str] = []

    welfare_text = " ".join(normalized.get("welfare_tags") or [])
    if normalized.get("weekend_policy") == "双休":
        score += 5
        ranking_reasons.append("双休匹配")
    elif normalized.get("weekend_policy") in {"大小周", "单休"}:
        score -= 8
        reject_reasons.append(f"工作制偏弱：{normalized['weekend_policy']}")

    if normalized.get("work_time"):
        score += 3
        ranking_reasons.append(f"上下班时间明确：{normalized['work_time']}")
    if any(word in welfare_text for word in ("五险一金", "年终奖", "带薪年假", "弹性工作")):
        score += 4
        ranking_reasons.append("福利信息较完整")

    score = max(0, min(100, score))
    normalized["match_score"] = score
    normalized["priority_level"] = priority_from_score(score, reject_reasons)
    normalized["match_reasons"] = unique_text(ranking_reasons)
    normalized["reject_reasons"] = unique_text(reject_reasons)
    return normalized


def _first_match(items: list[str], candidates: tuple[str, ...]) -> str:
    text = " ".join(items)
    return next((candidate for candidate in candidates if candidate in text), "")
