# MoneyPrinterTurbo 生产交接

更新：2026-10-23。已发布内容的独立修订协议 `76019bd` 已提交、双分支推送并受管部署，59项独立部署验收通过，旧数据和素材保全。Framery 新修订已创建并完成正式研究；研究仍未找到可适用的官方图片公开使用许可，已正常排空以避免再次转成全AI图。新成片正式QC和新发布尚未完成。部署、修订、许可缺口及后续进展见[本次重新发布记录](framery-republication-20261023.md)。以下10月20日至22日的发布、PID和候选说明均为历史快照；旧作品最近在创作者中心显示“私密”。

更新：2026-10-22。官方素材优先修复`8c324c2`、自然句配音及1.5倍保音高加速`a938f37`已提交、推送并受管部署。
新生成默认女声3、`+50%`，音量与本机识别配置不变；原公开作品和历史音轨不改写。
另已产出使用两份官方宣传图、AI图为0的19.8秒本地预览，实际新provider合成与严格ASR通过。
这份短预览未完成正式业务QC或新发布；音色自然度仍需试听，来源许可与新发布流程也须单独完成。
本次原因、修复、候选及独立验收见[官方素材与配音修复记录](source-first-voice-20261022.md)。

更新：2026-10-21 17:52（Asia/Shanghai）。源码`8f68051970db593882ceb1271a25b411fb61167f`已提交、双分支推送并隐藏部署到Main，纯图片项目改用真实60fps、2倍空间采样、低量化误差及整片统一码率预算。新完整候选已生成，156项技术核验通过；真实图片仍有可测误差和压缩取舍，不能称为观感彻底解决或正式QC通过。当前公开作品仍是下述10月20日的30fps版本。新证据与限制见[图片运动采样复核](video-motion-sampling-20261021.md)。

10月20日发布快照：02:24（Asia/Shanghai），当时的代码285b76ffc36f110635daba2181306a971ae8063c已双分支推送并部署。
**10月20日的30fps版本已真实发布。** 抖音作品 `7698443917590629670`，后台已显示“已发布”，公开页面已核对完整文案与AI声明；服务端发布任务完成并消费。这不是10月21日候选的新发布回执。

