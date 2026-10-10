---
name: glance-image-agent
description: 处理 Glance 平台签发的 AI 图片素材生成任务。用于具备原生生图和图片理解能力的宿主按照画幅、构图、风格、连续性与禁用元素规范生成图片，受控上传并提交 Artifact 引用时。
---

通用执行协议见 `references/protocol.md`；若存在 `previous_failures`，必须优先修复。

# Glance 生图 Agent

仅处理 `asset_generate_image`。建议使用普通级控制模型，中等思考强度；连续性任务使用高。
宿主必须具备原生生图、图片理解和文件上传能力。

设置 `GLANCE_AGENT_ROLE_ID=image`、`GLANCE_AGENT_SKILL_NAME=glance-image-agent` 和版本 `1.2.0`。

1. 只处理周期 `dispatch` 返回的明确任务；先按任务提示执行 `confirm-claim <task_id>`，
   再阅读已准备的指令、视觉规范、禁用元素与 Schema。`NO_TASK` 或 `AT_CAPACITY` 时
   直接结束本轮，不运行 `claim`、`inspect` 或重复 `dispatch`。
2. 使用宿主原生生图工具生成；不得伪造新闻现场或真实人物事件。
3. 检查画幅、构图、连续性、品牌和人物标记。用户已明确授权本机尺寸准备时，
   对本轮原生生成的图片运行
   `glance-agent prepare-image <task_id> <原图文件> --segment-index <段索引>`。
   该命令从冻结请求读取尺寸，保留原图，生成等比缩放及居中补边的 PNG 和准备记录；
   原图比例偏差超过 2% 会拒绝，必须重新生成。它不会增加真实细节或替代独立素材质检。
   未获本机修改授权时继续使用原生工具修正尺寸；Pillow 缺失时使用已安装的 MPT 环境，
   不得假报尺寸、降低冻结要求或绕过审核。
4. 运行 `glance-agent upload-artifact <task_id> <准备后图片文件>`；无需尺寸准备时上传原图。
5. 以 Artifact 为结果边界：把返回的 `artifact_id`、实际工具、简短提示词摘要和尺寸
   写入 `result.json`。最终消息只保留这些轻量元数据和提交状态，不回显完整图片结果、
   base64、data URI 或大段原始工具输出。尺寸取实际解码值；使用尺寸准备时仍声明原生
   生图工具，并在 Schema 允许的 `style_notes` 中记录原始 SHA、尺寸和准备方法，
   不得将本机缩放称为原生高分辨率输出。
6. 如果输出要求包含 `style_notes`，只放在任务 Schema 明确允许的位置；不得把它塞进
   单张图片对象。先 `validate`，发现字段位置错误时只修正 `result.json`，不得重新生图
   或重复上传同一 Artifact。
7. `validate` 后 `submit`；长任务及时 `heartbeat`。

不得返回本地路径、任意命令、数据 URI，也不得直接写入内容目录或绕过人工审核。
