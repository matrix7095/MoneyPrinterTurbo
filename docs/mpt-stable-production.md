# MoneyPrinterTurbo 按需生产运行手册

更新：2026-10-17 07:02（Asia/Shanghai）。本页是生产操作入口；排查历史见 [历史记录](mpt-stable-production-history.md)。运行状态会变化，开始操作前先刷新工作台。

## 当前验收状态

**端到端稳定生产尚未验收。** 配音与规格口播合同修复均已通过独立审查、业务回归并部署。第三版未领取诊断补丁也已通过完整回归、提交、推送和部署。第八轮正常接续后，脚本 QC、脚本审核、视觉计划、视觉计划 QC、新图片与素材 QC 均已完成并被服务端消费。新配音和新成片已生成，独立技术与封存清单核验 18 项通过；完整成片 QC 已正式不通过；恢复预算 round 3/3 耗尽后，系统正常归档该内容，未发布。诊断补丁只保存固定枚举、计数和消息哈希，不保存任意消息正文，不改变领取、停止、重试预算或 QC；本次正常推进不能证明此前提前返回的根因已解决。下表为 07:02（+08）快照。父子指令作用域及 ASR 证据判定一致性修复已独立审查、提交并部署；它们不能替代新一轮正式成片验收。

| 项目 | 已核实状态 |
| --- | --- |
| 生产服务 | API `8000` worker PID `28548`、启动器 PID `14176`，于 07:01 从主目录启动；此前 PID `21552/28588` 均已退出。工作台 `http://127.0.0.1:8510/` PID `24868`。认证接口 200、匿名 401、UI health 200；MPT Runtime Watchdog 已恢复启用，旧 Glance Watchdog 停用。 |
| 生产触发 | 按需从工作台点击“运行一次”。业务定时调度、自动选题关闭；不需要 Gemini 或周期生产任务。 |
| 代码 | 配音修复 `db64dfb85bf1b0a5603aeca38b669b3ccbf4e93e`、规格口播合同修复 `05c43348e31e3bcfb29213535dab00463edc31ec`、诊断补丁 `0915005562b6387e889f554cd9c6f07aba7382a3` 均已推送 Main 与实施分支。最新修复提交 `c7ea02012d10dd86637d96c8fe1ef3f85d416fca` 只澄清父/子指令作用域和 schema v3 ASR 证据判定；部署前新增 SQLite 备份 `pre-instruction-scope-20261017.db`，119,066,624 字节、quick_check=ok，部署后当前内容 37 个任务的状态/attempt/instructions/input/result 哈希全部不变。本次诊断补丁 SQLite 备份 `pre-preclaim-diagnostics-20261017.db` 为 115,744,768 字节，quick_check=ok。部署前后 30 个任务的状态、attempt、instructions/input/result 哈希一致。 |
| 自动化回归 | 最新指令/证据合同修复：主线程组合回归 **408 passed**（87 warnings，exit 0，262.35 秒）；作者 272 passed；独立 Conductor 审查 233 passed，独立音频合同/身份回归 136 passed。Ruff、compileall、diff 检查通过。此前诊断补丁：作者 456 项、独立审查 235 项、完整组合回归 **1737 passed**（8513 warnings，exit 0，941.79 秒；运行器 947.188 秒）；359 个源码/依赖文件前后一致。此前合同修复 1686 passed、配音修复 1651 passed 的证据保留。 |
| 当前内容 | `content_0e05066e9e8b48c092e5f3c3ffaa3637`。第八轮 `run_847ee6ec4e57016c589e5356c340e51f` 已停止；包装进程于 06:42:34 正常 exit 0，原生 PID `11436` 与包装进程 `10448` 均已退出，host_live=false。内容于 06:44:44 被系统归档，状态 `ARCHIVED/archived`，保留 `RECOVERY_BLOCKED`。新脚本 `agt_723466ed4dbb4c27a60c13aa3ae70f4e` 的脚本 QC `agt_bce2ee7f682a43c8980e92edad0453c1` 已通过；脚本审核 `agt_673ffed96d8a4596a65a693e7d737f7c`、视觉计划 `agt_8cbc3754e6ad4b289e6b37926f40d91c` 和视觉计划 QC `agt_916204acacf642b6aaab1af6e9a54d7f` 均已完成并被接受、消费。 |
| 第八轮新配音 | asset `asset_48df80306acf44688d27fa7b6f245c10`，`audio/voice-wy1nvt3i/narration.mp3`，SHA-256 `9e83e9d4ec9aea2dede7510fb5aec55de653249c1e3fa4fabe6de4cbebb73ac9`，217,440 字节、36.24 秒，完整解码无错误。12 条 TTS 句子边界包含 `1920乘以1080`、`60到80英寸`；这不是可听读音或正式成片 QC 通过证明。 |
| 第八轮新图片 | 图片任务 `agt_6eba426d344149eb9bdee5656f4cfbec` 已完成；8 张实际 PNG 均为 1080×1920、哈希和字节数匹配，原图处理证据保留。独立素材 QC `agt_0030940e76384b8ca784228bbed1a093` 正式 `passed=true`，已被接受、消费，结果哈希 `b0044f8e7959a8125f430317f060b0d8fc93f137885829c565a73f7da96d0f43`。 |
| 第八轮新成片 | asset `asset_5d484c4d91314fbe9bec9b12a316e51e`，`render/revisions/83ee694daa4c4b1fb9220598d99e74cc/final.mp4`，4,201,438 字节，SHA-256 `cba7e2b291a510e3a3e51a717de224c8aaa2981b27ae2bc57130151c4716196a`。独立技术与封存清单核验合计 18 项 PASS：1088 帧、30 fps、1080×1920、首帧 PTS 0、CFR 连续、完整 AV 解码无错误；36.266667 秒视频对应 36.24 秒新配音，PCM lag 0、相关系数 0.9999788584。1088 帧图＋272 帧页共 1360 个文件哈希匹配，清单绑定本轮脚本、新配音、8 张新图和对应素材 QC；冻结 render spec 的 `bgm=null`。这些核验不证明发音、事实或正式内容 QC 通过。 |
| 第八轮完整内容 QC | `agt_64cf821d62ad4f1294ea865e68ac8e40` 已 COMPLETED、attempt 1，正式 `passed=false` 已接受并消费，结果哈希 `22d44fc3d334ac90f25597a8a41bd7e79611dc2946a4a5963a71c4f880608915`。9 项中音频及字幕证据不足，具体为乘法与范围连接词；未认定物理错读。恢复 `agt_700d26e41a31410aa3e331a693f8b970` 正式 `mark_recovery_blocked`，同根因 round 3/3 耗尽。 |
| 合同实际下发 | 待领取任务 `agt_d7ed0867eeb44c619c41412a80900c69` 的下载 instructions 与 `bundle_served.served_prompt_hash=eecd5131501f98d40a4b11a4ad6352ee80a8f82ab7456b2ef8f3c3daa64a4b6e` 一致；包含新规则，旧全字段原样条款已移除。存档 instructions/input/result 不被改写，领取时正常解析 live 合同。 |
| 第五轮配音 | asset `asset_a58d0eb08bdc495eb35434a25ac3180b`，SHA-256 `8d67bcf0ef6ba9c727a4b6695a3b0e50f02c11c8e9059961ff6da20c96d045e8`，36.504 秒。 |
| 第五轮图片与成片 | 8 张新图通过独立素材 QC；原生 941×1672 图片按本轮已授权流程保留原件并适配为 1080×1920。新 final `asset_f1542174fde9466f93d9abb8a7c014de`，SHA-256 `6b002e005be033b132586865e828b69e6cdd1d5bde73bbf61e0756894fc38122`，3,754,404 字节。 |
| 成片技术核验 | 独立技术核验 **14/14 PASS**：视频 1096 帧、30 fps、1080×1920、首帧 PTS 0；音频 36.504 秒，视频 36.533 秒，与音频时长向上取整到帧边界一致；完整音视频解码、音频相关性，以及 1096 帧图和 274 帧页共 1370 个文件的哈希核对通过。 |
| 第五轮正式内容 QC | `agt_9d76f7b9bcc64887a251b4b66cc18d74` 已 `COMPLETED`、attempt 1，正式结果 `passed=false`，已接受并消费。9 个检查中 7 个通过，音频与字幕同步证据未通过：×及范围连接词仍缺充分读音证据，结果未认定已证实错读。该旧结果保留作历史记录，不能跳过门禁发布。 |
| 第六轮脚本 QC | 旧规则生成的 script822 把技术数字写成全中文，新合同下的脚本 QC `agt_d7ed0867eeb44c619c41412a80900c69` 已正常判定不通过，并创建正常恢复 `agt_110f4119f0964101b9215800dcc7522b`。不为该旧样例放宽数值保真要求。 |
| 第六轮交接停止 | `CONDUCTOR_HANDOFF_NOT_CLAIMED`：恢复子代理无工具调用、未 consume offer、未 claim 即回合终止；Conductor 按门禁机器停止，FINISHED/exit 0 不代表业务完成。正常新交接随后成功，尚无证据证明源码或权限拒绝。 |
| 第七轮交接停止 | 同类未领取终止在新脚本 QC 再次出现；相同初始合同与 Skill 此前成功过，未确认是 API 或权限拒绝。现有隐私日志不留任意消息正文，真实提前返回原因尚未知。停止盲目重启，先补固定枚举诊断，不改变领取、停止、预算和质检逻辑。 |
| 诊断补丁验收 | V1/V2 未接受且未部署。Root 和独立审查先后发现无关子代理异常污染、否定语句误分类、旧 turn 异常标记残留；V3 已修正，独立复审 APPROVE、完整回归通过。V2 完整回归因审查拒绝而主动停止，不能算通过。公开同标签源码显示 `fork_turns=none` 仍会克隆父 developer instructions（[固定提交 child_config.rs](https://github.com/openai/codex/blob/740e5af33c71225640e0c1c1555c514c2c93ab74/codex-rs/core/src/agent/child_config.rs#L131-L153)）；本机桌面二进制 SHA 与公开 release 资产不同，不能据此断言本机逐字实现或历史根因。父子作用域澄清已提交并部署，仅改变父请求中的说明字符串，不改变权限和门禁。顶层 Conductor 的 spawn-only 限制明确不适用于其 worker 子角色；子角色仍仅能执行被分配任务并使用其已有授权工具。 |
| 发布 | 尚无本轮真实发布的 `PUBLISHED` 记录、抖音作品 ID、链接及截图。 |

旧成片及其正式音频负面 QC 仍作为历史记录保留，不能充当这版新成片的验收。独立 large-v3 无提示诊断与 medium/int8 结果在语义上相符，但它们不是听回、概率校准或正式 QC；生产 ASR 配置没有更改。仅渲染成功、技术检查通过、任务 `FINISHED` 或上传成功都不代表内容 QC 通过或发布完成。

本轮新 final 的离线 large-v3 诊断已真实 exit 0：20 段、160 词；分辨率识别为 `1920×1080`，乘号词概率 0.666784；范围识别为 `60-80英寸`，连接符词概率 0.700707。两端数字保留，但更强模型也归一化成符号，不能据此声称听到“乘以/到”，不能跨模型比较概率后直接放行。原 final 哈希及先前 18 项技术证据哈希均保持不变。

本轮额外下载的 large-v3 模型仅用于隔离诊断，未改为生产默认；约 3.09 GB 的模型文件暂保留在 `storage/asr-models/faster-whisper-large-v3-edaa852`，待正式 QC 结果明确后决定采用或清理。它不计入此前被拒绝删除的旧数据体积。

最新 schema v3 音频规则已与共同规则对齐：ASR 低置信用于定位具体关键语义；格式化符号 token 的概率不能单独当作物理错读率。只有经过独立证据仍无法消歧的关键低置信读音才按该项失败；任何关键关系缺证、静音/削波或九项硬项不足仍必须不通过。同系列 Whisper 一致、符号转写、字幕或 TTS 原文均不能单独放行。该判定只对基础、自定义、历史提示中同类 ASR 冲突优先，不修改旧提示记录和其他硬项。第一版 AND/OR 错误、第二版自定义优先级缺口均经独立审查拒绝，未部署。

## 生产操作

1. 打开[工作台](http://127.0.0.1:8510/)，选择目标内容，先刷新运行状态，再点击一次“运行一次”。生产经正常入口按阶段接续，不靠定时任务启动。
2. 若页面显示该内容已有活动 run、`LEASED` 任务或活动 Worker，继续刷新并观察同一 run 和原生 Worker；不要重复启动、重复领取或重置尝试次数。界面心跳字段不是操作系统进程存活证明。
3. 图片本机适配默认关闭。只有当前内容获得明确授权时才为该次任务启用；授权不自动扩展到其他内容或之后另一次未授权运行。原生原图、哈希、适配产物和独立素材 QC 都应留存，最低成片素材要求仍为 1080×1920。
4. 让系统依次完成脚本质检、脚本审核、配音、视觉计划及其质检、素材 QC、渲染、完整成片 QC 和发布审核。只接受对应当前版本及其 subject 哈希的正式结果；诊断 ASR、抽帧、渲染检查不能替代正式 QC。
5. 发布只在正式状态为 `PUBLISHED`，且同一新成片记录包含真实平台作品 ID、URL 和证据截图后算完成。遇到登录失效、验证码等确需用户参与的步骤时，保留当前 run 和页面现场，仅处理该步骤要求的操作。

## 故障与续接

规格口播仅允许把已确定含义的乘号/范围连接符改成中文连接词，例如 `1920乘以1080`、`60到80英寸`。保留阿拉伯数字、数值顺序/关系、单位、标称与暗环境等条件；研究和技术展示仍保留规范符号。该例外不允许技术数字全中文化、单位/型号改写或省略条件。字幕随实际口播；每个新版本仍须独立声音与画面 QC。

- **Edge TTS HTTP 403 或配音失败：** 已将固定版本升级到 `edge-tts 7.3.1`，并将同一冻结脚本、审核、配置和 run 的重试限制为三次。失败不会提前消费通过审核；不要手动重置计数或覆盖已有配音。修复后等当前 run 通过正常生命周期到达终态，再从工作台正常启动续接并重新通过所有下游门禁。
- **原生子任务先开始、父任务后登记：** 已部署 Conductor 事件顺序修复；真实生产中已验证早到的子任务完成事件能被原父任务接收并继续下一阶段。仍活动时观察原子任务，不手动重复派发。若状态与实际进程不一致，保存当前证据并走现有停止/恢复入口，不直接写数据库或伪造 QC/Worker 事件。
- **Worker 中断或进程故障：** 保留旧任务输入、结果、预算和运行记录；等原生 Worker 与 run 按正常终止路径收尾，再依据最新服务状态使用正常恢复入口。不要把心跳超时单独当作进程退出，也不要用旧版本审核结果放行新配音或新成片。

## 可复核证据

证据目录：`storage/smoke/stable-production-20261013/`。

完整组合回归命令：`python -m pytest test/business test/services/test_video_project.py -q --tb=short`。

- [规格口播合同完整回归 manifest](../storage/smoke/stable-production-20261013/speech-spec-contract-full-regression-manifest-20261017.json)，SHA-256 `46894712d503cea307ef6f18a6efe67a925b27abf781b073e07b8a295f50cbd0`。
- [合同独立审查](../storage/smoke/stable-production-20261013/speech-spec-independent-review-20261017.json)：APPROVE，156 passed。
- [实际 Worker 合同下发核验](../storage/smoke/stable-production-20261013/speech-spec-live-bundle-proof-20261017.json)，SHA-256 `0bd65ee339459dc1fbde4c881cc1d914f9d7fcee2d0d582f7a58e509ee4bf829`。
- [合同部署前核验](../storage/smoke/stable-production-20261013/speech-spec-deployed-preflight-20261017.json)、[第六轮启动记录](../storage/smoke/stable-production-20261013/aurzen-run6-start-20261017.json)、[部署前数据库备份](../storage/smoke/stable-production-20261013/pre-speech-contract-backup-20261017.json)。
- [第六轮未领取交接终态](../storage/smoke/stable-production-20261013/run6-unclaimed-recovery-terminal-20261017.json)、[第七轮有界续接预检](../storage/smoke/stable-production-20261013/run7-bounded-handoff-preflight-20261017.json)、[第七轮启动](../storage/smoke/stable-production-20261013/aurzen-run7-start-20261017.json)、[同一恢复任务实际领取](../storage/smoke/stable-production-20261013/run7-normal-recovery-claimed-20261017.json)。
- [第七轮未领取脚本 QC 终态](../storage/smoke/stable-production-20261013/run7-unclaimed-qc-terminal-20261017.json)。
- [诊断补丁 V3 作者证据](../storage/smoke/stable-production-20261013/preclaim-diagnostics-v3-author-evidence-20261017.json)、[V3 独立审查](../storage/smoke/stable-production-20261013/preclaim-diagnostics-v3-independent-review-20261017.json)、[保留的 V2 拒绝记录](../storage/smoke/stable-production-20261013/preclaim-diagnostics-v2-independent-review-20261017.json)。
- [诊断补丁完整回归 manifest](../storage/smoke/stable-production-20261013/preclaim-diagnostics-v3-full-regression-manifest-20261017.json)，SHA-256 `356de5751f92e7ae8d52a72e241bd64e6c2b10f6d6eabfb10a3080ae6d6e1558`。
- [诊断补丁部署核验](../storage/smoke/stable-production-20261013/preclaim-diagnostics-deployed-verification-20261017.json)、[任务字段前后保持核验](../storage/smoke/stable-production-20261013/preclaim-post-deploy-state-20261017.json)、[部署前 SQLite 备份](../storage/smoke/stable-production-20261013/pre-preclaim-diagnostics-backup-20261017.json)。
- [第八轮启动](../storage/smoke/stable-production-20261013/aurzen-run8-start-20261017.json)、[同一脚本 QC 正常领取](../storage/smoke/stable-production-20261013/run8-normal-qc-claimed-20261017.json)。
- [第八轮正式脚本 QC 通过](../storage/smoke/stable-production-20261013/run8-formal-script-qc-pass-20261017.json)、[视觉计划 QC 通过](../storage/smoke/stable-production-20261013/run8-formal-visual-plan-qc-pass-20261017.json)、[新配音技术核验](../storage/smoke/stable-production-20261013/run8-new-voice-technical-20261017.json)。
- [8 张新图完整性核验](../storage/smoke/stable-production-20261013/run8-image-integrity-20261017.json)、[正式素材 QC 通过](../storage/smoke/stable-production-20261013/run8-formal-asset-qc-pass-20261017.json)、[新成片独立技术核验](../storage/smoke/stable-production-20261013/run8-render-independent-20261017.json)、[正式完整内容 QC 已领取](../storage/smoke/stable-production-20261013/run8-formal-content-qc-start-20261017.json)。
- [新成片封存清单独立核验](../storage/smoke/stable-production-20261013/run8-render-manifest-independent-20261017.json)，SHA-256 `b224f51b7796a81b3d9bcfd47c8438f9f9c2d5f974bfaca6a7824d17495d6b93`；初始技术证据 SHA-256 `f80f544deeb8d9427c608c9325937a4db001f09b9920854cc14dbbf6f7d2fa73` 保持不变。

- [第五轮启动记录](../storage/smoke/stable-production-20261013/aurzen-run5-start-20261017.json)
- [第五轮成片独立技术核验（14/14 PASS）](../storage/smoke/stable-production-20261013/release-render-run5-20261017-independent.json)，SHA-256 `a8fc9980d9f404b0f72e57930d949216a0ee06b55cfbd33a7e8b48230bd2f551`
- [第五轮正式内容 QC 未通过结果](../storage/smoke/stable-production-20261013/run5-formal-content-qc-result-20261017.json)，已接受结果哈希 `ca506aeb0c576a155d1fe36cc893acf42904b33324e4d30ffbcf4fe372046b44`；文件 SHA-256 `401ee2ea63a8d4b97191d2fdbb50bbfa7cccc162a19178ef9178364a54eea6ef`。
- [第五轮正式内容 QC 活跃租约证明](../storage/smoke/stable-production-20261013/run5-new-formal-content-qc-start-20261017.json)，SHA-256 `622bd8720f4278ef6fd4f2fc83d2e06af0601e41d4e30787a5c21cc4ede4c3af`
- [新配音 large-v3 无提示诊断](../storage/smoke/stable-production-20261013/new-voice-large-v3-canary-20261017.json)，SHA-256 `691c233999e4facd0ee2855e4846145e23ed3dff0a9b1de3dda006da01a08da9`；诊断证据，不是正式 QC。
- [配音修复部署核验（13 项 PASS）](../storage/smoke/stable-production-20261013/voice-repair-deployed-verification-20261017.json)，SHA-256 `9fccf8e915baab12e5b2eae3ca1aae0d954807742b8d33f8a68d8118f9b248bd`
- [第二版完整回归 manifest](../storage/smoke/stable-production-20261013/voice-repair-v2-full-regression-manifest-20261017.json)，SHA-256 `232b3ef3e9f0d3d83a3823d00844d25feba6939369c9aa2045a4471ebbb3fa48`
- [Main 与实施分支源码比对](../storage/smoke/stable-production-20261013/voice-repair-main-source-equivalence-20261017.json)：353 个文件字节一致，5 个文件只有既存 LF/CRLF 差异，无实质差异。
- [早到子任务事件的真实 Conductor 隔离烟测](../storage/smoke/stable-production-20261013/real-conductor-turn-order-smoke-result-20261017.json)：验证宿主生命周期，不是业务视频 QC 或发布证明。
- [第三轮正式内容 QC 结果](../storage/smoke/stable-production-20261013/run3-formal-content-qc-result-20261017.json)：对应旧成片的音频 `passed=false`，只用于解释本次返工背景。

- [第八轮正式内容 QC 不通过](../storage/smoke/stable-production-20261013/run8-formal-content-qc-negative-20261017.json)，文件 SHA-256 `08674c7b14be1fa7871dd4b50594b09239ca6e64f04665d4180e8335293529c0`。
- [第八轮恢复及终态](../storage/smoke/stable-production-20261013/run8-recovery-and-terminal-20261017.json)，SHA-256 `7b2afab7a7e7b2eeefb5e5a8e8dba38067cda1c4bbb97a9c1e1276dd59fa2101`。
- [新 final 的 large-v3 诊断](../storage/smoke/stable-production-20261013/run8-final-large-v3-canary-20261017.json)，SHA-256 `e5dda9756d6dae96f22f0ca70027962d6a40cf2dcc226a6605a127fcdae5b54b`；诊断完成，不是正式 QC 通过。

- [指令及音频合同修复验收](../storage/smoke/stable-production-20261013/instruction-scope-and-audio-contract-acceptance-20261017.json)，SHA-256 `e8accb503d48da3d11ffc72756132b304bad58f7268e70613b0481109a3280b8`。
- [本次部署验证](../storage/smoke/stable-production-20261013/instruction-scope-deployed-verification-20261017.json)、[37 个历史任务字段对比](../storage/smoke/stable-production-20261013/instruction-scope-task-state-after-20261017.json)、[本次数据库备份](../storage/smoke/stable-production-20261013/pre-instruction-scope-backup-20261017.json)。

## 仍需完成

1. 第八轮已终止并归档，不再等待旧 Worker，不重放 offer、不重置预算或手工取消归档。现有服务没有给该归档内容附加新 ASR 并重新签发正式 QC 的入口；后续修复须先确认实际证据缺口与正常业务路径。父子指令作用域及音频合同对齐已部署，但不能据此宣称当前归档内容通过或稳定端到端生产已验收。
2. 完成发布审核并从正常发布流程取得这版成片对应的真实 `PUBLISHED` 状态、抖音作品 ID、URL 和截图证据。
3. 用户授权的全机旧数据清理尚未完成。此前剩余候选的逻辑体积为 **25,936,296,584 字节**，不是当前重新汇总值或可释放物理空间。10 月 17 日 03:34 再核实：旧 Glance 为 5,906,578,968 字节、bootstrap 工作树为 9,504,598,141 字节、upstream rehearsal 工作树为 3,605,444,075 字节，未发现对应进程引用。重新授权后的旧数据库备份删除仍被自动审批以 `blocked by policy` 在进程创建前拒绝，upstream 工作树的正常归档也被固定任务保护拒绝；本轮新增删除量为 0。记录见 [本次清理复核](../storage/smoke/stable-production-20261013/cleanup-renewed-authorization-rejection-20261017.json)。
4. 上述门禁通过并核实发布后，再更新本页验收结论；目前不可称为稳定端到端生产已验收。
