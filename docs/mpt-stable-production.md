# MoneyPrinterTurbo 稳定生产与运维记录

更新日期：2026-10-11。本文随稳定生产修复提交，最终验收证据以文末记录为准。

当前结论：尚未完成稳定生产验收。代码修复已在主目录部署并推送，脚本审核合同冲突已修复并通过新周期运行验证；第7版新成片音频联合证据审核已通过，但完整动态观看仍缺有效证据，不能承诺后续无人值守稳定产出。

## 当前验收清单

下表是2026-10-11本轮实查结果；后文保留历史检查点，不能把旧版本通过结果当作当前版本成片验收。

| 要求 | 实际证据 | 状态 |
| --- | --- | --- |
| 主目录生产API和新界面 | API PID21672的app-dir、UI PID16940的Main.py均指向主目录；授权API200、匿名401、UI健康200 | 通过 |
| 守护与正式调度 | Watchdog最近执行结果0；身份叠加与名称交接修复均已部署，已再次恢复enabled=true、max_parallel=2，其他配置与维护前一致 | 通过运行配置检查 |
| 修复代码与独立验证 | 已部署f8f747e；pipeline SHA与冻结审查一致；971项完整业务回归通过，独立审查APPROVE及53项独立测试通过 | 通过 |
| 正确提交与文档 | 主目录干净，提交已快进并推送origin/main；本文与证明文件对应 | 通过当前提交检查 |
| 第7版脚本与素材 | 中文类别口播脚本、SCRIPT_QC、SCRIPT_REVIEW_ASSIST及VISUAL_PLAN_QC通过；六张新图片完成，正式自动调度的ASSET_QC已COMPLETED且passed=true | 通过脚本和素材阶段 |
| 新成片与影音审核 | 第6版实际画面已证明OpeningIdentity和Concept可见，但CONTENT_QC因关键品牌音频消歧及完整动态观看证据不足返回非通过；第7版音频、事实、权利和技术检查通过；完整动态观察缺失，CONTENT_QC passed=false，正常Recovery已领取 | 未通过 |
| 完整影音审核能力 | 无已启用、已实测的完整媒体理解Provider；Gemini仅是待授权实测入口 | 未通过 |
| 新视频真实发布 | 最新真实ID仍为旧作品7695010253280234779；本轮没有新PUBLISHED和外部ID | 未通过 |
| 后续无人值守授权 | 未来尺寸准备授权及Gemini外部上传授权仍待明确答复；本轮授权不可擅自扩大 | 未完成 |
| 后续稳定生产结论 | 必须补齐媒体能力、正常新成片质检及真实发布，再确认日常自动化范围 | 未完成 |

6秒合成能力探针的本地自检也已完成：实际解码画面在3秒前后从红转蓝，白方块持续右移；音轨1.5秒约440Hz、4.5秒约880Hz，其余校验时间点静音。证明为主目录稳定生产证据目录 `av-capability-probe-validation.json`，用于后续盲测对照；尚未向Gemini上传或验证其理解能力。

当前维护操作：独立代码与冻结任务诊断确认MPT渲染器已经通过OpeningIdentity ASS层显示已审script.opening.product_identity，范围0至min(8秒,实际时长)；但实际VISUAL_PLAN、QC与Recovery合同只声明了Concept标签，导致合法中文类别口播又被要求恢复品牌读音。身份层只包含正式身份，不自动拼接类别或陪护条件，后二者仍由实际旁白字幕承载。旧QC非通过和Recovery结果原样保留，证据 `opening-overlay-before-contract-repair-proof.json`。共享合同修复已在Stable工作树冻结：六个源码文件和一个专用测试文件，27项专用测试、446项相关回归及Ruff/AST/diff检查通过；独立代码审查APPROVE且另跑53项HTTP/真实渲染测试通过，Root完整业务回归971项通过（452.50秒、退出码0）。f8f747e已受管部署并推送，API PID15460/launcher8224指向主目录；授权200、匿名401、UI健康200，冻结七文件SHA一致，证明opening-contract-release-proof.json。调度原配置保存在 `scheduler-before-opening-contract-repair.json`，已恢复原enabled=true并实查所有配置与备份一致；API和UI健康，未停止在执行的Worker。

