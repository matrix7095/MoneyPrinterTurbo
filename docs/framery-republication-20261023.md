# Framery One Work 修订制作与重新发布

2026-10-25 后续进展见[创意重做与正式生产修复](creative-production-fixes-20261025.md)：已部署公共字幕排版修复，重新生成满足 20 秒门槛的 1.5 倍配音，并找到同型号官方视频。下文保留 10 月 23 日历史状态，不代表新修订已正式发布。

最新状态（2026-10-23）：按用户“少图就生成，一个视频若干张风格一致的图片，语音正常”的要求，已生成六张同风格、不同场景图片，与官方素材制作成九镜头新样片。新片 29.217 秒、1080×1920、60fps，独立内部预览验收通过并记录两处轻微画面限制。配音为完整自然句合成后保持音高加速 1.5 倍；音轨技术检查通过，尚无原生听感验收，不能声称机械感已解决。字幕修复及多图补缺规则已提交并于 21:40 受管部署至 `565e945`，独立现场验收及数据、媒体保全检查通过。当前生产正式脚本 QC 仍待两张官方图的实际许可用途范围与署名条件，尚无新发布作品 ID。

截至 2026-10-23，重新发布所需的业务修订功能已部署，本机效果预览已完成。用户明确表示已持有两张图片的书面许可，并再次要求完成正式质检后重新发布。18:00（北京时间）已通过正常素材补查接口提交这条真实用户声明，并启动本修订的新一轮制作。现有合同不要求先提供许可原件才能发起正式研究；之前将原件核对作为启动前提属于过度限制，已纠正。仍如实注明原件未由我们审阅，正式独立权益检查不省略；本地预览不记为真实发布，许可询问邮件不发送。

## 视觉返工方案与后续执行

本次失败发生在选材和分镜：两张文件来自同一产品视角，预览 helper 把六段固定为 001、001、002、001、001、002；没有原片视频，最长单段 9.6 秒。正式脚本的四项画面要求也只引用这两个素材。推拉、字幕和 60fps 不能补足不同场景及功能展示。

已只读核对现存 Glance 原代码：footage 路线先获取原视频并分析 scenes，计划按 scene_id 和 source_start_sec/source_end_sec 选择原片片段，渲染器裁出对应区间；image_only 才走图片路线。旧图片修复会拆长段、用预算内 AI 请求替代重复 asset_id，但只数资产 ID，不能识别同视角不同文件。MPT 仍保留原片剪辑能力。来源优先 v1 为避免机械凑 4/5 张而制造全 AI 视频，停止旧自动补图；同时图片数量、重复画面及部分节奏要求主要还是 warning，没有可靠拦住视觉信息不足。当前预览尚未经过正式 visual_plan/visual_plan_qc，因此不能用最终正式门禁可能拦住它来辩护这份预览的内容质量。

修正按以下顺序推进：

1. **先选素材路线。** 按同型号官方产品视频、不同视角宣传图、细节图、真实使用图重新盘点。找到可用视频后，在制作模式确定前补入 video_url，使用既有 footage 场景分析与剪辑链；不继续在 image_only 预览模板里强塞视频，也不为此重建系统。
2. **按信息覆盖写分镜。** 为每段旁白明确“观众此时新看到了什么”，绑定实际片段起止或图片资产。整体、桌面细节、实际使用、预约机制等必须有不同的视觉表达；同视角裁切归为一组，不能当作多个真实机位。
3. **按缺口补充。** 官方素材覆盖不了的场景才提出具体缺口，补采、使用清楚标注的示意画面或少量 AI 概念图。无法证明的升降动作和预约 UI 不伪造；若资料不足以支撑卖点，缩减旁白或重新选题，不用同一张图撑满时长。
4. **重写本条节奏。** 约 30 秒先规划 6–8 个有不同信息的镜头作为本条编辑目标，不把数字当作凑图条件。开场兑现具体场景，随后说明产品及一两个差异点，最后收束；删掉没有新增信息的口播。保留用户要求的 1.5 倍语速，并按新音轨重新对齐。
5. **先过分镜再渲染。** 输出带缩略图、时间、旁白、来源和缺口的分镜表，明确检查连续复用、长时间单图、同视角重复、旁白与画面不匹配。重复导致无新信息和关键卖点缺乏画面支撑应返工，不仅记 warning；少量图但确实表达完整的方案不被数量规则误伤。
6. **统一预览与正式路线。** 删除本次固定轮图模板的生产用途，预览使用正式分镜及相同渲染输入。先验收“有内容、能看懂、节奏成立”，再检查帧率、运动、字幕、音频、来源及正式九项 QC，通过后才沿现有重新发布协议执行。

