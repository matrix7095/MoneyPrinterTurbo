# MoneyPrinterTurbo 按需生产运行手册

更新：2026-10-17 05:04（Asia/Shanghai）。本页是生产操作入口；排查历史见 [历史记录](mpt-stable-production-history.md)。运行状态会变化，开始操作前先刷新工作台。

## 当前验收状态

**端到端稳定生产尚未验收。** 配音与规格口播合同修复均已通过独立审查、业务回归并部署。第五轮新成片正式 QC 未通过；第六轮已实证新合同下发，但恢复子代理未执行交接便结束，Conductor 因此安全停止。旧运行终态核实后，第七轮通过正常入口有界续接，同一恢复任务已正常领取为首次尝试。下表为 05:04（+08）快照。

| 项目 | 已核实状态 |
| --- | --- |
| 生产服务 | API `8000` worker PID `27892`、启动器 PID `17280`；工作台 `http://127.0.0.1:8510/` PID `24868`。认证接口 200、匿名 401、UI health 200；MPT Runtime Watchdog 已恢复启用，旧 Glance Watchdog 停用。 |
| 生产触发 | 按需从工作台点击“运行一次”。业务定时调度、自动选题关闭；不需要 Gemini 或周期生产任务。 |
| 代码 | 配音修复 `db64dfb85bf1b0a5603aeca38b669b3ccbf4e93e`；规格口播合同修复 `05c43348e31e3bcfb29213535dab00463edc31ec`。两者均已推送 Main 与实施分支；本次部署前 SQLite 备份 quick_check=ok。 |
| 自动化回归 | 最新合同修复：作者 365 项、独立审查 156 项、完整组合回归 **1686 passed**（8513 warnings，exit 0，957.53 秒）；359 个源码/依赖文件前后一致。此前配音修复完整回归 1651 passed 的证据保留。 |
| 当前内容 | `content_0e05066e9e8b48c092e5f3c3ffaa3637`，第七轮 `run_670cb2f24502fa09b8853a6d8a934f89`、generation 7、原生 PID `7500`、启动 `05:03:31`、到期 `07:03:31`（+08）；同一恢复任务 `agt_110f4119f0964101b9215800dcc7522b` 已 LEASED、attempt 1。第六轮已终态、旧进程缺失、全部 intents TERMINAL；未重置任务或预算。 |
| 合同实际下发 | 待领取任务 `agt_d7ed0867eeb44c619c41412a80900c69` 的下载 instructions 与 `bundle_served.served_prompt_hash=eecd5131501f98d40a4b11a4ad6352ee80a8f82ab7456b2ef8f3c3daa64a4b6e` 一致；包含新规则，旧全字段原样条款已移除。存档 instructions/input/result 不被改写，领取时正常解析 live 合同。 |
| 第五轮配音 | asset `asset_a58d0eb08bdc495eb35434a25ac3180b`，SHA-256 `8d67bcf0ef6ba9c727a4b6695a3b0e50f02c11c8e9059961ff6da20c96d045e8`，36.504 秒。 |
| 第五轮图片与成片 | 8 张新图通过独立素材 QC；原生 941×1672 图片按本轮已授权流程保留原件并适配为 1080×1920。新 final `asset_f1542174fde9466f93d9abb8a7c014de`，SHA-256 `6b002e005be033b132586865e828b69e6cdd1d5bde73bbf61e0756894fc38122`，3,754,404 字节。 |
| 成片技术核验 | 独立技术核验 **14/14 PASS**：视频 1096 帧、30 fps、1080×1920、首帧 PTS 0；音频 36.504 秒，视频 36.533 秒，与音频时长向上取整到帧边界一致；完整音视频解码、音频相关性，以及 1096 帧图和 274 帧页共 1370 个文件的哈希核对通过。 |
| 正式内容 QC | `agt_9d76f7b9bcc64887a251b4b66cc18d74` 已 `COMPLETED`、attempt 1，正式结果 `passed=false`，已接受并消费。9 个检查中 7 个通过，音频与字幕同步证据未通过：×及范围连接词仍缺充分读音证据，结果未认定已证实错读。正常恢复任务 `agt_6f12269dcc6b4e75bb9478196ee21814` 已创建，不能跳过门禁发布。 |
| 第六轮脚本 QC | 旧规则生成的 script822 把技术数字写成全中文，新合同下的脚本 QC `agt_d7ed0867eeb44c619c41412a80900c69` 已正常判定不通过，并创建正常恢复 `agt_110f4119f0964101b9215800dcc7522b`。不为该旧样例放宽数值保真要求。 |
| 第六轮交接停止 | `CONDUCTOR_HANDOFF_NOT_CLAIMED`：恢复子代理无工具调用、未 consume offer、未 claim 即回合终止；Conductor 按门禁机器停止，FINISHED/exit 0 不代表业务完成。正常新交接随后成功，尚无证据证明源码或权限拒绝。 |
| 发布 | 尚无本轮真实发布的 `PUBLISHED` 记录、抖音作品 ID、链接及截图。 |

旧成片及其正式音频负面 QC 仍作为历史记录保留，不能充当这版新成片的验收。独立 large-v3 无提示诊断与 medium/int8 结果在语义上相符，但它们不是听回、概率校准或正式 QC；生产 ASR 配置没有更改。仅渲染成功、技术检查通过、任务 `FINISHED` 或上传成功都不代表内容 QC 通过或发布完成。

本轮额外下载的 large-v3 模型仅用于隔离诊断，未改为生产默认；约 3.09 GB 的模型文件暂保留在 `storage/asr-models/faster-whisper-large-v3-edaa852`，待正式 QC 结果明确后决定采用或清理。它不计入此前被拒绝删除的旧数据体积。

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

## 仍需完成

1. 跟进第七轮正常恢复 `agt_110f4119f0964101b9215800dcc7522b`，在原有有限预算内生成符合统一合同的脚本，再正常经过审核、新配音、视觉计划、素材与完整成片 QC。不能改写旧结果、重置预算或用低置信单独认定已证实错读。
2. 完成发布审核并从正常发布流程取得这版成片对应的真实 `PUBLISHED` 状态、抖音作品 ID、URL 和截图证据。
3. 用户授权的全机旧数据清理尚未完成。此前剩余候选的逻辑体积为 **25,936,296,584 字节**，不是当前重新汇总值或可释放物理空间。10 月 17 日 03:34 再核实：旧 Glance 为 5,906,578,968 字节、bootstrap 工作树为 9,504,598,141 字节、upstream rehearsal 工作树为 3,605,444,075 字节，未发现对应进程引用。重新授权后的旧数据库备份删除仍被自动审批以 `blocked by policy` 在进程创建前拒绝，upstream 工作树的正常归档也被固定任务保护拒绝；本轮新增删除量为 0。记录见 [本次清理复核](../storage/smoke/stable-production-20261013/cleanup-renewed-authorization-rejection-20261017.json)。
4. 上述门禁通过并核实发布后，再更新本页验收结论；目前不可称为稳定端到端生产已验收。
