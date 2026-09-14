from datetime import datetime, timezone
import json
import sqlite3
from typing import Any

from app.config import get_settings


DEFAULT_RULES = [
    {"id": "experience", "question": "相关经验", "keywords": ["经验", "做过", "年限"], "answer": "有 3 年相关产品经验，负责过从 0 到 1 的产品设计和增长迭代。", "enabled": True},
    {"id": "availability", "question": "到岗时间", "keywords": ["到岗", "入职", "什么时候"], "answer": "目前在职，预计两周内可以到岗，具体时间可以协商。", "enabled": True},
    {"id": "salary", "question": "期望薪资", "keywords": ["薪资", "期望", "待遇"], "answer": "期望薪资在目标区间内，具体可结合岗位职责和整体方案沟通。", "enabled": True},
]

DEFAULT_CAMPAIGN = {
    "name": "嘉兴产品岗位探索",
    "city": "嘉兴",
    "keywords": ["产品经理", "AI 产品", "增长产品"],
    "excluded_keywords": ["销售", "电话客服"],
    "industries": ["互联网", "人工智能", "企业服务"],
    "experience": "3-5年",
    "salary_min": 15,
    "salary_max": 30,
    "greeting_template": "你好，我对贵司的{job_title}岗位很感兴趣。我有{experience}相关经验，方便的话想进一步了解岗位重点，谢谢。",
    "answer_rules": DEFAULT_RULES,
    "daily_limit": 20,
    "interval_min": 45,
    "interval_max": 120,
    "work_start": "09:30",
    "work_end": "18:30",
    "dry_run": True,
}