恢复调度后的原生交接验证发现另一个明确缺陷：服务端v2 Worker title含冒号与连字符，但本宿主原生spawn task_name仅接受小写字母、数字和下划线。原样创建被拒绝，控制器返回HANDOFF_ERROR并停止补位；没有任务被领取、没有重试同一offer。服务端名称生成与真实HTTP契约测试修复已冻结，32项HTTP测试通过，独立审查APPROVE并另跑32项测试通过（9.49秒），be950ce已受管部署并推送，API PID21672/launcher22224及原UI16940健康，授权200/匿名401/UI健康200，调度配置已完整恢复；随后本轮16轮控制器中15个全新Worker名称均通过实际native spawn；最后图片Worker已自然结束，控制器停止补位，未重置预算。正式定时调度也已在新线程01a127aa-74d2-7343-a2a5-2c6199a52c72原样派发第7版素材QC，数据库显示COMPLETED且passed=true。名称交接缺陷已通过实际派发验证，完整成片和真实发布仍待验收。第6版素材QC曾提交超时，当前数据库为CANCELLED且保留passed=true结果，不能把它记成COMPLETED或CONSUMED；第7版QC为独立新任务。当前证据production-cycle-7-progress-proof.json，历史部署证据scheduler-native-name-release-proof.json。

第7版独立成片QC `agt_d26cda3f4bd24b30b780ca007ef9be55` 已COMPLETED，最终视频SHA256为 `94f54e5d77adf1a6ce3042bfb7f8469795dcfd16879d2ee5fdf558ae9343fee2`。批准中文类别口播与同片独立ASR/PCM联合证据支持audio=true，facts/rights/technical亦通过；未声称原生听回。整体仍passed=false：宿主没有可用完整动态理解，本地file播放被拒绝，17张抽帧不足以覆盖连续字幕、身份/概念标签、帧间对应和节奏；结尾轮椅局部出框另列warning。Recovery `agt_fe9cc9d53fdd4b17a89c912db506a87b` 正在执行，不能把再次改脚本或播放器ended当作媒体能力补齐。证据 `production-cycle-7-content-qc-proof.json`。

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

## 23:57 生产验收检查点（尚未完成）

新 Jackery 周期已正常完成恢复、修订脚本、脚本质检、脚本辅助审核、视觉计划及视觉计划质检。正式数据库显示当前阶段为 `asset_generate`，图片任务 `agt_4251ba4375864e838465993de4a512bf` 仍有租约；该内容尚无发布记录。只读检查证据：主目录 `storage/smoke/stable-production-20261010/new-cycle-2357-proof.json`。

实际图片 Worker 已生成并逐图检查 8 张原生图，均为 941×1672，未上传或提交。用户此前已授权本机放大；当前 Worker 属于另一个应用调度聊天，尚未收到该授权。跨聊天消息工具要求用户明确授权向该聊天转达，目前等待该项消息授权；不能把尺寸准备、独立素材质检或发布记为已完成。

发现并正在修复真正的渲染缺口：脚本与视觉计划中的开头产品型号没有进入成片字幕链路。修复复用 ASS，将已审核的 `opening.product_identity` 显示在开头 0–8 秒；不会把英文型号强塞进中文口播。真实 8.4 秒烟测已显示 Jackery 完整型号，同时保留口播和 AI 概念标识。独立审查发现换行会切断部分正式英文型号及改写标点间距，已要求修复并增加回归；当前仍未接受、提交或部署这一补丁，烟测不能代替正式成片验收。

