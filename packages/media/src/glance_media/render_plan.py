import math
from typing import Literal

from glance_core.task_schemas import SourceRedaction
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RenderSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str | None = None
    asset_id: str | None = None
    source_start_sec: float | None = Field(default=None, ge=0)
    source_end_sec: float | None = Field(default=None, ge=0)
    source_redactions: list[SourceRedaction] = Field(default_factory=list)
    narration_start_sec: float = Field(ge=0)
    narration_end_sec: float = Field(gt=0)
    layout: Literal["center_crop", "blur_background", "fit", "image_card", "screen_capture"]
    purpose: str
    motion: Literal[
        "ken_burns_in",
        "ken_burns_out",
        "pan_left",
        "pan_right",
        "focus_product",
        "hold",
    ] = "ken_burns_in"
    transition: Literal["hard_cut", "short_fade"] = "hard_cut"
    focus_x: float = Field(default=0.5, ge=0, le=1)
    focus_y: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_references(self) -> "RenderSegment":
        if (self.scene_id is None) == (self.asset_id is None):
            raise ValueError("Exactly one of scene_id or asset_id is required")
        for field_name in (
            "source_start_sec",
            "source_end_sec",
            "narration_start_sec",
            "narration_end_sec",
        ):
            value = getattr(self, field_name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{field_name} must be finite")
        if self.narration_end_sec <= self.narration_start_sec:
            raise ValueError("Narration end must be after start")
        if self.scene_id is not None and (
            self.source_start_sec is None
            or self.source_end_sec is None
            or self.source_end_sec <= self.source_start_sec
        ):
            raise ValueError("Scene segments require a valid source interval")
        return self


class RenderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[RenderSegment] = Field(min_length=1)
