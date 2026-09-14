from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import database, services
from app.boss_adapter import adapter
from app.browser_bridge import BrowserBridgeError, existing_browser_adapter
from app.schemas import AnswerRequest, CampaignPayload, CredentialInput, RadarCollectRequest, ReplyRunRequest, RunRequest, SessionRead, SmsLoginStart, SmsLoginVerify
from app.vault import mask_account, vault
from app.sms_login import sms_login_manager


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.init_db()
    yield
    await sms_login_manager.shutdown()
    vault.cleanup()


app = FastAPI(title="BossFind", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "product": "bossfind", "mode": "dry_run_first"}


@app.get("/api/dashboard")
def get_dashboard():
    return services.dashboard()


@app.get("/api/campaign")
def get_campaign():
    return services.get_campaign()


@app.put("/api/campaign")
def update_campaign(payload: CampaignPayload):
    try:
        return services.update_campaign(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/session", response_model=SessionRead)
def create_session(payload: CredentialInput):
    token, lease = vault.create(payload.account, payload.password or "")
    return {
        "session_token": token,
        "account_masked": mask_account(payload.account),
        "state": "credentials_staged",
        "expires_at": lease.expires_at,
        "message": "凭据仅保存在本机服务内存中，尚未提交或验证登录。",
    }


@app.delete("/api/session")
async def delete_session(x_session_token: str | None = Header(default=None)):
    if x_session_token:
        await sms_login_manager.close(x_session_token)
    vault.release(x_session_token)
    return {"cleared": True}


@app.post("/api/login/sms/start")
async def start_sms_login(payload: SmsLoginStart):
    try:
        return await sms_login_manager.start(payload.session_token, payload.accept_policy)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        detail = str(exc).splitlines()[0][:240] or type(exc).__name__
        raise HTTPException(status_code=503, detail=f"验证码登录服务暂时不可用：{detail}") from exc


@app.post("/api/login/sms/verify")
async def verify_sms_login(payload: SmsLoginVerify):
    try:
        return await sms_login_manager.verify(payload.session_token, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        detail = str(exc).splitlines()[0][:240] or type(exc).__name__
        raise HTTPException(status_code=503, detail=f"验证码校验服务暂时不可用：{detail}") from exc


@app.get("/api/login/sms/status")
async def sms_login_status(x_session_token: str | None = Header(default=None)):
    return await sms_login_manager.refresh_status(x_session_token or "")


@app.get("/api/browser/status")
async def browser_status():
    return await existing_browser_adapter.status()


@app.post("/api/browser/open-login")
async def open_browser_login():
    try:
        return await existing_browser_adapter.open_login_page()
    except BrowserBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/preview")
async def preview():
    try:
        return await services.preview_live_jobs()
    except (ValueError, BrowserBridgeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/radar/collect")
async def collect_radar(payload: RadarCollectRequest):
    try:
        return await services.collect_radar_jobs(payload.limit)
    except (ValueError, BrowserBridgeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/radar/collect/pause")
def pause_radar_collect():
    return services.pause_radar_collection()


@app.get("/api/radar/jobs")
def radar_jobs(limit: int = 100, priority: str | None = None):
    return services.list_job_snapshots(limit, priority)


@app.post("/api/run")
async def run(payload: RunRequest):
    try:
        return await services.run_campaign(payload)
    except (ValueError, BrowserBridgeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/start")
async def start_run(payload: RunRequest):
    try:
        return await services.start_live_workflow(payload)
    except (ValueError, BrowserBridgeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/runs")
def list_runs(limit: int = 20):
    return services.list_runs(min(max(limit, 1), 100))


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, records_limit: int = 120, security_limit: int = 50):
    try:
        safe_records_limit = min(max(records_limit, 1), 200)
        safe_security_limit = min(max(security_limit, 1), 100)
        return services.get_run_detail(run_id, safe_records_limit, safe_security_limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: int):
    try:
        return services.pause_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: int):
    try:
        return services.cancel_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/answers/simulate")
def simulate_answer(payload: AnswerRequest):
    return services.simulate_answer(payload.message)


@app.post("/api/answers/run")
async def run_auto_replies(payload: ReplyRunRequest):
    try:
        return await services.run_auto_replies(payload)
    except (ValueError, BrowserBridgeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        detail = str(exc).splitlines()[0][:240] or type(exc).__name__
        raise HTTPException(status_code=409, detail=f"BOSS 聊天自动回复联调失败：{detail}") from exc


@app.get("/api/records")
def records(limit: int = 50, run_id: int | None = None):
    safe_limit = min(max(limit, 1), 200)
    if run_id is not None:
        return database.list_records_for_run(run_id, safe_limit)
    return database.list_records(safe_limit)


@app.post("/api/site/probe")
async def probe_site():
    return (await adapter.probe_public_page()).to_dict()


