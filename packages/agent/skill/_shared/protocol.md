# Glance Agent 共享协议

业务 Worker 是父 Conductor 已创建的一次性业务叶子，不是调度者；只有父 Conductor 可以创建和派发 Worker。
若已签发交接文件，本会话必须亲自且仅执行一次，交接已完成时不得重复执行；取得明确任务 prompt 后亲自 `confirm-claim`、执行、`validate` 和 `submit`。
不得 spawn/create 子代理、创建新会话或把交接、领取、研究、执行、提交委派给其他 Agent；通用的并行代理建议不适用于业务叶子。
下文并行调用只指本会话自身的独立工具调用，不授予委派权。父 Conductor 的派发职责仍按调度协议执行。

1. 周期探测每轮只通过启动器执行一次服务端指定的秒级 `dispatch`；在拿到明确的任务
   prompt 前，不加载 Skill、记忆或工作区，也不执行 `claim`、`inspect` 或第二次
   `dispatch`。有任务时只按 prompt 工作，先执行该任务要求的 `confirm-claim`。
   `NO_TASK`（退出码 10）立即结束，`AT_CAPACITY`（退出码 11）结束本轮并交给已有
   在途会话继续；空输出、未知输出或退出码与正文不匹配均是 `HANDOFF_ERROR`，不得
   当作 `NO_TASK`。传统长轮询 claim 的等待时长以本轮下发的 prompt 为准。
2. `CLAIMED` 输出已包含最新 instructions、workspace 和必填字段；读取 `input.json` 后即可工作。
3. 若 bundle 含 `previous_failures`，优先逐项修复，再写入唯一允许的 `result.json`。
   `result.json` 位于宿主临时任务工作区，不是代码编辑；不要使用 `apply_patch`、编辑器或其它需要人工批准的文件变更工具写绝对路径，
   用宿主已授权的直接文件写命令写入原始 JSON，再继续校验和提交。
4. 提交前执行 `validate <task_id>`，通过后执行 `submit <task_id>`。
5. 长任务仍建议周期性 heartbeat，但持锁请求（submit / fail / upload-artifact / check-similar）
   会自动续租，漏发不再直接导致回收；无法完成时用 fail 返回结构化错误，
   禁止修改业务状态。
6. 环境类故障处置：
   - 启动器返回 `UNREACHABLE`、权限拒绝或连接失败时，属环境故障，不是业务状态，
     不得当作 `NO_TASK` 处理；
   - 不重试、不降级参数绕行，立即结束本轮；
   - 报告时明确区分三类结束原因：无任务（业务）、环境故障（基础设施，需人工修
     宿主配置）、任务失败（需 fail 上报），并原文附上启动器输出的 detail；
   - 环境故障的结论不得写入跨轮持久记忆。
7. submit 返回 422 时不重新 claim，按错误修正 result.json 并在剩余租约期内重提；
   返回 409 时读响应体的 `task_status` 与 `retry_after_sec` 决定等待或结束，
   不得自行判定 blocked。
8. 普通角色不发布、不删除内容、不读取发布凭据；只有服务端明确签发 `publish` 角色的发布任务时，
   该角色才可以在宿主本机使用已登录浏览器会话或平台 CLI 完成发布。发布 Agent 仍不得把 Cookie、密码、
   本地路径或 shell 命令写入结果，只能返回 Schema 允许的外部结果和已登记证据 Artifact。
9. 日志、报告与持久记忆中禁止输出完整 token（租约 token、角色 token 均同），
   需要引用时只写前八位以内的前缀。
10. 仅校验任务包明确声明的非空 `context_files` / `context_manifest` 必需素材清单：
    清单中的工作区 `context/` 文件缺失或哈希不符仍属环境故障，不得回退到内容目录找图，
    也不得缺图开工。未声明或空清单不要求 `context/` 目录，不得臆造未下发素材。
    `content_research` 的 `image_only` 输入允许仅有合法公开候选来源 URL、空字幕且无预置素材；
    应从该 URL 联网核查并收集 `source_images` 输出，不能因此上报 `CONTEXT_MISSING`。
    视觉/成片 QC 等任务已声明的必需素材不适用免检，缺失仍须如实失败。

## 执行效率纪律

11. `NO_TASK`（平台明确回报无可领任务）时不建待办、不写总结，一句话结束本轮。
12. 相互独立的搜索、抓取、查重调用必须合并为一批并行发出，禁止逐条串行。
13. 宿主提供正文直出的检索工具（如 Tavily、Brave、Firecrawl 等 MCP）时优先使用，
    避免"搜索结果列表 + 逐页打开验证"的两段式流程。
14. 抓取**外部来源页面**返回 4xx/5xx、登录墙或验证码时一次即弃，换来源并在 notes 说明；
    同一 URL 不得重试。
15. 五步以内的任务不建待办清单；检索型长任务维护一份主题提纲 checklist，
    每完成一个主题勾销一次；收尾总结不超过五行。
16. 检索结果只提炼为紧凑线索清单（标题/URL/一句判断），不在思考与总结中
    整段复述工具输出原文；同一信息不在上下文中重复出现第三次。
17. 输出字段以指令中「输出」小节为准，不主动阅读 result.schema.json；
    仅当 validate 失败时再读取 schema 定位字段问题。
