# MoneyPrinterTurbo 稳定生产与运维记录

更新日期：2026-10-10。本文随稳定生产修复提交，最终验收证据以文末记录为准。

## 目标与验收边界

目标是从来源发现、研究、脚本、原生图片、独立质检、渲染到真实抖音发布形成可恢复的日常流程。
进程存在、上传完成或数据库状态本身都不能证明完整成功。新的完整生产周期、故障恢复、
平台发布证据和定时任务实际配置必须一起验收。

当前已实际发布作品 `7695010253280234779`：
<https://www.douyin.com/video/7695010253280234779>。
这证明迁移后的生产和发布链路可用，尚不能代替本次稳定生产修复的运行验收。

## 生产目录与配置

| 项目 | 实际位置或要求 |
| --- | --- |
| 主目录 | `C:\Users\Administrator\Documents\MoneyPrinterTurbo` |
| 业务 API | `0.0.0.0:8000`，本机检查使用 `http://127.0.0.1:8000` |
| 业务公开地址 | `http://10.10.10.200:8000` |
| 主业务数据 | `storage/business-8000/data` |
| 生产设置 | `storage/smoke/final-cutover-20261010/business-settings-production.json` |
| API 私密运行配置 | `storage/business-8000/data/private/runtime.json` |
| WebUI | `http://127.0.0.1:8510/` |
| 原生产证据 | `storage/smoke/final-cutover-20261010/` |

生产恢复必须显式指定数据目录、生产设置和公开地址，不能让脚本默认值把生产切换到隔离数据或关闭 Handsfree。
令牌仅通过私密文件传递，不放命令参数、截图、日志或文档。API 的匿名业务读取应返回 401，授权读取应返回 200。

## 自动恢复

`scripts/start-mpt-business.ps1` 管理 API，`scripts/start-mpt-webui.ps1` 管理界面。
二者依据记录的 PID、创建时间、父进程、解释器及命令行确认归属；健康运行时重复调用不会重启。
运行实例未就绪、归属变化或端口被其它进程占用时报告错误并保留现状，不盲目杀进程。

`scripts/mpt-runtime-watchdog.ps1` 串行检查 API 和 WebUI；退出的受管实例可以恢复。
`scripts/install-mpt-runtime-watchdog.ps1` 生成或安装名称限定的 MPT 计划任务，使用当前用户交互式登录身份，
登录触发及每分钟检查，禁止重叠实例，替换前保存旧任务 XML。它不恢复旧 Glance 服务。
交互式身份需要该 Windows 用户已登录；不能把此方案写成未登录时也能执行浏览器发布的系统服务。

主目录的运行参数：

```powershell
$runtimeArgs = @{
    Port = 8000
    BindAddress = '0.0.0.0'
    DataDir = 'storage/business-8000/data'
    BusinessSettingsFile = 'storage/smoke/final-cutover-20261010/business-settings-production.json'
    PublicBaseUrl = 'http://10.10.10.200:8000'
    WebuiPort = 8510
    WebuiDataDir = 'storage/webui-8510'
}
& .\scripts\mpt-runtime-watchdog.ps1 @runtimeArgs
& .\scripts\install-mpt-runtime-watchdog.ps1 -Mode Verify @runtimeArgs
```

执行前使用 PowerShell 7、无交互配置，并切换到已部署主目录。查看
`storage/runtime-watchdog-8000/watchdog.log` 和对应服务日志；不要输出私密配置内容。
安装应先执行 `-Mode Plan` 核对归属、动作和参数，再 `-Mode Install`，最后 `-Mode Verify`。

## 图片、封面和发布

原生图片生成仍为必需能力。本机尺寸处理获得明确授权后，可以执行任务启动器的
`prepare-image <task_id> <原图> --segment-index N`。该命令从冻结请求读取比例与最低尺寸，
保留原始副本、准备 PNG 和哈希凭据；相同输入可重入，篡改或半成品会拒绝覆写。
比例明显不符必须重新生成。缩放不增加细节，不能伪报原生分辨率、修改冻结输入或跳过独立素材质检。
结果的真实工具、尺寸和准备方法须如实记录，不能在业务结果中泄露本地路径。

封面标题由共享生成器放入居中 3:4 预览的安全区域，同时保留 1080×1920 冻结封面。
仍须在平台保存后检查实际预览，确认文字和主体完整。

发布角色使用已登录浏览器分步操作。最终点击前核对实际账号、标题、完整正文、全部标签、AI 内容声明和保存后的封面，
保存预检证据后申请唯一发布意图；仅 `AUTHORIZED` 且 `click_authorized=true` 允许一次最终点击。
一键发布 CLI 不能替代这些检查。点击后结果不明返回 UNKNOWN，只读核实外部结果，禁止再次上传或点击。
成功必须有真实平台 ID 或 URL，并上传对应发布结果截图作为 Artifact。

## 定时调度与预算

使用现有应用自动化 `mpt-codex-scheduler` 和 `mpt-ops-maintenance`，不创建重复作业。
调度器第一步直接执行已部署启动器的 `scheduler-plan --scheduler-id mpt-codex --contract-version 2`。
只使用 READY 返回的全新原生 Worker，保持服务端给定模型、强度和完整提示；同一未完成 handle 持续等待，禁止重派。
NO_TASK 且无活动 handle 时结束；DISABLED、BLOCKED、HANDOFF_ERROR 停止补位，等待已有任务结束。

