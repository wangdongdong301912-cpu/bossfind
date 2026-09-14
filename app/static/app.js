const state = { campaign: null, preview: [], records: [], runs: [], radarJobs: [], radarPriority: "", radarCollecting: false, radarSelectedIds: new Set(), radarConfirmMode: false, browser: null, activeRunId: null, pollTimer: null, pollDelay: 2500, expandedRunId: null };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const raw = await response.text();
  const data = raw ? (() => { try { return JSON.parse(raw); } catch { return {}; } })() : {};
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === "string" ? detail : data.message || (Array.isArray(detail) ? detail.map((item) => item.msg).join("；") : raw || `请求失败（${response.status}）`);
    throw new Error(message);
  }
  return data;
}

function alertMessage(message, type = "success") {
  const area = $("#alert-area");
  area.innerHTML = `<div class="alert ${type}"><span>${type === "success" ? "✓" : "!"} ${escapeHtml(message)}</span><button aria-label="关闭">×</button></div>`;
  area.querySelector("button").onclick = () => { area.innerHTML = ""; };
  if (type === "success") setTimeout(() => { area.innerHTML = ""; }, 3500);
}

function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = value == null ? "" : String(value);
  return element.innerHTML;
}

function tags(value) { return value.split(/[，,]/).map((item) => item.trim()).filter(Boolean); }
function formatTime(value) { return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }

function showView(name) {
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  const labels = { dashboard: "总览", radar: "岗位雷达", strategy: "投递策略", answers: "智能问答", activity: "执行记录" };
  $("#page-title").textContent = labels[name];
  if (name === "radar") loadRadarJobs();
  if (name === "activity") { loadRuns(); loadRecords(); }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderCampaign() {
  const campaign = state.campaign;
  if (!campaign) return;
  $("#summary-name").textContent = campaign.name;
  $("#summary-city").textContent = campaign.city;
  $("#summary-salary").textContent = `${campaign.salary_min}-${campaign.salary_max}K`;
  $("#summary-experience").textContent = campaign.experience;
  $("#summary-tags").innerHTML = campaign.keywords.map((item) => `<span>${escapeHtml(item)}</span>`).join("");
  $("#field-name").value = campaign.name;
  $("#field-city").value = campaign.city;
  $("#field-keywords").value = campaign.keywords.join("，");
  $("#field-excluded").value = campaign.excluded_keywords.join("，");
  $("#field-industries").value = campaign.industries.join("，");
  $("#field-experience").value = campaign.experience;
  $("#field-salary-min").value = campaign.salary_min;
  $("#field-salary-max").value = campaign.salary_max;
  $("#field-greeting").value = campaign.greeting_template;
  $("#field-limit").value = campaign.daily_limit;
  $("#field-interval-min").value = campaign.interval_min;
  $("#field-interval-max").value = campaign.interval_max;
  $("#field-start").value = campaign.work_start;
  $("#field-end").value = campaign.work_end;
  $("#field-dry").checked = campaign.dry_run;
  updateGreetingPreview();
  renderRules();
  renderMode();
}

function renderMode() {
  if (!state.campaign) return;
  const live = !state.campaign.dry_run;
  $("#mode-label").innerHTML = `<i></i>${live ? "真实投递模式" : "演练模式"}`;
  $("#hero-mode").innerHTML = `<i></i>${live ? "已启用每日限额与去重" : "策略已就绪 · 安全演练"}`;
  $$(".run-button").forEach((button) => { button.textContent = "✦ 运行 5 条演练"; });
  $$(".live-run-button").forEach((button) => { button.textContent = "▶ 真实投递并回复 HR"; });
  $("#safe-title").textContent = live ? "真实投递已启用" : "安全演练模式";
  $("#safe-note").textContent = live ? "限额、时段、去重和人工验证生效" : "默认不发送，Chrome 手动登录";
}

function collectCampaign() {
  const current = state.campaign;
  return {
    name: $("#field-name").value.trim(), city: $("#field-city").value.trim(),
    keywords: tags($("#field-keywords").value), excluded_keywords: tags($("#field-excluded").value),
    industries: tags($("#field-industries").value), experience: $("#field-experience").value,
    salary_min: Number($("#field-salary-min").value), salary_max: Number($("#field-salary-max").value),
    greeting_template: $("#field-greeting").value.trim(), answer_rules: current.answer_rules,
    daily_limit: Math.max(20, Number($("#field-limit").value)), interval_min: Number($("#field-interval-min").value), interval_max: Number($("#field-interval-max").value),
    work_start: $("#field-start").value, work_end: $("#field-end").value, dry_run: $("#field-dry").checked,
  };
}

function updateGreetingPreview() {
  const template = $("#field-greeting").value;
  const experience = $("#field-experience").value;
  $("#greeting-count").textContent = `${template.length} / 500`;
  $("#greeting-preview").textContent = template.replaceAll("{job_title}", "AI 产品经理").replaceAll("{company}", "云杉智能").replaceAll("{experience}", experience);
}

function renderRules() {
  const rules = state.campaign?.answer_rules || [];
  $("#rule-count").textContent = `${rules.filter((item) => item.enabled).length} 条启用`;
  $("#rule-list").innerHTML = rules.map((rule, index) => `<article class="rule" data-index="${index}"><div class="rule-head"><div><span>✦</span><b>${escapeHtml(rule.question)}</b></div><label class="mini-switch"><input class="rule-enabled" type="checkbox" ${rule.enabled ? "checked" : ""}><i></i></label></div><label><span>触发词</span><input class="rule-keywords" value="${escapeHtml(rule.keywords.join("，"))}"></label><label><span>建议回复</span><textarea class="rule-answer" rows="3">${escapeHtml(rule.answer)}</textarea></label></article>`).join("");
  $$(".rule").forEach((element) => {
    const index = Number(element.dataset.index);
    element.querySelector(".rule-enabled").onchange = (event) => { state.campaign.answer_rules[index].enabled = event.target.checked; renderRules(); };
    element.querySelector(".rule-keywords").oninput = (event) => { state.campaign.answer_rules[index].keywords = tags(event.target.value); };
    element.querySelector(".rule-answer").oninput = (event) => { state.campaign.answer_rules[index].answer = event.target.value; };
  });
}

async function loadDashboard() {
  try {
    const data = await api("/api/dashboard");
    state.campaign = data.campaign;
    $("#stat-matched").textContent = data.stats.matched;
    $("#stat-processed").textContent = data.stats.processed;
    $("#stat-sent").textContent = data.stats.sent;
    $("#stat-limit").textContent = data.stats.daily_limit;
    $("#quota-text").textContent = `${data.stats.sent} / ${data.stats.daily_limit}`;
    $("#quota-progress").style.width = `${Math.min(100, Math.round(data.stats.sent / data.stats.daily_limit * 100))}%`;
    $("#adapter-note").textContent = data.live_adapter.reason;
    state.runs = data.recent_runs || state.runs;
    renderCampaign();
    renderRuns();
  } catch (error) { alertMessage(error.message, "error"); }
}

async function saveCampaign() {
  try {
    const payload = collectCampaign();
    if (!payload.keywords.length) throw new Error("至少填写一个岗位关键词");
    state.campaign = await api("/api/campaign", { method: "PUT", body: JSON.stringify(payload) });
    renderCampaign(); await loadDashboard(); alertMessage("策略已保存");
  } catch (error) { alertMessage(error.message, "error"); }
}

function renderJobs(jobs) {
  const container = $("#job-list");
  container.className = "job-list";
  container.innerHTML = jobs.map((job) => `<article class="job-card"><div class="score">${job.match_score}<small>%</small></div><div class="job-main"><strong>${escapeHtml(job.job_title)}</strong><span>${escapeHtml(job.company)} · ${escapeHtml(job.recruiter || "招聘者")}</span><div class="job-tags">${(job.tags || []).map((item) => `<i>${escapeHtml(item)}</i>`).join("")}</div></div><div class="job-meta"><strong>${escapeHtml(job.salary)}</strong><span>${escapeHtml(job.reason)}</span></div><span class="demo">BOSS 实时</span></article>`).join("");
}

function priorityLabel(level) {
  return { S: "强匹配", A: "适合投递", B: "可复核", C: "弱匹配", D: "排除" }[level] || "未评级";
}

function renderRadarJobs() {
  const jobs = state.radarJobs || [];
  state.radarSelectedIds = new Set([...state.radarSelectedIds].filter((id) => jobs.some((job) => job.job_id === id)));
  const counts = jobs.reduce((acc, job) => {
    acc[job.priority_level] = (acc[job.priority_level] || 0) + 1;
    return acc;
  }, {});
  $("#radar-stat-s").textContent = counts.S || 0;
  $("#radar-stat-a").textContent = counts.A || 0;
  $("#radar-stat-b").textContent = counts.B || 0;
  $("#radar-stat-total").textContent = jobs.length;
  $("#radar-note").textContent = jobs.length ? `已按匹配分排序 ${jobs.length} 个岗位` : "等待采集";
  const container = $("#radar-list");
  if (!jobs.length) {
    container.className = "empty";
    container.innerHTML = `<b>⌕</b><strong>还没有岗位快照</strong><span>点击“采集岗位”后，会根据投递策略生成优先级列表。</span>`;
    updateRadarSelectionActions();
    return;
  }
  container.className = "radar-job-list";
  container.innerHTML = jobs.map((job) => {
    const reasons = [...(job.match_reasons || []), ...(job.reject_reasons || [])].slice(0, 4);
    const welfare = job.welfare_tags || [];
    const checked = state.radarSelectedIds.has(job.job_id) ? "checked" : "";
    return `<article class="radar-job">
      <label class="radar-select"><input type="checkbox" data-radar-select="${escapeHtml(job.job_id)}" ${checked}><i></i></label>
      <div class="priority ${escapeHtml(job.priority_level)}"><strong>${escapeHtml(job.priority_level)}</strong><span>${priorityLabel(job.priority_level)}</span></div>
      <div class="radar-job-main"><div><strong>${escapeHtml(job.job_title)}</strong><span>${escapeHtml(job.company)} · ${escapeHtml(job.city || "城市未知")}</span></div><div class="job-tags">${welfare.slice(0, 6).map((item) => `<i>${escapeHtml(item)}</i>`).join("")}</div><p>${reasons.map(escapeHtml).join("；") || "暂无匹配说明"}</p></div>
      <div class="radar-job-meta"><strong>${escapeHtml(job.salary || "-")}</strong><span>${escapeHtml(job.experience || "经验未知")} · ${escapeHtml(job.education || "学历未知")}</span><span>${escapeHtml(job.weekend_policy || "工作制未知")} ${escapeHtml(job.work_time || "")}</span><b>${job.match_score}%</b><button class="btn secondary radar-single-outreach" data-radar-outreach="${escapeHtml(job.job_id)}">打招呼</button></div>
    </article>`;
  }).join("");
  $$("[data-radar-select]").forEach((input) => input.onchange = () => {
    if (input.checked) state.radarSelectedIds.add(input.dataset.radarSelect);
    else state.radarSelectedIds.delete(input.dataset.radarSelect);
    updateRadarSelectionActions();
  });
  $$("[data-radar-outreach]").forEach((button) => button.onclick = () => startRadarSelectedOutreach([button.dataset.radarOutreach]).catch((error) => alertMessage(error.message, "error")));
  updateRadarSelectionActions();
}

function updateRadarSelectionActions() {
  const count = state.radarSelectedIds.size;
  const batch = $("#radar-batch-outreach");
  if (batch) {
    batch.disabled = count < 1;
    batch.textContent = count ? `▶ 批量打招呼（${count}）` : "▶ 批量打招呼";
  }
}

async function loadRadarJobs() {
  try {
    const query = state.radarPriority ? `?priority=${encodeURIComponent(state.radarPriority)}&limit=120` : "?limit=120";
    state.radarJobs = await api(`/api/radar/jobs${query}`);
    renderRadarJobs();
  } catch (error) { alertMessage(error.message, "error"); }
}

async function collectRadarJobs() {
  const button = $("#collect-radar");
  if (state.radarCollecting) {
    button.disabled = true;
    button.textContent = "暂停中…";
    try {
      const result = await api("/api/radar/collect/pause", { method: "POST" });
      alertMessage(result.message || "已请求暂停岗位采集");
    } catch (error) {
      alertMessage(error.message, "error");
      button.disabled = false;
      button.textContent = "Ⅱ 暂停采集";
    }
    return;
  }
  state.radarCollecting = true;
  button.disabled = false;
  button.textContent = "Ⅱ 暂停采集";
  button.classList.remove("primary");
  button.classList.add("secondary");
  try {
    const result = await api("/api/radar/collect", { method: "POST", body: JSON.stringify({ limit: 120, force_live: true }) });
    state.radarJobs = result.snapshots || [];
    renderRadarJobs();
    alertMessage(result.message || "岗位采集完成");
  } catch (error) { alertMessage(error.message, "error"); }
  finally {
    state.radarCollecting = false;
    button.disabled = false;
    button.classList.remove("secondary");
    button.classList.add("primary");
    button.textContent = "⌕ 采集岗位";
  }
}

async function previewJobs() {
  const button = $("#preview-button"); button.disabled = true; button.textContent = "分析中…";
  try { state.preview = await api("/api/preview", { method: "POST" }); renderJobs(state.preview); }
  catch (error) { alertMessage(error.message, "error"); }
  finally { button.disabled = false; button.textContent = "↻ 重新分析"; }
}

async function runCampaign() {
  $$(".run-button").forEach((button) => { button.disabled = true; });
  try {
    const result = await api("/api/run", { method: "POST", body: JSON.stringify({ session_token: null, limit: 5, force_dry: true, confirm_external_action: false }) });
    alertMessage(result.message); await loadDashboard(); await loadRecords(); showView("activity");
  } catch (error) { alertMessage(error.message, "error"); }
  finally { $$(".run-button").forEach((button) => { button.disabled = false; }); }
}

function openLiveModeChooser() {
  $("#live-mode-modal").classList.add("open");
}

function closeLiveModeChooser() {
  $("#live-mode-modal").classList.remove("open");
}

async function runLiveWorkflow() {
  openLiveModeChooser();
}

async function runAutoLiveWorkflow() {
  closeLiveModeChooser();
  $$(".live-run-button").forEach((button) => { button.disabled = true; });
  $$(".live-run-button").forEach((button) => { button.textContent = "▶ 正在真实执行…"; });
  try {
    const latestCampaign = collectCampaign();
    if (!latestCampaign.keywords.length) throw new Error("至少填写一个岗位关键词");
    state.campaign = await api("/api/campaign", { method: "PUT", body: JSON.stringify(latestCampaign) });
    renderCampaign();
    $("#mode-label").innerHTML = `<i></i>本次真实执行中`;
    $("#hero-mode").innerHTML = `<i></i>正在连接 BOSS 执行真实投递`;
    const browser = await api("/api/browser/status");
    state.browser = browser;
    if (browser.state !== "ready") throw new Error(browser.message);
    const remaining = state.campaign.daily_limit - Number($("#stat-sent").textContent || 0);
    const count = Math.max(20, remaining);
    const confirmed = window.confirm(buildLiveConfirmationMessage(state.campaign, count));
    if (!confirmed) return;
    const run = await api("/api/runs/start", { method: "POST", body: JSON.stringify({ session_token: null, limit: Math.max(20, state.campaign.daily_limit), min_successful_contacts: 20, force_live: true, confirm_external_action: true }) });
    state.activeRunId = run.id;
    upsertRun(run);
    renderRuns();
    alertMessage(`已启动批次 #${run.id}，可在执行记录中查看进度。`);
    await loadDashboard();
    showView("activity");
    pollRun(run.id);
  } catch (error) {
    alertMessage(error.message, "error");
  } finally {
    $$(".live-run-button").forEach((button) => { button.disabled = false; });
    $$(".live-run-button").forEach((button) => { button.textContent = "▶ 真实投递并回复 HR"; });
    renderMode();
  }
}

async function prepareRadarConfirmWorkflow() {
  closeLiveModeChooser();
  state.radarConfirmMode = true;
  showView("radar");
  await loadRadarJobs();
  alertMessage("已进入雷达确认模式：先采集岗位，再勾选单个或多个岗位打招呼。");
}

async function startRadarSelectedOutreach(jobIds) {
  const selected = [...new Set((jobIds || [...state.radarSelectedIds]).filter(Boolean))];
  if (!selected.length) {
    alertMessage("请先选择至少一个岗位。", "error");
    return;
  }
  const latestCampaign = collectCampaign();
  state.campaign = await api("/api/campaign", { method: "PUT", body: JSON.stringify(latestCampaign) });
  renderCampaign();
  const browser = await api("/api/browser/status");
  state.browser = browser;
  if (browser.state !== "ready") throw new Error(browser.message);
  const confirmed = window.confirm(`即将对你在岗位雷达中确认的 ${selected.length} 个岗位发起真实打招呼。是否继续？`);
  if (!confirmed) return;
  const run = await api("/api/runs/start", {
    method: "POST",
    body: JSON.stringify({
      session_token: null,
      limit: selected.length,
      min_successful_contacts: selected.length,
      selected_job_ids: selected,
      force_live: true,
      confirm_external_action: true
    })
  });
  state.activeRunId = run.id;
  upsertRun(run);
  renderRuns();
  alertMessage(`已启动雷达确认批次 #${run.id}，共 ${selected.length} 个岗位。`);
  await loadDashboard();
  showView("activity");
  pollRun(run.id);
}

function selectTopRadarJobs() {
  const topJobs = (state.radarJobs || []).filter((job) => job.priority_level === "S").slice(0, Math.max(1, state.campaign?.daily_limit || 20));
  if (!topJobs.length) {
    alertMessage("当前列表没有 S 级岗位，请先采集或切换筛选。", "error");
    return;
  }
  topJobs.forEach((job) => state.radarSelectedIds.add(job.job_id));
  renderRadarJobs();
}

function buildLiveConfirmationMessage(campaign, remainingCount) {
  const keywords = campaign.keywords.join("，") || "未填写";
  const excluded = campaign.excluded_keywords.length ? campaign.excluded_keywords.join("，") : "无";
  const rulesEnabled = (campaign.answer_rules || []).filter((rule) => rule.enabled).length;
  return [
    "即将在 BOSS 直聘使用当前 Chrome 已登录账号执行真实外部操作。",
    "",
    `目标城市：${campaign.city}`,
    `岗位关键词：${keywords}`,
    `排除关键词：${excluded}`,
    `薪资范围：${campaign.salary_min}-${campaign.salary_max}K`,
    `今日剩余额度：最多 ${remainingCount} 个岗位`,
    `自动回复：开启，将扫描最多 5 个 BOSS 聊天；仅命中 ${rulesEnabled} 条已启用规则时发送`,
    "",
    "将执行的动作：",
    "1. 按当前投递策略搜索和阅读岗位；",
    "2. 对匹配岗位点击“立即沟通”并尝试发送招呼语；",
    "3. 扫描 BOSS 聊天并按规则回复普通问题。",
    "",
    "验证码、滑块、确认弹窗、敏感问题或无法判断的页面状态会自动暂停并转人工。",
    "即使页面仍勾选“演练模式”，本次按钮也会强制走真实执行。是否继续？",
  ].join("\n");
}

async function loadRuns() {
  try {
    state.runs = await api("/api/runs?limit=12");
    renderRuns();
  } catch (error) {
    alertMessage(error.message, "error");
  }
}

function upsertRun(run) {
  const index = state.runs.findIndex((item) => item.id === run.id);
  if (index >= 0) state.runs[index] = run;
  else state.runs.unshift(run);
}

function runStatusLabel(status) {
  return { running: "执行中", queued: "排队中", completed: "已完成", failed: "失败", paused: "已暂停", canceled: "已取消", pause_requested: "暂停中", cancel_requested: "取消中" }[status] || status;
}

function runActionHint(run) {
  if (run.status === "paused") return run.stop_reason || "已暂停，需要人工处理后再重新发起。";
  if (run.status === "failed") return run.stop_reason || "执行失败，请查看安全事件和执行记录。";
  if (run.status === "canceled") return run.stop_reason || "已取消，不会继续执行。";
  return run.current_step || "-";
}

function renderSecurityEvents(run) {
  const events = run.security_events || [];
  if (state.expandedRunId !== run.id) return "";
  if (!events.length) return `<div class="run-events"><span>暂无安全事件。</span></div>`;
  return `<div class="run-events">${events.map((event) => `<div><b>${escapeHtml(event.severity)}</b><span>${escapeHtml(event.action)}</span><small>${escapeHtml(event.message)}</small></div>`).join("")}</div>`;
}

function renderRuns() {
  const container = $("#run-list");
  if (!container) return;
  if (!state.runs.length) {
    container.innerHTML = `<div class="empty"><b>RUN</b><strong>还没有执行批次</strong><span>真实投递启动后会在这里显示进度。</span></div>`;
    return;
  }
  const actionable = new Set(["running", "pause_requested", "cancel_requested"]);
  container.innerHTML = state.runs.map((run) => {
    const percent = run.total_limit ? Math.min(100, Math.round((run.processed / run.total_limit) * 100)) : 0;
    const canControl = actionable.has(run.status);
    const securityCount = run.security_event_count ?? (run.security_events || []).length;
    return `<div class="run-card" data-run-id="${run.id}"><div class="run-row">
      <div><strong>#${run.id} <i class="run-status ${escapeHtml(run.status)}">${runStatusLabel(run.status)}</i></strong><small>${escapeHtml(runActionHint(run))}</small></div>
      <div><span>${run.processed} / ${run.total_limit}</span><div class="progress"><i style="width:${percent}%"></i></div></div>
      <div><span>已发送 ${run.sent}</span><small>安全事件 ${securityCount}</small></div>
      <div class="run-actions">
        <button class="btn secondary" data-run-action="details">详情</button>
        <button class="btn secondary" data-run-action="pause" ${canControl ? "" : "disabled"}>暂停</button>
        <button class="btn secondary" data-run-action="cancel" ${canControl ? "" : "disabled"}>取消</button>
      </div>
    </div>${renderSecurityEvents(run)}</div>`;
  }).join("");
  $$("[data-run-action]").forEach((button) => {
    button.onclick = () => controlRun(Number(button.closest("[data-run-id]").dataset.runId), button.dataset.runAction);
  });
}


async function pollRun(runId) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  try {
    const run = await api(`/api/runs/${runId}?records_limit=80&security_limit=20`);
    upsertRun(run);
    state.records = run.records || state.records;
    renderRuns();
    renderRecords();
    const active = ["queued", "running", "pause_requested", "cancel_requested"].includes(run.status);
    if (active) {
      state.pollDelay = Math.min(5000, state.pollDelay + 500);
      state.pollTimer = window.setTimeout(() => pollRun(runId), state.pollDelay);
    } else {
      state.activeRunId = null;
      await loadDashboard();
      await loadRecords();
      alertMessage(`批次 #${run.id} ${run.status === "completed" ? "已完成" : "已结束，请查看详情"}`, run.status === "completed" ? "success" : "error");
    }
  } catch (error) {
    alertMessage(error.message, "error");
  }
}