23:56 主目录运行检查：API 8000 的正确内容接口授权访问 200、匿名访问 401；WebUI 8510 的 Streamlit 健康接口 200，Windows 守护任务最近执行结果 0。MPT 不提供旧 Glance 的 `/healthz` 路径，其 404 不能作为 MPT 服务故障证据。当前线上代码仍为 `e19100197acb10f240eeb471287a3463035e9516`。
## 2026-10-11 开头型号修复与图片恢复

开头型号补丁已通过独立代码和像素审查：英文型号、连字符及重音字母保持完整，连续中文按实际字体宽度换行，原空格和标点不改写。开头 0–8 秒采用现有 ASS 字幕链路显示已审核型号，保留口播字幕和 AI 概念标识；旧调用无型号时输出不变。正常中文和中英混合身份已通过两种渲染器真实烟测。最终五模块 148 项通过；独立复审 20 项专项和 31 项既有回归通过，结论 APPROVE。最终完整业务回归已完成：816 passed、3261 条依赖弃用警告、412.75 秒，进程退出码 0；日志 business-tests-opening-identity-final.log。补丁仍须完成受管部署和正式周期验收。

图片旧任务按终态失败保留，自动恢复任务 `agt_4876445949204da5bd504cb7ea1298fd` 已完成并记录恢复阻断，未篡改历史结果、冻结尺寸或质检。旧自动调度聊天及其图片 Worker 均已结束。当前主线程具有用户直接授权的原文“允许本机放大并继续质检发布”，因此后续改由当前任务的全历史原生子代理继承该授权，不向其它应用聊天发送消息；此前跨聊天转达问题不再作为当前恢复前提。

使用现有业务方法 `repair_stalled_asset_generation` 创建谱系关联的新任务 `agt_8063b6337aad401c9f81fe39eb6ccfd9`，原失败任务 ID 保存在 `manual_repair_source_task_id`，8 项冻结请求和最低尺寸保持不变。该版本当前 PENDING，尚未领取、上传、质检或发布；证明文件为主目录 `storage/smoke/stable-production-20261010/image-authorized-repair-proof.json`。这次恢复是正常业务方法创建新版本，不是直接改写旧任务或数据库放行。

为代码更新暂时通过正式调度配置关闭新派发，已有原生 Worker 自然结束，最新只读检查无 LEASED 任务。服务和界面仍在线；完成受管部署核验后必须恢复调度 enabled=true，保持原容量与每日 1 条、120 分钟间隔限制。
00:17 补丁 `396424e5674a5c5f843a510d3f3c3798c89d55b9` 已快进部署到主目录并推送 origin/main；停止前租约数为 0。API 新监听 PID 20060、启动 PID 8848，UI 原健康 PID 16940 保留。正式内容接口授权 200、匿名 401，UI 健康 200；Watchdog 已恢复 Ready，正式调度配置 enabled=true、容量 2。部署证明：主目录 storage/smoke/stable-production-20261010/opening-identity-release-runtime-proof.json；最终测试日志和最新六次真实渲染证据已复制保留在主目录证明目录。

00:18 主线程开始新的契约 2 调度轮，READY 返回图片修复任务与另一个 RECOVERY 任务；均已使用计划原文创建全新当前任务原生 Worker，实际继承 Sol/high。原生 spawn 接口只提供合法 task_name，不提供标题字段，服务端要求标题保存在计划元数据中，未把含冒号的标题误传为 task_name。新图片 Worker 会直接继承本线程的真实用户授权，未向旧聊天发送消息或修改冻结交接文件。
00:24 图片修复 Worker 已直接收到并确认用户的本机准备授权，成功领取新任务后原生生成首图；因此授权传递已不再阻塞当前周期。但真实 `prepare-image` 调用暴露本地租约识别故障，返回 `Image preparation requires this agent''s local task lease`。Worker 未绕行、重复领取或上传，正常 fail --retryable 上报 `IMAGE_PREPARATION_LOCAL_LEASE_REJECTED`，服务端任务回到 PENDING；其余 7 图未生成。首图原件保留在该 Worker 的原生生成目录，不能把本次记为已生成全套或已完成素材审核。

