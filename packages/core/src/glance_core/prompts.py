"""任务提示词：服务端下发给 Agent 的指令，可在管理台查看与修改。

分层与运行时配置一致：workflow.TASK_INSTRUCTIONS 是内置默认值，
`task_prompt` 表按任务类型覆盖。AgentTask.instructions 保留创建时快照供审计；
Agent 实际领取后下载 bundle 时，服务端必须再次调用 resolve_instructions，
确保每轮执行拿到最新提示词。
"""

from __future__ import annotations

import re

from sqlmodel import Session, select

from glance_core.models import AuditEvent, TaskPrompt, TaskType, utc_now
from glance_core.runtime_config import effective_settings
from glance_core.settings import Settings
from glance_core.task_instructions import _SCRIPT_FACT_FIDELITY_RULE
from glance_core.task_schemas import IMAGE_BASE_FORBIDDEN_ELEMENTS, declared_field_names


def _render(template: str, settings: Settings) -> str:
    values = {
        "min_video_seconds": f"{settings.min_video_seconds:g}",
        "max_video_seconds": f"{settings.max_video_seconds:g}",
    }
    for key, value in values.items():
        template = template.replace(f"{{{key}}}", value)
    return template


def default_instructions(task_type: TaskType) -> str:
    # 局部导入避免与 workflow 形成循环依赖。
    from glance_core.workflow import TASK_INSTRUCTIONS

    return _render(TASK_INSTRUCTIONS[task_type], Settings())


_HANDSFREE_STAGE_FACT_RULE = """

## Handsfree 阶段事实一致性（五阶段共同硬规则）
本节优先于基础及自定义提示中禁止一切项目状态、缺少众筹声明不得报错、仅列举少数 blocker 的旧规则。
仅当研究明确支持尚在众筹、计划或尚未量产/交付，且不说明会把计划产品误当现成答案时，
在 full_narration 首个场景/问题句之后的产品介绍处用一次简短事实限定，不得声称已量产或已交付。
句式只用与来源相符的“众筹中的计划产品”“计划中的产品”“尚未交付的设备”等上述阶段+产品类别表达，
所在介绍句不超过 28 字；只放在 full_narration，不在标题、opening、CTA 或其他脚本字段重复。
retention_beats[*].beat 只记录节奏/动作关系，不复制阶段声明；
合法阶段限定仍只在 full_narration 出现一次。
422 禁用文案错误按明确标出的字段路径局部修正，
不删合法 full_narration 限定、已核验 claim 或无关节奏点；
若该字段本身被指出事实矛盾则按证据修正，不能为了消除其他字段的重复而移除已合法的事实限定。
不是所有视频必填众筹模板；来源阶段未知不得编造状态，研究与表述不符也必须拒绝。
脚本写作、SCRIPT_QC、SCRIPT_REVIEW_ASSIST 必须在渲染前核对：无来源阶段限定或真实阶段误导，
QC 列 error 且 passed=false，审核列 blocker 并返回非通过结论，不得只记 warning 后放行。
CONTENT_QC 与 FINAL_REVIEW_ASSIST 沿用同一来源与误导标准，不得新增“必须出现众筹字样”等隐性要求；
已有来源支持的简短限定且未误导时，不因缺少长声明打回。
阶段事实真假由角色 Agent 判断，schema 仅查句式。
禁止阶段/价格/观望开头、重复阶段、长免责、价格或购买观望建议；数字、卖点和素材证据硬门槛保持不变。

## Handsfree 必要适用条件与风险免责边界（五阶段共同规则）
本节覆盖基础/自定义旁白禁区中一律禁止兼容条件的旧规则，以及要求删除事实必要条件的历史相反建议。
来源支持且防止能力绝对化所必需的简短适用条件，不等于购买风险/观望免责；
不能仅因条件存在就报 error / blocker，不能机械要求把研究中全部条件逐项列举。
角色 Agent 按来源及实际表述判断必要范围，用简短准确的条件或收窄能力主张避免误导；
例如来源限定本地运行取决于模型和资源时，不能把它改成所有模型/工作负载均可本机运行的保证。
条件不得编造、扩大或用无来源细节补齐；删除条件使能力范围扩大、绝对化或失真，同样必须拒绝。
这不允许长免责、价格/观望建议或购买风险播报，也不豁免既有 Schema 禁词；
阶段限定的位置/次数/长度、数字保真、开场/节奏、权益及其他 QC 硬门槛保持不变。
SHORT_SCRIPT、SCRIPT_QC、SCRIPT_REVIEW_ASSIST、CONTENT_QC、FINAL_REVIEW_ASSIST 使用同一事实边界，
不得为服从历史建议删掉必要边界后又按绝对化返工；真实缺证或误导仍须定位并拒绝，不自动通过。
"""

_PRONUNCIATION_IDENTITY_RULE = """
## 读音与元数据身份一致性（五阶段共同规则，覆盖旧英文展开要求）
仅在研究未提供可靠读音依据，或正常易读口播修复需要时，可用已核验产品类别与自然中文规格说明承载口播。
准确品牌、完整型号与正式身份必须保留在既有 title_options、发布文案等可核对元数据，
与已核验研究一致且无混淆；不得编造中文品牌名或中文正式名称、删除关键数字事实或改变规格含义。
opening.product_identity 仍保留来源中的正式身份；难读名称不口播时，至少一项 title_options
必须准确完整包含该身份，不得仅留在发布文案或用泛化类别替换正式身份字段。
此规则覆盖旧“正式产品名必须在 full_narration 实际说出、不能只放标题”的要求；
正常英文口播仍支持。Schema 只验证旁白或标题有身份锚点，名称准确性与来源支持仍由角色 Agent 核验。
前8秒仍须明确产品身份并兑现核心价值/payoff，元数据不能替代成片中缺失或不清的身份与价值。
满足上述条件的脚本，审核不得仅因首次口播未展开难读英文全名打回；本节优先于基础及自定义展开要求。
缺少可核对完整身份、身份混淆或不清、关键数字事实丢失、前8秒身份/payoff未兑现仍列 error / blocker，
返回非通过结论，不得只记 warning。后续声音、画面及全部事实 QC 保持不变。
这是角色 Agent 的语义选择，不作字符串强换、拼接读音或向 ASR 注入预期答案。
"""

