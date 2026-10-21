# 2026-10-21 图片运动采样复核

## 当前结论

用户在抖音端仍看到推拉“一跳一跳”，并明确指向视频本身的观感。10月20日的几何、时间戳和正式QC通过，不能作为已经解决该观感问题的证明。

每个图卡镜头使用一张固定图片，由FFmpeg逐帧计算变换；不存在AI为相邻帧重新绘图的过程。当前30fps基线的固定共同特征检查没有发现重复帧、反向或停走。运动位置连续仍可能伴随细节闪烁和有限帧率的离散观感，不能把这次问题直接归因为“帧内容不连贯”。

## 对照与候选

同图、同速度的6秒实验比较30fps基线、2倍空间采样、真实60fps、120fps四帧混合后输出30fps。真实60fps在变换前增加采样，179个内部新增位置均在前后原位置之间；单帧位移约减半。独立测量中，2倍空间采样的纹理拉普拉斯能量变异系数下降34.10%，平均能量下降2.79%，存在少量柔化。时间混合没有显示收益，未加入候选。

原生60fps与2倍空间采样组合小样为6秒、360帧；该小样只经过一次编码，首级缩放也与正式链路不同，不能用它替代正式两级编码后的完整成片验收。

候选正式链路对纯图片项目使用60fps，在2倍画布上完成运动再缩小；每秒运动速度、范围与焦点保持原规则。混合/原视频项目仍使用30fps。累计帧切点、字幕时钟、编码与拼接使用一致帧率。当前缓存版本为`local-ffmpeg-v5-global-vbv`。

纯图片准备与镜头编码采用libx264、CRF10、mbtree=0；整片导出增加一次相同质量设置的全局编码，配置maxrate=20000kbps、bufsize=40000kbits。没有BGM时复制音频，有BGM时保留既有混音规则。普通VideoProject的两个预算字段默认0，保留原有导出行为；预算只参与导出缓存指纹。没有增加依赖。

新revision保存不可变`render-parameters.json`，并通过`outputs.json`绑定其哈希，包含fps、spatial_sampling、video_crf及两个码率预算。审核模板读取该revision的参数并核对实际帧率、revision和输出凭据，不能用全局可变请求推断历史视频的参数。新参数缺失或不匹配时拒绝签发。历史30fps凭据保持兼容。

## 旧Glance比较

保留的旧发布视频为32.03秒、30fps，约92.5%的时间使用真实视频片段，静态图推拉仅2.4秒。它不能证明旧系统的全图片慢速推拉优于当前系统；当前业务也没有全局关闭真实视频路线。

## 验证状态

当前完整候选使用冻结七图、同一Kokoro配音、脚本、七段计划与严格字幕对齐。14个准备文件按SHA复用，9375字节准备代码前缀与已验版本完全相同；之后真实新MPT/VideoProject链路重新执行并签出新revision。这不是重新生图或重跑全部准备计算，不修改业务数据库，也不继承原片QC或发布状态。

当前文件：`storage/smoke/stable-production-20261013/motion-production-global-vbv-preview-20261021/render/revisions/571085a10acc42ec808e2e2bd4c7c269/final.mp4`。SHA `1168d274e8d351977d21e5772234e6979367a92c6b28f39f60fdba96835f351b`；1080×1920、60fps、1936帧、32.266667秒、83,447,180字节。原公开版本为16,499,736字节，当前候选仍约为其5倍。

独立技术核验156项通过，覆盖完整音视频解码、PTS、七段切点、15条字幕与121词映射、实际编码参数、不可变凭据和缓存复用。报告总判定保留PARTIAL：运行输入/结果冻结了三个渲染源文件，但没有同时捕获pipeline.py运行前后哈希。当前pipeline哈希符合冻结版本，绑定逻辑另作函数级检查；不能补写为当时已捕获的运行证据。

准备/镜头无VBV，整片SEI为CRF10/mbtree0/VBV 20000/40000。平均视频码率约20.49Mbps，初始缓冲允许短时超过20Mbps，不能将maxrate描述为任意时间窗绝对上限。实际DTS/NAL预算筛查在20Mbps下约需17.14Mbit，小于配置40Mbit；该筛查及本机解码不是完整HRD符合性证明或目标平台验收。

