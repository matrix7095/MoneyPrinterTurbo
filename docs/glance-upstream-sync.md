# MoneyPrinterTurbo 上游同步规程

> 目标：把经核验的上游改动纳入 fork 和 Glance 迁移代码，同时保持 MPT 运行契约、迁移数据和发布闸门。建议每周检查上游、每月做一次固定版本集成；这是人工节奏建议，本轮没有创建自动化。

## Remote 与版本基准

- `origin` 固定指向用户 fork：`https://github.com/matrix7095/MoneyPrinterTurbo.git`。
- `upstream` 固定指向原仓库：`https://github.com/harry0703/MoneyPrinterTurbo.git`。
- Bootstrap 已 fetch 的 `upstream/main` 为 `b819f7e1213ca2bc444cbf53ad3e0e3080c8a866`，与本轮完整 MPT 基线相同。每次同步都要重新记录 SHA；不得把这个快照当作以后永远不变的头部。

## 固定 SHA 的集成流程

所有 Git 命令使用 Git Bash；在 Windows PowerShell 7 中通过下面的显式入口启动，后续命令均在该 Git Bash 会话执行：

```powershell
& 'C:\Program Files\Git\bin\bash.exe' --noprofile --norc
```

每轮开始前，先在本地 `main` checkout 核对工作区干净且 `main` 与 `origin/main` 完全相同；不满足时暂停并通过正常审查流程同步/提交，不能重置或丢弃本地提交。合并前再次确认 `main` 仍等于记录的集成基线且工作区干净；基线已漂移就从新基线重建 integration worktree。

```bash
cd /c/Users/Administrator/Documents/MoneyPrinterTurbo
git status --porcelain
git rev-parse main
git rev-parse origin/main
```

1. 更新两个远端引用，记录 fork 基线和唯一上游 SHA。不要在 `main` 上直接合并浮动的 `upstream/main`：

   ```bash
   cd /c/Users/Administrator/Documents/MoneyPrinterTurbo
   git fetch origin main
   git fetch upstream main
   git rev-parse origin/main
   git rev-parse upstream/main
   git log --oneline --decorate <last-integrated-upstream-sha>..upstream/main
   ```

2. 为这一个 SHA 建立独立 integration worktree。以已选定的 MPT 集成基线（通常为 `origin/main`）起分支，再合并固定 SHA；迁移功能尚未进该基线时，先在包含迁移代码的分支集成，不能丢弃该分支内容。

   ```bash
   git worktree add -b codex/upstream-sync-<short-sha> \
     /c/Users/Administrator/.codex/worktrees/mpt-upstream-<short-sha> \
     <mpt-integration-base-sha>
   git -C /c/Users/Administrator/.codex/worktrees/mpt-upstream-<short-sha> \
     merge --no-commit --no-ff <fixed-upstream-sha>
   ```

3. 按提交和目录检查差异，逐段处理冲突并保留必要的 MPT 行为。禁止一键选 ours/theirs 后跳过复核。重点审查 `app/services/`、API/WebUI 调用、任务/状态与 artifact 安全边界、Docker/启动入口、`pyproject.toml`、`uv.lock`。依赖锁文件由单一维护者处理；默认生产安装闭包与启动导入路径不得安装或导入模型 SDK，所有语义处理必须进入 AgentTask。锁文件可以包含明确隔离的 optional compatibility extra 的 SDK；门禁检查实际默认安装环境和启动导入，不能要求整个锁文件没有 SDK。需要检查未迁移上游功能时，在隔离的可选兼容环境验证，不能把 SDK 装进目标生产环境；不照搬供应商 SDK、配置项或凭据。

4. 在 integration worktree 用干净配置、独立 DB/artifact 目录完成验证：MPT 回归与 lint；Glance 迁移的来源发现、AgentClient 真实 HTTP、schema、重启后 SQLite 持久化、错误角色/过期租约拒绝、幂等入库、Artifact 与失败恢复；Python 3.11/3.12/3.13 矩阵；生产依赖/启动路径不安装或导入模型 SDK；8010 隔离预生产验证。发布恢复样本须覆盖 UNKNOWN/NEEDS_USER 和“提交后未返回结果”，证明仅只读核对且幂等恢复不重复发起 Douyin 请求。新旧行为差异和失败都要有记录。任何一项失败，停止该 SHA，不快进、不重启、不推送。

5. 所有门禁通过后才提交集成结果；再次确认本地 `main` 干净且仍等于记录的 MPT 基线，然后才用 `git merge --ff-only codex/upstream-sync-<short-sha>` 推进。基线不符时停止并重建集成分支。重启目标服务后，先证明实际进程和版本，再检查 API、AgentTask SQLite、Artifact 读写及生产 readiness；保留上一版本 SHA、数据备份和快速回退步骤。只有重启验证有证据且代码/数据回滚路径仍可用，才 push 到 `origin`。禁止 `reset --hard` 和强推；代码回退用经审查的 revert，数据回退用已验证的 DB/Artifact 备份。

6. 每次同步留一条可审计记录（建议 `docs/upstream-sync-log.md`）：日期、上游 SHA、MPT 基线/集成提交、冲突文件及取舍、依赖/锁文件变化、测试与 Python 矩阵、8010 结果、重启证据、push SHA 和回滚目标。不得把密钥、token、生产素材或完整环境配置写入记录。

当前上游 SHA 与 bootstrap 基线相同，`Already up to date` 不能算升级演练通过。最终演练须使用实际不同的两个固定历史 SHA，或新出现的上游 SHA，在包含迁移代码的独立 integration worktree 完成上述门禁；多 writer 尚未提交的工作区不能用于这个演练。

## 停止与回退条件

发生协议/schema 变化、任务状态不兼容、锁文件意外扩大或生产模型 SDK 混入、质量/权益闸门回归、数据不一致、8010 或重启验证失败时，保留 integration worktree 和日志供修复，不推进 `main`。若已进入部署阶段，用经审查的 revert 恢复之前已验证的 MPT 提交，并按演练恢复对应 DB/artifact 备份；不得用旧 Glance 与新 MPT 同时写同一份数据。没有经过验证的样本、恢复证据或实际服务重启结果时，不宣称同步完成。