_HANDSFREE_VISUAL_EVIDENCE_RULE = """
## Handsfree 前置与成片共同画面事实标准（覆盖旧矛盾要求）
区分结构实拍、来源支持的规格说明与概念示意：规格事实可用诚实的产品说明镜头承载，
不要求把所有数字或内部结构拍出来，但不能将外观镜头称为看到了内部传感器或性能实测。
逐段检查裁剪、source_redactions 和布局之后实际留下的可辨识证据；被遮挡的标签不能算可见证据。
purpose 自述不是画面事实；旁白声称“直接看到”的结构或操作必须真实可见，否则改写说明方式。
实际演示、操作或性能测试的要求不能用规格说明镜头代替，核心展示硬门槛不变。
图内文字与视频后期标签是两件事：asset_request.text_rendering_enabled=false 和图像禁字规则保持不变。
SHORT_SCRIPT、脚本审核、VISUAL_PLAN / VISUAL_PLAN_QC 及 ASSET_QC 在未渲染阶段，
不得仅因计划或原生成图没有“AI概念示意·非实拍”判 error；不要为了标签开启生图文字、
移除 forbidden_elements，或要求新增不存在的 Schema 字段。按实际可执行机制核对：
pipeline 以可信 agent_generated_image 身份传入 generated_image_ids，MediaExecutor 仅在其实际使用的
image_card 成片区间通过 ASS 独立 Concept 层叠加标签，与底部旁白字幕分离；不是在素材图内生字。
素材未生成的计划阶段（含 VISUAL_PLAN / VISUAL_PLAN_QC）只核对 Schema 有效 asset_request、
允许生成类型/预算、拟用 image_card 和时间区间可执行性，以及既有 pipeline 受控生成登记/后期打标机制；
不得要求尚未产生的 artifact_id、generated_image_ids 实例、成片像素或可见标记。
ASSET_QC 核对已登记生成素材的确切 lineage 身份与拟用用途，不因纯图缺标签判错，
但注册、哈希、尺寸、UI 与全部实际素材门槛仍须验证，缺少现阶段所需证据须定位缺口。
purpose 自述或任意登记 asset_id 不能证明生成身份或最终可见。
这只描述视频后期叠加，不代表 cover、来源原图、FOOTAGE 或任意生成视频已经加标，不扩大生成范围。
当实际成片在生成图时段可见“AI概念示意·非实拍”，且旁白仅作来源支持的功能说明时，
core_demonstration 核对动作关系、解释序列与核心价值是否兑现，不一律要求真实 UI 或实拍操作。
CONTENT_QC / FINAL_REVIEW_ASSIST 必须真实查看每个生成图有效时段的标签，
缺标、遮挡或时间漏标须不通过并定位，不能因为代码说会加就放行。
purpose 中写概念不等于屏幕有标识；最终成片没有可见标识仍须返工。概念图不能证明实测、性能或实物操作，
旁白若声称实际测试、真实操作或直接观察结果，仍必须对应真实证据，不因角标放行。
开场兑现、画面/字幕质量、UI 与图像禁用元素、rights、budget、音频及技术硬门槛均不因标签放宽。
"""

_MPT_OPENING_IDENTITY_RULE = """
## MPT 开场身份叠加执行边界（覆盖历史及自定义的机制缺失推断）
本规则仅由已声明支持此能力的 MPT renderer 注入；不代表其他 renderer 具备该能力。
pipeline 正常通过当前脚本质检/审核门禁后，将确切脚本 opening.product_identity 传入
MptMediaExecutor；它沿既有 ASS OpeningIdentity 层独立显示正式身份，
从0秒到 min(8秒,实际配音时长)，与 Default 旁白字幕和 Concept 概念标签分层。
不自动拼接中文类别或陪护条件；类别、功能、价值及必要条件仍须实际旁白和同期字幕兑现。
VISUAL_PLAN / VISUAL_PLAN_QC 以服务端 opening_identity_overlay 的确切 script_task_id、
script_result_sha256、text 与时间区间核对可执行性；空身份或证据缺失不保证可见身份。
旧任务的 opening_identity_overlay_read_only 仅为现行只读执行说明，不改冻结材料、
输入 hash、旧结果、预算或 subject，不是旧成片已经显示身份的证明。
原图仍 text_rendering_enabled=false 且保留全部禁字禁logo禁项；正式身份由视频后期显示，
不能仅因纯概念素材无品牌文字或不仿真实外观而认定这个后期机制不可执行。
计划阶段不要求尚未产生的素材ID或成片像素；purpose 自述仍不是执行能力证据。
空白身份不生成身份层，超出可读安全区域则渲染失败关闭；缺失或不清身份仍不能通过。
最终成片必须实际核验正式身份、同期类别与价值、字幕及概念标签的可读性和时间覆盖，
不能凭代码、计划、标题或本规则推定成片通过；完整动态、音频及全部事实硬项保持不变。
Recovery 使用现行确切合同核对历史建议，不因旧合同未声明该机制而强制恢复难读品牌口播，
但不能用可执行身份层替代真实声音硬项或将旧视频、旧通过结果当作新版本证据。
"""

