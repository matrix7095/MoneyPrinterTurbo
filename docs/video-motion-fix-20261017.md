# 图片成片慢速运动卡顿修复

当前代码、制作进度及真实发布结果以[生产交接](production-handover-20261017.md)为准。下列日期快照保留当时状态，不代表后续版本已通过审核。

2026-10-21：用户仍反馈低帧率观感。此前几何/解码/QC通过没有证明这一问题已经解决。新源码`8f68051`已提交、推送并隐藏部署，纯图片项目采用真实60fps、2倍空间采样和整片统一码率预算；完整隔离候选已生成，但真实图片测量仍有误差与压缩取舍，尚无新正式QC或发布。本轮对照、当前候选与失败证据见[图片运动采样复核](video-motion-sampling-20261021.md)。

2026-10-19后续修复：第五版首片的完整968帧检查发现两个镜头末帧位移异常，原技术FAIL保留。逐级解码对照排除了帧数、PTS、封装和解码差异；使用同一固定角点方法的编码对照确认，两层libx264均设置`mbtree=0`后，这两个镜头分别137和93个相邻帧对不再出现原反向、低步长或跳步。`3c4dca1`将该参数用于图卡准备和MPT镜头编码，保留CRF18、30fps和原浮点几何，缓存版本升为`local-ffmpeg-v3-cfr-pcm-no-mbtree`。两个镜头对照通过本身不等于新成片全片通过。

`7785623`修复正常未分发QC没有ownership标记却被返工接口拒绝的问题，保留任务隔离、已有标记核验、不可变receipt、当前材料及已完成上游任务的来源检查。37项独立回归通过；正常一次返工请求返回200，只取消被替代的PENDING/attempt0审核，旧成片、旧输入结果、attempt和恢复预算不改。新成片的完整技术检查、正式QC及发布以交接文档中的最终实际回执为准。

证据：[首片技术FAIL](../storage/smoke/stable-production-20261013/framery-script5-independent-technical-20261019.json)、[分级诊断](../storage/smoke/stable-production-20261013/framery-script5-stage-tail-motion-diagnostic-20261019.json)、[独立源码审查](../storage/smoke/stable-production-20261013/terminal-motion-independent-source-review-20261019.json)、[返工接口独审](../storage/smoke/stable-production-20261013/pending-render-revision-retry2-independent-review-20261019.json)、[正常返工回执](../storage/smoke/stable-production-20261013/framery-script5-motion-rerender-retry1-request-20261019.json)。

2026-10-20当前S5已完成全片复验：新成片SHA `3f137051b87a616cfa1c2e533a05916ffcf723feabd3f3f26498630239f1900d`，1080×1920、30fps、968帧。961个镜头内相邻对的反向、低步长、跳步、重复ROI、黑边及比例异常均为0，最大段RMSE为0.024247像素；七段prepared与最终码流均核实CRF18/mbtree0，PTS连续，完整解码通过。正式第38代九项QC全部通过、high，接受和消费已完成。浏览器播放至结尾不等于零掉帧保证；音频QC采用独立ASR/PCM路线，没有另作主观听音。

证据：[当前全片技术PASS](../storage/smoke/stable-production-20261013/framery-script5-motion-repair-independent-technical-20261019.json)、[正式九项QC](../storage/smoke/stable-production-20261013/framery-script5-formal-qc-receipt-motion-repair-independent-20261019.json)。最终实际发布结果见[生产交接](production-handover-20261017.md)。

历史快照（2026-10-19 20:22）：生产代码 `2911776` 已提交、推送、隐藏部署；新片 `asset_ca80101f3aef4c639f1bbdfd9ab1b2ef` 正式QC九项全通过。发布包披露、来源证据和下载冲突修复已部署，新封面已正常重建，Native34正在新文案/审核，尚未真实发布。当前操作与证据以[生产交接](production-handover-20261017.md)为准。以下旧进程、成片与“当前/正在”描述均为各自时间点的历史记录。

10月19日16:40，浮点运动修复 `f4454c19d2586c7c7ca31b45d6fda2ff2d25d87d` 已提交并推送到 main 和稳定生产分支，主目录已实际部署。API worker 29808、launcher 5884，认证API与界面200、匿名401；3114条任务、45份内容媒体和旧负向历史在切换前后相同。S4正式渲染已成功返回：`asset_075684b1708a4d3a84e04542340e2da7`，SHA `8ac7b64cdaefb81d8208450248024a5e5f3883b1e27b3ef4b09e0054f6193b23`；808帧、26.933333秒。正式内容QC已完成且不通过：8项通过，仅字幕断词/时序未通过；旧片不能发布，正在修复字幕生成器后走正常新render revision。

修复在30fps时间轴使用 cubic perspective 浮点角点代替整数 zoompan，首帧使用 `on-1`，保留焦点、运动范围和原有上限。图卡准备与纯图卡项目的二次编码采用CRF18，普通素材仍默认23；质量参数纳入缓存指纹与请求凭据。没有新增依赖。

