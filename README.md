# BossFind

本地优先的 Boss 直聘岗位筛选、招呼文案和简单问答 MVP。

## 当前能力

- 目标岗位、城市、薪资和经验配置
- 岗位雷达模式：采集岗位快照并生成 S/A/B/C/D 优先级
- 结构化采集岗位薪资、城市、经验、学历、福利、工作制和上下班时间
- 招呼模板与变量预览
- 规则化问答建议
- 岗位匹配演示预览
- 安全演练与审计记录
- 复用你手动登录的 Chrome BOSS 会话
- 实时岗位搜索、薪资解码和完整职位描述分析
- 每日限额、执行时段、随机间隔和岗位去重
- 点击“立即沟通”后尝试发送配置好的招呼语
- 扫描 BOSS 聊天并按问答规则自动回复；敏感/模糊问题转人工
- Boss 适配器安全状态机与人工验证码熔断

当前版本默认保持演练模式。关闭演练模式后，每次真实执行仍会弹出明确确认；不会绕过验证码、滑块或站点安全验证。

## 启动

```powershell
cd D:\test\bossfind
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

API 文档：

```text
http://127.0.0.1:8000/docs
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

详细规划见 [docs/01-产品规划.md](docs/01-产品规划.md)。

## 岗位雷达模式

岗位雷达只采集和排序，不会自动投递。它会复用当前投递策略，从 BOSS 职位页读取岗位信息并保存到本地 `job_snapshots` 表。

核心接口：

```text
POST /api/radar/collect
GET  /api/radar/jobs
```

优先级说明：

```text
S：强匹配，建议优先投递
A：匹配，适合投递
B：部分匹配，建议人工复核
C：弱匹配，不建议优先处理
D：命中排除或明显不合适
```

移动端当前采用 PWA 方案：访问 `http://127.0.0.1:8000` 后可添加到手机桌面。真实投递仍建议在电脑端执行，因为它依赖本机 Chrome 调试窗口和人工安全验证。

## GitHub 发布注意事项

提交前确认以下内容没有进入仓库：

```text
data/*.db
data/chrome-profile/
.env
真实账号、Cookie、验证码、投递记录
```

公开版本应默认保持 `dry_run=true`，真实投递必须由使用者手动配置 Chrome 会话并确认。

## 复用 Chrome 中的 BOSS 登录会话

1. 先启动后端服务。
2. 另开一个 PowerShell，执行：

   ```powershell
   cd D:\test\bossfind
   .\start-chrome-boss.ps1
   ```

   这会打开一个 BossFind 专用 Chrome 窗口，并开启本机调试端口 `9222`。
3. 在这个 Chrome 窗口里手动登录 BOSS 直聘。短信验证码、滑块和安全验证必须由你本人完成。
4. 回到 `http://127.0.0.1:8000`，点击“检查 Chrome 会话”。
5. 在“投递策略”中配置城市、岗位关键词、排除词、薪资、经验、每日上限和执行时段。
6. 点击“读取当前 BOSS”预览真实岗位。程序会逐个读取职位描述并计算匹配分，不会发起沟通。
7. 保持“演练模式”可只生成审计记录；关闭后点击“开始今日投递”，在确认框中确认本次外部操作。
8. 真实执行会点击匹配岗位的“立即沟通”，并在安全识别到唯一聊天输入框和发送按钮时发送招呼语。遇到确认弹窗、滑块、验证码或无法确认的状态会立即停止并转人工。
9. “智能问答”页可以点击“扫描 BOSS 聊天并自动回复”。只有命中已启用规则的普通问题会发送回复；隐私、验证码、薪资承诺、面试邀约等问题会转人工。

桥接只允许连接 `127.0.0.1`/`localhost` 的调试端口，并且只附加 `zhipin.com` 标签页。程序不会导出 Cookie、手机号、密码或验证码。默认端口为 `9222`，可通过 `BOSSFIND_BROWSER_CDP_URL` 修改。