_HANDSFREE_SCRIPT_VISUAL_RULE = """

## Handsfree 渲染前脚本画面可执行性（写作/质检/审核共同硬规则）
VISUAL_PLAN / VISUAL_PLAN_QC 收到 visual_timing_evidence 且 boundaries 非空时，使用服务端冻结的实际 TTS
句子/词文本和 start_sec / end_sec 匹配对应画面，不按段数均分或估计口播位置；
这是音轨/句界哈希绑定的时间证据，不是 Agent 自声明可靠。语义匹配仍由角色 Agent 执行。
boundaries=[] 仅冻结配音身份和文件哈希，没有句界或语义同步证据；仍须通过后续实际 ASR 与完整成片 QC。
修复沿用同一配音证据；配音改变须正常重建计划，不能用旧证据猜新时间。
提交与审核需逐段核对实际口播开始及结束、核心动作画面是否及时兑现；
通过后执行器核对同一证据并保留该时间轴，不再按最近句界移动画面；有真实句界时字幕使用句界，空边界字幕仅估时，不能当作同步证据。
没有该证据的旧任务仍走既有安全校准及完整 QC，不把估计时间视为已核验的语义同步。
本节优先于基础或自定义提示的表达偏好、建议钩子或仅记 warning 规则。
opening.scene 和 visual_requirements 必须能由确切脚本版本对应的实际素材兑现，
或由明确允许且标为概念示意的生成计划兑现；实拍场景不能用想象、无关画面或概念图冒充。
footage 模式核对 scene_analysis 的真实动作与时间段，特别是开场 0-3 秒及钩子兑现；
scene_analysis.start_sec / end_sec / duration_sec 是源片坐标；opening、retention_beats、
visual_requirements 的时段是成片/口播目标坐标。两套坐标不能混为相等限制。
VisualPlan 的 narration_start_sec / narration_end_sec 指成片时间，source_start_sec / source_end_sec
指源片裁剪区间。源片后段 scene 可以裁切重排到成片开场，不要求从原片 0 秒开场，
不得仅因源片绝对位置不等于目标位置判 error；应核对对应 scene 的实际内容、可用时长与允许裁剪范围。
例如源片 31.76-32.84 秒的真实 scene 可以放到成片开场做 1.08 秒快切，再接其他有证据的画面。
重排不补造点击、未展示的操作或持续动作；0.6 秒素材不能凭空算够 17 秒连续操作，
定格或循环不等于新增实拍动作证据。时长不足应缩短该目标段、接其他真实素材或返工，不能猜测够用。
后期 source 区间仍须落在对应 scene 的真实边界内，完整成片时间轴与画面 QC 硬门槛不变。
事实、数字和单位原样保真仍是独立硬检查，不得借坐标重排放行事实改写。
未取得场景资料、visual_evidence_gaps 标记缺口或证据不足时，不猜测通过。
image_only 模式核对确切 research.source_images / source_images，不要求视频场景，
仅在已声明 asset_image_enabled 与 image_only_gen_budget 允许时使用概念生图范围，
明确标为概念示意，不得当实测、真实功能演示或生成产品事实证据。
没有证据的实拍开场要求是可执行脚本返工问题：写作改写为可证场景，
SCRIPT_QC 返回 error 且 passed=false，SCRIPT_REVIEW_ASSIST 返回 blocker / REVISE，
不得只 warning 放行到渲染；缺口无法修复时沿既有有限预算失败关闭，不请求人工接手。
这些检查不替代或放宽后续 VISUAL_PLAN_QC、CONTENT_QC 与成片审核。
"""

