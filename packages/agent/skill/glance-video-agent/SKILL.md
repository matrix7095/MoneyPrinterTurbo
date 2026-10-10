---
name: glance-video-agent
description: 处理 Glance 平台签发的 AI 视频素材生成任务。用于具备原生视频生成能力的宿主按照镜头、时长、运动、连续性与禁用元素规范生成视频，持续心跳、受控上传并提交 Artifact 引用时。
---

通用执行协议见 `references/protocol.md`；若存在 `previous_failures`，必须优先修复。

# Glance 视频 Agent

仅处理 `asset_generate_video`。建议使用普通级控制模型和高思考强度。
宿主必须具备原生视频生成、进度查询、文件上传和长任务心跳能力。

设置 `GLANCE_AGENT_ROLE_ID=video`、`GLANCE_AGENT_SKILL_NAME=glance-video-agent` 和版本 `1.0.0`。

1. 只处理周期 `dispatch` 返回的明确任务；先按任务提示执行 `confirm-claim <task_id>`，
   再阅读已准备的镜头、时长、画幅、运动、连续性、禁用元素与 Schema。`NO_TASK` 或
   `AT_CAPACITY` 时直接结束本轮，不运行 `claim`、`inspect` 或重复 `dispatch`。
2. 生成期间在租约到期前持续 `heartbeat`，不得重复创建同一生成任务。
3. 检查实际时长、尺寸、人物、品牌、文字和水印；成片画面不得出现文字、字幕、署名、水印或 logo。
4. 运行 `glance-agent upload-artifact <task_id> <视频文件>`。
5. 把返回的 `artifact_id`、工具、提示词摘要、尺寸、时长以及文字/水印检查结果写入 `result.json`。
6. `validate` 后 `submit`。

不得返回本地路径或命令，不得直接写成片目录或发布内容，也不得绕过 Artifact 技术 QC；Handsfree 的审核推进
只能由服务端在明确开关和高置信度审核结果通过后执行，Agent 本身不拥有批准权限。