证据：[当前候选结果](../storage/smoke/stable-production-20261013/motion-production-global-vbv-preview-20261021/result.json)、[156项独立技术核验](../storage/smoke/stable-production-20261013/motion-production-global-vbv-independent-20261021.json)、[整片预算筛查](../storage/smoke/stable-production-20261013/motion-production-global-vbv-cpb-budget-check-20261021.json)。

源码独审通过，无阻断代码发布的合同问题；14个文件匹配冻结哈希，独立41项回归及绑定、缓存、音频分支核验通过。作者169项、29项定向及43项兼容回归通过，其中29项有16项与169项重叠；不将它们相加为不重复总数。Ruff、compileall、diff-check通过。源码提交`8f68051970db593882ceb1271a25b411fb61167f`已推送两个既有分支。证据：[源码独立门禁](../storage/smoke/stable-production-20261013/motion-global-vbv-source-independent-20261021.json)。

实际v5 VideoProject另以五份按SHA核验的prepared缓存执行后续镜头编码和全局导出，原测量阈值不变。五种运动均为0反向/0低步长/0跳步，RMSE分别为推近0.00521457、推远0.00415637、聚焦0.00525081、左移0.01587328、右移0.01621893像素。每段360帧/60fps/6秒，完整解码与AAC逐包复制核验通过。证据在Stable工作树`.artifacts/native-image-sampling-20261021/v5-source-global-vbv/report.json`，不是只引用FFmpeg原型。前四例的命令trace未留存，实际SEI、不可变spec和缓存记录均已核对；不能称为完整命令审计。

## 真实图片复核与取舍

独立测量先精确重现旧对照92项数字，再按同样方法测量实际推近和推远两段，保留固定共同特征、全部离群及原阈值。相比无界CRF10，全局限码率后锚点步长RMSE由约0.00704/0.00703增至0.01123/0.01113像素，同源纹理能量下降5.2%/9.7%；不能宣称没有质量回退。相比同为60fps的CRF18候选，RMSE增加40.3%/3.1%，同源纹理能量增加6.1%/12.1%，各配对的共同特征人口不同，数字不能交叉相除。

旧公开30fps本地成片另按镜头局部时间与新片隔帧对齐，轨迹差RMS约0.072/0.112像素，无精确重复ROI；没有将30fps与60fps阈值混用。这说明运动路径接近，不证明用户低帧率观感已解决。新片实际质量仍为NOT_ESTABLISHED，源码门禁PASS不能覆盖它。见[完整真实图片报告](../storage/smoke/stable-production-20261013/motion-real-image-global-vbv-comparison-independent-20261021.json)。

### 保留的历史对照

早期CRF18预览耗时512.30秒，1080×1920、60fps、1936帧、32.266667秒；SHA为`1ff51fc98d8fcfeb03b4333635e7def09379b9ab341f0496610cace60f2cf1de`。

源码独审的100项非编码测试及11项参数核验通过，但不构成部署门禁通过。持久化真实网格测试在推近/推远的部分极小位移中仍出现离群，严格运动回归为FAIL。独立复测收紧LK收敛精度后仍存在，灰度重心法交叉检查也支持约0.03–0.04像素的调制，位置集中于首帧对和关键帧250附近；这些数据不能证明肉眼倒退或真实丢帧。

同一固定源角点的阶段对照显示：CRF18候选raw变换局部误差小，第一次x264编码后增大，第二次继续增大。该候选未部署或发布，基线、FAIL和当前已发布视频保持不变。