async function controlRun(runId, action) {
  try {
    const run = action === "details" ? await api(`/api/runs/${runId}?records_limit=80&security_limit=20`) : await api(`/api/runs/${runId}/${action}`, { method: "POST" });
    upsertRun(run);
    if (action === "details") {
      state.expandedRunId = state.expandedRunId === runId ? null : runId;
    } else {
      alertMessage(action === "pause" ? `已请求暂停批次 #${runId}` : `已请求取消批次 #${runId}`);
      state.pollDelay = 2500;
      pollRun(runId);
    }
    renderRuns();
  } catch (error) {
    alertMessage(error.message, "error");
  }
}


async function loadRecords() {
  try { state.records = await api("/api/records?limit=80"); renderRecords(); }
  catch (error) { alertMessage(error.message, "error"); }
}

function renderRecords() {
  const body = $("#record-body");
  if (!state.records.length) { body.innerHTML = `<div class="empty"><b>≡</b><strong>还没有执行记录</strong><span>先运行一次安全演练。</span></div>`; return; }
  const labels = { dry_run: "演练", sent: "已沟通", auto_replied: "已回复", already_contacted: "已沟通", needs_human: "待人工", uncertain: "待核验", failed: "失败" };
  body.innerHTML = state.records.map((item) => `<div class="record-row record-item"><span><strong>${escapeHtml(item.job_title)}</strong><small>${escapeHtml(item.company)} · ${escapeHtml(item.salary)}</small></span><span><b>${item.match_score}%</b></span><span><i class="status-chip">${labels[item.status] || escapeHtml(item.status)}</i></span><span class="message-cell" title="${escapeHtml(item.reason || item.message)}">${escapeHtml(item.reason || item.message)}</span><span>${formatTime(item.created_at)}</span></div>`).join("");
}

