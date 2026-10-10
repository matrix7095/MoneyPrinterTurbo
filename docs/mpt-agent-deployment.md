# MPT Agent 部署与 contract 2 接点

当前已有独立启动器 staging、显式部署登记调用方和 MPT deployment/contract 2 服务接点。本切片仅用临时 ASGI/SQLite 验证登记，未修改现役 Glance launcher、宿主全局 Skills、Codex 自动化，也未调用在线 scheduler-plan。`STAGED` 只证明本地 CLI/技能可执行；`REGISTERED` 仅表示选定角色均收到服务端 VERIFIED，不代表 native Worker 已签发或业务完成。

## 独立安装

先使用锁定的默认 MPT 环境；不安装模型 SDK extra。PowerShell 7 示例：

```powershell
& '.\scripts\install-mpt-agent-launchers.ps1' `
  -StagingDirectory "$PWD\storage\agent-launcher-staging\review-001"
```

目录必须为当前仓库 `storage/agent-launcher-staging/` 内的新目录；已存在目录拒绝覆盖。默认凭据文件是当前仓库 `storage/business-8010/private/runtime.json`，可用 `-RuntimeConfig` 指定当前仓库 storage 内的另一份独立 MPT 文件。安装器不复制、输出 token；每次启动读取私有文件，只把派生的角色或 scheduler token 放进子进程环境，固定 API 为 `http://127.0.0.1:8010`。CLI 包名和进程内 `GLANCE_AGENT_*` 协议变量暂时保留，但不会继承旧安装配置。

生成 8 个业务角色 launcher、1 个 scheduler launcher、局部技能与 manifest。真实执行每个 PowerShell launcher 的 `skill-version`，不访问服务 API。没有默认认定 publish 宿主能力；只有实际验证过的能力才通过重复 `-PublishCapability` 参数声明。生产定时任务或全局技能安装需另一个经审查的部署切片。

此前真实 staging 样本：`storage/agent-launcher-staging/verified-20261010-024048/`，9 个 executable selftests 通过，`deployment_verified=false`。当前专属测试 `test/business/test_agent_launcher_staging.py` 9 项通过：实际执行 PS staging launcher，再用登记 CLI 参数入口和真实临时 ASGI/SQLite 验证角色 token、fresh whoami、host 字段提交/保存及无业务自检任务。本切片没有重启现役服务或运行在线登记。

## 显式登记

独立 Sol 审查通过且操作窗口明确授权后，对已存在的 staging 执行登记。例如以下容量与模型参数必须按宿主实际能力填写，不是安装器自动探测的结果：

```powershell
& '.\scripts\install-mpt-agent-launchers.ps1' `
  -StagingDirectory "$PWD\storage\agent-launcher-staging\review-001" `
  -Register -Role research,script -MaxConcurrency 2 `
  -SupportedModel gpt-6.1-sol,gpt-6-luna `
  -ModelName gpt-6.1-sol -ReasoningEffort high
```

登记不会重装文件，也不会读取 scheduler-plan。选定角色先重新执行本地 launcher/技能检查，然后用独立角色 token 请求 whoami 和 deployment，实际提交 `host.max_concurrency`、`host.supported_models`、宿主标识及 `model.name/reasoning_effort`。只接受明确指定的角色，publish 必须已有实际验证并 staging 的完整能力。所有角色 VERIFIED 才输出 REGISTERED；拒绝时输出 REGISTRATION_INCOMPLETE 并以20退出，已登记角色不会假装回滚。HTTP/配置错误输出不含凭据的 HANDOFF_ERROR。

`request_selftest=false` 是当前 consumer 契约要求。CLI/技能可执行和 whoami 连通性检查仅为部署检查，不伪造 AGENT_SELFTEST 业务结果，也不把它计为生产制作或发布验证。

## 当前服务契约

MPT 有 whoami、dispatch、confirm-claim、heartbeat、bundle、complete/fail，以及 `app/controllers/v1/deployment.py` 和 `app/business/scheduler.py` 中的下列部署/计划接点；没有复制旧 `glance_server` 或 `glance_worker`。匿名 health/readiness 与在线部署验收仍需独立证据。