JSON_FIELDS = {"keywords", "excluded_keywords", "industries", "answer_rules"}


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(get_settings().database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL, city TEXT NOT NULL,
                keywords TEXT NOT NULL, excluded_keywords TEXT NOT NULL, industries TEXT NOT NULL,
                experience TEXT NOT NULL, salary_min INTEGER NOT NULL, salary_max INTEGER NOT NULL,
                greeting_template TEXT NOT NULL, answer_rules TEXT NOT NULL, daily_limit INTEGER NOT NULL,
                interval_min INTEGER NOT NULL, interval_max INTEGER NOT NULL, work_start TEXT NOT NULL,
                work_end TEXT NOT NULL, dry_run INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outreach_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_title TEXT NOT NULL, company TEXT NOT NULL,
                salary TEXT, recruiter TEXT, match_score INTEGER NOT NULL, status TEXT NOT NULL,
                message TEXT, reason TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL,
                job_id TEXT, job_url TEXT, description TEXT, run_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS outreach_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                total_limit INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                current_step TEXT NOT NULL DEFAULT '',
                stop_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS security_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                severity TEXT NOT NULL,
                action TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                job_url TEXT,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                salary TEXT,
                city TEXT,
                experience TEXT,
                education TEXT,
                company_size TEXT,
                company_industry TEXT,
                welfare_tags TEXT NOT NULL DEFAULT '[]',
                work_time TEXT NOT NULL DEFAULT '',
                weekend_policy TEXT NOT NULL DEFAULT '',
                recruiter TEXT,
                description TEXT,
                match_score INTEGER NOT NULL DEFAULT 0,
                priority_level TEXT NOT NULL DEFAULT 'C',
                match_reasons TEXT NOT NULL DEFAULT '[]',
                reject_reasons TEXT NOT NULL DEFAULT '[]',
                source_keyword TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'boss_live',
                collected_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            """
        )
        existing_columns = {row[1] for row in db.execute("PRAGMA table_info(outreach_records)")}
        for column in ("job_id", "job_url", "description", "run_id"):
            if column not in existing_columns:
                column_type = "INTEGER" if column == "run_id" else "TEXT"
                db.execute(f"ALTER TABLE outreach_records ADD COLUMN {column} {column_type}")
        db.execute("CREATE INDEX IF NOT EXISTS idx_outreach_job_status ON outreach_records(job_id, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_outreach_company_status ON outreach_records(company, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_outreach_created_status ON outreach_records(created_at, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_outreach_records_run ON outreach_records(run_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_outreach_records_created_id ON outreach_records(id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_outreach_runs_status ON outreach_runs(status, updated_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_security_events_run ON security_events(run_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_job_snapshots_priority ON job_snapshots(priority_level, match_score DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_job_snapshots_seen ON job_snapshots(last_seen_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_job_snapshots_company ON job_snapshots(company)")
        exists = db.execute("SELECT 1 FROM campaigns WHERE id=1").fetchone()
        if not exists:
            save_campaign(DEFAULT_CAMPAIGN, db)


def _campaign_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for field in JSON_FIELDS:
        data[field] = json.loads(data[field])
    data["dry_run"] = bool(data["dry_run"])
    return data


def get_campaign() -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM campaigns WHERE id=1").fetchone()
    if row is None:
        init_db()
        return get_campaign()
    return _campaign_from_row(row)


def save_campaign(payload: dict[str, Any], db: sqlite3.Connection | None = None) -> dict[str, Any]:
    own_connection = db is None
    connection = db or connect()
    updated_at = datetime.now(timezone.utc).isoformat()
    values = {key: (json.dumps(value, ensure_ascii=False) if key in JSON_FIELDS else value) for key, value in payload.items()}
    values["dry_run"] = int(bool(payload["dry_run"]))
    columns = list(DEFAULT_CAMPAIGN.keys())
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns)
    connection.execute(
        f"INSERT INTO campaigns (id, {', '.join(columns)}, updated_at) VALUES (1, {placeholders}, ?) ON CONFLICT(id) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
        [values[column] for column in columns] + [updated_at],
    )
    connection.commit()
    if own_connection:
        connection.close()
    return get_campaign()


def add_record(record: dict[str, Any]) -> int:
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO outreach_records (job_title, company, salary, recruiter, match_score, status, message, reason, source, created_at, job_id, job_url, description, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["job_title"], record["company"], record.get("salary"), record.get("recruiter"),
                record["match_score"], record["status"], record.get("message"), record.get("reason"),
                record.get("source", "demo"), datetime.now(timezone.utc).isoformat(), record.get("job_id"),
                record.get("job_url"), record.get("description"), record.get("run_id"),
            ),
        )
        return int(cursor.lastrowid)


def list_records(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM outreach_records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def list_records_for_run(run_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM outreach_records WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def add_security_event(run_id: int | None, severity: str, action: str, message: str) -> int:
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO security_events (run_id, severity, action, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, severity, action, message[:500], datetime.now(timezone.utc).isoformat()),
        )
        return int(cursor.lastrowid)


def list_security_events(run_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as db:
        if run_id is None:
            rows = db.execute("SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM security_events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
    return [dict(row) for row in rows]


def upsert_job_snapshot(job: dict[str, Any]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    payload = {
        "job_id": job_id,
        "job_url": job.get("job_url") or "",
        "job_title": job.get("job_title") or "",
        "company": job.get("company") or "",
        "salary": job.get("salary") or "",
        "city": job.get("city") or job.get("location") or "",
        "experience": job.get("experience") or "",
        "education": job.get("education") or "",
        "company_size": job.get("company_size") or "",
        "company_industry": job.get("company_industry") or "",
        "welfare_tags": json.dumps(job.get("welfare_tags") or job.get("tags") or [], ensure_ascii=False),
        "work_time": job.get("work_time") or "",
        "weekend_policy": job.get("weekend_policy") or "",
        "recruiter": job.get("recruiter") or "",
        "description": job.get("description") or "",
        "match_score": int(job.get("match_score") or 0),
        "priority_level": job.get("priority_level") or "C",
        "match_reasons": json.dumps(job.get("match_reasons") or job.get("ranking_reasons") or [], ensure_ascii=False),
        "reject_reasons": json.dumps(job.get("reject_reasons") or [], ensure_ascii=False),
        "source_keyword": job.get("source_keyword") or "",
        "source": job.get("source") or "boss_live",
        "collected_at": job.get("collected_at") or now,
        "last_seen_at": now,
    }
    columns = list(payload)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column not in {"job_id", "collected_at"})
    with connect() as db:
        cursor = db.execute(
            f"""
            INSERT INTO job_snapshots ({', '.join(columns)})
            VALUES ({', '.join('?' for _ in columns)})
            ON CONFLICT(job_id) DO UPDATE SET {updates}
            """,
            [payload[column] for column in columns],
        )
        row = db.execute("SELECT id FROM job_snapshots WHERE job_id=?", (job_id,)).fetchone()
    return int(row["id"] if row else cursor.lastrowid)


def list_job_snapshots(limit: int = 100, priority: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if priority:
        where = "WHERE priority_level=?"
        params.append(priority)
    params.append(limit)
    with connect() as db:
        rows = db.execute(
            f"""
            SELECT * FROM job_snapshots
            {where}
            ORDER BY match_score DESC, last_seen_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    snapshots = [dict(row) for row in rows]
    for item in snapshots:
        for field in ("welfare_tags", "match_reasons", "reject_reasons"):
            try:
                item[field] = json.loads(item.get(field) or "[]")
            except json.JSONDecodeError:
                item[field] = []
    return snapshots


def create_run(mode: str, total_limit: int, current_step: str = "starting") -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO outreach_runs
                (mode, status, total_limit, processed, sent, failed, current_step, stop_reason, created_at, updated_at)
            VALUES (?, 'running', ?, 0, 0, 0, ?, '', ?, ?)
            """,
            (mode, int(total_limit), current_step, now, now),
        )
        run_id = int(cursor.lastrowid)
    return get_run(run_id)


def get_run(run_id: int) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM outreach_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("run not found")
    return dict(row)


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT r.*, COUNT(e.id) AS security_event_count
            FROM (SELECT * FROM outreach_runs ORDER BY id DESC LIMIT ?) AS r
            LEFT JOIN security_events AS e ON e.run_id = r.id
            GROUP BY r.id
            ORDER BY r.id DESC
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_run(run_id: int, **fields: Any) -> dict[str, Any]:
    if not fields:
        return get_run(run_id)
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    assignments = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [run_id]
    with connect() as db:
        db.execute(f"UPDATE outreach_runs SET {assignments} WHERE id=?", values)
    return get_run(run_id)


def mark_run_control(run_id: int, action: str) -> dict[str, Any]:
    if action == "pause":
        return update_run(run_id, status="pause_requested", current_step="pause requested", stop_reason="user requested pause")
    if action == "cancel":
        return update_run(run_id, status="cancel_requested", current_step="cancel requested", stop_reason="user requested cancel")
    raise ValueError("unsupported run control action")


def get_run_status(run_id: int) -> str:
    with connect() as db:
        row = db.execute("SELECT status FROM outreach_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("run not found")
    return str(row["status"])


def refresh_run_progress(run_id: int, current_step: str | None = None) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """
            SELECT
                COUNT(*) AS processed,
                SUM(CASE WHEN status IN ('sent', 'auto_replied') THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
            FROM outreach_records
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
    fields: dict[str, Any] = {
        "processed": int(row["processed"] or 0),
        "sent": int(row["sent"] or 0),
        "failed": int(row["failed"] or 0),
    }
    if current_step is not None:
        fields["current_step"] = current_step
    return update_run(run_id, **fields)


def count_records() -> int:
    with connect() as db:
        return int(db.execute("SELECT COUNT(*) FROM outreach_records").fetchone()[0])


def count_sent_today() -> int:
    from datetime import datetime, timedelta, timezone as datetime_timezone

    china_tz = datetime_timezone(timedelta(hours=8))
    now = datetime.now(china_tz)
    start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc)
    with connect() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM outreach_records WHERE status='sent' AND created_at>=? AND created_at<?",
            (start_utc.isoformat(), end_utc.isoformat()),
        ).fetchone()
    return int(row[0])


def was_contacted(job_id: str) -> bool:
    if not job_id:
        return False
    with connect() as db:
        row = db.execute(
            "SELECT 1 FROM outreach_records WHERE job_id=? AND status IN ('sent', 'already_contacted') LIMIT 1",
            (job_id,),
        ).fetchone()
    return row is not None


def was_company_contacted(company: str) -> bool:
    if not company:
        return False
    with connect() as db:
        row = db.execute(
            "SELECT 1 FROM outreach_records WHERE company=? AND status IN ('sent', 'already_contacted') LIMIT 1",
            (company,),
        ).fetchone()
    return row is not None
