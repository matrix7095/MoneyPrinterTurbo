# Glance 能力迁入 MoneyPrinterTurbo 方案

> 状态：仅完成仓库 bootstrap 与方案；截至 2026-10-10，尚未迁移业务代码、切换数据或发布服务。

## 已核实的基线

- 目标仓库是 `matrix7095/MoneyPrinterTurbo` fork，代码目录为 `C:\Users\Administrator\Documents\MoneyPrinterTurbo`；上游是 `https://github.com/harry0703/MoneyPrinterTurbo.git`。本方案的隔离工作区位于 `C:\Users\Administrator\.codex\worktrees\glance-migration-bootstrap\MoneyPrinterTurbo`。
- 当前完整 MPT `main` 基线为 `b819f7e1213ca2bc444cbf53ad3e0e3080c8a866`；已 fetch 的 `upstream/main` 也指向该 SHA。它不是 benchmark 使用的 MPT v1.3.8 实验基线 `fafec0fbf3142ad5ad7212c2e17996bf247c360a`。
- Glance 只读参考源码在 `C:\Users\Administrator\.codex\worktrees\becd\Glance`。这是参考实现，不是迁移后运行时依赖。
- benchmark 报告：`C:\Users\Administrator\.codex\worktrees\becd\Glance\data\mpt-benchmark\REPORT.md`。

## 目标和边界

把 Glance 的业务编排、Agent 执行协议、持久化任务、内容与权益审核、Handsfree 和 Douyin 发布证据收进 MPT，使 MPT 成为唯一应用、部署与数据入口。MPT 保留确定性视频渲染、API 与 WebUI；脚本、语义搜索、研究、视觉判断及 AI 图片/视频生成在生产中统一走 AgentTask，Douyin 发布只走服务端签发的发布任务。业务编排集中在 `app/business/`，通过现有 FastAPI router/controller 接入，不广泛重构上游模块。MPT 当前直接调用 LLM 的 API/WebUI/`task.py` 路径需切到 AgentTask；最终生产依赖锁和启动入口不得安装或导入模型 SDK，迁移完成后，MPT 的构建、启动和运行也不导入或调用旧 Glance server/worker 源码。

必须迁移并保持的业务契约：

- `AgentTask` 持久化、SQLite 状态转换、领取租约、心跳、超时回收、有界重试和 attempt 记录。
- Artifact 的登记、哈希/元数据、安全路径、完整性校验和任务结果关联。
- 来源发现、候选去重与入库；Rights 证据检查；研究、脚本、视觉计划、素材及成片 QC 的 schema 校验和 fail-closed 闸门。
- Handsfree 的阶段推进、重试预算和人工审核边界；Douyin 发布意图、真实外部发布 ID/URL 及证据。上传成功或生成发布包不能冒充已发布。
- 未迁移、没有消费者的任务类型必须明确拒绝；不接受“可以创建但永远没人处理”的通用 AgentTask。

Glance 的 `AgentClient` 固定调用 `/api/v1/agent/...`。MPT 需在同一 URL 空间实现实际支持的 HTTP 行为，并通过真实 `AgentClient` 发请求验证；仅拼接 URL 或 unit mock 不算兼容。第一条垂直链限定为：真实来源发现 → Agent 领取/心跳/提交 → 服务端 schema 校验 → `CollectionService.ingest_agent_result` → 候选可查询。结果提交与候选入库分属事务，必须覆盖“提交已成功、消费失败”后的恢复和幂等，不得重复入库。

## 阶段与代码落点

| 阶段 | MPT 落点与工作 | 验收门槛 |
|---|---|---|
| 0. Bootstrap（已完成） | 本分支从完整 MPT 基线 `b819f7e` 开始；主仓库保留 fork `origin`，新增 `upstream` 并 fetch `main`；本轮只写本方案和上游同步方案。 | `git diff --check` 通过；无运行配置、源码、数据库或远端发布变更。 |
| 1. AgentTask 与发现闭环 | 在 `packages/core/src/glance_core/`、`packages/agent/src/glance_agent/`、`packages/collector/src/glance_collector/` 选择性移入并作为本地包安装，保留包名；`app/business/` 只放 MPT 编排和接入代码，不把源码目录伪装成可导入包。新增 `app/controllers/v1/agent.py`，由 `app/router.py` 挂载兼容路由。测试放 `test/business/`，例如 Agent HTTP 契约和消费恢复用例。 | 用真实 feed/source 产生任务；真实 AgentClient 完成 claim、heartbeat、artifact/result 提交；schema 无效结果被拒；有效结果进入候选库并可查询；重启进程后 SQLite 中任务、attempt 和租约状态仍可读；错误角色和过期租约不能提交；模拟提交后消费失败，再运行恢复，证明只入库一次；只允许此阶段有消费者的任务类型。 |
| 2. 权益和内容生产 | 继续在 `app/business/` 增加受支持的角色、任务 schema、rights 与 candidate/research/script/visual-plan consumers；为每种类型接真实输入、结果验证和下一阶段消费者。 | 缺来源/授权证据、坏 schema、越权结果均阻断后续阶段；任务重试有界且可审计；未消费的任务类型拒绝签发。 |
| 3. 素材、成片与质量闸门 | 业务编排在 `app/business/`；复用 MPT `app/services/task.py`、`video.py`、`task_artifacts.py` 和现有 API/WebUI，不复制 Glance media/server 层。固定各阶段输入快照与哈希，产物完整性通过后登记；内容 QC 仍是发布前置条件。 | 覆盖冻结输入、断点恢复、坏素材、音画/字幕/时间轴不符及质量拒绝；失败恢复不复用错误版本产物，不自动绕过 QC。 |
| 4. Handsfree 与 Douyin | Handsfree 仅在用户明确启用后免逐条人工确认，仍经过已有 QC/权益闸门。发布仅执行 server-issued 的单账户 `douyin_publish` 任务；不得让 MPT 自动 cross-post 绕过发布闸门。发布意图、外部结果和审计/Artifact 关联由 `app/business/` 持久化。 | 从来源到审核、成片和发布结果均可追溯；UNKNOWN、NEEDS_USER、已提交但未返回结果时只读核对平台状态，禁止盲重试；只有确认平台未创建发布后才可按规则重试；仅存在上传记录时状态不能标为已发布。 |
| 5. 数据切换与退役 | 使用独立目标 SQLite 和独立 Artifact 根目录。先写幂等迁移/核对工具，再按冻结快照切换；观察期后移除部署和源码依赖中的旧 Glance 服务。 | 备份可恢复；迁移前后按 task/candidate/artifact/publication intent、外部发布 ID、证据和审计关联/数量/哈希核对；UNKNOWN 发布保持待核对，不重新创建发布；回滚演练通过；MPT 全链路运行不依赖旧 Glance 服务。 |

