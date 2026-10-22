# 官方素材优先与配音修复（2026-10-22）

## 问题与修复

本次 Framery One Work 研究没有回传任何 `source_images`，并不能据此认定官网没有素材。
旧链路还会为凑4/5张图片，将重复原图替换为AI请求，导致整条采用AI概念图，偏离素材优先要求。
新版要求新图文研究实际盘点同型号官方图片和视频；新计划绑定这份已接受研究和实际取图收据，
有可用原图必须实际使用，AI逐项说明信息缺口。图片数量建议不再触发自动AI补图。
旧未盘点内容在脚本审核、准备或失败状态下，经正常入口补充研究后重新走脚本/QC，不改写历史结果。
旧PACKAGED专用重配音缺盘点时仍会被拒绝，本次没有增加它或PUBLISHED的自动返工入口。
不得直改已发布状态或复制选题绕过去重来重发。
字段、恢复路径与兼容边界见[产品素材优先合同](source-media-priority.md)。

旧配音使用全段合成和默认MP3编码质量。新版使用一个本机Kokoro引擎按自然句原速合成，
逐句验证PCM后拼接，再通过`atempo=1.5`保留音高加速，显式使用`libmp3lame -q:a 2`。
采用更口语化的本地草稿，减少书面表达；字幕按严格ASR词映射组织成语义短语，避免闪字和拆词。
这组字幕短语编辑仅属于本次候选剪辑，不是对历史字幕的改写。

生产配音已设为`kokoro:3`、`tts_rate=+50%`、音量`+0%`，继续使用本机CPU双线程；
识别保持`medium / cpu / int8`，不接远端服务、不新增模型或依赖。
新schema2凭据区分`model_speed=1.0`、`requested_speed=1.5`和`tempo_factor=1.5`，
记录逐句文本/PCM区间、输入PCM哈希、FFmpeg命令和版本、最终音轨哈希。
旧schema1、旧摘要与签名形状保持兼容，不把后期加速伪装成模型原生1.5倍速。

## 实际素材与候选

使用了两份已核验的[官方公告](https://framery.com/en/framery-launches-one-work/)和
[官方首页](https://framery.com/en/)宣传图，AI图为0。
两份图是同一视角的不同构图，不代表两次实拍或多个真实场景；预约文字标为流程示意。
已找到官方LinkedIn新品视频线索，但访问转入登录页，尚未取得、核验视频文件。
旧Framery One视频没有混作One Work使用。
[官方媒体入口](https://framery.com/en/for-media/)是报道用途证据，不能自动视为任意用途的重新许可。

本地候选采用新口语草稿，未替换历史批准脚本。旧公开作品仍为
[7698443917590629670](https://www.douyin.com/video/7698443917590629670)。
约19.8秒的预览低于现有默认20秒门槛；没有补时或放宽门槛，也没有据此声明正式QC或新发布。
音频识别通过证明逐字一致，不证明主观机械感已经消除；仍需实际试听判断。

实际新provider完整合成和一次fresh严格ASR通过，结果位于
`storage/smoke/source-first-recut-20261022/final-producer-voice/result.json`。
最终候选是`storage/smoke/source-first-recut-20261022/candidate-conversational-producer/render/revisions/e3733a91d43f425e891e7d75e7a09705/final.mp4`，
SHA256为`386f3d5e0e7711708d98f89ff13043d95965010bb1c0c7a8bc6cfe2b3d532fe9`，
12,016,511字节、1080×1920、60fps、1188帧、19.8秒。
独立增量12项技术检查通过，完整解码、逐帧时钟、13条字幕覆盖109词及产品裁切已核验；
报告为`storage/smoke/source-first-recut-20261022/source-first-producer-independent.json`。
原生1.5倍速等失败实验及识别原文均保留，没有修改识别提示或阈值重写结论。
正式发布仍须绑定新版本的事实、权利、脚本、素材、完整成片与平台结果；本地技术预览不代替业务审核。

## 验证与部署记录

来源合同初版和v2独立审查发现的绕过均保留失败证据，最终v3修复空脚本引用并补回归。
来源综合回归441项通过，排除2项旧实际编码用例；v3增量156项通过。
配音及schema2合同91项通过；独立审查复核旧摘要、哈希、签名与旧实现逐项相同。
这些计数有重叠，不能相加作为不重复测试总数。

源码提交为`8c324c2`（来源优先）与`a938f371cd219be1e5937c19dc0446a51e6ea45c`（配音），
Main与生产分支均已推送。16份源码/测试/合同文档与冻结清单完全相同。
Main API8000受管重启后worker15092/launcher30008，旧15108/20044已停止；工作台8510保留Main实例。
启动配置只改`tts_rate`，备份保留在原配置旁；新设置哈希为
`02be5a8f92a9dfaabbe514172ce7bb568c4680ff7e3749fbb620c4482e4d2767`。
切换前后任务、提交、素材、内容、发布、阶段和调度七表逐行相同，118份登记媒体及当前run相同；
仅两条维护时间/游标更新，因此最初“八表全部相等”断言失败记录保持原样，须看独立部署差异核对报告。
最终源码、运行配置与候选文件的精确哈希及部署验收以对应独立JSON为准。
独立部署报告为`storage/smoke/source-first-recut-20261022/source-first-voice-deployment-independent.json`；
最初严格差异与受管切换过程保存在`deployment.json`及切换前后快照，不能用最终结论覆盖原始失败记录。

## 官方技术依据

- [Kokoro中文模型说明](https://k2-fsa.github.io/sherpa/onnx/tts/all/Chinese-English/kokoro-multi-lang-v1_1.html)：模型速度参数的含义。
- [固定版官方中文示例](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh/blob/4f4ed9d286bcdaea692b6d514eaf14dd8af9602f/samples/make_zh.py)：长音素序列可能出现rushing，不能据此承诺所有文本自然。
- [FFmpeg atempo](https://ffmpeg.org/ffmpeg-filters.html#atempo)：改变音频tempo。
- [FFmpeg libmp3lame](https://ffmpeg.org/ffmpeg-codecs.html#libmp3lame-1)：`q`控制VBR质量。