_HANDSFREE_INSTRUCTIONS = {
    TaskType.SHORT_SCRIPT: """

## Handsfree 无人值守脚本规则
下述硬约束优先于自定义提示词、推荐钩子与历史保留要求。
verified_claims 和实际 scene_analysis 是硬约束，suggested_hook / recommended_angle 只是建议。
opening、旁白和 visual_requirements 必须同时符合已验证事实与实际画面；推荐场景无法被现有素材支撑时，
重写开场和叙事，不保留矛盾 opening，不把无关画面当作功能或使用场景的证据。
image_only 模式按现有 source_images 和允许的生图范围规划，生成画面不能充当功能事实证据。
每次重写后重新核对 full_narration 的实际字数、自然口播预算与 estimated_duration_sec，
同步 retention_beats 和包含 CTA 的完整时长；不得扩写后沿用上一版字数或时长，不得虚填估值。
不能容纳时压缩非事实修饰，或在既定 {min_video_seconds}-{max_video_seconds} 秒范围一致重排；
不得改变硬时长约束或已生成 voice 的实际时长约束。
按 previous_script 与质检问题修正根因；证据不足时失败关闭，
存在 previous_script 时，reviewer_note 的局部返工约束优先于基础/自定义完整重写要求；
仅改问题相关字段与必要联动，保留未被问题或权威证据否定的标题、计划事实限定、权益表述与开场兑现，
不保留已指出的错误或矛盾字段，改后仍完整重新 QC。
由现有有限预算跳过并继续，不要求人工接手。
""",
    TaskType.RECOVERY: """

## Handsfree 无人值守恢复规则
本节优先于基础或自定义提示中的人工升级与矛盾保留要求。
只依据输入提供的 current_input / current_result、failure_result、validation_errors
和 repair_history 定位问题。
failure / validation_errors 与 repair_history 是历史诊断，不能当作当前契约。
须与 current_input.current_contract 提供的确切 source_task_id / source_task_type
及现行 instructions 比较。
旧契约冲突已被现行规则消除且仍有预算时，按现有允许动作签发对应角色修复或任务上下文重建，
不得仅因历史 422 / VALIDATION_CONFLICT 断言当前仍无解或 mark_recovery_blocked。
缺少 current_contract 时不得猜测规则已改变；无法核验时依现有失败关闭机制处理。
当前契约不是业务批准：不得自行放行、篡改 QC、修改原失败或绕过全部重新验证。
available_sources 只是有限索引，不含完整结果；不得要求读取未提供的上游内容或领取其它任务。
QC 问题用于定位，不是事实来源，也不能据此编造场景或认定新 claim。
BUSINESS_QC 输入指出 opening / 旁白与实际画面矛盾时，应选 reroute_to_role，
target_task_type=short_script，让写作角色核对完整研究与场景证据、改写不成立的开场，重新通过全部 QC；
仅脚本成立而镜头编排有误时修复 visual_plan。
must_preserve 只保留已验证事实、真实素材和审计记录，不能强制保留矛盾 opening，
不能要求补造不存在的场景。
必须说明与 repair_history 中矛盾要求的取舍，不累积不可能同时满足的 must_change / must_preserve。
不使用 escalate_to_human；无法验证、预算耗尽或重复无进展时返回 mark_recovery_blocked，
由既有机制自动跳过并继续，不扩大预算、不跳过 QC、不把失败改成通过。
""",
    TaskType.SOURCE_DISCOVERY: """

## Handsfree 无人值守来源规则
本节覆盖基础/自定义的仅搜新闻、等人工确认规则。主动寻找并核验可复用素材的公开许可、
适用范围和权威依据，优先可证许可或可合法自制的内容路线；不能把官方新闻等同素材许可。
事实来源可以是新闻，但实际画面须有独立可核验使用依据，或明确允许的自制概念说明，
不得冒充产品实拍、测试或品牌授权；缺少可靠素材路线则换来源，不请求人工补许可。
本节同时覆盖旧“是否允许转载交人工”“不判断许可”的规则。你可以核验并报告权威公开许可事实，
输出 candidates[] 的现有 rights_status / rights_note：
仅确有公开许可适用于拟用素材与制作/发布方式时，
如实填写 EXPLICIT_LICENSE；rights_note 完整保留许可发布者、权威引用 URL、素材范围、商业使用/改编、
署名及其他限制。核验不等于签发授权；不能凭官方身份、用户开启 Handsfree 或默认值授予许可。
未证实许可时不填 EXPLICIT_LICENSE，不把非空 note 当批准；说明缺口/真实替代路线，无法兑现则省略。
不得编造权利状态或把用户启用 Handsfree 当作第三方素材许可。
以输入 allowed_production_modes / require_video / asset_image_enabled / image_only_gen_budget 为准。
可用视频优先 footage；仅允许 image_only 且预算为正时，可回传无视频但有一手事实、可授权原图或
原创概念说明可兑现的候选。概念图不能冒充产品实拍/性能测试；第三方原图/参考重绘仍需实际许可。
未允许 image_only、require_video=true 或 disabled/预算0 时，不能回传仅靠生图且无视频的候选。
""",
    TaskType.CANDIDATE_ASSIST: """

## Handsfree 无人值守选题规则
本节优先覆盖基础及自定义的“辅助人工确认”“风险留给人工”“建议不直接批准”规则。
SELECT 仅允许进入 CONTENT_RESEARCH，不是事实、权益、脚本、成片或发布审批，也不是送人工确认。
本角色只根据输入判断选题价值及已明示路线是否可执行，不重新承担来源搜索或内容研究。
核对 source_context_read_only、现有 rights 登记和每项冻结的 production_mode；
登记、官方身份或非空 note 均不等于许可，不升级 rights_status。
image_only 仅在输入明确启用生图且预算为正时可用。无需第三方素材的原创概念说明是可选路线，
不得仅因缺少第三方转载许可证、rights_note 为空或研究尚未完成而拒绝该路线。
SELECT 时明确采用原创概念说明、不使用或重绘未授权第三方素材，而非写“仍需人工确认采用原创路线”。
不得把理论预算当作任意实拍能力；概念图不能冒充产品实物、操作或性能测试。
对必须复用第三方素材却缺乏使用依据、输入明确禁止或无法具备可执行路线时不得 SELECT。
Handsfree 不返回 HUMAN、不要求人工确认；无可执行路线返回 REJECT，服务端安全跳过并继续换题。
事实核验、来源适用范围及真实素材使用依据由后续 CONTENT_RESEARCH 和正常 QC 硬闸判断；
研究未取得充分依据不得进入无依据脚本，未知实测不得假称，不得将 SELECT 当作免检或许可。
""",
    TaskType.SCRIPT_QC: """

## Handsfree 无人值守质检规则
`passed=true` 仅表示所有硬性事实、时长和画面证据检查均通过。失败由服务端按既定有限预算自动返工；
预算耗尽或问题无法验证时自动跳过并换题，不转交人工。不得降低阈值、隐去问题或为了推进而改成通过。
""",
    TaskType.SCRIPT_REVIEW_ASSIST: """

## Handsfree 无人值守审核规则
按证据作出结构化结论，不要求人工审批。仅在事实、质检和输入证据均支持时通过；可明确修复的问题返回
具体 `REVISE`；不确定、证据不足或无法核验时返回非通过结论。服务端执行有限返工后自动跳过未解决项，
不得猜测、放宽质检或请求人工接手。
""",
    TaskType.FINAL_REVIEW_ASSIST: """

## Handsfree 无人值守审核规则
仅在完整检查的成片和输入证据均满足要求时通过。发现可修复问题时给出具体整改；证据不足、无法完整检查
或问题无法验证时返回非通过结论。服务端执行既定恢复预算后自动跳过未解决项，不要求人工批准，也不得
把缺少证据当成通过。
""",
    TaskType.PUBLISH_REVIEW_ASSIST: """

## Handsfree 无人值守发布包审核规则
只有发布包、成片、封面、内容与现有权利证据均可核验时才返回高置信度 READY。缺少权利或平台合规证据、
存在冲突或无法核验时返回 BLOCKED/HUMAN 等非通过结论；服务端有限复核后自动跳过，不要求人工补批。
不得推断授权、编造证据或仅为推进发布而放行。
READY 仅表示发布包本阶段就绪；实际平台账号、完整正文、标签、横竖封面裁切及 AI 声明，
由后续 DOUYIN_PUBLISH 在登记发布意图前强制核验，不要求本审核者登录或先行上传。
""",
    TaskType.DOUYIN_PUBLISH: """

## Handsfree 无人值守发布规则
仅当任务中的 `publish_policy.initiated_by=handsfree`、`user_clicked=false` 且
`explicit_user_opt_in=true` 时适用：发布前遇登录、扫码、短信、验证码或风控挑战时，
返回 `FAILED` 并结束；不等待人工或绕过挑战。
若发布意图已存在或最终点击后结果不确定，先只读核验抖音外部结果；
仅在取得真实外部 ID/URL 和已上传证据 Artifact 后返回 `PUBLISHED`，否则返回 `UNKNOWN`；
不再点击或上传。
服务端会自动终止该内容并继续其它选题。手动用户触发的任务仍按基础指令返回 `NEEDS_USER`。
""",
}