目前保留原有素材优先原则与已经验证的运动、字幕修复；不直接恢复旧版盲目凑图逻辑，不部署或发布被退回的样片。

用户补充要求后已实际生成六张新图：开放办公分心、坐姿专注、站姿办公、进入共享空间、离开并接力使用、书写工作细节。统一人物、服装、暖白／深灰／浅木色、自然日光与写实摄影风格。生成图只用于明确标注的通用办公使用场景概念示意，不冒充精确的 Framery 产品结构、真实功能演示或预约 UI；官方原图负责产品身份和真实规格。

原生输出均为 941×1672。按本线程已有本机放大授权，以 Pillow LANCZOS 居中适配为 1080×1920，保留六份原生文件、逐图提示词、原图与处理图 SHA，以及[适配凭据](../storage/smoke/source-plus-ai-recut-20261023/generated/adaptation-receipt.json)。凭据 SHA：`889b897c4debd0a47295848dfe7512e0ade8d868e02145e09d70c95abbab3dc4`。[六图独立视觉审核](../storage/smoke/source-plus-ai-recut-20261023/review/six-ai-images-independent.json)已通过，未见必须返工的明显畸形，人物与视觉风格一致、六种场景可区分；该结论要求保留概念示意标注，不代替正式 ASSET_QC。

制作规则两文件修改也已独立 APPROVE，74 项来源及提示上下文测试通过：新脚本明确缺口可用 `preferred_source=ai_generate`；image_only 在预算内主动申请多张同风格补图；不同关键信息段缺乏视觉信息、持续无意义复用升为 error。图片数量单独不足仍只是 warning，同一语义回扣和片尾产品重现允许。仍由 Agent 结合实际素材判断语义，不伪称新增了确定性的视觉理解算法或自动回填既有任务。

按原音轨实际 ASR 词时码确定九镜头，依次为 0–2.62 开放办公、2.62–6.16 官方整体、6.16–9.78 官方桌面细节与规格、9.78–11.30 坐姿、11.30–13.40 站姿、13.40–18.22 进入共享空间、18.22–22.30 接力使用、22.30–26.16 工作细节、26.16–29.209958 官方整体收束。最长单段 4.82 秒；新 helper 已读取独立分镜数据并调用现有正式渲染器，不再硬编码两图轮换。

## 最新九镜头效果预览

- [播放九镜头新视频](../storage/smoke/source-plus-ai-recut-20261023/rendered/render/revisions/c472149f8751469d9a941ec5f3000f2b/final.mp4)：29.216667 秒，1080×1920、60fps，共 1,753 帧，63,483,279 字节。SHA-256：`fdc61cf6b90442cc58fa1a9a9a1e9fd9ba9f905cbec0faa1ec85da81394662e3`。
- 六张 AI 使用场景图全部入片，搭配官方产品整体及桌面细节；人物、服装、用色、光线统一，内容包括分心、坐姿、站姿、进出共享空间和工作细节。两张官方文件仍属于同一产品视角，未把裁切冒充新机位。
- 配音 29.209958 秒，维持已要求的 1.5 倍语速；14 个自然句字幕，最短显示 0.72 秒。全片音画解码通过，逐帧时间戳连续且无重复，音画尾差约 6.71ms；这些是技术检查，不能单独证明主观听感或推拉观感。
- [九镜头画面总览](../storage/smoke/source-plus-ai-recut-20261023/rendered/contactsheet.jpg)、[实际分镜](../storage/smoke/source-plus-ai-recut-20261023/storyboard.json)、[渲染与自检回执](../storage/smoke/source-plus-ai-recut-20261023/rendered/result.json)。本片暂为内部效果预览，不冒充正式九项 QC 或已发布作品。
- [独立九镜头验收](../storage/smoke/source-plus-ai-recut-20261023/review/nine-shot-preview-independent.json)：六项检查通过，逐镜头从实际最终 MP4 抽帧并查看，六图均入片、风格连贯、标注和字幕正确。报告 SHA：`81b6a98b17d8b8e2cea1357fbcd967b3fbb6ebf0000cbbe1827a0d345026d972`。保留两处轻微限制：第 3 镜官方桌面局部放大偏软，第 9 镜产品右外框轻微出画；听感标记为 UNVERIFIED。

