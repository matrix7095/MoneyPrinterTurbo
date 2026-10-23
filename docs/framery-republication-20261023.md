# Framery One Work 修订制作与重新发布

截至 2026-10-23，重新发布所需的业务修订功能已部署，新修订的正式研究已完成。研究确认官方图片存在，但公开使用许可仍缺失，后续制作暂未放行。本文不把部署验收或旧作品链接当作本次发布成功。

## 制作要求

- 以 Framery One Work 同型号的官方宣传视频、图片为优先，AI 图片仅补明确的信息缺口。
- Kokoro 中文配音按自然句原速合成后保留音高加速至 1.5 倍；实际声音、字幕和节奏另行审核。
- 图片镜头使用真实 60fps 的现有渲染器。不能用帧率参数替代画面连续性审核。
- 新片重新完成研究、脚本、配音、计划、素材和九项正式成片质检，再审核发布包。
- 旧任务、媒体、审核与作品记录保留；新发布必须有不同的真实作品 ID。

## 已完成部署

Main 与远端 Main 已更新至 `76019bd5ce3507dec25de12bd71f173e46e3d0d0`。受管重启后的 API worker 为 8132，launcher 为 30860；8510 工作台可正常刷新，watchdog 已恢复，仅作服务恢复，业务周期调度仍关闭。

最初三条漂移均为 `SKIPPED / DIRECTION_BUSY`。隔夜新增 90 条记录逐条核实为 86 条同类跳过和 4 条既有采集通道唯一键冲突失败；后者不属于本次迁移，尚未修复。来源配置、授权字段和旧业务数据没有被这些采集尝试改写。

停服期间完成一致性私密备份，SQLite 检查通过。迁移前后 52,300 条旧记录按原列比较通过；1,294 件已登记媒体的观察结果保持一致，目标内容的 118 件素材全部通过实际大小及 SHA 校验。另有 117 件其他内容的历史媒体不一致问题，保留原状，不计为本次修复成果。

独立部署验收 59 项全部通过，覆盖 24 份冻结文件、可空字段及索引、旧数据保全、鉴权、服务与工作台归属、配音配置。

## 当前修订

- 内容：`content_c4e05b9b597a4697a8844d904c48206b`
- 新修订：`rev_8271eb3c803e404188ae39f24d64febf`
- 正常制作运行：`run_553fb0f62bc65e604b5cb85b2da56304`，generation 43。
- 修订创建、prepare、execute 均由正常管理接口返回 HTTP 200；未直接编辑生产数据库。
- 正式研究任务 `agt_9eedc16ea61f46a08be66ff25bc4391d` 已完成、接收并消费。结果确认同型号图片存在，但因许可不足没有放入可制作素材清单。
- 为防止退回全 AI 图，Root 经正常 run/stop 请求排空；运行已 `stopped`，宿主正常 `FINISHED / exit 0`，待写脚本任务仍为 attempt 0。没有修改已完成研究或强制结束进程。
- 历史作品 ID：`7698443917590629670`，仅作历史引用。最近创作者中心检查显示匹配旧作品为“私密”，不据本地 PUBLISHED 状态声称当前公开。

## 尚待完成

正式新视频、九项成片质检、发布包及新作品 ID 尚未完成。现有 19.8 秒本地预览低于 20 秒门槛，也未通过这次正式审核。当前制作规则在研究阶段就排除没有使用依据的第三方图片，不能先把它们制成正式成片再绕过素材门禁。

两张 2026 年官网产品图的公开转载许可尚未建立；2024 年媒体包不能证明这两张图片的授权。该缺口已写入新修订反馈，须在研究和正式质检中如实保留。联系品牌的邮件草稿已准备，尚未获发送指令，未发送。正式发布须通过实际素材权利门禁。

## 证据

- [隔夜采集漂移逐项核对](../storage/smoke/published-revision-20261022/preservation-static-deployment-reconciliation-reviewed-independent-20261023.json)
- [受管部署回执](../storage/smoke/published-revision-20261022/deployment-20261023.json)
- [迁移后旧数据与媒体保全](../storage/smoke/published-revision-20261022/preservation-after-migration-20261023.json)
- [59 项独立部署验收](../storage/smoke/published-revision-20261022/deployment-independent-acceptance-20261023.json)
- [新修订创建回执](../storage/smoke/published-revision-20261022/formal-revision-create-20261023.json)
- [按需制作启动回执](../storage/smoke/published-revision-20261022/formal-revision-execute-20261023.json)
- [本次正式研究结果](../storage/business-8000/data/agent-submissions/agt_9eedc16ea61f46a08be66ff25bc4391d/1/result.json)
- [防止全 AI 分支的正常排空请求](../storage/smoke/published-revision-20261022/formal-revision-official-route-drain-20261023.json)
- [官方素材使用依据调查](../storage/smoke/source-first-recut-20261022/rights-research-20261022.json)
- [已发布内容修订操作协议](published-revisions.md)