同轮另一个 RECOVERY Worker 已 COMPLETED/CONSUMED，正常创建另一条内容的脚本修复计划。两个当前 Worker 均已终态；控制面下一轮返回契约 2 DISABLED。为修复本地租约识别暂时关闭新派发，代码诊断正在进行；服务和界面保持在线。此处记录的是部署后发现的新运行缺陷，816 项既有绿测未覆盖真实新 Worker 身份的准备命令调用链。
本地租约缺陷已修复：dispatch 使用一次性 Worker 身份，后续 CLI 恢复启动器基础身份，prepare-image 原先未读取租约身份。现仅接受基础身份或其合法 12 位十六进制派发后缀，继续使用既有精确任务、身份、令牌与文件校验。修改前同路径回归复现 1 failed / 35 passed；修改后四模块联合 172 passed，独立复审 36 passed、APPROVE、0 问题，Ruff、编译与 diff 检查通过。此改动仅影响新 CLI 进程，无需重启 API 或 UI；816 项完整回归属于前一渲染器版本，不能宣称覆盖本次 CLI 改动。正式新 Worker 的图片准备、后续质检与真实发布仍待运行验收。
00:36 本地租约修复提交 2ee6506edc3f170ca76dffbd70085265c59137b8 已快进主目录并推送 origin/main；CLI 模块路径确认为主目录 packages/agent/src/glance_agent/cli.py。未重启 API/UI，正式调度 enabled=true、max_parallel=2；API 200、UI 200、Windows Watchdog 最近执行结果 0，证明文件 image-lease-release-runtime-proof.json。契约 2 新计划已创建新图片 Worker 与脚本 Worker。新图片 Worker attempt 2 确认租约，真实新原生首图 941×1672 已经 prepare-image 成功 PREPARED 为 1080×1920；后续 7 图、素材质检、正式成片和新发布仍未完成。这是真实生产路径对修复的验证，不代表整个新周期已验收。
## 新图片提交后的独立审核检查点（尚未完成稳定生产验收）

Jackery 新图片任务 agt_8063b6337aad401c9f81fe39eb6ccfd9 的 attempt 2 已 VALID、COMPLETED / CONSUMED，8 张真实 PNG 均为 1080×1920，原生 941×1672 原件哈希与本机准备方法如实保留。实际定时调度领取独立素材质检 agt_9c30bd154c894f2f98ea9b376ece5a49；该质检返回 passed=false，第 0、2、4 段含冻结要求禁止的端口、插头和具体接线。正常恢复任务 agt_dcaacd10f8cd4eb7b41e229cecd2550a 已领取，须走新素材版本与独立重审，未放行旧图。证据：主目录 storage/smoke/stable-production-20261010/new-cycle-asset-qc-failed-proof.json。

另一条 Eve 内容的独立新 Worker 已完成 5 张原生图片的尺寸准备和提交，图片任务 agt_a179917e918a4ac1873573fef16e35e5 已 COMPLETED / CONSUMED，独立素材质检 agt_418bada3ce4e4444854a22cd6d539e7f 尚待执行。脚本辅助审核此前提出遗漏“受训陪护持续近身在场”的 REVISE；旧 MPT 脚本辅助意见只记录建议，配音由脚本质检通过触发，不能把辅助意见记录描述为已阻止配音。最终独立成片验收与高置信度发布审核仍是强制门禁，事实遗漏仍待正常脚本返工。当前没有这两条新内容的真实发布记录，不能以旧 IKEA 发布代替本轮验收。
## Handsfree 脚本辅助审核缺口与修复中检查点