[打开本次公开作品](https://www.douyin.com/video/7698443917590629670)。本次修正版为1080×1920、30fps、32.27秒，使用Kokoro离线配音。按需生产链路已跑通；后续仍逐条通过质量、来源、登录与实际发布门禁，不承诺无人值守永不阻断。

## 当前生产状态

| 项目 | 已核实结果 |
|---|---|
| 服务 | API 8000，worker 15092 / launcher 30008，均归属Main；工作台 http://127.0.0.1:8510/ 返回200，认证API200、匿名401 |
| 切换保全 | 10月21日切换窗口的前后八表逐行哈希完全一致，含3137个任务；当前内容118份已登记媒体逐份核验相同。随后维护线程正常更新两项运维时间/游标，不能宣称运行中数据库永远不变 |
| 工作流 | 原生 Agent 按需处理；不接 Gemini；业务周期任务暂停；隐藏 Watchdog 仅做基础服务恢复 |
| 当前内容 | Framery One Work，共享专注工作舱；原创 AI 概念示意并标明“非实拍”，不冒充实物演示 |
| 事实来源 | 以[官方公告](https://framery.com/en/framery-launches-one-work/)及已封存研究为准；概念画面不证明实物或实际性能 |
| 内容 ID | content_c4e05b9b597a4697a8844d904c48206b；脚本及素材审核通过不等于新成片合格 |
| 脚本与素材 | S5 批准稿、脚本 QC、计划审核及七图素材 QC 已通过；历史八份登记保留，新封面另登记一份；当前发布使用七图加新封面的八份凭据 |
| 配音 | asset_bb29a0b0469e43cea125ada6dd26f1aa；Kokoro 中文女声 3、原速，32.255667 秒；SHA 5a4954cca1474f69d3b5ac9f3b45254eb163aae0067b2ae836163b75531f09be |
| 新生成配音配置 | CPU双线程、+50%速率 / +0%音量、女声3、远端endpoint为空；本机medium / cpu / int8 ASR保留。上行已发布音轨仍是历史原速文件 |
| 当前公开成片 | asset_6cdefc32359c44f4b04da3b0020d4da6；SHA 3f137051b87a616cfa1c2e533a05916ffcf723feabd3f3f26498630239f1900d；1080×1920、30fps、968帧、32.266667秒、16,499,736字节；历史独立技术预检通过 |
| 10月21日运动候选 | SHA1168d274e8d351977d21e5772234e6979367a92c6b28f39f60fdba96835f351b；1080×1920、60fps、1936帧、32.266667秒、83,447,180字节。无新正式QC/平台发布；不覆盖旧成片 |
| 10月22日官方素材预览 | SHA386f3d5e0e7711708d98f89ff13043d95965010bb1c0c7a8bc6cfe2b3d532fe9；1080×1920、60fps、1188帧、19.8秒、12,016,511字节；13语义字幕覆盖109个严格对齐词。低于默认20秒门槛；不作为正式合格发布片 |
| 正式审核 | 第38代 run_73440fa6f48c7a63ba9744bf1c6b4bd8；任务 agt_f824c55a5f93494fbe0f27b47f8caaa4 完成242页审核，九项全部通过、high、issues=[]，结果已接受并消费；Native 正常退出 |
| 当前封面 | asset_004f4cfe9a0844e69ee23fb2735886d2；SHA 417d52c2e3a33ccf7bd1c23afb5c066f23fa2d8f393a5eac2cc505a6d6da066d；标题“专注空间／怎么借”，标注“AI概念示意·非实拍”；原图、处理与构图凭据已绑定登记 |
| 首个 S5 成片 | asset_b92bfaaf6b6c4d7e8b5daba6f5dc392c；SHA 5be3ff9ccfef454954bc028bf19cd7e18a8a2e6004bb86ab987f1c4502155863；32.266667 秒、968 帧，运动技术检查 FAIL，保留为失败证据 |
| 图片来源补证 | 当前八图逐份核对 Codex 原生完成事件、原生输出与登记原图字节；官方 c2patool 生成器签名和硬绑定通过，时间戳信任提示保留；官方公开条款使用依据另行登记 |
| 发布 | 新文案及八图补证完成；第41代已认可图片使用依据，但因manual误继承Handsfree和平台检查相位冲突为BLOCKED。285b76f修复后第42代为READY/medium、3条info且无blocker，置信度未改；按用户显式授权的operator路径发布成功，真实ID 7698443917590629670；后台已发布、公开页面已核验 |

## 真实发布与归档

以下记录对应10月20日公开版本。现有业务流程拒绝PUBLISHED内容直接重渲染或重复排队发布；10月21日隔离候选没有绕过该限制，也没有继承旧审核。

发布任务 `agt_0eaa0a4d7b0a454582b0093677a836ba` 正常签发和领取一次，attempt=1。实际Chrome账号为“歪果玩意儿”（抖音号43674775675），下发成片和封面SHA与当前版本一致。保存后分别检查竖版3:4及横版4:3的真实预览，标题、主体和“AI概念示意·非实拍”完整；页面的AI自主声明已保存。

平台标题为“办公室里的专注空间，能像会议室一样借用吗？”，正文完整保留厂商来源、尚未交付、AI概念示意及非实拍说明。四个实际话题为“共享办公、办公空间、专注工作、FrameryOneWork”；最后一项因平台空格终止话题而规范化，正文产品全名未删。没有选择平台推荐的其它话题或替换AI封面。

预检截图保存后，登记一次性意图，收到 `AUTHORIZED` 且 `click_authorized=true`，最终发布只点击一次。02:14提交后平台先显示“审核中”，随后显示“已发布”；真实编辑入口的mid与公开页面均为 `7698443917590629670`。只读查看已保存内容后取消，没有提交作品修改。发布截图Artifact `asset_5a208709bd3b4e44a527503cf3bc1c76`，SHA `b94bbd30e5061dbb8a8b20e48d70da3a8517f85cdd502ec73cc14a3cee505bad`；validate返回VALID，submit返回COMPLETED/CONSUMED。发布记录 `pub_720eafc721e94447b8d59717f6bd4e05` 为PUBLISHED。

归档经正常 `ArchiveService.snapshot` 单次更新，Git提交 `080170297e8341054652c4a126a446a26b86e3f3`；元数据SHA `244f3d93ecc6e41a1fe3b64d3b1c211aea08b74350f94c49df7026bfa8cfb324`，包含真实发布记录。归档前后content/task/artifact/publication完整行不变，仅正常新增归档审计和Git快照；没有将MP4或模型加入Git。

## 本轮修复与保全

正常一次 request-rerender 返回 HTTP 200；只取消尚未分发、attempt0 的待审核任务 agt_e6d7f5a8903647f1a9d100f0933b6569。其它旧任务完整行、媒体和 offers 保持原样；被取消任务仅状态及去重键变化。

3c4dca 以 CRF18、禁用两级 mbtree 和 renderer v3 修复推拉尾帧编码停顿，作者80项相关回归通过。7785623 的微修复纠正了未分发 QC 因缺少 ownership 标记被拒绝的问题，37项独立回归通过；若标记存在仍验证。任务隔离、服务器创建审计、未分发状态、输入hash、不可变审核凭据及已完成上游来源检查均保留。

5934c17 新增受控图片使用证据登记：服务端重放已接受素材的原图处理链、核对固定宿主生图事件及原生输出，再用固定官方验证器校验 C2PA；把公开条款使用依据与实际验证报告保存为不可变产物，只加入新审核输入。作者100项相关回归及独立59项回归通过。实际八图登记均 HTTP 200，旧任务、素材、发布意图与完整 scheduler offers 保留。

285b76f 将发布包预检与真实平台检查分开：只有完整且来源匹配的 Handsfree 策略才能启用自动路径，手动包审核不提前要求上传后的页面证据。正式发布仍须先核对实际账号、完整文案、标签、横竖封面与 AI 声明，再登记意图，授权后只点击一次。作者160项、独立86项相关回归通过；旧审核负结果、发布门禁与其它角色指令保留。

正常重渲染、任务替代或恢复不得重写旧输入、结果、attempt、媒体或恢复预算；负结果及其已消费记录继续保留。不得用数据库直改、旧成片、技术预检或脚本审批替代本次正式 QC。

新 revision 必须单独绑定本次视频、素材、音轨、字幕和审核回执；旧版 sidecar 缺失时不能借用旧 QC 文件。未分发任务的 owned marker 只按新门禁的受限条件处理，不能绕过 task quarantine 或来源所有权校验。

配音预检只作渲染前筛查；最终成片须独立识别音轨并核验逐词字幕映射、完整音视频和画面运动。旧成片的通过记录不覆盖新 revision。

修正版实际968帧、961个镜头内相邻对经固定角点全量诊断，7段反向、低步长、跳步、重复ROI、黑边及比例越界均为0；最大段RMSE为0.024247像素，小于原0.04门槛。7份prepared及最终码流均确认CRF18/mbtree0，PTS连续1/30秒，完整音画解码通过。最终混音独立识别121词与15条字幕完整唯一覆盖，保留原标点和2.5数字，最大边界差0.34秒低于0.4秒；这些仍不替代正式九项QC、主观播放及听音。[新成片技术证明](../storage/smoke/stable-production-20261013/framery-script5-motion-repair-independent-technical-20261019.json) SHA ae257930662aa78e47baec3f0625003f6096eeed7e49d617200070baf14c8031。

[播放已通过正式 QC 的修正版](../storage/business-8000/data/content/content_c4e05b9b597a4697a8844d904c48206b/render/revisions/c06a090c5e6c4f15b6cc7c98526f8478/final.mp4)。正式 QC 的音频项采用政策 v2 的独立 ASR／PCM 证据路线，未另作主观听音；浏览器已完整播放至32秒结束，但未据此声称零掉帧。

## 验收与后续发布规则

当前 revision、字幕及完整音视频技术核验和正式成片 QC 已完成；新封面已生成并登记来源及本机适配凭据。第42代发布包审核为 READY/medium，无blocker；medium缘于该辅助角色未重新完整播放听审，依赖精确绑定的正式成片QC和音频证据。手动接口不以辅助审核high作为前置；high是Handsfree专属附加门禁。正式成片QC的PASS/high及全部文件、权利、版本门禁保留，后续发布仍须检查实际抖音页面；本次横竖封面裁切、账号、完整文案、标签与AI声明均已现场完成核验。

当前八图来源由三个独立部分支持：宿主原生 imageGeneration 完成记录与原生输出 SHA、已登记原图到处理图的重放、官方 C2PA 生成器证书与图片硬绑定验证。验证工具固定为 c2patool 0.28.1，EXE SHA 31f1e87c05be3aba9277f4b5fd20148f5f809678196e54b2fc549ef9eee8370d；仅使用官方生成器信任列表和 SDK 官方默认 EKU，禁远程清单及网络抓取。初次缺默认 EKU 的 Invalid 诊断保留，补回官方默认后的八份报告均为 Trusted、failure=[]；仍有 timeStamp.untrusted，不能称时间戳受信任、在线撤销或 CAWG 身份已验证。

图片使用依据引用 [OpenAI Terms of Use](https://openai.com/policies/terms-of-use/)、[Services Agreement](https://openai.com/policies/services-agreement/) 的输出归属及 [Sharing & publication policy](https://openai.com/policies/sharing-publication-policy/) 的一般社交发布条款。生成账号的具体合同类型未独立确认；这些公开条款不是逐图授权书、完整第三方清权证明或法律保证。原始 SDK input.json 字节缺失的历史局限仍保留，旧处理凭据的 false 字段不改写。

发布须领取准确的服务端任务，核对账号及当前视频、封面、标题、正文和标签；点击前登记一次性发布意图。结果不明时只读核对，不重复上传或点击。只有服务端 PUBLISHED、真实外部作品 ID / URL 和证据齐全才算完成；平台审核中须明确标注。

发布由当前可访问已登录 Chrome 的发布角色执行；不得以此前无法访问该浏览器的独立宿主替代。扫码或验证码需要用户操作时保留现场，不代填或绕过。

旧 S4 成片 asset_ca80101f3aef4c639f1bbdfd9ab1b2ef（1080×1920、30fps、808 帧、26.933333 秒；SHA fd0bd0a8c0f0711c45f189a458e09505357ca731e18324383b649b7a79bfb294）只列为“历史 S4”，不是本次新片：[播放历史 S4](../storage/business-8000/data/content/content_c4e05b9b597a4697a8844d904c48206b/render/revisions/5cfc935621134c068df7697ab035b5fe/final.mp4)。

早先未批准候选措辞及音频属于历史诊断，不能作为现行脚本或成片依据。当前批准稿和实际 Kokoro 3 原速配音已正式生成，并由本次新成片独立通过 QC。

## 依赖、权利与运行限制

历史 Edge 请求的实际 provider 未持久化，未知；请求选项不证明实际返回值或使用许可。Kokoro 冻结模型和词典共 392 个文件、427,102,586 字节；清单 SHA 1c800a9e53b68af368827ac9818310d615e3b43649fcfa29fd16c981bc365b32。

Kokoro 冻结包仅增加 sherpa-onnx==1.13.8 与 sherpa-onnx-core==1.13.8；部署使用可选 kokoro 依赖组，通用 uv sync 可能移除它们。按冻结清单核对本机安装，不联网补模型或自动切回 Edge。

模型、词典、eSpeak、ORT 各自遵守组件许可，不统称 Apache。模型发布方声明中文训练数据按 LongMaoData 许可提供；音频使用依据是模型许可和发布方声明的推断，不代表另有声权合同。当前不对外分发运行库；历史词典构建版本和 eSpeak 二进制完整构建证明等限制继续披露。

已登记的九份 AI 原图与处理凭据只覆盖本轮授权，其中一份属于历史封面；当前使用的八份完整绑定。未来封面如发生适配，仍需单独正常登记。处理入口须验证实际接受记录、授权和 prepared 图片，再逐像素重放；不伪造本机处理。业务调度没有恢复；Watchdog 不负责生产、选题或发布。登录、外部服务和 QC 故障仍会正常阻断。

从工作台选择内容并点击“运行一次”推进流程；已有活动 run 或租约时观察同一个 Worker，不重复启动。审批、成片 QC 和发布均走正常签发入口，不直接领取、消费或重置任务。

对使用本机适配的原生 AI 图片，在生产进入 PACKAGED、对应原生运行自然结束后，用受控 `image-use-evidence/{prepared_artifact_id}` 入口补入当前图片的生图项 ID、已登记处理凭据及冻结条款 profile，再正常重建同一封面以签发新发布审核。该入口不自动批准或排队；验证器、配置、原生输出或来源记录缺失会显式阻断，交由 Codex 按需补证。固定工具及条款文件位于生产 data/verification-tools，不提交模型或二进制到代码仓库。

## 证据

| 证据 | 文件 |
|---|---|
| 10月21日运动与码率源码独审 | [14文件冻结、五种运动及独立门禁](../storage/smoke/stable-production-20261013/motion-global-vbv-source-independent-20261021.json) |
| 10月21日真实候选与取舍 | [完整候选156项技术检查](../storage/smoke/stable-production-20261013/motion-production-global-vbv-independent-20261021.json)、[真实图片对照及保留回退](../storage/smoke/stable-production-20261013/motion-real-image-global-vbv-comparison-independent-20261021.json) |
| 10月21日实际源码部署 | [隐藏切换回执](../storage/smoke/stable-production-20261013/motion-sampling-service-cutover-v5-20261021.json)、[切换后八表与媒体保全](../storage/smoke/stable-production-20261013/motion-sampling-cutover-after-v5-global-release-20261021.json)、[独立30项部署验收](../storage/smoke/stable-production-20261013/motion-sampling-deployment-independent-20261021.json) |
| S5 批准脚本与正式配音 | [批准及配音记录](../storage/smoke/stable-production-20261013/framery-script5-approval-and-voice-20261019.json) |
| 原图及处理凭据登记 | [八份登记](../storage/smoke/stable-production-20261013/framery-image-preparation-registration-20261019.json) |
| 新封面原图及处理凭据 | [HTTP 200 登记及旧状态保全](../storage/smoke/stable-production-20261013/framery-script5-new-cover-preparation-registration-20261020.json) |
| 当前正式成片 QC | [九项 PASS 独立回执](../storage/smoke/stable-production-20261013/framery-script5-formal-qc-receipt-motion-repair-independent-20261019.json)、[Native 正常结束](../storage/smoke/stable-production-20261013/framery-script5-formal-qc-native38-lifecycle-independent-20261020.json) |
| 当前成片批准及不可变发布副本 | [正常批准回执](../storage/smoke/stable-production-20261013/framery-script5-motion-repair-final-approval-20261019.json) |
| 图片补证代码与部署 | [独立代码审查](../storage/smoke/stable-production-20261013/image-use-evidence-independent-review-20261020.json)、[隐藏切换回执](../storage/smoke/stable-production-20261013/image-use-evidence-service-cutover-20261020.json) |
| 发布相位修复与部署核对 | [独立代码审查](../storage/smoke/stable-production-20261013/publish-review-phase-independent-review-20261020.json)、[原切换异常记录](../storage/smoke/stable-production-20261013/publish-review-phase-service-cutover-20261020.json)、[仅两运维字段变化的独立复核](../storage/smoke/stable-production-20261013/publish-review-phase-cutover-reconciled-independent-20261020.json) |
| 发布包第42代实际回执 | [独立验收（保留READY/medium，仅manual）](../storage/smoke/stable-production-20261013/framery-script5-package-ready-independent-20261020.json) |
| 实际平台预检 | [账号、完整正文、标签、AI及横竖封面](../storage/smoke/stable-production-20261013/framery-script5-douyin-ui-preflight-20261020.json) |
| 真实作品ID及平台截图 | [ID核验](../storage/smoke/stable-production-20261013/framery-script5-douyin-external-id-verification-20261020.json)、[后台已发布](../storage/smoke/stable-production-20261013/framery-script5-douyin-result-20261020.jpg)、[公开作品页面](../storage/smoke/stable-production-20261013/framery-script5-douyin-public-page-20261020.jpg) |
| 最终真实发布独立验收 | [唯一发布记录、已消费回执、历史保全及服务就绪](../storage/smoke/stable-production-20261013/framery-script5-real-publication-independent-20261020.json)、[工作台PUBLISHED](../storage/smoke/stable-production-20261013/framery-script5-published-workbench-final-20261020.jpg) |
| 发布后归档 | [单次正常归档及保全](../storage/smoke/stable-production-20261013/framery-script5-published-archive-20261020.json) |
| 当前八图真实补证登记 | [八次 HTTP 200 与旧状态保全](../storage/smoke/stable-production-20261013/framery-script5-image-use-registration-20261020.json) |
| 原发布包阻断（保留）与新复审 | [BLOCKED 独立回执](../storage/smoke/stable-production-20261013/framery-script5-package-blocked-independent-20261020.json)、[新审核签发](../storage/smoke/stable-production-20261013/framery-script5-image-use-review-refresh-20261020.json) |
| 代码门禁与真实部署 | [独立门禁](../storage/smoke/stable-production-20261013/pending-issuance-independent-gate-20261019.json)、[切换回执](../storage/smoke/stable-production-20261013/pending-issuance-service-cutover-20261019.json) |
| 推拉尾帧修复门禁 | [独立门禁](../storage/smoke/stable-production-20261013/terminal-motion-independent-gate-20261019.json) |
| 本次正常重渲染请求 | [Retry 1 请求回执](../storage/smoke/stable-production-20261013/framery-script5-motion-rerender-retry1-request-20261019.json) |
| 首个 S5 技术失败 | [完整运动诊断](../storage/smoke/stable-production-20261013/framery-script5-full-motion-failure-diagnostic-20261019.json) |
| 旧 S4 正式 QC（仅历史） | [历史回执](../storage/smoke/stable-production-20261013/framery-subtitle-repair-formal-qc-receipt-independent-20261019.json) |
| 旧发布包负结果（仅历史） | [BLOCKED 回执](../storage/smoke/stable-production-20261013/framery-disclosed-cover-package-blocked-independent-20261019.json) |
| Kokoro 依赖冻结与安装 | [冻结说明](../.artifacts/kokoro-dependency-freeze-20261019/README.md)、[双包安装回执](../storage/smoke/stable-production-20261013/kokoro-frozen-runtime-install-20261019.json) |

最终独验SHA `0fb9e0b869964d8d1d605c4a2998890800fe8436eb2c53be40ace6ff22ac4b6d`。73份旧任务、117份旧Artifact完整行保全，118份当前Artifact文件字节核验，10份历史负面结果及预算不变；API认证200、匿名401、工作台200，进程归属Main且无活动业务任务。公开页面与唯一点击为Root现场操作，独立验收复核已保存截图与服务回执，没有再次发布或跨会话读取浏览器。

## 历史与清理边界

旧 QC、发布包负结果、attempt 和恢复预算均保留；历史负预算不因新 revision 重置。清理自动审批曾以 “blocked by policy” 拒绝旧 Glance、pytest 残留及约 3.09 GB 诊断缓存的永久删除；这些请求未执行，不能记为已清理。

此前 C 盘实测净可用空间增加 1,871,228,928 字节；这是期间净变化，不能全部归因于某次删除。旧工作树与嵌套仓库仅作可恢复归档。pytest-1325 的重解析点仍保留；pytest-1422 至 1424 已不存在但原因未证实。当前服务、项目、生产模型、审核证据和聊天记录保留。

曾提及的“超过 50 GB”是候选逻辑体积与较早自述的混合数值，不是已证实的可删除空间，也不是本次释放量。
