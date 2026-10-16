# MoneyPrinterTurbo 稳定生产与运维记录

更新日期：2026-10-17。本文记录当前验收状态；历史检查点另存独立文件，旧调度恢复要求不再适用于按需生产。定时任务不是生产前提；内部按序派发和故障守护不承担周期选题或周期生产触发。

当前结论：尚未完成稳定生产验收。2f7be13组合修复已在10月17日00:18加载到主目录后台，新API监听PID12376、launcher22512；界面仍为主目录PID24868。授权API200、匿名401、界面健康200；七角色启动器/清单/Skill哈希一致，业务调度与自动选题仍关闭。升级前后3015个历史任务的输入、结果、指令及尝试预算哈希全部一致。新成片技术验收通过，但正式CONTENT_QC及真实发布仍未完成。

当前按需运行：10月17日00:19通过正常execute入口仅启动一次run_5676c3e0d4b33935b790d0f78c0131be，generation3，有效期至02:19，实测窗口120分钟。原生父线程01a14582-9a0b-7ec2-a603-cae7f592baf1，正式QC子线程01a14582-f230-7143-ae8d-8debc315ab24，Sol/high/default/ephemeral；同一QC任务正常确认第2次尝试（2/3），原第1次未重置。新强确认回执及连续三次宿主业务续租已真实核对，绑定当前claim/lease/输入/attempt，审计主体明确为mpt-content-host，非伪造Worker心跳；仍无语义审核结果。证据deployed-native-liveness-20261017.json、aurzen-run3-start-20261017.json、run3-host-business-lease-renewal-20261017.json。

第二轮运行复核：10月16日22:45:35，第二次正式QC的30分钟业务租约到期，Core正常回收为PENDING，attempt仍1/max3，结果为空。最后业务heartbeat为22:15:20；原生宿主虽每30秒上报存活，原实现只更新run观测，不续业务租约，长模型推理及断流重试暴露了此缺陷。22:48:41已通过正常execution/stop停止新增派发，原run有效期22:48:53保持，原生child随后于23:34:40自然终态，运行现为stopped、执行记录FINISHED/exit0、host_live=false，旧三个进程均已退出；没有强杀、恢复过期租约或重置尝试次数。尚无正式CONTENT_QC通过或发布结果，不能把191次imageView事件计为完成审核。证据 `native-qc-business-lease-expiry-20261016.json`、`run2-lease-loss-cooperative-stop-20261016.json`。后续有效租约的宿主续租修复d953775已提交并经独立审查，完整回归1509项已通过；待安全部署后，再从正常入口续接剩余尝试。

10月16日独立重渲染技术验收18项PASS：真实新视频为 `asset_d8ece8e213614f2b9c43fa66bd3cdd4c`（`asset_8d57c284b88942b7a19a1b9901774c6d` 是验收输入凭据），SHA256 `75a83899a2e1e66d38baa5a857897fa292393653365b4dd7274e66cf1a3f579b`。新鲜ffprobe核实1080帧、36秒、1080×1920、30fps、首PTS0；1080连续帧及270页原校验通过，脚本、研究、8图片和完整原旁白链保持，新音频35.976秒。旧final仍保留。安全证据 `release-render-20261016-independent.json`，SHA256 `bf6a8d3d9a42093c1a816fd107f6723a79523532518fe8a4edb8072f5b4b4e4d`；正式CONTENT_QC和发布仍须分别取得结果。

## 当前操作方式与部署证据

本系统按需工作：在工作台选择内容，点击“运行一次”，从当前阶段继续到制作、独立审核和发布，完成后结束。无需创建或恢复业务定时任务。若本次新AI概念图片需要本机适配，可明确勾选仅本次授权；默认不勾选，原图、处理记录、冻结最低尺寸及独立审核仍保留。运行中通过“刷新运行状态”查看；需要收尾时使用“停止新增工作”，等待已有Worker结束，不重复启动。