真实 Eve 周期的 SCRIPT_QC 通过后，SCRIPT_REVIEW_ASSIST 提出 REVISE / blocker（遗漏持续受训陪护），但旧实现已继续制作。只读调查确认这是旧自动流程的缺口：辅助结果只记录为人工建议，既有 auto_revise_script_from_assist 方法没有调用者；后续成片与发布审核没有显式承接这个 blocker。稳定生产补丁正在改为当前可信、高置信度批准后才自动配音，明确返工复用一次有界修订；配音、渲染、发布检查共用当前任务证明与谱系校验，保持人工显式审批。

回归已用 7 项失败复现原缺口，又用 4 项配置关闭竞态及 1 项相同时间戳歧义复现旁路；第一版修复 6 模块 249 passed，独立前阶段 3 模块 135 passed。最终版仍在补齐真实人工审批与自动发布的证据区分，尚未独立批准、完整回归或部署，不能把前阶段数字当最终结果。扩展版新增红测已复现配音期间合法审核变化仍遗留新视觉任务、取消旧任务，以及旧人工证明未绑定当前计划配音等问题（74 项中 6 failed / 68 passed）；这些红测属于缺陷复现，不是最终通过结果。管理员令牌若被用于固定人工审批证明签名，轮换会使旧人工证明失效；最终实现与运维处理以验收后记录为准。

已核对正常返工入口：WorkflowService.create_recovery_task 接受 failure_class="BUSINESS_QC"、当前审核任务和 repair_target_task_id=当前 SHORT_SCRIPT 任务 ID；恢复计划只能通过现有白名单动作生成新脚本版本，再走 SCRIPT_QC 和 SCRIPT_REVIEW。人工 approve-script HTTP 仅允许 SCRIPT_REVIEW 阶段，因此旧内容已进入 WAITING_AGENT、PRODUCING 或 PACKAGED 时，不能直接重新审批或改数据库状态。令牌轮换、迟到审核使人工证明失效后，当前可信 APPROVE 仍可独立满足门禁；若当前审核要求返工，应按真实审核问题完成上述恢复，回到脚本审核阶段后再批准。尚无可信审核结果时保持等待，不能伪造质检失败或审批记录。此处记录的是已读代码支持的路径；Eve 的生产返工仍待新门禁部署后执行并记录证据。

新派发暂时 disabled，容量仍为 2。定时调度图片返工任务 agt_2a314850510d4e7ba3a6c618639de43a 已自然 FAILED（IMAGE_DIMENSION_AUTHORIZATION_REQUIRED），8 张原生原件保留，未缩放或上传；自动调度聊天 01a126bc-609c-71e1-9f26-49be0c5ddd2f 已 DISABLED / 无活动 Worker。当前主线程仍有本轮图片准备的真实用户授权，使用正常业务修复创建 agt_de827b376abc40b9af7a5689bb7dc115，8 项新视觉计划冻结请求保持不变，目前 PENDING；证明文件 image-second-authorized-repair-proof.json。没有改写旧任务结果或最低尺寸。

扩展修复中间验证：上述 6 项红测修复后，脚本门禁和全部 publish_policy 合计 113 passed；随后又用 4 项红测复现有效人工覆盖在渲染/打包时被拒绝，以及自动审核关闭后手工渲染未校验失效证明。共享门禁正在补齐整条人工覆盖路径，并保持自动制作的开关检查和禁止旧人工证明重复配音。113 项是中间结果；最终文件尚未冻结，完整业务回归、独立最终批准、部署和新视频发布仍未完成。

最终补丁已冻结，pipeline SHA256 为 a3a699b3d52186f1173b46c5a354236a2b3f064254aa4e116bb493d2d63a17af。实现同时覆盖真实管理员审批、自动制作和发布意图检查：人工证明使用既有管理员密钥签名，绑定当前任务快照和当前视觉计划冻结的配音；版本、配音、密钥或审核变化使旧证明失效。有效人工批准允许继续渲染、打包和发布，但不能借旧证明重复配音，不能绕过关闭的自动制作开关。配音前固定实际脚本 ID 和哈希，配音后重核，防止旧音频绑定新脚本。最终三模块回归 134 passed / 32.58 秒（含 91 项门禁用例），独立最终五模块 237 passed / 89.82 秒，十文件 Ruff、AST、diff 检查通过，独立审核 APPROVE / 0 未修复问题。主线程完整 test/business 回归 919 passed / 428.70 秒，0 failed，3291 条既有依赖弃用警告；日志为 .artifacts/script-assist-full-business-final.log。此时生产部署和新视频发布尚待执行。