async function checkBrowserSession(showAlert = true) {
  try {
    const result = await api("/api/browser/status");
    state.browser = result;
    $("#mini-account").textContent = result.connected ? "Chrome BOSS 会话" : "未连接 Chrome";
    $("#mini-state").textContent = result.message;
    $("#connect-button").textContent = result.state === "ready" ? "✓ Chrome 会话已连接" : "↻ 检查 Chrome 会话";
    if (showAlert) alertMessage(result.message, result.state === "ready" ? "success" : "error");
  } catch (error) {
    if (showAlert) alertMessage(error.message, "error");
  }
}

async function openChromeLogin() {
  const button = $("#open-login-button");
  button.disabled = true;
  button.textContent = "正在打开…";
  try {
    const result = await api("/api/browser/open-login", { method: "POST" });
    alertMessage(result.message);
  } catch (error) {
    alertMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "↗ 打开 Chrome 登录页";
  }
}

async function runAutoReplies() {
  const button = $("#auto-reply-button");
  button.disabled = true;
  button.textContent = "正在扫描聊天…";
  try {
    if (!window.confirm("即将在当前 Chrome 已登录的 BOSS 账号中扫描聊天，并按已启用问答规则发送自动回复。是否执行？")) return;
    const result = await api("/api/answers/run", { method: "POST", body: JSON.stringify({ limit: 5, confirm_external_action: true }) });
    alertMessage(result.message);
    await loadDashboard();
    await loadRecords();
    showView("activity");
  } catch (error) {
    alertMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "▶ 扫描 BOSS 聊天并自动回复";
  }
}

