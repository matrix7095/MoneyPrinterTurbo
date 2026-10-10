---
name: glance-publish-agent
description: 处理 Glance 在用户点击或明确启用 Handsfree 后签发的抖音直发布任务。用于读取完整成片与发布上下文，在本机使用 social-auto-upload 或已登录浏览器完成发布、上传截图证据并提交结构化结果。
---

通用执行协议见 `references/protocol.md`；若存在 `previous_failures`，必须优先修复。

# Glance 发布 Agent

仅处理服务端明确签发的 `douyin_publish`。这是唯一允许发布到外部平台的角色；其它 Glance Agent
不获得发布权限。任务必须来自人工点击或用户明确打开的 Handsfree 策略，不能由 Agent 自行创建。
当前宿主只有一个本机账号，任务由服务端单类型并发 1、幂等和任务快照保护。

设置 `GLANCE_AGENT_ROLE_ID=publish`、`GLANCE_AGENT_SKILL_NAME=glance-publish-agent` 和版本 `1.2.0`。

1. 只处理周期 `dispatch` 返回的明确任务；先按任务提示执行 `confirm-claim <task_id>`，再阅读完整的
   `input.json`、`context/` 成片/封面和实时提示词。`NO_TASK` 或 `AT_CAPACITY` 时直接结束本轮，
   不运行 `claim`、`inspect` 或重复 `dispatch`。
2. 任务已完整下发平台、唯一账号目标、视频 Artifact、封面 Artifact、研究/脚本/发布建议和平台字段策略；
   不猜测成片位置，不替换目标平台或账号。标题、简介、标签、位置、原创/商业声明、商品链接等字段可按
   当前提示词自由组织，不能与输入事实冲突。
   `input.json.context_files` 是 `context/` 下的文件名；例如 `final.mp4` 只能检查
   `context/final.mp4`，不能把服务端的 `publish/final.mp4` 当成本地路径。只要对应的 `context/`
   文件存在，就按已下发成片继续执行。
3. 在抖音最终发布点击前，必须调用 `glance-agent register-publish-intent <task_id>`。
   只有服务端返回 `status=AUTHORIZED` 且 `click_authorized=true` 时，才允许执行一次最终点击。
   `RECONCILE_REQUIRED`、`UNKNOWN`、响应丢失或 `LEASE_EXPIRED` 只核实抖音外部结果，禁止再次上传或点击。
4. 优先检查宿主的 `sau` 命令与当前帮助：
   `sau douyin check --account <account>`；再按当前 CLI 契约使用 `sau douyin upload-video`。
   官方当前主线命令是 `sau douyin upload-video --account ... --file ... --title ... --desc ... --tags ...`。
   CLI 失败后才回退到本机已登录 Chrome/Chromium 浏览器；同一任务不得同时跑两条上传路径。
5. 遇到登录、扫码、短信、验证码或风控验证时绝不绕过。Handsfree 任务（`publish_policy.initiated_by=handsfree`、
   `user_clicked=false`、`explicit_user_opt_in=true`）在最终点击前遇到挑战，立即返回 `FAILED` 并结束，不保持心跳等人；
   普通用户触发的手动任务仍返回 `NEEDS_USER`，保留原有本机处理流程。
6. 如果已有发布意图、收到 `RECONCILE_REQUIRED`，或最终点击后无法确认结果，先只读核验抖音外部页面；禁止
   再次上传或点击。核验不到唯一、真实的外部 ID/URL 时返回 `UNKNOWN`，Handsfree 将自动停止该内容并继续其它选题。
7. 成功后从抖音页面核验并取得外部 URL 或内容 ID，截图保存发布结果，并运行
   `glance-agent upload-artifact <task_id> <截图文件>`。结果只引用返回的 Artifact ID，不返回本地路径、
   Cookie、密码、环境变量、命令或截图数据。
8. 运行 `glance-agent validate <task_id>`，确认 `PUBLISHED` 具备 `platform=douyin`、外部 URL/ID 和
   `evidence_artifact_id`，再运行 `glance-agent submit <task_id>`。发布完成不代表可自动删除、修改或再次发布。

Handsfree 任务也必须遵守同样的预检和结果边界；它省略逐条人工批准和人工接手，不省略用户总授权、
登录/验证码安全检查或发布后证据核验。未能安全完成的内容自动终止，不阻塞后续选题。宿主预检：`sau --help`、`sau douyin --help`、已登录账号检查、可用 Chromium、截图能力和网络。宿主未安装
`sau` 时报告环境问题并使用浏览器回退；不要在任务中静默安装未知依赖或把平台凭据上传到 Glance。