并发限制：现有业务 helper 内部多次提交，因此配音到创建计划的整个过程并非原子事务。已验证的 TTS 期间版本变动会在排新计划或取消旧下游之前拒绝；极窄的后续提交窗口仍可能留下等待后续门禁拒绝的计划。已完成的付费配音可能保留为未授权素材；渲染、打包、最终发布意图均须重新检查当前授权和谱系，不把这些剩余文件当作通过证明。此次没有重构 core 事务，也没有增加依赖。

为避免后续日常调度再次缺少本机准备许可，已提出长期授权范围问题；没有把尚未回答的选项当作授权，也尚未更新自动化许可内容。当前生产周期授权仍可由本主线程原生 Worker 继承。独立竞态实验留下的 C:\Users\Administrator\AppData\Local\Temp\mpt-review-race-6y03lntd 清理被自动审批以 blocked by policy 拒绝，未执行，生产路径未受影响。


## 2026-10-11 当前运行验证与未完成项

修复提交 `877a851a1bfa344af802481d3956e96bc2338d39` 已完成独立审核与 919 项业务测试，已快进主目录、推送 origin/main 并重启主目录 API。API 监听 PID 22896，WebUI 监听 PID 16940，端口分别为 8000、8510；守护任务已恢复启用。部署证明为主目录 `storage/smoke/stable-production-20261010/script-assist-release-runtime-proof.json`，完整测试日志为同目录 `business-tests-script-assist-final.log`。此前文末“生产部署尚待执行”是历史检查点，不代表当前部署状态。

Jackery 新图片任务 `agt_de827b376abc40b9af7a5689bb7dc115` 已完成并消费，八张尺寸准备后的图片通过独立素材质检 `agt_e2e52e3644c24b5291ff6b3e29b1d362`。服务端已生成最终视频并执行独立成片审核 `agt_311da1e299cf46018cab66b4c7924a61`。该审核真实返回 passed=false：21 张冻结抽帧、音视频完整解码与视频哈希通过，但缺完整动态观看；10.36–11.14 秒功率单位 ASR 置信度不足，缺独立听回消歧。未认定实际错读，也未把抽帧伪称完整观看。浏览器拒绝 file://，没有绕过该限制。服务端正常创建恢复任务 `agt_c96275da35df4dabb109fe106a06c3c6`，未发布本轮视频。

Eve 的正常恢复任务 `agt_89ca4a9ad0d148cb9ee518f3d3202c07` 已应用并产生新脚本 `agt_ef052b54c0bf4aeb8a596777735eeb00`。独立脚本质检 `agt_074a92d1e05a4594bf4c5acbbc43968a` 发现当前合同禁止观众字段使用条件，与研究和脚本审核要求明确持续近身受训陪护相冲突，开场场景价值兑现也不足。第三轮恢复 `agt_61e21b2292344ee2a9ac2d3dd61560cc` 按三轮预算阻断，内容当前 SCRIPT_REVIEW/recovery_blocked。保留研究事实、旧任务和恢复预算，不直接修改状态或批准脚本。

最新真实发布记录仍为 IKEA 作品 `7695010253280234779`，不能用它代替上述新周期的最终验收。当前业务阻塞快照为主目录 `storage/smoke/stable-production-20261010/new-cycle-current-blockers-proof.json`。后续日常 AI 概念图尺寸准备的长期授权仍未收到明确答复；当前周期授权不会自动扩展为永久授权。