作者完整95项、独立代码审查22项和独立定向3项均通过。独立复测17段视频：旧网格推近/推远分别40/49次反向、83/83次低步长、105/79次跳步；新五种运动均为零，步长RMSE为0.00481–0.02978像素。真实素材仍在二次编码最后一对帧测得三项低步长，偏差0.05858–0.08879像素，保留为有损编码余限，不能宣称所有帧零误差。正式S4七镜头另经808帧/801个镜头内相邻对检查，无反向、跳步、重复ROI或黑边，RMSE 0.00723–0.02337像素；两个拉远镜头末尾一至两对仍有7个低步长分量，偏差0.0521–0.1183像素。Chrome观察正常播放至结尾，不能据此宣称零实际丢帧或完整听审。

证据：[独立几何复测](../storage/smoke/stable-production-20261013/subpixel-camera-independent-20261019.json)、[独立部署门禁](../storage/smoke/stable-production-20261013/subpixel-camera-independent-gate-20261019.json)、[实际服务切换](../storage/smoke/stable-production-20261013/subpixel-camera-service-cutover-20261019.json)。

新片证据：[全808帧微步核验](../storage/smoke/stable-production-20261013/framery-script-v4-full-microsteps-20261019.json)、[正式绑定技术预检](../storage/smoke/stable-production-20261013/framery-script-v4-final-independent-technical-20261019.json)、[122词音频信号预检](../storage/smoke/stable-production-20261013/framery-script-v4-audio-preflight-20261019.json)、[浏览器播放观察](../storage/smoke/stable-production-20261013/framery-script-v4-browser-playback-20261019.json)。正式语义、许可与内容质量仍由同一新片的正式QC决定。

以下为本次早期复验及10月17日历史记录，旧结论保留各自适用边界。

10月19日复验：原4倍方案只降低取整步进，没有消除用户看到的推拉停走。旧的“加速度小于1像素/帧²”通过，只能排除较大突跳，不能据此宣称已经流畅。当前正在验证浮点 `perspective` 及真实MPT两级编码策略，尚未部署该候选或生成新的合格正式成片。

[修正后的亚像素诊断](../storage/smoke/stable-production-20261013/framery-pushpull-subpixel-diagnostic-corrected-20261019.json)在180帧合成真值中复现：理想水平步长约0.12025像素/帧，4倍方案推近5次、拉远17次反向小步；cubic浮点变换原始帧没有同类反向、停走或跨步，步长误差明显降低。原始诊断的Y轴坐标方向错误已另存修正报告，旧报告未覆盖。后续编码仍会引入量化误差：单次或单方向通过不代表两级编码及所有推拉都通过；当前不能把CRF18试验称作最终解决。

以下为10月17日的历史修复和当时的验证边界。

宜家成片的1081帧时间戳连续，输出为1080×1920、30fps。卡顿来自图片推拉和平移的空间取整：FFmpeg zoompan 的裁剪位置及尺寸使用整数，低分辨率与色度对齐使慢速移动间歇跳动。仅检查帧率或“画面有变化”不足以验证流畅度。

修复在运动计算前使用4320×7680、yuv444p中间画面，再由zoompan输出原来的1080×1920。运动曲线、焦点、时长、字幕和最终格式保持原规则；静止画面不增加此开销。没有增加依赖，也没有更改旧成片、旧审核或恢复次数。

实现位于 `packages/media/src/glance_media/executor.py`，新增真实FFmpeg回归位于 `test/business/test_image_motion_smoothness.py`。缓存已按新画面字节哈希区分，新输出不会覆盖旧revision。

## 验证和预览

- 主线程明确导入Main源码，运动、成片不可变性、时间线三文件共25项通过，随后新增的hold检查1项通过；Ruff和diff检查通过。独立审查的五种运动真实渲染测试也通过，均使用PATH中的FFmpeg 9.0.2。
- 既有cap/early-freeze参数化测试进程结束码为0；该次原始pytest输出未保留，不据此复述测试数量。
- 独立预览 `storage/smoke/stable-production-20261013/ikea-smooth-motion-preview-20261017/smooth-preview.mp4`，36.033333秒、1080×1920、30fps、1081帧，严格音视频解码通过。完整重建耗时约73.8秒。
- 预览SHA-256：`8d01f486088355c9b42d1484f554867bae762118de5c8e52189280d7b55aace1`。原片、历史冻结七图与字幕哈希在生成前后均核对，配音及背景音乐直接复制原片AAC流并核验编码音频哈希相同。
- 对七个镜头各抽60帧比较ROI中心的光流位移，位置二阶差分的95分位降低约83%—91%；抽样预览中没有测得超过1像素/帧²的突变。此指标只证明抽样运动改善，不替代全片语义审核和实际播放观感。

预览是独立比较文件，没有替换工作台中的正式旧成片，也没有获得正式QC或发布回执。旧视频的音频证据问题仍然保留，不能因运动修复而改为审核通过。

证据位于同一预览目录的 `proof.json` 与 `seven-cut-motion-comparison.json`；首镜头四方案对照位于 `storage/smoke/stable-production-20261013/motion-precision-ab-20261017/`。该首轮对照使用FFmpeg 7.1，最终预览及生产回归使用9.0.2，二者分别记录。

代价是运动镜头的渲染计算和中间帧内存增加；首轮3秒对照的4倍方案编码约4.9秒，基线约1秒。最终文件仍是常规1080×1920视频，不增加播放端分辨率。

机制参考：[FFmpeg官方zoompan整数裁剪源码](https://github.com/FFmpeg/FFmpeg/blob/140fd653aed8cad774f991ba083e2d01e86420c7/libavfilter/vf_zoompan.c#L153-L194)。