## 被退回的单视角效果预览

- [播放 29.217 秒新版视频](../storage/smoke/formal-script-preview-20261023/rendered/render/revisions/f6a31eb4934741509360f5b7cb0f0c04/final.mp4)：1080×1920、60fps、1,753 帧，36,789,403 字节。视频 SHA-256：`51d2a4f2c8705248a5b04ccef8cb28645324e7a8a17a573acae9b19b9c6726a1`。
- 逐字使用正式脚本任务 attempt 2 的完整旁白，Kokoro 女声 3 原速生成后保留音高加速 1.5 倍。音轨 29.209958 秒；字幕及六段画面的时轴均来自这份音轨的新识别，不沿用旧样片时轴。
- 两张官方宣传图、AI 图片 0 张，依旧只有一个产品视角；推拉与不同裁切增加节奏，但不构成真实升降、预约操作演示或多个实拍机位。当前画面丰富度仍有限。
- fresh medium 识别后，现有本机 large-v3 按 PCM 静音选取的两半完整重识别，保留全部原始证据；唯一“它／他”字形差异有精确位置及时间记录。独立 canary 验收 10 项通过，非 ta 同音字仍拒绝。成片的字幕显示时长、逐帧运动及完整音画独立验收另行记录，不以此代替正式九项 QC 或主观听回。
- [渲染回执](../storage/smoke/formal-script-preview-20261023/rendered/result.json)、[画面总览](../storage/smoke/formal-script-preview-20261023/rendered/contact-sheet.png)、[真实音轨对齐回执](../storage/smoke/formal-script-preview-20261023/canary-subtitle-v2-20261023/result.json)、[独立 canary 验收](../storage/smoke/subtitle-alignment-acceptance-20261023/canary-independent.json)。渲染器和额外全流解码均 exit 0。

### 此前 21.367 秒样片（保留历史证据）

