from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class AnswerRule(BaseModel):
    id: str
    question: str = Field(min_length=1, max_length=80)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    answer: str = Field(min_length=1, max_length=500)
    enabled: bool = True


class CampaignPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    city: str = Field(min_length=1, max_length=30)
    keywords: list[str] = Field(min_length=1, max_length=12)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=20)
    industries: list[str] = Field(default_factory=list, max_length=12)
    experience: str = Field(min_length=1, max_length=30)
    salary_min: int = Field(ge=0, le=200)
    salary_max: int = Field(ge=0, le=200)
    greeting_template: str = Field(min_length=4, max_length=500)
    answer_rules: list[AnswerRule] = Field(default_factory=list, max_length=20)
    daily_limit: int = Field(ge=1, le=100)
    interval_min: int = Field(ge=15, le=3600)
    interval_max: int = Field(ge=15, le=7200)
    work_start: str = "09:30"
    work_end: str = "18:30"
    dry_run: bool = True

    @field_validator("keywords", "excluded_keywords", "industries")
    @classmethod
    def clean_list(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.salary_max < self.salary_min:
            raise ValueError("最高薪资不能低于最低薪资")
        if self.interval_max < self.interval_min:
            raise ValueError("最大间隔不能小于最小间隔")
        return self


class CampaignRead(CampaignPayload):
    id: int
    updated_at: datetime


class CredentialInput(BaseModel):
    account: str = Field(min_length=4, max_length=128)
    password: str | None = Field(default=None, min_length=4, max_length=256)


class SessionRead(BaseModel):
    session_token: str
    account_masked: str
    state: str
    expires_at: datetime
    message: str


class RunRequest(BaseModel):
    session_token: str | None = None
    limit: int = Field(default=5, ge=1, le=100)
    min_successful_contacts: int = Field(default=20, ge=1, le=100)
    confirm_external_action: bool = False
    force_dry: bool = False
    force_live: bool = False


class JobSnapshotRead(BaseModel):
    id: int
    job_id: str
    job_url: str | None = None
    job_title: str
    company: str
    salary: str | None = None
    city: str | None = None
    experience: str | None = None
    education: str | None = None
    company_size: str | None = None
    company_industry: str | None = None
    welfare_tags: list[str] = Field(default_factory=list)
    work_time: str = ""
    weekend_policy: str = ""
    recruiter: str | None = None
    description: str | None = None
    match_score: int = 0
    priority_level: str = "C"
    match_reasons: list[str] = Field(default_factory=list)
    reject_reasons: list[str] = Field(default_factory=list)
    source_keyword: str = ""
    source: str = "boss_live"
    collected_at: str
    last_seen_at: str


class RadarCollectRequest(BaseModel):
    limit: int = Field(default=80, ge=1, le=300)
    force_live: bool = False


class ReplyRunRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=30)
    confirm_external_action: bool = False


class AnswerRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class SmsLoginStart(BaseModel):
    session_token: str = Field(min_length=16)
    accept_policy: bool


class SmsLoginVerify(BaseModel):
    session_token: str = Field(min_length=16)
    code: str = Field(pattern=r"^\d{6}$")