| 接点 | 输入和鉴权 | 返回/复用 |
| --- | --- | --- |
| `POST /api/v1/agent/deployment` | 精确角色 token、fresh whoami；`agent_id, role_id, launcher_path, skill{name,path,content_hash}, host{device_label,max_concurrency,supported_models,...}, model{name,reasoning_effort}, self_check, protocol_version, request_selftest=false` | `status: VERIFIED/REJECTED, findings, selftest_task_id=null, selftest_already_verified=false`；验证当前仓库 staging 的固定 wrapper、manifest、技能版本/协议/hash，再保存 `AgentWorker` 与 `mpt_deployment_verified` 审计证明。 |
| `GET /api/v1/agent/scheduler/plan?scheduler_id=...&contract_version=2` | 独立 scheduler token；`verify_scheduler_token`，不可用角色/admin/root token替代 | `contract_version=2,status,scheduler_id,generated_at,worker_requests,blocked`；READY 每条保留 `request_id,task_id,title,task_type,role_id,skill_name,model,reasoning_effort,agent_id,launcher,prompt`。未知版本拒绝，不降级。 |
| `GET/PUT /api/v1/agent/scheduler/config` | MPT admin 鉴权 | 复用 `load_scheduler_config/save_scheduler_config/default_scheduler_config`；没有持久化启用配置时 DISABLED，不自动修改宿主自动化。 |

服务计划在 `begin_immediate` 内用 `build_scheduler_plan` 原子计算 leased + 未到期 SchedulerOffer 容量，限制为 MPT 已安装 consumer 和未隔离任务，并使用部署证明中的宿主容量/支持模型。每条生成全新 request/agent IDs 和 10 分钟 offer；冻结 CLI 交接文件的有效时间为 9 分钟，避免晚到达超服务端 offer。未登记或 staging 证明改变会阻断；没有计划、没有真实本地活动证据返回 NO_TASK。WAITING_UPSTREAM 需要真实 PRODUCING 内容的 RUNNING media StageRun，加当前持有的 MPT 内容 OS lock，retry_after_sec 为1..30；单独遗留的 stage 行不算活动执行。

`BusinessRuntime.maintain_dispatch()` 已接入恢复与 `AgentTaskService.reap_expired_leases()`，并由 claim/dispatch/scheduler-plan 接点调用。回收在独立事务中只处理当前 MPT owned 且未隔离的租约，不能回收历史导入/未知租约；预约未确认不消耗 attempt，确认后的过期遵守重试预算。claim 会删除领取任务对应 offers，过期 offer 不再占计划容量。未知发布结果仍只读恢复，不能自动重复发布。

MPT 部署支持 gpt-6.1-sol/gpt-6-luna 和 medium/high/xhigh/max effort，登记的 supported_models 必须覆盖选定 model.name。portable scheduler profiles/CLI handoff 已加入这两个模型，并为上游兼容保留5.6系列；MPT不会因此授权部署旧模型。安装器不改写服务签发的 model/effort/title/prompt。原生 spawn/wait/close 由 scheduler Skill 所在宿主执行；PowerShell 不模拟 native Worker，也不创建 chat thread 替代。

现有 `glance_agent.scheduler_handoff.validate_worker/materialize_offer/consume_offer` 提供目录约束、launcher hash、一次性 consumed 文件、deadline、角色与任务匹配。继续复用，不重复保存 DB 状态。登记调用方使用真实 `AgentClient.whoami/deployment`；scheduler不属于业务 role，不能经角色 deployment 登记。

部署 readiness 应分别证明真实 API/DB、owned 同步媒体执行能力、ffmpeg/ffprobe 和配置/实测 ASR，Agent registrations 单列。不能把它们计为旧 ExecutorNode `online_nodes`，也不能据本地 staging 或临时 ASGI 测试声称现役 Worker 在线。

## 调度验收

专属 staging 测试证明当前登记 CLI 的 host字段确实抵达真实服务并持久化，没有创建业务自检任务。服务 owner 的 `test/business/test_deployment_http.py` 覆盖计划/回收/鉴权等契约；本切片没有代替独立 Sol 审查或运行现役计划。最后才在无在途 handle 的安全窗口做真实登记并启用新部署。

实际调度线程第一条调用为 `scheduler-plan --scheduler-id <id> --contract-version 2`。READY 一条对应一个 fresh native Worker；不可复用会话或用 create_thread 替代。只有完成事件或有效 WAITING_UPSTREAM 授权下一次计划。原 handles 的等待不超过30秒，超时继续原 handle，不重派。NO_TASK 只有无 active handles 才终态；DISABLED/BLOCKED/HANDOFF_ERROR 停止补位并等待已有 handles。最多16轮或45分钟，不关闭仍运行的 Worker。
