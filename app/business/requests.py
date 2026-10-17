# Extracted request contracts from Glance; keep wire fields compatible.
from typing import Any
from typing import Literal

from glance_core.agent_roles import AgentRole
from glance_core.models import TaskType
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DispatchRequest(StrictRequest):
    agent_id: str = Field(min_length=1)
    role_id: AgentRole | None = None
    task_type: TaskType | None = None
    runtime: "AgentRuntimeReport | None" = None
    workspace_dir: str = Field(default="", max_length=1000)
    launcher: str = Field(default="", max_length=1000)
    task_id: str | None = Field(default=None, max_length=80)
    run_id: str | None = Field(default=None, pattern=r"^run_[0-9a-f]{32}$")
    run_generation: int | None = Field(default=None, ge=1, strict=True)
    request_id: str | None = Field(default=None, max_length=120)


class DeploymentSkill(StrictRequest):
    name: str
    path: str = ""
    content_hash: str


class DeploymentReportRequest(StrictRequest):
    agent_id: str = Field(min_length=1)
    role_id: AgentRole
    launcher_path: str
    skill: DeploymentSkill
    host: dict[str, Any] = Field(default_factory=dict)
    model: dict[str, Any] = Field(default_factory=dict)
    self_check: dict[str, str] = Field(default_factory=dict)
    protocol_version: str = "1"
    request_selftest: bool = True


class SearchTaskRequest(StrictRequest):
    direction: str = Field(min_length=1, max_length=160)
    query: str = Field(default="", max_length=500)
    purpose: str = Field(default="发现近期值得制作的海外新项目", max_length=1000)
    max_candidates: int = Field(default=10, ge=1, le=30)


class AgentRuntimeReport(StrictRequest):
    app_name: str = Field(default="", max_length=80)
    app_version: str = Field(default="", max_length=80)
    model_name: str = Field(default="", max_length=160)
    model_tier: str = Field(default="", max_length=40)
    reasoning_effort: str = Field(default="", max_length=40)
    skill_name: str = Field(default="", max_length=100)
    skill_version: str = Field(default="", max_length=40)
    device_label: str = Field(default="", max_length=100)
    protocol_version: str = Field(default="1", max_length=20)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    self_check: dict[str, str] = Field(default_factory=dict)


class ClaimRequest(StrictRequest):
    agent_id: str = Field(min_length=1)
    role_id: AgentRole | None = None
    # deprecated：旧启动器仍可能携带该字段；分发只看 role_spec，此处不消费也不校验。
    # 约定：协议入参中不被消费的字段不得携带严格枚举校验（枚举收缩时会误伤旧客户端）。
    capabilities: list[str] = Field(default_factory=list)
    runtime: AgentRuntimeReport | None = None


class HeartbeatRequest(StrictRequest):
    runtime: AgentRuntimeReport | None = None


class WhoamiRequest(StrictRequest):
    agent_id: str = Field(min_length=1)
    role_id: AgentRole
    runtime: AgentRuntimeReport | None = None


class SimilarityQueryRequest(StrictRequest):
    task_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=500)
    url: str = Field(default="", max_length=2000)
    direction_key: str = Field(default="", max_length=120)
    limit: int = Field(default=10, ge=1, le=30)


class CompleteRequest(StrictRequest):
    agent_id: str = Field(min_length=1)
    result: dict[str, Any]


class FailRequest(StrictRequest):
    agent_id: str = Field(min_length=1)
    retryable: bool
    error_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    #: 租约已失效时的身份佐证：与 assigned_agent_id 同时匹配才允许迟到上报。
    attempt: int | None = Field(default=None, ge=0)


class ConfirmClaimRequest(StrictRequest):
    agent_id: str = Field(min_length=1)


class PublishIntentRequest(StrictRequest):
    agent_id: str = Field(min_length=1)


class RejectScriptRequest(StrictRequest):
    note: str = Field(min_length=1, max_length=2000)


class DouyinPublishRequest(StrictRequest):
    confirm_not_published: bool = False


class HostProcess(StrictRequest):
    pid: int = Field(gt=0, strict=True)
    started_at: str = Field(min_length=1)
    instance_id: str = Field(min_length=1, max_length=100)
    binary_path: str = Field(min_length=1, max_length=1000)
    binary_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    version: str = Field(min_length=1, max_length=100)


class HostAttachRequest(StrictRequest):
    generation: int = Field(ge=1, strict=True)
    expected_host_epoch: int = Field(ge=0, strict=True)
    parent_thread_id: str = Field(min_length=1, max_length=100)
    process_identity: HostProcess


class ExpectedHostBinding(StrictRequest):
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    generation: int = Field(ge=1, strict=True)
    host_epoch: int = Field(ge=1, strict=True)
    parent_thread_id: str = Field(min_length=1, max_length=100)
    process_identity: HostProcess


class StopRunRequest(StrictRequest):
    expected_binding: ExpectedHostBinding


class ReconcileHostLossRequest(StrictRequest):
    launch_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$", strict=True)
    generation: int = Field(ge=1, strict=True)


class SpawnIntentRequest(StrictRequest):
    generation: int = Field(ge=1, strict=True)
    host_epoch: int = Field(ge=1, strict=True)
    event_id: str = Field(min_length=1, max_length=100)
    offer_hash: str = Field(min_length=1, max_length=100)


class NativeStarted(StrictRequest):
    parent_thread_id: str = Field(min_length=1, max_length=100)
    parent_turn_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=100)
    agent_path: str = Field(min_length=1, max_length=300)
    child_id: str = Field(min_length=1, max_length=100)


class NativeChild(StrictRequest):
    child_id: str = Field(min_length=1, max_length=100)
    parent_thread_id: str = Field(min_length=1, max_length=100)
    native_agent_type: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    ephemeral: StrictBool
    started: NativeStarted
    metadata_rpc_id: StrictStr | StrictInt
    issued_prompt_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class HostObservationRequest(StrictRequest):
    generation: int = Field(ge=1, strict=True)
    host_epoch: int = Field(ge=1, strict=True)
    event_id: str = Field(min_length=1, max_length=100)
    outcome: Literal["VERIFIED", "HEARTBEAT", "TERMINAL", "REJECTED", "UNKNOWN", "OBSERVATION_LOST"]
    child: NativeChild | None = None


class HostIntentRecord(StrictRequest):
    state: Literal["SPAWN_INTENT", "VERIFIED", "CLAIMED", "TERMINAL", "REJECTED", "UNKNOWN", "OBSERVATION_LOST", "ABANDONED_BEFORE_CLAIM"]
    host_epoch: int = Field(ge=1, strict=True)
    offer_hash: str = Field(min_length=1)
    child: NativeChild | None
    claim_id: str | None