主线程进一步验证了现有业务工作台播放路径：选择 Jackery、读取冻结 final_video，播放器使用既有 Streamlit HTTP media URL（不包含令牌），从0播放到39秒，ended=true、readyState=4；HTTP视频字节 SHA256 与冻结的 c73cd2515d0cd18d83c8976e11d2f1d727b850d8f98aca558c13e91fec69a7e6 一致。证明和截图为同证据目录 `current-final-video-existing-player-proof.json`、`current-final-video-existing-player.png`。这只证明加载及播放能力，不能替代独立完整画面审核或真实听回，未据此改写失败 QC。内容下拉切换后的旧详情只在 Streamlit 重跑期间短暂保留，最终已显示同一 Jackery 内容与成片；未因瞬态添加缓存修复。

Eve 的冲突已定位到提示词配置上下文：已有 Handsfree 必要适用条件共享覆盖规则，但 MPT workflow/bundle 调用的 resolve_instructions 使用默认 Settings()，丢失生产启动的 Handsfree=true，而数据库没有显式覆盖行。显式传 Handsfree=true 时该规则出现，当前实际 served_prompt_hash 与缺规则版本匹配。正在修复启动 baseSettings 透传，保持数据库设置覆盖优先，覆盖任务创建、真实 bundle 和 Recovery current_contract；修复完成与新周期通过仍需后续独立验证。


配置补丁已完成独立复审：APPROVE，0 未修复问题。首次审查发现 Collector 的 WorkflowService 构造和 SOURCE_DISCOVERY 直接解析未传启动配置；两处现已补齐，新增8项真实HTTP来源发现场景，加上原16项，共24项配置矩阵。最终六模块285 passed / 55.83秒；复审独立配置矩阵与播放器指令共25 passed / 10.47秒，九个Python文件Ruff、AST、diff检查通过。配置、显式发布策略和完整动态/音频质检门槛保持。当前补丁冻结，完整test/business重新运行中；此前因漏点中断的完整测试不能视为通过。为了切换配置修复，正式Agent调度暂设enabled=false，并保留max_parallel=2；API与UI继续运行。验收和部署后须恢复原调度开关并重新检查。


最终冻结补丁的完整业务回归已完成：944 passed / 424.35 秒，0 failed，3819 条既有依赖弃用警告，进程exit0。日志为稳定工作树 `.artifacts/business-tests-prompt-settings-final.log`。该结果覆盖原919项及新增24项真实HTTP配置合同矩阵、1项媒体审核指引合同。此次修复不修改既有事实边界、历史冻结任务、恢复预算或质检通过状态；将在主目录部署后通过正规reject-script生成新脚本修订。

本轮Root原生调度在四个Worker终态后，第6轮因调用了不存在的mpt-codex-scheduler.ps1而按HANDOFF_ERROR停止，未重复交接或提交业务结果；实际通用控制启动器为现存mpt-codex.ps1。下一次控制运行应从该启动器scheduler-plan --scheduler-id mpt-codex --contract-version 2开始，不复用旧offer。


配置修复已提交、部署并推送：`f97c6f5`。主目录 API 新监听PID3592、启动器PID11976，WebUI仍是主目录PID16940；API授权200/匿名401、UI健康200，守护任务Ready/last_result=0。正式调度已恢复enabled=true、max_parallel=2，Handsfree三开关保持开启，日限1、间隔120分钟保持。部署证明 `prompt-settings-release-runtime-proof.json`、完整944项日志 `business-tests-prompt-settings-final.log` 位于主目录稳定生产证据目录。

