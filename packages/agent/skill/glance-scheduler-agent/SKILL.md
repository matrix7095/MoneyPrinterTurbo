---
name: glance-scheduler-agent
description: 读取 Glance 服务端签发的 Codex 调度计划，并按计划为每个可用槽位创建一个全新一次性 Subagent。用于周期调度线程分发业务 Worker；不得直接领取、理解或执行业务任务。
---

# Glance 调度 Agent v2

只执行控制面调度。业务语义必须由计划指定的角色 Skill 和一次性 Subagent 处理。
此版本需配套服务端/启动器 v2 与父审核部署的新 automation prompt；不自行更新安装版或自动化。
旧命令不带 --contract-version 2 仍是 v1，旧 NO_TASK 仍为终态，不能擅自升级。

## 执行流程

### 零前置快速路径

- 调度线程启动后的第一条工具调用必须是一次 `scheduler-plan`。在拿到计划前不读取
  自动化记忆、工作区、业务任务详情或业务 Skill；这些内容不是控制面计划的一部分。
- 要求返回 contract_version=2，否则 HANDOFF_ERROR，不回退旧契约。
- 记录 active handles、已派发 request IDs、轮数与开始时间；最多16轮计划或45分钟，先到即停止补位，
  等待已授权 Worker 自然结束，不关闭仍运行的 Worker，不取消业务任务。

1. 本轮只执行一次调度启动器命令：

   ```text
   <调度启动器> scheduler-plan --scheduler-id <scheduler-id> --contract-version 2
   ```

2. 严格按结构化结果处理：

   - `NO_TASK`：没有 active handles 才一行终态结束；否则只继续等原 handles，不轮询计划，
     不关闭它们或假报 done；下一个完成事件才授权下一轮补位。
   - `DISABLED` / `BLOCKED`：停止补位，等待已有 Worker 自然终态再一行结束，不猜测任务。
   - `WAITING_UPSTREAM`：服务端证实异步推进/媒体工作，不是 READY，不建 Worker。
     retry_after_sec 必须为1..30整数；有 handles 使用该时限的原生 wait，无 handles 用该秒数短等
     （例如 Start-Sleep），之后授权下一轮一次计划，计入16轮/45分钟；缺字段为 HANDOFF_ERROR。
   - `READY`：逐条处理 `worker_requests`；若同时带有 `blocked`，本轮结束时一并报告，
     但不得为阻断项补建 Worker。
   - 空输出、未知状态、重复 request ID 或字段缺失：报告 `HANDOFF_ERROR`，停止补位，等已有 handles 结束。

3. 每个 `worker_request` 只创建一个全新 Subagent，必须使用宿主原生 Subagent spawn 动作
   （如 `multi_agent_v1__spawn_agent`）；允许宿主原生等价 spawn/wait/close 动作，不限定工具 namespace：

   - 模型必须等于 `model`。
   - 思考强度必须等于 `reasoning_effort`。
   - 初始消息必须使用 `prompt` 原文，不追加任务判断。
   - 标题使用 `title`（若有）。
   - 不复用、恢复或续接旧会话。
   - 禁止用 `create_thread`、`fork_thread`、`send_message_to_thread` 替代 Subagent。
   - 原生工具不可用、创建失败或没有有效 handle 时报告 `HANDOFF_ERROR`，
     不得假报成功、替代模型或新建用户聊天，不得重复派发。

4. 一个 Worker 完成即记录终态并 close，在预算内取下一轮一次计划填真实空闲槽，保留其它 handles，
   不等慢 discovery 全部结束。每轮只一次请求，服务端原子限制 leased+live offers，不自行扩槽。
   补位结束后等待本轮全部 Worker 结束后再汇总成功、无任务、满额、环境故障和计划阻断数量，
   不能创建后立即退出。记录终态后用原生 close 动作（如 `multi_agent_v1__close_agent`）关闭已完成子 Agent，
   不关闭仍运行的 Worker。

### 等待与上下文效率

- 所有独立 `worker_requests` 应尽量并行创建；创建后用原生 wait 动作（如 `multi_agent_v1__wait_agent`）
  只等待尚未结束的 handles，单次 `timeout_ms <= 30000`（等价工具使用不超过 30 秒的等待参数），
  等待期间按上级要求提供简短进度。
- 某个 Worker 完成后保留剩余 handles；超时继续等待同一 handle，不得重派、重试。
  只有完成事件或有效 WAITING_UPSTREAM 才授权下一轮计划；不因普通 wait 超时轮询计划。
  不使用外层忙轮询；汇总只写终态、短结果和异常分类，不回显 prompt 或完整 bundle。
- 空转轮次不读取、不写入 `memory.md`，不创建 inbox 通知。

## 禁止事项

- 不调用 Codex `automation_update`，不读取或修改 `automation.toml`。
- 不直接调用业务 `dispatch`、`claim`、`confirm-claim`、`bundle` 或 `submit`。
- 不在创建 Subagent 前读取业务 Skill、任务工作区、记忆或任务详情。
- 不因某个 Worker 返回 `NO_TASK`、`AT_CAPACITY`、空输出或未知状态而补建、重试或改派。
  该 offer 到此终态；完成事件后的新计划是新授权，不能复制旧计划。
- 不改变 Glance 任务状态、业务状态、发布状态或人工审批结果。

服务端计划只是创建 Worker 的授权，不是业务任务租约。真正的任务领取由一次性 Subagent 按其 `prompt` 执行一次。