### 首阶段的具体边界

MPT 入口为 `app/asgi.py`、`app/router.py`、`app/controllers/v1/`；CLI/WebUI 的视频生产最终走 `app/services/task.py:start`。现有 MPT 语义生成直接调用分布在 `app/services/task.py`、`app/controllers/v1/llm.py` 和 `webui/Main.py`。迁移必须将这些脚本、关键词、社交元数据入口切换到 AgentTask，并核对 CLI/WebUI 与 API 每个调用者。只允许未切换的上游兼容验证环境暂时保留旧 MPT 生成入口；该环境不是迁移生产，生产不能继续调用这些 provider 路径。

不要整包复制 `apps/server/src/glance_server/app.py`：其顶层导入 collector、media、worker，挂载整个 server 会带入尚未迁移的依赖和入口。Glance 六个 packages/apps 合计约 58 个 Python 文件、34,471 行，也不应一次性盲拷。`tests/conftest.py` 会拉起完整 server/worker/Huey 并读取实际配置，不能复制到 MPT；为新业务使用临时 SQLite、独立 Artifact 目录和窄 fixture。Glance `create_db` 会补列/索引并更新旧数据，因此任何迁移测试都不得用现役 Glance 数据库启动。

## 运行时、依赖与切换安全

MPT 当前要求 Python `>=3.11`，CI 覆盖 3.11 和 3.13；Glance 相关包要求 `>=3.12,<3.13`。首阶段先在独立 Python 3.12 环境做真实垂直验证，不据此宣称支持整个 MPT 矩阵。完成移植后，3.11、3.12、3.13 均须通过项目测试和启动检查，或者明确提出并批准改变 MPT 的支持范围。Glance core/agent 本身不需模型 SDK。最终生产 `pyproject.toml`、`uv.lock` 和 `requirements.txt` 不安装模型 SDK，生产启动路径也不导入这些 SDK；现有 MPT/上游语义 provider 只能留在隔离、可选的上游兼容验证环境。`pyproject.toml` 与 `uv.lock` 由单一维护者统一修改和验证。

数据库、Agent artifacts 与 MPT 视频任务目录必须物理隔离；测试使用临时路径，不连接现役 MPT/Glance 数据库。切换前对源 DB 和全部 artifact 做一致快照并备份，运行只写入新目标的幂等迁移，按 task/candidate/artifact/publication intent 主键、外部发布 ID、证据/审计关联、数量和内容哈希核对；重复运行结果不变。过期租约、部分 artifact、未知状态须报告并隔离，不静默丢弃。UNKNOWN、NEEDS_USER 或已提交但未返回结果的发布只读核对平台和审计证据，不能因迁移中断或幂等重跑再发一次；只有确认平台未创建发布后才可重试。切换门禁包括备份恢复演练、全量回归、断电/消费失败恢复、8010 独立配置预生产验收、服务重启后 Agent/SQLite/artifact 检查，以及回滚到未改写旧数据的能力。全部门禁通过前，旧系统仍是数据权威；通过后采用可回退切换，观察期结束才移除旧服务依赖。

## Benchmark 能说明什么

报告中的 MPT 调用基于 v1.3.8 `fafec0f`，只覆盖三个真实选题的合成切片以及失败/恢复样本，没有跑完整来源发现、业务任务或真实发布链。历史 Glance 11–15 秒是旧数据库中单独的 render stage；MPT 数百秒是不同版本、不同阶段的合计，充电宝还把首轮素材准备和修正后的重跑相加。它们不是同版本受控排名，不能推出速度倍数、P95、容量或当前主干表现。现有证据只支持先做隔离的业务闭环迁移，不支持以性能收益为由一次性替换生产链。
