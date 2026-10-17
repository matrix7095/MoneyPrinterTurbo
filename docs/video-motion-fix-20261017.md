# 图片成片慢速运动卡顿修复

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