通过现有管理员reject-script正规创建Eve第4版脚本任务 `agt_6cbc55da601744baa38a9da92fff1f08`（初始PENDING，input_hash=20421c3a9ad6d9526ff6bd0df6a1910c6a176e24ec0294798be454e20ab077a0）。实际服务端新任务已包含Handsfree必要事实边界规则；旧第三轮Recovery `agt_61e21b2292344ee2a9ac2d3dd61560cc` 仍COMPLETED，结果哈希不变。此动作是驳回并要求新修订，不是批准、旧QC重写或恢复预算重置。正规修订证明 `eve-config-repair-normal-revision-proof.json`。新版本尚未通过业务质检或真实发布，当前稳定生产结论仍为未完成验收。

### 2026-10-11 03:21 新周期运行验收

上述第4版脚本随后通过 SCRIPT_QC（passed=true、issues=[]）和独立 SCRIPT_REVIEW_ASSIST（APPROVE/high）；视觉计划及独立视觉计划质检通过。五张原创纸艺概念图已由原生工具生成，原生941×1672图片按本轮真实用户授权经prepare-image准备为1080×1920，保留原图和哈希。独立素材质检 `agt_bf202f0897cf4e96910e6bc85e73c5f7` 实际逐张核验并通过；其submit响应超时，未重复提交。随后只读数据库证实该任务COMPLETED，服务端实际渲染并签发CONTENT_QC，证明提交已被接受。

独立成片质检 `agt_8e5be968168c4e26ab8ca786918d875c` 已COMPLETED并返回passed=false：30.27秒成片的冻结帧、最终SHA、完整解码与PCM一致性通过，但缺完整动态观看证据；开头0–0.48秒Eve被两次独立无提示ASR识别为低置信“If”，尚未消歧，不能认定物理错读或放行。正常Recovery `agt_ec27c7e32c0e42e08a6db7b1d89e216e` 已COMPLETED，并实际创建第5版SHORT_SCRIPT `agt_89f4861844994f7c9e264adabb82332b`（PENDING），以已核验中文类别承载口播，保留正式身份和必要陪护条件，再正常重制、重新质检。该口播修复不会自动补齐动态审核能力。

Root本轮使用实际mpt-codex.ps1执行契约2调度，完成16轮后停止新增并等待全部原生Worker终态；没有重领、重提、修改失败质检或提高预算。另一个发现任务已提交三条NO_SIMILAR候选，仍为OFFICIAL_REFERENCE，不表示厂商转载许可。运行证明已保存主目录 `storage/smoke/stable-production-20261010/eve-production-cycle-4-proof.json`。03:21实查主目录API监听3592、UI监听16940，UI健康200，守护任务最近执行结果0。最新真实发布仍是旧作品7695010253280234779，未产生本周期新发布ID。

### 完整影音审核能力核对

只读检查未发现项目中已启用且可确认的整段video+audio语义审核Provider：TwelveLabs的analyze_clip没有业务调用方，实际密钥为空且SDK未安装；当前moonshot配置也没有模型或密钥。faster-whisper与ffprobe仅提供识别、信号和技术证据。角色声明或换文本模型不能证明实际媒体理解能力。独立设计审查还指出，Handsfree提示允许充分的独立ASR/PCM联合音频证据，不能只因没有原生听回判失败；本轮存在具体产品名未消歧及完整动态画面缺口，两项必须分别处理，脚本重制不会补齐后者。

浏览器只读检查发现现有已登录Gemini Pro入口及文件上传菜单；官方说明支持视频和音频文件（https://support.google.com/gemini/answer/14903178?hl=en）。这仍是待实测候选能力，并未接入或批准业务审核。已用现有FFmpeg制作6秒纯合成动态/音轨探针，不含业务素材、个人信息或凭据，完整解码exit0。文件 `av-capability-probe.mp4`、清单 `av-capability-probe.json` 位于主目录稳定生产证据目录，SHA256为4be91b53bfbf1bffcb3c0aa0af2497777b99e1fa2863433b075a10dcb9c765bb；入口截图为gemini-capability-entry.png。上传到Gemini属于此前本机图片准备和抖音发布授权以外的新外部传输，已提出明确授权问题，尚未得到答复；未上传任何文件，不能假称能力探针通过。
