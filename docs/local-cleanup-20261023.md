# 临时文件清理核查（2026-10-23）

本次四个空文件已经删除，PowerShell 命令正常退出，四个目标复查均不存在。无需修改 Codex 全局权限、规则或重启正在工作的应用。

## 证据与结论

四个文件来自预览制作时两条命令各调用两次 `GetTempFileName()` 重定向 stdout/stderr，均位于 `C:\Users\Administrator\AppData\Local\Temp`：

| 文件 | 删除前字节数 | 创建时间（UTC） | 删除后 |
| --- | ---: | --- | --- |
| `tmpyij5g3.tmp` | 0 | 2026-10-23 08:38:47.1932590 | 不存在 |
| `tmphs1ufu.tmp` | 0 | 2026-10-23 08:38:47.1932590 | 不存在 |
| `tmpfbj1ue.tmp` | 0 | 2026-10-23 08:38:12.5500140 | 不存在 |
| `tmpxjrxs4.tmp` | 0 | 2026-10-23 08:38:12.5500140 | 不存在 |

原工具返回 `CreateProcess ... Rejected ... blocked by policy`，没有 PowerShell 的执行输出，属于执行前拒绝。当前配置为 `approval_policy="never"`、`approvals_reviewer="user"`、`sandbox_mode="danger-full-access"`。对原命令进行本机 `default.rules` 检查返回 `matchedRules: []`；这只排除了该规则文件的匹配，不能据此断言所有执行层均无策略，也不能证明是 Auto-review 或 Windows 文件权限拒绝。[官方规则文档](https://learn.chatgpt.com/docs/agent-configuration/rules)说明了 `execpolicy check` 的检查范围。

原命令含不必要的 `-Force`，还把当地时间 16:38 当作 `16:38Z` 比较 `CreationTimeUtc`，导致时间保护条件必定失败。此次先核实四个确切路径、普通文件属性、零字节及正确的 08:38 UTC 时间范围，再执行以下非递归、非强制删除，成功退出。由于路径表达、保护条件和强制选项都发生了变化，不能认定 `-Force` 是原拒绝的唯一原因；原策略没有返回更细的判定理由。

```powershell
Remove-Item -LiteralPath 'C:\Users\Administrator\AppData\Local\Temp\tmpyij5g3.tmp','C:\Users\Administrator\AppData\Local\Temp\tmphs1ufu.tmp','C:\Users\Administrator\AppData\Local\Temp\tmpfbj1ue.tmp','C:\Users\Administrator\AppData\Local\Temp\tmpxjrxs4.tmp' -ErrorAction Stop
```

本次成功证明这些普通文件可以正常删除，并不意味着任意删除都应无条件放行。四个目标本来都是零字节，本次不声称释放了显著磁盘空间。

## 后续执行约定

[项目 AGENTS.md](../AGENTS.md)已固定以下做法：渲染诊断日志存入各任务自己的目录，使用明确名称或直接捕获输出；普通清理先验证目标，再按确切路径删除并复查；UTC 字段使用 UTC 比较。后续遇到拒绝，保留原始错误并区分执行策略与文件权限，不通过换删除 API 隐藏操作，也不降低全局安全设置。

本次只修正清理执行方式与记录，没有修改视频、生产数据库或业务代码。
