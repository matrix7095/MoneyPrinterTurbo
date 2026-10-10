from pathlib import Path

from platformdirs import user_data_path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PUBLISH_REQUIRED_CAPABILITIES = (
    "浏览器控制",
    "本机 Chrome 会话",
    "social-auto-upload",
    "截图",
    "结构化 JSON",
)


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GLANCE_AGENT_",
        extra="ignore",
        populate_by_name=True,
    )

    api_url: str = "http://127.0.0.1:8000"
    token: SecretStr
    agent_id: str = Field(validation_alias="GLANCE_AGENT_ID")
    role_id: str
    workspace_dir: Path = user_data_path("glance-agent") / "tasks"
    timeout_seconds: float = 30
    app_name: str = ""
    app_version: str = ""
    model_name: str = ""
    model_tier: str = ""
    reasoning_effort: str = ""
    skill_name: str = ""
    skill_version: str = "1.0.0"
    device_label: str = ""
    protocol_version: str = "1"
    capabilities: list[str] = Field(default_factory=list, max_length=100)

    def runtime_report(self) -> dict[str, object]:
        capabilities = list(
            dict.fromkeys(value.strip() for value in self.capabilities if value.strip())
        )
        if (
            self.role_id == "publish"
            and not capabilities
            and "capabilities" not in self.model_fields_set
        ):
            capabilities = list(PUBLISH_REQUIRED_CAPABILITIES)
        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "model_name": self.model_name,
            "model_tier": self.model_tier,
            "reasoning_effort": self.reasoning_effort,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "device_label": self.device_label,
            "protocol_version": self.protocol_version,
            "capabilities": capabilities,
            "self_check": {},
        }