_HANDSFREE_AUDIO_RULE = """
## Handsfree 机器音频证据与自动修复共同规则
本节覆盖旧提示/历史整改中的必须原生听回或人工听审要求，不能把它们写入 acceptance_checks。
未原生听回不自动等于 audio=false 或物理音频故障；也不因 ASR available 自动通过。
禁止要求人工听审或人工最终验收，未原生听回不能作为唯一失败理由。
必须定位实际词段、独立识别置信或信号缺口；方法的一般局限不是已观测的错读证据。
实际音轨独立 ASR 与 PCM 联合证据不是“仅文字或技术 QC”；未原生听回时也可由角色 Agent
作证据充分的高置信无人验收判断，但所有音频硬项仍须有证据，真实缺口必须失败并有限修复。
从实际成片独立 ASR、词级时间戳/置信与 PCM 证据逐段判断充分性，诚实声明未听回及方法限制。
“耳垫/耳电”“读数/读术”等同音文字替换可能是转写歧义，不凭文字不一致断言实际错读；
EEG、品牌、数字等非同音差异仍须核验，不以预期文本纠正后伪造 ASR 结果。
确切冻结 acceptance_evidence 与 current_contract 是当前依据，历史 QC 结论不是音频事实。
证据不足时定位具体缺口，可按现有能力独立使用更强本地 ASR，或路由 short_script 改为来源支持的
易读表达后正常重制/验收；不可把同一歧义无限返工、要求人工、删硬项或编造已听回。
真正关键品牌或规格读音仍不确定时，BUSINESS_QC 用现有 reroute_to_role -> short_script
改为已核验类别与易读中文规格表达，保留关键数字和含义，再正常声画 QC；只改 visual_plan 不能修复读音。
ASR/PCM 不能单独证明音色、音乐平衡或所有杂音；这些方面仍需足够可核验证据，不确定则有限修复/跳过。
Recovery 的 rights error 可用 reroute_to_role -> content_research
核验新的真实使用依据或合法替代路线，
仅适用于 Handsfree 同内容的前置权益证据失败及结构化 rights error，不包括发布/平台/账号失败。
不能自行授予许可、改 rights 记录或恢复预算；没有可证路线时 mark_recovery_blocked 自动跳过。
自制素材须由现有 visual_plan/素材任务白名单规划并重新质检，不得生成虚假的实物性能证据。
"""


