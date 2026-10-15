# MoneyPrinterTurbo 稳定生产与运维记录

更新日期：2026-10-15。本文记录当前验收状态；历史检查点另存独立文件，旧调度恢复要求不再适用于按需生产。定时任务不是生产前提；内部按序派发和故障守护不承担周期选题或周期生产触发。

当前结论：尚未完成稳定生产验收。修复组合 `40227d4` 已部署到主目录并推送main，1455项组合回归通过，API、界面与七个角色已重新验证。Aurzen已真实完成8张新图片、独立素材质检和首次新成片；其正式CONTENT_QC因时轴漂移未能执行。时轴和恢复事务的修复已部署，旧Recovery已正常消费，内容现为FAILED/recovery_blocked，可从正常渲染入口重制；当前磁盘容量不足，尚未重制并完成正式QC、真实发布。第7版旧成片及隔离研究成功均不能代替本轮生产结果。

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

存储前提：本次完整审核需要生成连续帧包，还会产生审核Worker的下载副本。16:00核验C盘可用约0.57GB，不足以安全生成下一份完整生产审核包。此前4个已终态隔离诊断的frames/pages目录约2.43GiB，其清理遭自动审批拒绝，未执行，正等待具体确认；原生产素材、旧成片、清单、日志和最终v3验证包均保留。磁盘容量未恢复前，不启动大体积重渲染和审核包复制。

验证范围：`40227d4` 组合版本已执行 `python -m pytest test/business test/services/test_video_project.py -q --tb=short`，1455 passed/905.02秒，进程exit0；354个纳入快照的Python、Skill及role文件前后不变。安全证明为 `root-post-fixes-full-regression-result.json`、`root-post-fixes-source-freeze.json`、`root-post-fixes-full-regression.log`。8059条警告主要为现有依赖弃用提示，不等于测试失败。此前中断运行不计为通过；隔离研究、时轴验证和测试不代表本轮正式内容QC或真实发布成功。

## 本轮剩余验收

1. 在前述具体清理确认后恢复磁盘容量，保留旧诊断清单、日志、视频及最终v3验证包，重新核算渲染与审核副本的空间。
2. 通过正常render入口生成新revision，保留旧成片；核对实际1080帧/36秒及冻结输入，通过按需宿主执行新的正式CONTENT_QC。不能用隔离时轴证明代替该项。
3. 通过后续独立审核与正常发布流程，核实本轮新作品的PUBLISHED记录、真实平台ID/URL和截图，再完成总体稳定生产验收并更新本文。

## 历史证据

此前检查点已原文保存在 [历史生产与诊断记录](mpt-stable-production-history.md)，旧调度恢复指令不适用于当前按需生产。