async function simulateAnswer() {
  try {
    const result = await api("/api/answers/simulate", { method: "POST", body: JSON.stringify({ message: $("#question-input").value }) });
    const container = $("#answer-result");
    container.className = `answer-result ${result.matched ? "matched" : "review"}`;
    container.innerHTML = `<strong>${result.matched ? `✓ 已命中 · ${Math.round(result.confidence * 100)}%` : "! 需要人工处理"}</strong><p>${escapeHtml(result.answer || result.message)}</p>`;
  } catch (error) { alertMessage(error.message, "error"); }
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.onclick = () => showView(button.dataset.view));
  $$('[data-goto]').forEach((button) => button.onclick = () => showView(button.dataset.goto));
  $$(".run-button").forEach((button) => button.onclick = runCampaign);
  $$(".live-run-button").forEach((button) => button.onclick = runLiveWorkflow);
  $("#close-live-mode").onclick = closeLiveModeChooser;
  $("#live-auto-mode").onclick = runAutoLiveWorkflow;
  $("#live-radar-mode").onclick = () => prepareRadarConfirmWorkflow().catch((error) => alertMessage(error.message, "error"));
  $("#preview-button").onclick = previewJobs;
  $("#collect-radar").onclick = collectRadarJobs;
  $("#refresh-radar").onclick = loadRadarJobs;
  $("#radar-select-all").onclick = selectTopRadarJobs;
  $("#radar-batch-outreach").onclick = () => startRadarSelectedOutreach([...state.radarSelectedIds]).catch((error) => alertMessage(error.message, "error"));
  $$("#radar-filter button").forEach((button) => button.onclick = () => {
    state.radarPriority = button.dataset.priority || "";
    $$("#radar-filter button").forEach((item) => item.classList.toggle("active", item === button));
    loadRadarJobs();
  });
  $("#save-strategy").onclick = saveCampaign;
  $("#save-answers").onclick = saveCampaign;
  $("#simulate-button").onclick = simulateAnswer;
  $("#auto-reply-button").onclick = runAutoReplies;
  $("#refresh-records").onclick = async () => { await loadRuns(); await loadRecords(); };
  $("#connect-button").onclick = () => checkBrowserSession(true);
  $("#open-login-button").onclick = openChromeLogin;
  $("#field-greeting").oninput = updateGreetingPreview;
  $("#field-experience").onchange = updateGreetingPreview;
  $("#strategy-form").onsubmit = (event) => event.preventDefault();
}

bindEvents();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/service-worker.js").catch(() => {});
Promise.all([loadDashboard(), loadRuns(), loadRecords(), loadRadarJobs(), checkBrowserSession(false)]);
