# Glance 发布 Agent 协议 1.3.0

本文件随 `glance-publish-agent` `skill_version=1.3.0` 发布；`role.json` 的共享
`protocol_version` 仍为 `1`。

Handsfree 任务遇到登录、验证码或风控挑战时不得等待人工：若尚未最终点击，返回 `FAILED` 并结束；
若发布意图已存在或点击后结果不明，只读核验外部结果，无法证实时返回 `UNKNOWN`，禁止再次点击。
普通手动任务仍可按任务指令返回 `NEEDS_USER`。

使用已登录浏览器分步操作，禁止一键发布 CLI。最终点击前读取实际账号、标题、完整正文、全部标签和
AI 声明；保存封面后检查实际预览的完整文字与主体。文本只规范化空白和零宽字符，任何遗漏均禁止授权。
保存预检截图后，必须运行：

```text
glance-agent register-publish-intent <task_id>
```

只有服务端 JSON 同时返回 `status=AUTHORIZED` 和 `click_authorized=true`，才允许对当前
抖音目标执行一次最终发布点击。`RECONCILE_REQUIRED`、`UNKNOWN`、响应丢失或
`LEASE_EXPIRED` 都只能核实抖音外部结果，禁止再次上传或点击。

核验成功仍必须取得抖音 `external_id` 或 `external_url`，上传发布结果截图作为 evidence
Artifact，并在提交的 `PUBLISHED` 结果中引用外部 ID/URL 与该 Artifact；不能用服务端接口成功、
本地文件或截图本身代替外部结果。

本命令只读取任务 workspace 的 `.lease.json` 并输出服务端 JSON，不自动点击、不重试；Bearer
lease 无效或过期时结束本轮并按服务端响应处理。