发现角色使用 Luna/max，其余已注册质量和发布角色使用 Sol/high；宿主容量为 2。
原生视频生成当前不可用，不注册该能力，也不以其它文件冒充原生视频。

每日发布限额为 1，发布间隔为 120 分钟；达到预算后按服务端策略等待，不为测试提高额度。
素材权利、独立质检和审核失败均走现有有界恢复流程，不直接修改业务数据库放行。

## 数据与回滚

原 Glance 目录、冻结数据及历史证据保留用于回滚，旧 Glance Watchdog 保持禁用。
迁移后的历史隔离任务不会重新领取或发布。运行服务只能使用主目录数据，不能混用工作树里的隔离测试数据。
不要删除旧工作树中未跟踪的媒体和证据；Git 提交不包含这些大文件及令牌。

回滚前先核对当前任务是否仍有租约、上传或不确定发布意图，保留运行配置、任务 XML 和证据。
使用精确受管停止操作，恢复已验证代码与相应配置；不直接回写数据库状态，不自动重启旧系统并发生产。

## 本次验收记录

本节在生产部署与实际周期完成后补齐，不以代码测试代替上线证据。

- 共享封面真实渲染：修复前两条测试复现裁切丢字，修复后两条通过。
- 发布策略、唯一意图、预算、启动器、渲染版本引用：95 条测试通过。
- 自定义发布提示边界、角色契约和历史隔离：119 条测试通过。
- 图片准备：25 条定向测试及相关契约测试通过；独立代码审查通过。
- 完整业务回归：755 项通过；服务和生产流水线独立审查均通过。
- 稳定生产代码提交：`83bb3cf055f8e1ac9e578b6b9675b1ffa07746de`，已快进部署到主目录。
- 主目录 API 8000 与 WebUI 8510 已由受管脚本启动，运行模块和生产数据库路径均来自主目录。
- 正式 `MPT Runtime Watchdog` 计划任务已安装并连续实际执行，最近结果为 0；健康进程没有被重复重启。
- 独立 Windows 计划任务烟测通过，退出实例恢复验证通过；测试任务和隔离进程已清理。
- 七个原生角色注册及真实自检 READY；宿主图片与发布 Skill 已更新并保留备份。
- 已登录抖音浏览器只读预检确认原真实作品、完整正文和 AI 声明；运维自动化已启用。
- 修复后新生产周期正在执行；现有每 10 分钟生产调度自动化已启用；周期验收和文档最终记录仍待完成；代码 `83bb3cf` 已推送 origin/main。

- 22:36 正式 API 复核：匿名读取 401、授权读取 200。浏览器扩展随后提示需更新，更新后的发布操作尚未复核，不能把之前的只读预检写成持续可发布保证。

## 新周期推进与补充检查

2026-10-10 22:52:44（上海时间），正常维护周期基于已消费且哈希匹配的 SELECT/high 审核，创建新的 Jackery 内容 `content_a3f58350c2d549139be6b0bd3a4094a3`。研究任务 `agt_e4156a031b5b4f5da4219d015f0bdb3a` 在 22:54:51 开始；实际应用自动化聊天已返回契约 2 的 READY 计划，并创建 Sol/high 研究 Worker 与 Luna/max 发现 Worker。研究与脚本已由原生 Worker 正常完成和消费。脚本独立质检指出前 8 秒未展示完整型号，正常生成 RECOVERY 任务，待修复后重新质检；未手动放行。

普通 Session 的共享 WIP 计数曾把迁移隔离内容计入槽位，已在稳定工作树提交 `47f4453`，独立审查通过，相关两测试文件 79 项通过。该补丁已于 23:10 随日期修复快进部署到主目录。正式维护循环原有的会话过滤已经排除隔离内容，选题前实际 WIP 为 0；因此不能把共享计数补丁误写成此次生产推进的原因。旧隔离质检任务仍不可领取、内容与结果未被改写。

浏览器此前出现请求头策略加载失败及工具配置超时；随后正常工具调用已恢复，内置主界面和已登录 Chrome 抖音作品列表均可读取，留存 browser-connection-restored.png。没有修改策略、更新扩展或使用其它浏览器控制机制绕过限制；该只读预检不能替代新视频发布验收。

日期入库修复 `285deea34d8088db26e15218618e7c32df12264f` 已独立审核通过，相关调用方 91 项测试通过（独立日期和 HTTP 回归 17 项通过）；完整业务回归已完成：790 passed / 380.79 秒，日志 business-tests-date-release.log。23:10 排空全部 Worker 后受管停止 API、快进代码并正常启动，WebUI 原进程保留。23:11 实测匿名内容读取 401、授权 200、UI 200，调度 enabled=true/capacity=2，Watchdog 实际结果 0。此前失败的发现任务 `agt_2f9ae3f12f8a44aea22f44656c140741` 自动恢复，原结果、接受结果与唯一消费回执哈希一致，原始提交内容未修改。证据为主目录 storage/smoke/stable-production-20261010/date-release-runtime-proof.json 和 date-recovery-proof.json。

23:12 的手动控制面调度返回契约 2 READY，但原生创建工具拒绝包含冒号的任务名，该次交接按 HANDOFF_ERROR 终止，未创建 Worker、未领取或提交任务，未重复派发。现有定时调度继续 ACTIVE；新视频修复及完整成片、真实发布仍待验收。
