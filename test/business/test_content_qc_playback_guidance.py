"""Content QC playback stays within authorized media and evidence boundaries."""

from glance_core.models import TaskType
from glance_core.prompts import default_instructions


def test_content_qc_allows_existing_workbench_and_preserves_full_review_gate():
    instructions = default_instructions(TaskType.CONTENT_QC)

    assert "宿主原生视频和音频理解能力" in instructions
    assert all(
        boundary in instructions
        for boundary in ("file://", "遵守拒绝", "不得绕过策略", "媒体服务器", "切换浏览器")
    )
    assert all(
        boundary in instructions
        for boundary in (
            "用户已授权",
            "同一 `content`",
            "`video_artifact_id`",
            "若工作台显示 Artifact ID",
            "冻结相对路径",
            "SHA-256",
            "不得猜测或替换素材",
        )
    )
    assert all(
        boundary in instructions
        for boundary in ("ASR", "抽帧", "不能单独证明", "如实返回非通过", "不得降低完整动态/音频门槛")
    )