日常使用入口为本机 [业务工作台](http://127.0.0.1:8510/)。先选择准备生产的内容，确认本次授权，再点击“运行一次”。同一内容有活动运行时不要另开一次。只有正常服务的发布记录为 `PUBLISHED`，并取得与本次成片对应的平台作品ID、链接及截图证据，才算发布完成；`FINISHED`、Worker完成、上传完成或技术QC通过都不能单独证明发布成功。若遇登录失效、平台验证码、审核拒绝或容量不足，保留现场和原失败记录，修复条件后从正常入口续接。

故障守护仅负责服务异常恢复，可保持启用；它不会定时选题或启动视频生产。2026-10-15核对应用自动化：`mpt-codex-scheduler`（ID `glance-codex-scheduler`）为PAUSED；既有 `mpt-ops-maintenance` 为ACTIVE，每日仅巡检和受限可用性恢复，不执行scheduler-plan、不创建生产Worker、不审批或发布。后续独立生产也不自动继承本轮图片放大授权，原生工具无法满足冻结最低尺寸时仍须具备该次明确授权。

2026-10-15 15:53（Asia/Shanghai）核验：主目录API PID28492、界面PID25264分别监听8000和8510；授权内容API200、匿名401、界面健康200。运行时app、agent、collector、core、media全部来自主目录。七个角色使用同一新不可变stage `production-stable-20261015-40227d4-8000`，全部VERIFIED且findings为空；discovery为Luna/max，其余六角色Sol/high，容量2；未注册未经验证的原生视频角色。新stage仅连接本机127.0.0.1:8000，业务公开地址仍为10.10.10.200:8000。`scheduler.enabled=false`、`handsfree_auto_topic_enabled=false`，自动审核及授权发布保留。MPT Runtime Watchdog已恢复启用、最近结果0，旧Glance守护Disabled；守护不触发生产。安全部署证明 `storage/smoke/stable-production-20261013/deployed-state-after-40227d4-20261015.json`。

16:00独立部署验收20项全部通过：实际进程与启动链、七角色真实部署审计和launcher/manifest/Skill哈希、版本模型、3010旧任务、原QC/Recovery全字段及预算、原成片哈希、唯一恢复消费和冻结源均一致。证据 `release-40227d4-independent-verification.json`，SHA256 `52f6930c1816a6ee9be0ac54209cb17091eee82d9e07049ce33306330191ae63`。结论为部署PASS、总目标PARTIAL；该检查没有把未执行的重制、正式QC或发布列为通过。

实际只读数据库核验：原3010个任务的输入、结果与指令哈希全部保持，当前任务总数3014。本轮正常生图修复任务 `agt_5b1689d5c7bb494e89361d85a865e8a2` 含8个请求，已COMPLETED/attempt1；旧失败任务保持不变，没有重置预算。修复证明 `aurzen-manual-repair-proof.json` 记录最初正常签发时的状态。部署前新数据库一致性备份 `private/pre-final-fixes-20261015-40227d4.db` 的quick_check为ok；同目录private仅当前用户与SYSTEM可访问，不公开凭据。

本次按需运行 `run_91f166dd8a353358a4fc5973d70862da` 由正式API启动，实际原生子线程身份为Sol/high/default/ephemeral，父子关系已验证。8张图片提交已ACCEPTED并正常消费，独立ASSET_QC通过（passed=true、issues=[]），随后自动生成1080×1920/30fps新成片及ASR/PCM音频证据、1081连续帧和271页审核图。新final SHA256 `f307d55c92785204de86e0a9a9317420b4de975f25cd4fbb9b27f22b081fe710`；技术QC通过不能替代正式内容审核。

正式schema3 CONTENT_QC任务 `agt_25a22284dea6475181b22015b819d977` 的输入校验失败：`ENVIRONMENT_MISSING_INPUT / visual_evidence: VISUAL_EVIDENCE_PLAN_END`。只读对真实manifest复现：批准计划35.976秒，最终视频36.033333秒，差0.057333大于校验上限max(末帧0.033333秒,0.05秒)。进一步量化发现逐段再次按WAV时长生成场景，随后按含AAC的MP4容器拼接，出现起点偏移和段间PTS漂移，最后MoviePy保留偏长容器时长。修复目标是统一累计30fps时间轴，不能放宽校验或伪改冻结manifest。原失败任务、成片及预算保留。已通过正常“停止新增工作”收尾，现有Recovery完成后本次运行FINISHED/exit0、stopped、active_task_count0，未强杀进程。

该Recovery `agt_042bc05e7ffa4f539161c61c123966f7` 最初因缺少MPT失败回执而未被消费。修复 `40227d4` 已部署，并在更新角色部署证明之前，通过正常管理员retry-recovery入口核验旧原生执行、accepted结果、审核主体和权益，新增明确的 `mpt_bundle_failure_recovery_reconciled` 记录；正常维护消费者随后将恢复动作与唯一一次 `mpt_agent_consumed` 原子提交，accepted/consumed哈希同为 `961d2cde438bf620d7c7081fd156a6bf2622ce968e58b978c2289c65d401369f`。原QC及Recovery的输入、结果、指令、attempt/max_attempts均未变。原方案要求修复CONTENT_QC上下文，既有portable合同不支持该目标，因此正常记录recovery_escalated，内容进入FAILED/recovery_blocked，而非假报修复成功；下一步从正常render入口生成新版本并重新审核。证明 `recovery-reconciliation-40227d4.json`、`recovery-consumed-40227d4.json`。未直接写数据库或放宽QC、权益、发布对账闸门。

本次同时发现原图留存缺陷：prepare-image原先把原图和处理记录放在完成即删除的任务工作区。8张原图已从实际原生生成目录按accepted结果中的source SHA完整找回；按原Pillow EXIF/LANCZOS/padding方法重新计算后，输出与8个已上传PNG逐字节一致。原图、输出和明确标注为新恢复证明的recovery.json保存在内容目录 `image-preparation-evidence/agt_5b1689d5c7bb494e89361d85a865e8a2`；没有伪称原preparation.json被恢复。安全证明 `aurzen-image-recovery-proof.json`。修复 `4997a52` 将后续新图片证据直接存入不会被submit/fail/GC删除的持久目录，image Skill版本1.2.1；7文件独立APPROVE、207项相关测试通过（76.72秒），已随 `40227d4` 部署。

已验证部署Skill实际读取的独立研究运行：`2b5ffce` 上task `agt_a297c8d9ae1b4719b14f2b913b0f5a0b` COMPLETED/attempt1，submission `sub_81dc4b2f125f476eb9a8ba6f7619a9f2` ACCEPTED，Core、submission、completed和消费记录同哈希 `f8d7e417a613d0cad518cf387e540a14fddf04d1120bcfd7d43dad2ea0d1c70c`。实际读取已签发stage中的SKILL.md及同包协议，18条命令exit0，649次HTTP均200，父子自然结束，无全局Skill读取。安全摘要 `staged-skill-native-safe-final-evidence.json`；初始消息独立正文哈希及developer指令继承未被接口暴露，不作已验证声明。

时轴修复 `dffa768` 已随 `40227d4` 部署。使用本次8张图片、完整原始旁白和批准计划的最终隔离重渲染，证明首PTS为0、30fps、1080帧、36秒；1080连续帧及270页证据通过原有联合证据输入校验。独立审查通过，85项相关测试通过，字幕、开场标识及完整旁白保留。混音验证使用隔离测试音轨，不能称为本次生产视频已有实际BGM；技术输入校验也不能代替正式CONTENT_QC。

恢复修复的独立审查发现并关闭两处问题：消费前必须重验实际成片、权益和来源上下文；reconciled恢复动作与消费标记必须原子提交。最终独立49项测试通过，包含两类故障完全回滚、重试成功且恰一次消费。四文件冻结及独立证据为 `bundle-failure-recovery-freeze.json`、`bundle-failure-recovery-v4-independent-review-evidence.json`。

存储前提：完整审核需要连续帧包及Worker下载副本。2026-10-15 16:00 C盘仅约0.57GB；随后原生Windows磁盘清理移除了约4.18GB的MEMORY.DMP，归档会话NTFS无损压缩另省179,596,538字节。压缩前后6677文件名称、长度及mtime一致，3个稳定样本SHA一致，未删除聊天记录。2026-10-16 22:02 C盘可用27,865,042,944字节，已满足本轮继续审核；较昨晚新增空间的来源仍在核实，不能都计为本代理清理成果。

已授权、但执行仍受策略限制的诊断清理范围为实施工作树 `C:\Users\Administrator\.codex\worktrees\mpt-stable-production\MoneyPrinterTurbo` 下这4个目录：

- `.artifacts/render-timeline-native-probe-final/content/render/joint-proof/frames`
- `.artifacts/render-timeline-native-probe-final/content/render/joint-proof/pages`
- `.artifacts/render-timeline-native-probe-final-v2/content/render/joint-proof/frames`
- `.artifacts/render-timeline-native-probe-final-v2/content/render/joint-proof/pages`

只读复核合计2,612,023,510字节；对应旧探针已终态。用户已明确授权清理旧版本及无用数据，删除命令仍在进程创建前被自动审批以“blocked by policy”拒绝，未执行；这不是缺少用户授权。两个旧托管工作树的原生归档另被固定任务保护拒绝，未绕过保护删除。保留两探针的原视频、manifest、joint-manifest、字幕与日志，以及整个最终 `render-timeline-native-probe-final-v3` 包及全部生产材料。

全盘元数据盘点覆盖本地C盘可访问用户目录、AppData、ProgramData、Program Files和Windows；E/F/G/U为网络存储，个人影音和NAS备份不按垃圾处理。旧工作树、旧Glance环境和历史诊断合计超过50GB逻辑体积，逐项核实运行依赖及清理结果；受硬链接、压缩影响，逻辑体积不能当作可释放物理空间。盘点证据为 `whole-machine-user-data-size-inventory-20261015.json`、`whole-machine-app-system-size-inventory-20261015.json`、`whole-machine-other-local-size-inventory-20261015.json`，压缩证据为 `archived-session-lossless-compression-20261015.json`。活跃数据库、当前会话、主目录素材、运行环境及模型不因体积大而删除。

10月16日复核发现旧CosyVoice目录（昨日约14.55GB）及三份旧安装/诊断文件（合计2,527,044,557字节）已不在原路径，无法归因于本轮代理操作。两个旧工作树约10.05GB及3.61GB、旧Glance目录约5.91GB仍在，其中旧data为5,202,822,283字节。当前145条Main内容路径全部指向主目录，旧data引用为0；进程、服务、任务也无旧Glance路径引用。用户再次明确要求删除整批旧数据后，七月MSIX单文件删除和旧Glance/data目录删除仍均被自动审批在进程创建前拒绝，未执行，不能写作已清理。没有换工具重试这些被拒绝目标。

Windows原生清理程序已不在运行；MEMORY.DMP及2.01GB的旧WATCHDOG转储均已不在原路径，但原wrapper退出码未恢复，不能宣称完整清理流程成功退出。仅本轮创建的两项 `StateFlags8763` 临时清理配置已移除并复核，安全证据 `windows-native-dump-cleanup-recheck-20261016.json`。

后续只读核对另一任务“检查电脑运行状态和磁盘占用”（`01a13f9f-a944-77c0-9c00-bf8f74b88d45`）的已完成记录，找到额外释放来源：该任务报告清理uv缓存、旧CosyVoice环境、Codex安装/诊断文件、NVIDIA安装缓存及少量工作树缓存，净释放23.37GiB，清理后可用26.01GiB。其记录与本轮观察到的文件消失及容量相符；净释放量来自该任务的前后取样，未在此逐文件重算，不与逻辑体积相加。本线程没有向该任务发送指令，也没有委托它重试本线程被拒绝的删除。对账证据 `cleanup-cross-task-reconciliation-20261016.json`；此前“无法归因”的描述指独立复核当时，现已有来源记录。

另已定位终态维护误报：旧Recovery为CONTEXT_MISSING/repair_task_context且恰一次消费，维护扫描却把所有非mark_recovery_blocked方案都送入专属QC目标拒绝回执校验。该日志最后写入早于新QC创建，并非当前审核任务的新失败。最小修复 `93ecb3d` 仅在策略确实适用时调用原严格校验，普通上下文链跳过该策略，真正QC拒绝的回执缺失/篡改仍拒绝。回归先复现RED，作者及独立审查三模块各189项通过；独立证据 `terminal-context-policy-independent-review-evidence.json`，SHA256 `76ca809730c4e3971566fd92b34b6aea2edbebcca86e84d5136b877e2882b3d9`。已推送实施分支并合入主目录源码；初次合入时后台仍是原PID28492。原活动运行自然结束后，已于10月17日00:18随2f7be13在新后台PID12376加载，不能把此前仅磁盘源码更新误写为当时已加载。

界面全局刷新也已复现缺陷：点击“刷新业务状态”会在内容选择控件渲染前额外rerun，清除所选内容并跳回首条。修复 `b22c800` 移除这次多余rerun，按钮点击本身仍触发正常刷新。真实Streamlit AppTest先在旧代码复现失败；修复后验证保留选题、展示服务端新状态及全程GET请求，作者与Root独立两模块各10项通过。2026-10-16 22:36已在主目录独立重启界面并复测：8510新PID24868、launcher2796，界面健康200；选择Aurzen后全局刷新仍保留该选题，显示WAITING_AGENT/content_qc和正在执行。8000仍为原PID28492，原生审核运行及心跳保持；守护已恢复Enabled。证据 `webui-deployed-b22c800-20261016.json`、`webui-refresh-fixed-production-20261016.png`。

单次运行窗口修复 `6825158` 已推送实施分支：仅后续新run由45分钟改为有上限的120分钟，旧run及重复启动保持原expires。理由是实际生图/素材审核/渲染已用约27分钟，完整270页成片审核另需较长时间，原45分钟难覆盖整条按需链。作者及独立审查四模块各183项通过，含原16轮上限、过期drain、任务预算及权限边界；精准9项先RED后PASS。独立证据 `content-run-120min-independent-review-evidence.json`，SHA256 `d8ccbf37f27f679ad616f7dc14f3f9bf4516acf28bc2a6d74396735c0a05d02f`。已随2f7be13在新后台加载，第三轮实际窗口120分钟，旧run未续期；120分钟是一次执行许可，不是周期触发或业务定时任务。

协作取消组合 `a355034`、`2f7be13` 已补齐手动停止和自动到期后的失效租约收尾：只处理本连接真实活动turn、完整原claim/task/run/child绑定且当前任务PENDING的情形；自动取消另须可信带时区截止时间已到。有效LEASED、未知身份、未来或无效期限及其他内部失败不会被误取消。每个turn只请求一次，响应不当作终态，等待匹配的真实completed/interrupted/failed事件。作者与独立Sol十模块分别422 passed/139.46秒、422 passed/140.63秒，冻结源码前后不变；独立证据 `native-interrupt-v2-independent-review-evidence.json`，SHA256 `2d3a1654ef48edbc6bc1775e127fe14b8b271177828ac69d36872251674c35fc`。固定原生二进制的隔离真实烟测也已PASS：唯一child真实interrupted、parent idle、自然exit0，未调用生产API；结果 `native-interrupt-live-smoke-v2-result-20261017.json`，SHA256 `eb1be8c47778391e65d0a06759c4fd2ad7c536c2415898d128c9ce43031b35fb`。首次烟测脚本把正常关闭后的EOF误判为失败，旧失败原件保留；仅修正测试收尾后重新验证，未改写旧结果。

验证范围：`40227d4` 组合版本已执行 `python -m pytest test/business test/services/test_video_project.py -q --tb=short`，1455 passed/905.02秒，进程exit0；354个纳入快照的Python、Skill及role文件前后不变。`d953775` 的同范围1509 passed/934.38秒及354文件不变证明另已保存；最终 `2f7be13` 组合的全量验证正在独立运行，使用新前缀 `root-final-native-liveness-regression-20261017`，不能把旧1509结果写成当前全量已通过。此前中断运行不计为通过；隔离研究、时轴验证、协议烟测和测试不代表本轮正式内容QC或真实发布成功。

宿主业务续租修复 `d953775` 已通过作者及独立Sol六模块各275项测试，分别61.14秒及61.05秒，并已推送实施分支。新的宿主心跳只在原生子任务、run、generation、epoch、原始领取及确认回执、输入、当前attempt和未过期租约全部匹配时续租；事件去重先于续租，观测与续租审计原子提交。Core原执行期限及预算保持，宿主不伪造Worker在线心跳。旧弱回执和当前PENDING任务均不会补证或复活。独立证据 `content-host-lease-v1-independent-review-evidence.json`，SHA256 `c11efdfdf89306de8421fe5f7f65c1c8c4bed9e2e9b0ea122c681f03fca3c397`；新的完整业务及视频回归1509 passed/934.38秒，exit0；354文件前后SHA不变，结果证明为 `root-native-liveness-regression-20261016-full-regression-result.json`，SHA256 `ed4bcf8adca817c3fcd5c2d40f2ceb9316cf41445bc3fffc20b6c2a608ce11d6`。该续租修复已随2f7be13在主目录新后台PID12376加载，并有第三轮真实强绑定续租记录。

23:14再次核对两份旧Codex修复备份数据库：2026-10-09 restart-backup和2026-10-10 upgrade-backup共3,167,350,784字节，无进程引用及路径重解析。用户明确要求全部删除后，原生PowerShell单文件删除仍在进程创建前被自动审批拒绝，理由仅为 `blocked by policy`；未执行，仍按未清理记录。证据 `obsolete-codex-backups-deletion-rejected-20261016.json`。当时C盘可用约28.0GB，不能把被拒绝文件计入释放量。

## 本轮剩余验收

1. 核实全盘清理最终结果及容量变化来源，保留生产和最终验证证据；不重复请求已给出的清理授权。
2. 新revision已独立通过18项技术检查，实际1080帧/36秒，final SHA256为 `75a83899a2e1e66d38baa5a857897fa292393653365b4dd7274e66cf1a3f579b`。正式CONTENT_QC尚未通过：第二轮曾因业务租约过期回到PENDING/attempt1，原宿主已自然收尾。安全部署后，第三轮已从正常入口确认同一任务的第2次尝试（2/3），现为LEASED且有真实宿主续租审计，等待实际结果。没有复活旧租约或重置预算；技术检查不能代替正式审核。
3. 通过后续独立审核与正常发布流程，核实本轮新作品的PUBLISHED记录、真实平台ID/URL和截图，再完成总体稳定生产验收并更新本文。

## 历史证据

此前检查点已原文保存在 [历史生产与诊断记录](mpt-stable-production-history.md)，旧调度恢复指令不适用于当前按需生产。