def resolve_instructions(
    session: Session,
    task_type: TaskType,
    *,
    handsfree: bool | None = None,
    schema_version: str = "1",
    base_settings: Settings | None = None,
    opening_identity_overlay: bool = False,
) -> str:
    """当前有效指令：共同身份规则，并按模式叠加无人值守规则。"""
    row = session.get(TaskPrompt, task_type.value)
    if row is not None and row.instructions.strip():
        template = row.instructions
    else:
        from glance_core.workflow import TASK_INSTRUCTIONS

        template = TASK_INSTRUCTIONS[task_type]
    if task_type in {TaskType.DOUYIN_PUBLISH, TaskType.ASSET_GENERATE_IMAGE, TaskType.PUBLISH_REVIEW_ASSIST} and row is not None and row.instructions.strip():
        from glance_core.task_instructions import TASK_INSTRUCTIONS

        # Legacy overrides must retain the current preparation and final-click boundaries.
        boundary = {TaskType.DOUYIN_PUBLISH: "发布", TaskType.ASSET_GENERATE_IMAGE: "图片",
                    TaskType.PUBLISH_REVIEW_ASSIST: "发布包审核"}[task_type]
        template += f"\n## 固定{boundary}执行边界：以下规则覆盖自定义提示词中冲突的执行方式\n"
        template += TASK_INSTRUCTIONS[task_type]
    current = effective_settings(session, base_settings if base_settings is not None else Settings())
    if opening_identity_overlay and task_type in {
        TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC, TaskType.SCRIPT_REVIEW_ASSIST,
        TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC, TaskType.ASSET_QC,
        TaskType.CONTENT_QC, TaskType.FINAL_REVIEW_ASSIST, TaskType.RECOVERY,
    }:
        template += _MPT_OPENING_IDENTITY_RULE
    use_handsfree = (handsfree is True if task_type == TaskType.PUBLISH_REVIEW_ASSIST
                    else current.handsfree_enabled if handsfree is None else handsfree)
    if task_type in {
        TaskType.CONTENT_RESEARCH, TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC,
        TaskType.SCRIPT_REVIEW_ASSIST, TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC,
        TaskType.CONTENT_QC, TaskType.FINAL_REVIEW_ASSIST, TaskType.RECOVERY,
    }:
        template += """
私有许可证据：用户针对指定素材和本次具体用途明确确认持有许可时，保留原话、提问上下文与素材对应关系，
可将该可追溯声明作为使用依据交由独立审核；不得仅因未附许可原件、完整合同或公开许可 URL 判定失败。
准确区分“用户确认持有许可”和“已审阅许可原件”，不得虚构授权方、条款或署名条件，不扩展至其他素材或用途。
核对声明与实际用途、已知限制及其他证据；存在具体矛盾或影响本次使用的范围歧义时如实报告，不能自动通过。
本规则不修改权益登记、不改写历史审核结果，也不代替正常研究、素材及成片审核。
"""
    if use_handsfree:
        template += _HANDSFREE_INSTRUCTIONS.get(task_type, "")
        if task_type in {TaskType.RECOVERY, TaskType.SCRIPT_QC, TaskType.SCRIPT_REVIEW_ASSIST}:
            template += """
scene_analysis 是有限摘要。scene_analysis_context_files 指向本次带租约 bundle 的 context JSON，
包含确切已完成上游批次的全部场景 ID、源时间与描述；按索引读取相关产品段，不能因前120条无关
画面断言完整素材不存在，也不能猜其它任务文件路径。摘要遗漏不构成事实；完整资料仍缺证据则不通过。
"""
        if task_type == TaskType.CONTENT_RESEARCH:
            template += """
Handsfree 来源/素材补证据：核验现有 source_context_read_only 与 recovery_plan 的具体缺口，
本节覆盖旧 image_only 必须依赖网页原图/source_images 的规则；仅适用已声明的可执行生成能力。
production_mode=image_only 时使用实际 asset_image_enabled/image_only_gen_budget 规划来源支持的
原创概念说明或有使用依据的配图；仅 asset_image_enabled=true 且预算>0、路线实际可兑现时
source_images 可以为空，不因没有视频/图片就否定明确允许的原创路线。
disabled/预算0不允许空素材生图路线。不能捏造产品外观/实测效果；
无许可的第三方图不得作为 source_images/重绘参考，不修改登记或免除后续 QC。
主动验证公开可复用许可及适用范围，或可执行的合法自制替代路线；只提交带来源的研究事实。
不得把官方身份、登记/非空 rights_note 当授权，不修改权益记录；
无可核验依据明确报告，自动跳过不找人工。
使用现有 verified_claims / primary_sources 记录核验到的许可原文含义、引用 URL、适用素材与限制，
许可事实不受旁白最多3条产品卖点 claims_used 限制；下游须核对完整 research，而非只核对已选旁白事实。
不得新增 Schema 字段或把查到新许可写成登记已改变；无依据如实记录 limitations，不假称已获许可。
"""
        if task_type in (
            TaskType.SCRIPT_QC,
            TaskType.SCRIPT_REVIEW_ASSIST,
            TaskType.VISUAL_PLAN,
            TaskType.VISUAL_PLAN_QC,
            TaskType.CONTENT_QC,
            TaskType.FINAL_REVIEW_ASSIST,
        ):
            template += """
来源/权利统一核对：source_context 或 source_context_read_only 提供现有登记、精确场景来源
与素材 Artifact 记录，不是服务端授权结论。read_only 是旧任务补充证据，未绑定旧 subject；
不得修改或升级 acceptance_subject。新 source_context_version=1 由签发时冻结并绑定 hash。
rights_status、rights_note、来源网页及研究证据必须一起核验；OFFICIAL_REFERENCE 仅官方参考
分类，不等于 EXPLICIT_LICENSE，也不要求凭空编造许可证。逐项判断实际使用方式的权利依据，
素材来源登记/哈希本身不是许可。确实缺少可核验的使用依据或 evidence_gaps 无法补足时，
SCRIPT_QC / VISUAL_PLAN_QC 在渲染前报 error / passed=false；成片验收 rights.check 不得通过。
不能把“没有提供来源登记”误写成“确定侵权”，也不能把有登记误写成“确定获授权”。
研究中确切版本的已核验公开许可与权威引用可作为实际使用依据；登记 note 为空不自动抹掉新证据，
也不因研究提及许可就自动通过：必须核对许可适用范围与实际素材/制作方式，来源身份/材料 hash 保持不变。
image_only 的实际原创概念素材须核对生成 Artifact 与引用/重绘来源，不能因为事实网页的登记仍为
OFFICIAL_REFERENCE 就假定每张原创图必须有第三方转载许可，也不能把 generated 类型当权利自动通过。
原图、参考重绘与品牌/产品实测仍按真实使用依据核验；不将登记升级为 SELF_OWNED / EXPLICIT_LICENSE。
VISUAL_PLAN 在选材前核对实际使用依据：OFFICIAL_REFERENCE 空 note 的网页原图只是参考线索，
不是可直接用于发布的素材；若完整研究未提供适用许可，不选该原图或把它用于参考重绘。
仅在已声明生成能力与预算允许时规划无需该第三方素材的原创概念说明，仍须素材 QC 与权益核验；
没有合法可执行路线则失败关闭，不能为了填满 segments 选择无使用依据的 asset_id。
"""
        if task_type in (
            TaskType.SHORT_SCRIPT,
            TaskType.SCRIPT_QC,
            TaskType.SCRIPT_REVIEW_ASSIST,
            TaskType.VISUAL_PLAN,
            TaskType.VISUAL_PLAN_QC,
        ):
            template += _HANDSFREE_SCRIPT_VISUAL_RULE
            template += """
核心展示在渲染前即核对可制作性；计划/质检同样使用成片验收的事实与真实展示标准。
素材不能支持实际产品动作时，只能改为来源支持的功能概念说明并明确标为概念示意，
不得冒充实物操作、性能测试或成品效果；不得用字幕代替必须兑现的真实演示。
VISUAL_PLAN_QC 对无法兑现的核心展示返回 error 且 passed=false，不把问题留到成片审核。
"""
    if task_type == TaskType.CONTENT_QC and schema_version in {"2", "3"}:
        from glance_core.task_instructions import TASK_INSTRUCTIONS

        # The v2 task owns BOTH complete-video contracts, including custom old templates.
        template += "\n## 成片验收 v2：以下规则覆盖旧提示词的仅提示/人工决定表述\n"
        template += TASK_INSTRUCTIONS[TaskType.CONTENT_QC]
        template += TASK_INSTRUCTIONS[TaskType.FINAL_REVIEW_ASSIST]
        template += """
本任务统一 CONTENT_QC 和 FINAL_REVIEW_ASSIST 的全部硬检查。必须完整核验成片及声音，
不能仅凭抽帧、文字或技术 QC 推测通过；身份/前8秒价值兑现、与真实或明确概念路线一致的核心展示、
重复镜头、节奏、
同期画面/旁白事实、字幕可读完整、声音完整清晰、rights 与来源及技术硬项均须真实核验。
输入 acceptance_subject 是服务端冻结的材料身份，subject 必须逐键原样回传；不能自选素材/hash。
contract_version=2，confidence 真实填写。checks 必须逐项填写 facts、rights、core_demonstration、
visual_narration、subtitles、audio、pacing、identity_and_payoff、technical。
每项填写 passed 和可核验 evidence，
声音/节奏/展示问题注明实际成片时间段。无法查看或核验任意硬项时 passed=false，不得假称观察到。
音频核验方式必须如实声明：能原生听回则报告实际观察；不能听回时，新签发的
audio_evidence_version=1 提供从最终成片独立识别的全文/词级时间戳和实际 PCM 信号，
不是 TTS 原文或来源字幕。逐段与旁白/字幕核对漏读、截断、错读、识别置信与静音/削波迹象；
ASR 和信号不能证明所有听感、杂音或音乐平衡，不能写“已听回”。结合实际证据判断清晰度
与一致性；无 ASR、缺片段、关键读音低置信且无法凭独立证据消歧、信号失败或其他硬项证据不足，
audio 必须 false 并定位缺口，不把普通同音转写差异直接当成物理错读。
旧任务不因这段规则获得新音频证据或 subject 升级，不得以音轨存在/技术QC推定通过。
只有高置信且所有硬项均通过才 passed=true；否则 issues 明确定位返工，交由现有预算修复。
不得请求人工、不触发发布、不篡改旧QC。相同材料的失败直接修订，不重复签发自己重审。
"""
    if task_type == TaskType.CONTENT_QC and schema_version == "3":
        from glance_core.task_instructions import CONTENT_QC_VISUAL_JOINT

        template += CONTENT_QC_VISUAL_JOINT
    if task_type in (
        TaskType.SHORT_SCRIPT,
        TaskType.SCRIPT_QC,
        TaskType.SCRIPT_REVIEW_ASSIST,
        TaskType.CONTENT_QC,
        TaskType.FINAL_REVIEW_ASSIST,
    ):
        if use_handsfree:
            template += _HANDSFREE_STAGE_FACT_RULE
        template += _PRONUNCIATION_IDENTITY_RULE
    if task_type in (
        TaskType.SHORT_SCRIPT,
        TaskType.SCRIPT_QC,
        TaskType.SCRIPT_REVIEW_ASSIST,
        TaskType.CONTENT_QC,
        TaskType.FINAL_REVIEW_ASSIST,
        TaskType.RECOVERY,
    ) and _SCRIPT_FACT_FIDELITY_RULE.strip() not in template:
        template += "\n" + _SCRIPT_FACT_FIDELITY_RULE
    if use_handsfree and task_type in (
        TaskType.SHORT_SCRIPT,
        TaskType.SCRIPT_QC,
        TaskType.SCRIPT_REVIEW_ASSIST,
        TaskType.VISUAL_PLAN,
        TaskType.VISUAL_PLAN_QC,
        TaskType.ASSET_QC,
        TaskType.CONTENT_QC,
        TaskType.FINAL_REVIEW_ASSIST,
    ):
        template += _HANDSFREE_VISUAL_EVIDENCE_RULE
    if use_handsfree and task_type in (
        TaskType.RECOVERY,
        TaskType.CONTENT_QC,
        TaskType.FINAL_REVIEW_ASSIST,
    ):
        template += _HANDSFREE_AUDIO_RULE
    if use_handsfree and task_type in (
        TaskType.SCRIPT_QC,
        TaskType.VISUAL_PLAN_QC,
        TaskType.ASSET_QC,
        TaskType.CONTENT_QC,
    ):
        template += """
## Handsfree QC 权利问题结构化约定（覆盖旧字段示例）
使用现有 issues[] Schema：权利/许可使用依据不足的硬错误统一写 severity=error、field=rights；
涉及具体镜头或素材时用 segment_index 定位，并在 message 说明确切 asset_id/使用方式与证据缺口。
不得将权利问题仅写为 segments[n].asset_id，否则无法按既有白名单路由补证据。
asset_id 不存在、错素材、画面不符等非权利错误仍写其真实字段，不得伪标 rights。
field 是语义 Agent 的结构化分类，服务端不解析 message 猜测权利问题，不因标为 rights 就授予许可。
Recovery 对确切 SCRIPT_QC/VISUAL_PLAN_QC/CONTENT_QC 的上述失败使用 BUSINESS_QC，
依原预算 reroute_to_role -> content_research 核验依据后重新通过正常门禁；
ASSET_QC 沿现有素材修复白名单，不借本约定扩展角色权限。不能改原失败结果或恢复归档项。
"""
    if use_handsfree and task_type in (
        TaskType.SHORT_SCRIPT,
        TaskType.SCRIPT_QC,
        TaskType.SCRIPT_REVIEW_ASSIST,
    ):
        template += """
## Handsfree 脚本保真与完整自检（覆盖基础/自定义的逐字符原样及历史漏检要求）
遵守上述已验证事实保真规则，包括口播乘号/范围连接词的有限例外；其余数值、单位、量级、
精度限定、大小写、型号、符号及兼容事实边界必须保真。
仅数量限定词后、数值与单位间不改变含义的排版空格可不同：研究“约 93 克”与脚本“约93克”
等价，不是事实 error，也不得仅为该空格触发返工；这不是通用去空格或事实归一化规则。
不得合并数字内部间隔（“1 2”不能变“12”），不得改变范围/乘积关系，或忽略正负、小数、
单位大小写或正式型号词间空格；48MP 必须保留 48MP，技术单位不得口语改写，
“约”不可丢，“三块或更多”不可变成“三块”；任何数值、精度或边界变化仍是硬错误。
SHORT_SCRIPT 提交前按当前有效规则完整自检：已核验主体及兼容/阶段边界、真实开场场景、
前8秒身份与核心价值兑现、当前实际旁白字符数/自然口播时长/estimated_duration_sec 与 CTA，
最多3条 claims_used 及全部数字保真、概念画面的明确标注计划与可执行素材依据。
发现硬错先在内部修正再提交既有 Schema，不输出自检新字段；自检不替代后续独立 QC。
局部返工仅改真实违规字段及必要联动，保留合法限定、权益表述与其他合格内容；
完整自检发现上轮未报告的既有硬错也必须修，不能因历史 QC 没报就保留错误，
不能只调整排版空格却原样提交未解决的硬错，也不能编造新的检查条件。
SCRIPT_QC / SCRIPT_REVIEW_ASSIST 每轮一次完成全部当前既有检查，不只报告第一个问题，
对每个真实违规定位精确字段和权威依据，区分 error / blocker 与 warning；
warnings 不得升格 error，历史未报告不等于本轮通过。所有硬项有充分证据才可通过；
无法核验仍按既有失败关闭和有限修复处理，不降低质量/事实/权益门槛、不要求人工接手。
"""
    if use_handsfree and task_type in (
        TaskType.SHORT_SCRIPT,
        TaskType.SCRIPT_QC,
        TaskType.SCRIPT_REVIEW_ASSIST,
        TaskType.VISUAL_PLAN,
        TaskType.VISUAL_PLAN_QC,
        TaskType.ASSET_QC,
        TaskType.CONTENT_QC,
        TaskType.FINAL_REVIEW_ASSIST,
        TaskType.RECOVERY,
    ):
        template += "\n## Handsfree 生成意图与不可删基础禁项（优先于基础/自定义及历史修复建议）\n"
        template += "IMAGE_BASE_FORBIDDEN_ELEMENTS 权威基础项：\n" + "\n".join(
            f"- {item}" for item in IMAGE_BASE_FORBIDDEN_ELEMENTS
        )
        template += """
上述基础项由服务器强制注入，Agent 只能追加，不能删改；提交时删除后，模型归一化会重新加入。
本地 validate VALID / 提交 COMPLETED 只说明契约可接收，不代表生成意图与禁项的语义矛盾已修复。
subject、information、purpose 等要求基础禁止内容时，角色 Agent 必须修正实际意图/画面需求；
必要时只按既有允许动作/角色白名单修订来源支持的脚本表达，不新增路由或授予许可。
不能只删禁项、移除要求字段、伪造许可或声称冲突已经消失；真实必要展示不能以无关画面替代。
VISUAL_PLAN 及其修复提交前，一次完整核对所有请求与平台硬约束、预算、时间轴及旁白兑现的语义一致性。
QC 一轮报告全量具体冲突及字段/依据，不提出删除基础禁项的建议，不能只修上一轮指出的一个请求。
Recovery 以 current_input.current_contract 的现行规则校验历史建议，不复制不可执行的删禁项方案。
保留基础禁项、text_rendering_enabled=false、预算、权益、开场兑现、事实及全部 QC 门槛；
原图禁字与受控视频后期概念标签仍分阶段检查，不要求原图内生标签，最终仍验真实可见像素。
现有素材/允许路线无法兑现必要展示时，按既有有限修复/跳过机制失败关闭，不要求人工接手。
这些语义核对由匹配角色 Agent 完成，服务器不按 subject 字符串猜测或自动批准。
"""
    return _render(template, current)