已排除的对照：两级CRF18/mbtree0均增加`ipratio=1:pbratio=1`。推近的RMSE虽略降，严格离群从2个变为7个；推远仍有8个离群。因此不将该参数组合纳入候选源码。上游确认CRF值不等于所有帧相同QP，且关闭MB-tree时pbratio生效；这只是对照的依据，并非有效修复的证明。见[x264维护者说明](https://mailman.videolan.org/pipermail/x264-devel/2010-February/006943.html)及[FFmpeg libx264文档](https://www.ffmpeg.org/ffmpeg-codecs.html#libx264_002c-libx264rgb)。

证据位于`storage/smoke/stable-production-20261013/`的`motion-sampling-ab-independent-20261021.json`、`motion-sampling-source-independent-20261021.json`及`motion-full-preview-20261021/result.json`。完整预览是候选输出，不能称为正式QC或新发布结果。

完整预览的独立技术验收见`motion-full-preview-independent-20261021.json`：1935个相邻PTS全部为1/60秒，全音视频严格解码通过；七段累计帧边界为231/578/852/1127/1502/1690/1936。15条旁白字幕与冻结121词映射一致，音视频尾差11毫秒。该PASS仅限技术完整性，不覆盖仍FAIL的网格微步回归或用户实际观感。

两级CRF10且无VBV的五种合成运动严格通过，但完整输出246,481,219字节、约61Mbps，交付成本过高。相对同为60fps的CRF18候选，真实两段图片共同特征步长RMSE改善约12.8%/35.1%，仍有极小离群，部分纹理指标变差；这不是与原公开30fps直接比较，不能从合成图通过推断所有真实图完美。见`motion-full-preview-q10-final-independent-20261021.json`和`motion-real-image-q10-comparison-independent-20261021.json`。

逐镜头CRF16/VBV的推远仍有微小低步长FAIL，弃用。逐镜头CRF10/VBV虽合成运动通过，拼接却重复使用每段初始缓冲额度，整片20Mbps预算需约107.73Mbit、超过40Mbit，也未采用。当前实现改为整片统一限制，所有历史输出与失败报告保留。

## 发布边界

代码`8f68051970db593882ceb1271a25b411fb61167f`已于17:52隐藏部署到Main；API worker15108/launcher20044取代旧21784/13304，认证200、匿名401、UI200。切换前后八表逐行哈希及118份登记媒体完全一致，运行参数保持原值。见[实际切换](../storage/smoke/stable-production-20261013/motion-sampling-service-cutover-v5-20261021.json)及[切换后保全](../storage/smoke/stable-production-20261013/motion-sampling-cutover-after-v5-global-release-20261021.json)。本次决定是通过源码合同后的代码投产，不是对真实质量、主观观感或平台兼容性的批准。

[独立部署验收](../storage/smoke/stable-production-20261013/motion-sampling-deployment-independent-20261021.json)30项必要检查通过，另对实际118媒体重新校验。切换后的额外数据库恒等诊断为FALSE，原值保留：仅`mpt_maintenance_last_run`和`mpt_terminal_recovery_scan_cursor`由既有维护线程在快照之后更新，具体行、时间及源码依据见报告；其余七表和运行状态不变。没有将这两项豁免后重写切换快照PASS。

当前公开作品仍为[7698443917590629670](https://www.douyin.com/video/7698443917590629670)，对应30fps旧成片。该内容已PUBLISHED，现有正常流程没有同一内容成片修订并重发入口。新候选不能继承旧片第38代QC或发布回执，不能通过直接改状态、替换旧Artifact或复制候选绕过去重来重发。

## 上游依据

- [MoviePy原始问题183](https://github.com/Zulko/moviepy/issues/183)：维护者建议大画布变换后缩小，报告者确认改善；该历史建议针对取整问题，不代表当前浮点变换已被它证明修好。
- [FFmpeg fps](https://ffmpeg.org/ffmpeg-filters.html#fps)：仅改变输出fps会通过丢帧/重复帧实现，不能代替变换前的真实新增位置。
- [FFmpeg minterpolate](https://ffmpeg.org/ffmpeg-filters.html#minterpolate)和[tmix](https://ffmpeg.org/ffmpeg-filters.html#tmix)：运动估计插帧与时间混合是不同处理。已知静态图片几何可直接计算新采样位置，本次未增加光流插帧依赖。

抖音重新编码是否保留60fps，以及用户对正式链路新成片的实际观感，仍须使用对应的新成片核实。播放器播放到结尾、零精确重复帧或轨迹误差较小，都不能单独证明观感问题彻底解决。
