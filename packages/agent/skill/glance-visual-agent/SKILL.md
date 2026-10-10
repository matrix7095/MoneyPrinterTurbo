---
name: glance-visual-agent
description: 处理 Glance 平台签发的场景分析、视觉计划、视觉计划质检和素材质检任务。用于理解视频或关键帧、规划竖屏镜头或核验生成素材并提交结构化结果时。
---

通用执行协议见 `references/protocol.md`；若存在 `previous_failures`，必须优先修复。

# Glance 视觉 Agent

仅处理 Glance 服务端签发的视觉任务。建议使用旗舰级模型和高思考强度。
宿主必须具备图片理解、视频或关键帧理解和结构化 JSON 输出能力。

## 接入

使用平台生成的绝对路径启动器运行全部命令，不依赖当前目录或 `.env`。
角色 ID 为 `visual`，Skill 为 `glance-visual-agent`。

## 工作流

1. 只处理周期 `dispatch` 返回的明确任务；先按任务提示执行 `confirm-claim <task_id>`。
   `NO_TASK` 或 `AT_CAPACITY` 时直接结束本轮，不运行 `claim`、`inspect` 或重复 `dispatch`。
2. 读取本轮已准备好的任务指令、关键帧、转写、脚本、素材清单和结果 Schema。
3. 只引用任务包提供的 scene ID 和 asset ID；不得返回任意文件路径或命令。
4. 规划竖屏构图、镜头节奏、字幕安全区、素材连续性和缺失素材，不凭空宣称画面事实。
   原素材中的营销字幕或其他必须遮挡内容要在 VisualPlan 的 `source_redactions` 结构化字段
   中填写归一化矩形，不能只写在 `purpose`。
   `asset_qc` 则逐项查看任务包的真实生成素材，对照请求核验事实、构图、连续性、禁用元素和使用依据；
   问题用现有 `segment_index` 准确定位，按任务 Schema 提交，失败不得假称通过或自行重新生成。
5. 只把最终结构化结果写入 `result.json`，运行 `validate` 后再 `submit`。
6. 长任务及时运行 `heartbeat`。

生图和 AI 视频属于独立素材任务；当前视觉任务只能提出素材需求，不得绕过平台直接写入成片目录。