def lint_prompt(task_type: TaskType, instructions: str) -> list[str]:
    """返回不阻断保存的字段告警（BUG-6）。

    校验集 = 输出 Schema 递归属性名 + 枚举取值 + 该任务的输入字段 + 通用白名单；
    点号路径（如 `research.verified_claims`）按段拆分，逐段在校验集内即视为已声明。
    """
    declared = declared_field_names(task_type)
    common = {
        "input.json",
        "result.json",
        "previous_failures",
        "task_id",
        "context_files",
        "items",
    }
    known = declared | common
    referenced = {
        token
        for token in re.findall(r"`([A-Za-z][A-Za-z0-9_.-]*)`", instructions)
        if "_" in token and not token.startswith("glance-agent")
    }
    unknown = sorted(
        token
        for token in referenced - known
        if not all(segment in known for segment in token.split(".") if segment)
    )
    if not unknown:
        return []
    return [
        f"提示词引用了未在输入/输出契约中声明的字段：{', '.join(unknown)}（可能是笔误或契约漏声明）"
    ]


def list_prompts(session: Session) -> list[dict[str, object]]:
    """全部任务类型的提示词现状，供管理台展示。"""
    overrides = {row.task_type: row for row in session.exec(select(TaskPrompt)).all()}
    rows: list[dict[str, object]] = []
    for task_type in TaskType:
        override = overrides.get(task_type.value)
        default = default_instructions(task_type)
        rows.append(
            {
                "task_type": task_type.value,
                "instructions": override.instructions if override else default,
                "default_instructions": default,
                "customized": override is not None,
                "updated_at": override.updated_at if override else None,
                "lint_warnings": lint_prompt(
                    task_type, override.instructions if override else default
                ),
            }
        )
    return rows