- [播放新版视频](../storage/smoke/preview-complete-20261023/rendered/render/revisions/4bf243c4b1254790aef8ce8cfaf0e6de/final.mp4)：21.367 秒，1080×1920、60fps，共 1,282 帧；没有通过补静音或重复结尾凑时长。
- 使用两份 Framery 官方宣传图片，未新增或使用 AI 生图；两图属于同一产品视角的不同构图，并非七个实拍场景。七段镜头配有连贯推拉和平移，电动升降与预约功能是文字说明，不伪装成动作实拍。
- 真实 Kokoro 女声 3，按自然句原速合成后用 `atempo=1.5` 保留音高加速。音频 21.364 秒，字幕由新的 `medium/cpu/int8` 识别结果对齐，共 16 个字幕段。
- 第一版新增旁白的“工作舱”被识别成“工作场”；保留失败音轨与原始识别记录，改用自然同义表达“共享的专注空间”后重新生成并通过严格识别比对，没有改写识别结果或放宽阈值。
- 标题卡中电动桌的 68–118 cm 范围及全尺寸描述来自[厂商公告](https://framery.com/en/framery-launches-one-work/)，并标明厂商规格。当前视频尚不是正式成片审批或公开发布结果。
- 视频 SHA-256：`e7ad115c2a884df6b74c905b5d88af9d9f12c3a1e92e93d238fa7d546a8c37e6`。[渲染回执](../storage/smoke/preview-complete-20261023/rendered/result.json)与[画面总览](../storage/smoke/preview-complete-20261023/contact-sheet.jpg)已保存。
- [独立预览验收](../storage/smoke/preview-complete-acceptance-20261023/acceptance-final.json)的 18 项检查通过：1,282 帧时间戳及音频包连续，完整音画解码通过；16 段字幕完整覆盖 122 个词，最短显示 0.76 秒。七段动态镜头逐帧测量没有超过 1px 的骤跳，最大锚点位移为 0.257px，音画尾差约 2.54ms。
- 已逐章查看画面。第 3、6 章推拉会轻微裁掉舱体右侧外框，桌、椅、屏幕保留；本轮效果预览接受并记录该限制，不能声称产品外框全程完整。独立检查未作原生听回，不把 ASR 通过等同于主观声音自然度通过，也不替代正式业务 QC。

制作仅新增本机预览与验收资料，未改变生产数据库或审批状态。收尾产生的 4 个零字节临时文件现已全部删除并逐个确认不存在。原记录将执行前的 `blocked by policy` 称为“自动审批拒绝”，证据不足，已更正；[清理核查与后续约定](local-cleanup-20261023.md)记录了成功命令和判断边界。

## 后续定期生产

可按 Glance 的业务闭环继续接通：定期采集热门众筹、去重和选题、事实与官方素材研究、脚本和视频制作、独立质检、抖音发布及外部作品链接回填。定期触发与具体任务执行分开，由触发入口启动 Codex Worker，不依赖 Gemini。

当前业务周期调度仍关闭，用户本次询问后续能力没有被解释成马上启用定时发布。正式无人值守验收还需证明当前版本连续完成真实生产与发布；下文记录的历史采集通道唯一键冲突也需在恢复稳定采集前处理。失败项目应按既有规则返工或跳过，不能为了发布而降低素材和成片质量要求。

## 制作要求

- 以 Framery One Work 同型号的官方宣传视频、图片为优先，AI 图片仅补明确的信息缺口。
- Kokoro 中文配音按自然句原速合成后保留音高加速至 1.5 倍；实际声音、字幕和节奏另行审核。
- 图片镜头使用真实 60fps 的现有渲染器。不能用帧率参数替代画面连续性审核。
- 新片重新完成研究、脚本、配音、计划、素材和九项正式成片质检，再审核发布包。
- 旧任务、媒体、审核与作品记录保留；新发布必须有不同的真实作品 ID。

## 已完成部署

本次字幕和多图规则以 `36a2764bc57eaae7f372ec42a7840b709868a4f2` 提交；第一次启动因受管脚本白名单缺少 `subtitle_alignment_fallback_model_dir` 被配置校验拒绝。已立即恢复原配置和服务，保留失败回执及恢复前后保全结果，没有将这次失败称为策略拦截或部署成功。

启动器遗漏随后以 `565e945d413c9ef1a7b504a4b513a56bcd75248a` 修复。独立 70 项测试通过，并使用 Main 实际解释器和真实 proposed 配置验证白名单、严格类型/本机目录检查、环境传递与 Settings。启动检查只查看必要文件的存在与非空，不每分钟读取约 3GB 模型；实际字幕运行仍保留完整 SHA 校验。未知/私有配置键继续拒绝。

21:40 重试受管部署成功：8000 worker 为 14016、launcher 为 23364；8510 工作台仍为 Main 的 12048。唯一新增配置是本机 `subtitle_alignment_fallback_model_dir`，主识别与最终声音 QC 的 `medium/cpu/int8` 不变；业务周期调度保持关闭，watchdog 已恢复。新一致性备份通过 SQLite quick_check，部署前后 22 张表、52,660 行及 1,296 件已登记媒体的观察结果一致，目标 120 件素材全部通过；117 件其他内容的历史媒体问题仍保留原状。

[重试部署回执](../storage/smoke/published-revision-20261022/subtitle-alignment-retry-deployment-20261023.json)、[重试部署数据保全](../storage/smoke/published-revision-20261022/subtitle-alignment-retry-after-deployment-20261023.json)、[首次启动失败及恢复记录](../storage/smoke/published-revision-20261022/subtitle-alignment-start-failure-recovery-20261023.json)。此前历史部署记录如下。

[独立现场验收](../storage/smoke/subtitle-alignment-acceptance-20261023/live-deployment-independent.json)已通过：重新核对九文件、实际进程链及监听、接口与工作台健康、鉴权、配置、业务周期调度关闭，以及 watchdog 实际执行成功；独立重算数据库比较，并读取全部约 5.707GB 已登记媒体核对哈希。仅两个既有维护游标按原允许范围更新，无新业务任务或媒体损坏；历史 117 项媒体问题与部署前相同。

独立修订协议先以 `76019bd5ce3507dec25de12bd71f173e46e3d0d0` 部署；来源补查入口随后以 `34d918998ee8e12d58a309be686c60edd76bcf1a` 提交、推送到两个远端分支并受管部署。该次部署时 API worker 为 9180，launcher 为 19132，8510 工作台进程为 12048，均来自 Main；工作台重新加载后显示 `WAITING_AGENT / short_script`。这些是历史部署快照，最新进程和业务状态见下文。watchdog 只作服务恢复，业务周期调度仍关闭。

最初三条漂移均为 `SKIPPED / DIRECTION_BUSY`。隔夜新增 90 条记录逐条核实为 86 条同类跳过和 4 条既有采集通道唯一键冲突失败；后者不属于本次迁移，尚未修复。来源配置、授权字段和旧业务数据没有被这些采集尝试改写。

停服期间完成一致性私密备份，SQLite 检查通过。迁移前后 52,300 条旧记录按原列比较通过；1,294 件已登记媒体的观察结果保持一致，目标内容的 118 件素材全部通过实际大小及 SHA 校验。另有 117 件其他内容的历史媒体不一致问题，保留原状，不计为本次修复成果。

独立部署验收 59 项全部通过，覆盖 24 份冻结文件、可空字段及索引、旧数据保全、鉴权、服务与工作台归属、配音配置。

来源补查入口支持收到新素材或许可后，经正常研究流程继续当前修订，不覆写已完成研究。仅支持有独立目录的业务修订、未领取的脚本待办，保留旧素材、发布记录和运行预算，并校验幂等及并发边界。新增 46 项测试，邻近组合 127 项通过，1 项不涉及本次修改的真实渲染测试未重复运行；独立审查 APPROVE，另跑 8 项关键用例通过。

第二次受管部署的独立验收 42 项全部通过；22 张表的 52,408 条记录及 1,294 项媒体观察部署前后完全一致，目标 118 项媒体仍全部有效。重启前私密 SQLite 备份通过完整性检查，服务鉴权、新接口注册、配音与调度配置均验证通过。当时尚未调用补查接口；后续纳入用户已有书面许可的真实声明，补查与制作进展见下文。

18:28 完成第三次受管部署，代码提交 `8336558b079929f50f604e9d000e1a80109c0d64`。正式脚本曾因任务包缺少官方图的资产 ID 与图片文件返回 `CONTEXT_MISSING`；修复后，脚本、脚本质检和审核辅助均从已接受并消费的研究收据派生精确 URL→资产映射，校验当前修订、文件大小及 SHA，并传递实际图片。原冻结输入、失败记录和研究结果保留。200 项组合测试通过，独立代码审查 APPROVE。

本次部署前后 22 张表的 52,507 条记录及 1,296 项媒体观察完全一致；目标内容 120 项媒体全部有效。私密一致性备份通过 `quick_check`。API 新 worker 为 21028、launcher 为 6944，工作台进程不变，watchdog 已恢复。新的按需运行 generation 45 接续原待重试脚本。

第三次部署独立验收六项通过：代码冻结、设置、部署快照复算、进程归属、API 鉴权与 UI 健康、watchdog 恢复。代码已推送至 Main 和 Stable 两个远端分支。正式脚本任务已在 attempt 2 完成并消费，四段画面全部引用上述两张官方图，未请求 AI 图片。独立脚本质检随后仅返回 `rights` 问题：认可用户持有许可的声明及准确素材身份，但研究结果仍记载具体许可范围与署名要求未知，尚未通过。当前没有强制上传原件的规则；需要补充对应实际用途的许可内容，再走正常研究、脚本和质检流程。

19:03 部署许可补证入口，提交 `35e0201d8227297f67471425f37659b8267e725f`。173 项组合测试、46 项独立测试与独立代码审查通过。受管部署前后 22 表的 52,650 条记录、1,296 项媒体观察保持一致；目标 120 项媒体有效。API 新 worker 6428、launcher 8116，UI 12048/8510 不变。设置未改、watchdog 正常，六项独立部署验收通过。

该入口只处理当前已排空的 manual 修订，以及确切的 rights-only 脚本质检失败和被自动路由范围限制阻断的恢复任务。管理员 `GET /api/v1/contents/{id}/rights-evidence/preflight` 使用 `expected_revision_id`、`research_task_id`、`qc_task_id`、`recovery_task_id` 验证并返回绑定；当前真实内容预检已返回 200。`POST /rights-evidence` 还要求幂等请求键、三个结果哈希及真实新增 `evidence_text`。请求会生成待独立核验的新研究，不修改权益标签或旧审核结果，也不自动发布。研究→新脚本→新质检均保留原恢复根和预算，真实集成测试证明轮次递增至 2、3，耗尽后拒绝继续。未收到新增许可内容，因此尚未实际提交 POST。

### 新脚本的内部音轨检查

为提前发现音画问题，已在隔离输出目录按新脚本生成真实 Kokoro 女声 3 音轨：原速合成后保留音高加速 1.5 倍，音频 29.209958 秒，原脚本与生产数据未变。`medium/cpu/int8` 原始识别与脚本有“想/像、它/他、长/涨”三处字形差异；现有渲染前字幕对齐要求去标点后逐字相等，因此拒绝，不能仅据此断言实际错读。

本机已有 large-v3 模型的无提示词离线复核纠正了前三处字形，但漏掉结尾问句；关闭 VAD 后结果相同。尾段独立识别能找回完整问句；整轨按全部静音点切成 21 段则产生更多转写差异，仍被原校验拒绝，没有复用局部成功结果伪造整轨通过。

最后一组对照仅依据 PCM 数值选取最靠近音轨中点的真实静音区间中心：14.450–14.580 秒的中心 14.515 秒。两个连续半轨从头重新识别后，180 字全文及尾句完整，仅 2.62–2.82 秒把“它”写成同音的“他”；160 个词的全局时码经原约束检查没有异常。原 v1 字形校验仍如实失败，没有改写识别或生成绕过校验的字幕。

独立设计审查认可有限修正：仅新版本字幕凭据允许“他／她／它”逐位置等价，并记录原字、目标字、位置和时码；其他错字、缺漏、数字及 ASR 内部词段不一致继续拒绝，历史 v1 保持原语义。字幕专用的本机备用模型路径默认为空；必要时只做一次由 PCM 决定的完整两半重识别。最终成片音频 QC 的原 medium 配置与独立证据不变。

20:13 实现通过独立复审：160 项作者组合测试、113 项独立测试通过，五文件静态检查通过。修复同时保留后端异常原因，并对每个原始 ASR segment 的文字及词记录逐段校验，防止跨段挪字后总串恰好相等。新版冻结 SHA 为 `9e517748ce190fa61ee38eff40e52990a6d0de95bed1683b497c76cd17092a3a`。随后已用精确冻结源码重新运行完整真实音轨，独立 canary 的 10 项检查通过；九镜头样片沿用这份经过校验的完整识别凭据，未反复重跑或改写识别记录。此验收不等同于正式声音 QC 或主观听感通过。

## 当前修订

- 内容：`content_c4e05b9b597a4697a8844d904c48206b`
- 新修订：`rev_8271eb3c803e404188ae39f24d64febf`
- 初始制作运行：`run_553fb0f62bc65e604b5cb85b2da56304`，generation 43；当前最新运行是下文的 generation 45。
- 修订创建、prepare、execute 均由正常管理接口返回 HTTP 200；未直接编辑生产数据库。
- 正式研究任务 `agt_9eedc16ea61f46a08be66ff25bc4391d` 已完成、接收并消费。结果确认同型号图片存在，但因许可不足没有放入可制作素材清单。
- 为防止退回全 AI 图，Root 经正常 run/stop 请求排空；运行已 `stopped`，宿主正常 `FINISHED / exit 0`，待写脚本任务仍为 attempt 0。没有修改已完成研究或强制结束进程。
- 历史作品 ID：`7698443917590629670`，仅作历史引用。最近创作者中心检查显示匹配旧作品为“私密”，不据本地 PUBLISHED 状态声称当前公开。
- 18:00 已补交 `user-attestation:framery-user-attested-two-images-20261023-v1`：记录用户原话、当时提问的使用场景、两张精确图片 URL/SHA 及“原件未审阅”的限制，没有虚构授权条款。补查接口返回 HTTP 200，新研究为 `agt_194e133533c1458094e6a64e4e365d4f`；旧未执行脚本按协议取消，旧研究保留。
- 新按需运行 `run_60b1bc74a7fce8c983607075d72e21f5`（generation 44）启动成功，18:02 确认宿主及研究 Worker 运行、心跳正常。它接续本修订的正式链路，不复用本机预览的审核结论。
- 补查研究 `agt_194e133533c1458094e6a64e4e365d4f` 已正式完成并消费，登记两张与用户声明 URL/SHA 完全一致的官方图。generation 44 在脚本上下文缺失后正常排空，部署修复后以 `run_cf40f1d3231e22f1257006569d74b79f`（generation 45）继续原脚本 `agt_7a3ca289c8d248c792da8d0f88e00273`，不改写旧失败历史。
- generation 45 已排空，宿主 `FINISHED / exit 0`，无活动任务。脚本 attempt 2 完成；`agt_8f7f9ee02b5f4e5d88f2e7785f013532` 的正式脚本 QC 因使用范围未核实而失败，恢复任务的自动研究路由不适用于 manual 内容，当前为 `SCRIPT_REVIEW / recovery_blocked`。这不是清理策略再次拦截，也不是服务未运行；后续使用已部署的正常补证入口处理真实新材料。

## 尚待完成

正式新视频、九项成片质检、发布包及新作品 ID 尚未完成。最新完整本地预览为 29.217 秒九镜头版，包含六张新生成图及官方图，使用正式 attempt 2 旁白；它已替代被退回的单视角样片，但不能替代本修订的正式审核。两张官方素材的使用依据已包含用户提供的书面许可持有声明，当前正式 QC 仍缺实际用途范围与署名条件；补证问题已提出，不再重复索要发布授权，也不将“生成多图”的新制作要求伪写成许可信息。

此前公开渠道未查到两张 2026 年官网产品图的转载许可，2024 年媒体包也不能证明这两张图片的授权。后续用户声明持有书面许可是新增的私人证据来源，应与此前公开检索结果区分；我们没有审阅原件，也不推定未披露的具体条款。联系品牌的邮件草稿未发送。正式发布仍须通过实际素材权利门禁。

为寻找用户已有材料，补充检索了已连接 Gmail 的 Framery 邮件、Google Drive 的 Framery／One Work 文件，以及本机 Documents、Downloads、Desktop 中相关名称；没有找到可核对条款的许可文件。这些有限关键词检索不证明许可不存在，也未改变用户声明或生产权益记录。

10 月 23 日追加核查区域官方媒体页、官方 LinkedIn、投资者新闻分发，以及 Mynewsdesk、PRNewswire、BusinessWire、Cision、Pressloft、Vimeo、YouTube 等渠道。找到准确型号的官方发布视频线索，但没有找到适用于跨平台转载、裁切和字幕的许可凭据；该检索结果不等于断言所有渠道都不存在许可。

| 交付项 | 当前结果 |
| --- | --- |
| 采集记录核对、代码提交推送、受管部署 | 已完成 |
| 当前修订正式研究 | 补查已完成并消费，两张精确官方图已登记 |
| 官方素材许可 | 用户声明已提交；正式 QC 仍待具体用途覆盖范围与署名条件 |
| 正式新脚本 | attempt 2 已完成并消费，四段画面均使用两张官方图 |
| 新正式成片、九项成片质检、发布包 | 尚未完成 |
| 本次新作品链接 | 尚无；不能用旧作品链接替代 |

下一步是在收到实际许可范围和署名条件后，提交新证据，经新的研究及脚本 QC 继续正式制作；此前的失败结论保留。保留 1.5 倍配音、60fps、20 秒最低时长及官方素材优先要求，不以放宽门槛解决阻塞。对外许可询问邮件没有发送；本次使用依据是忠实记录的用户声明及其上下文，不能记成我们取得了品牌回函。

## 证据

- [用户许可声明补查回执](../storage/smoke/framery-formal-republish-20261023/refresh-receipt.json)
- [本修订 generation 44 启动回执](../storage/smoke/framery-formal-republish-20261023/execute-receipt.json)
- [素材传递修复部署回执](../storage/smoke/published-revision-20261022/script-source-mapping-deployment-20261023.json)
- [素材传递修复部署前后保护比较](../storage/smoke/published-revision-20261022/script-source-mapping-after-deployment-20261023.json)
- [本修订 generation 45 恢复回执](../storage/smoke/framery-formal-republish-20261023/retry-script-receipt.json)
- [素材传递修复独立部署验收](../storage/smoke/framery-formal-republish-20261023/deployment-independent.json)
- [本修订正式新脚本](../storage/business-8000/data/agent-submissions/agt_7a3ca289c8d248c792da8d0f88e00273/2/result.json)
- [本修订脚本质检结果](../storage/business-8000/data/agent-submissions/agt_8f7f9ee02b5f4e5d88f2e7785f013532/1/result.json)
- [许可补证入口部署回执](../storage/smoke/published-revision-20261022/rights-evidence-deployment-20261023.json)
- [许可补证入口独立部署验收](../storage/smoke/framery-formal-republish-20261023/rights-evidence-deployment-independent.json)
- [当前真实内容的许可补证预检](../storage/smoke/framery-formal-republish-20261023/rights-evidence-preflight.json)
- [新脚本实际音轨及首次对齐失败](../storage/smoke/formal-script-preview-20261023/voice-attempt2-run-02/result.json)
- [同音轨 large-v3 独立识别结果](../storage/smoke/formal-script-preview-20261023/asr-large-v3-20261023/result.json)
- [关闭 VAD 的对照结果](../storage/smoke/formal-script-preview-20261023/asr-large-v3-vad-off-20261023/result.json)
- [21 段全轨重新识别的失败结果](../storage/smoke/formal-script-preview-20261023/asr-large-v3-clips-20261023/result.json)
- [两半轨重新识别的原始结果](../storage/smoke/formal-script-preview-20261023/asr-large-v3-two-half-20261023/result.json)
- [已有许可文件的有限检索](../storage/smoke/framery-formal-republish-20261023/permission-locations-search-20261023.json)
- [隔夜采集漂移逐项核对](../storage/smoke/published-revision-20261022/preservation-static-deployment-reconciliation-reviewed-independent-20261023.json)
- [受管部署回执](../storage/smoke/published-revision-20261022/deployment-20261023.json)
- [迁移后旧数据与媒体保全](../storage/smoke/published-revision-20261022/preservation-after-migration-20261023.json)
- [59 项独立部署验收](../storage/smoke/published-revision-20261022/deployment-independent-acceptance-20261023.json)
- [来源补查受管部署回执](../storage/smoke/published-revision-20261022/source-refresh-deployment-20261023.json)
- [来源补查部署前后数据保全](../storage/smoke/published-revision-20261022/source-refresh-after-deployment-20261023.json)
- [42 项独立部署验收](../storage/smoke/published-revision-20261022/source-refresh-deployment-independent-20261023.json)
- [新修订创建回执](../storage/smoke/published-revision-20261022/formal-revision-create-20261023.json)
- [按需制作启动回执](../storage/smoke/published-revision-20261022/formal-revision-execute-20261023.json)
- [本次正式研究结果](../storage/business-8000/data/agent-submissions/agt_9eedc16ea61f46a08be66ff25bc4391d/1/result.json)
- [防止全 AI 分支的正常排空请求](../storage/smoke/published-revision-20261022/formal-revision-official-route-drain-20261023.json)
- [官方素材使用依据调查](../storage/smoke/source-first-recut-20261022/rights-research-20261022.json)
- [新增官方渠道许可调查](../storage/smoke/source-first-recut-20261022/alternate-official-rights-search-20261023.json)
- [用户确认已有书面许可](../storage/smoke/source-first-recut-20261022/permission-user-confirmation-20261023.json)
- [尚未发送的素材许可询问邮件](../storage/smoke/source-first-recut-20261022/framery-image-permission-request-20261022.txt)
- [已发布内容修订操作协议](published-revisions.md)