def save_prompt(
    session: Session,
    task_type: TaskType,
    instructions: str,
    *,
    actor_id: str = "admin-ui",
) -> bool:
    """保存提示词；与默认值相同则删除覆盖行。返回是否发生了变更。"""
    text = (instructions or "").strip()
    if len(text) > 20_000:
        raise ValueError("任务提示词不能超过 20000 个字符")
    warnings = lint_prompt(task_type, text)
    default = default_instructions(task_type)
    existing = session.get(TaskPrompt, task_type.value)

    if not text or text == default.strip():
        if existing is None:
            return False
        session.delete(existing)
        action, detail = "prompt_reset", "恢复默认"
    elif existing is None:
        session.add(TaskPrompt(task_type=task_type.value, instructions=text))
        action, detail = "prompt_updated", text[:200]
    elif existing.instructions.strip() == text:
        return False
    else:
        existing.instructions = text
        existing.updated_at = utc_now()
        session.add(existing)
        action, detail = "prompt_updated", text[:200]

    session.add(
        AuditEvent(
            actor_type="human",
            actor_id=actor_id,
            entity_type="task_prompt",
            entity_id=task_type.value,
            action=action,
            metadata_json={
                "task_type": task_type.value,
                "detail": detail,
                "lint_warnings": warnings,
            },
        )
    )
    session.commit()
    return True
