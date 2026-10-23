import socket
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_TTS_PROVIDERS: tuple[str, ...] = ("edge", "cosyvoice3", "kokoro")
KOKORO_VOICES: dict[str, int] = {"zm_009": 58, "zf_001": 3}
DEFAULT_COSYVOICE_ENDPOINT = "http://127.0.0.1:8765"
DEFAULT_COVER_STYLE_PRESET = (
    "账号 Brand Kit v1：深石墨背景（#101722），冷蓝单一强调色（#58B9FF），"
    "高对比电影感光比，几何构图，主体位于下三分之二或左侧，顶部留白，"
    "极简，无文字、无拼贴、无水印"
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def resolve_kokoro_voice(voice: str) -> tuple[str, int]:
    for name, sid in KOKORO_VOICES.items():
        if voice in {name, f"kokoro:{sid}"}:
            return name, sid
    raise ValueError("Kokoro 中文音色必须为 kokoro:58 (zm_009) 或 kokoro:3 (zf_001)")


def normalize_tts_provider(value: object, *, default: str | None = "edge") -> str:
    """Normalize and reject provider names at every configuration boundary."""
    if not isinstance(value, str):
        raise ValueError("TTS Provider 必须是字符串")
    provider = value.strip().casefold()
    if not provider:
        if default is None:
            raise ValueError("TTS Provider 不能为空")
        provider = default
    if provider not in SUPPORTED_TTS_PROVIDERS:
        raise ValueError(f"不支持的 TTS Provider：{provider}")
    return provider


def validate_tts_provider_endpoint(
    provider: object,
    endpoint: object,
    *,
    allow_unconfigured: bool = False,
) -> tuple[str, str]:
    """Validate the provider/endpoint pair without ever accepting a public target."""
    normalized_provider = normalize_tts_provider(provider, default=None)
    if endpoint is None:
        normalized_endpoint = ""
    elif isinstance(endpoint, str):
        normalized_endpoint = endpoint.strip().rstrip("/")
    else:
        raise ValueError("TTS Provider 地址必须是字符串")

    if normalized_provider in {"edge", "kokoro"}:
        if normalized_endpoint:
            raise ValueError(f"{normalized_provider} TTS 不接受 Provider 地址")
        return normalized_provider, ""

    if not normalized_endpoint:
        if allow_unconfigured:
            return normalized_provider, ""
        raise ValueError("CosyVoice3 必须配置本机 loopback Provider 地址")
    parsed = urlparse(normalized_endpoint)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or port is None
        or parsed.username
        or parsed.password
    ):
        raise ValueError("CosyVoice3 Provider 地址必须是无凭据的本机 HTTP loopback 地址")
    return normalized_provider, normalized_endpoint


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GLANCE_",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/database/app.db"
    huey_database_path: Path = Path("data/database/huey.db")
    bootstrap_agent_token: SecretStr = SecretStr("change-me-before-network-use")
    executor_token: SecretStr = SecretStr("change-me-before-network-use")
    admin_token: SecretStr = SecretStr("")
    ui_storage_secret: SecretStr = SecretStr("change-me-before-network-use")
    agent_lease_minutes: int = Field(default=45, ge=5, le=180)
    agent_max_inflight: int = Field(default=1, ge=1, le=16)
    # claim 长轮询上限（秒）：让 Agent 一次唤醒能等到新任务，降低排队延迟。
    agent_claim_wait_cap: int = Field(default=300, ge=0, le=840)
    # dispatch 预约租约时长（秒）：需大于宿主从取得 prompt 到 Agent 发出首个
    # 请求的耗时，部署时实测后确定；未 confirm 的预约到期自动回收。
    # 180 秒覆盖已观测的本地 Worker 启动延迟，同时仍保持预约租约短于正式租约。
    agent_reserve_seconds: int = Field(default=180, ge=10, le=600)
    # 场景分析分批阈值：超过该场景数按批拆分为多个子任务，服务端按批号合并。
    scene_batch_size: int = Field(default=60, ge=20, le=200)

    # 监听地址。默认只绑本机；设为 0.0.0.0 可让内网其它机器访问。
    # 注意：/ui 管理台与 /content-media 没有鉴权，对外暴露前请确认网络可信。
    host: str = "127.0.0.1"
    port: int = 8000

    #: 仅用于需要绝对地址的场景（发布包元数据等）；管理台内的媒体一律用相对路径。
    public_base_url: str = "http://127.0.0.1:8000"

    # 成片时长门槛（秒）；配音合成后即校验，渲染后 QC 复验。
    min_video_seconds: float = 20
    max_video_seconds: float = 90

    # 无 Key TTS（edge-tts）。语速用 edge-tts 语法（如 "+20%"）。
    tts_voice: str = "zh-CN-YunxiNeural"
    tts_rate: str = "+20%"
    tts_volume: str = "+0%"
    tts_provider: str = "edge"
    tts_provider_endpoint: str = ""
    # Server environment only; never editable through runtime settings or Agent input.
    kokoro_model_dir: str = ""
    # CosyVoice access token, if the isolated loopback service requires one.
    # This is never exposed to the console or Agent task payloads.
    cosyvoice_token: SecretStr = SecretStr("")

    @field_validator("tts_provider", mode="before")
    @classmethod
    def validate_tts_provider(cls, value: object) -> str:
        return normalize_tts_provider(value)

    @model_validator(mode="after")
    def validate_tts_configuration(self) -> "Settings":
        # An explicitly selected CosyVoice3 may remain unconfigured; readyz then
        # reports the missing local service instead of silently selecting Edge.
        validate_tts_provider_endpoint(
            self.tts_provider,
            self.tts_provider_endpoint,
            allow_unconfigured=True,
        )
        if self.tts_provider == "kokoro":
            resolve_kokoro_voice(self.tts_voice)
        return self

    # 视频编码器：libx264（默认）或 h264_nvenc（需要 NVIDIA 显卡）。
    video_encoder: str = "libx264"

    # 成片背景音乐：曲库在 data/bgm，由运营放入已授权音轨。
    bgm_enabled: bool = True
    bgm_track_id: str = ""
    bgm_volume: float = Field(default=0.15, ge=0.05, le=0.4)

    # 本地 ASR：faster-whisper 模型名（如 "large-v3" / "small"）。留空禁用。
    asr_model: str = ""
    asr_device: str = "auto"
    asr_compute_type: str = "float16"
    subtitle_alignment_fallback_model_dir: str = ""

    # yt-dlp 下载携带的 YouTube Cookies（Netscape 格式 cookies.txt 绝对路径）。
    # 留空则裸请求；用于破解 "Sign in to confirm you're not a bot" 人机验证。
    ytdlp_cookies_file: str = ""
    # Kickstarter 等站点的来源页/直链请求使用的浏览器 UA；应与导出 Cookies 的浏览器保持一致。
    media_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    )

    # 候选 enrich 批量调度：单个批量任务最多打包候选数（≤20）；
    # PENDING 超过 TTL 小时数的 enrich 任务自动取消。
    enrich_batch_size: int = 15
    enrich_task_ttl_hours: int = 48
    # 「页面有可下载视频」的唯一判据：video_url 为空时只有来源域名命中本白名单才放行。
    # 新闻媒体文章页即使 yt-dlp 内置整站提取器也必须先补录 video_url。
    video_platform_domains: str = (
        "youtube.com,youtu.be,vimeo.com,tiktok.com,bilibili.com,"
        "x.com,twitter.com,instagram.com,reddit.com"
    )
    assistant_auto_reject_enabled: bool = True
    assistant_auto_reject_max_score: int = Field(default=45, ge=0, le=100)
    # 当前产品分享流程首期全部人工验收；保留字段只为兼容已有运行时配置，worker 不据此自动批准。
    assistant_auto_approve_script_enabled: bool = False
    # Handsfree 只改变服务端是否自动推进已通过结构化闸门的任务；语义判断仍由 AgentTask 完成。
    # 默认关闭，避免升级后无意中改变现有人工流程。
    handsfree_enabled: bool = False
    handsfree_auto_topic_enabled: bool = True
    handsfree_auto_review_enabled: bool = True
    handsfree_auto_publish_douyin_enabled: bool = True
    handsfree_min_candidate_score: int = Field(default=70, ge=0, le=100)
    handsfree_daily_publish_limit: int = Field(default=1, ge=0, le=20)
    handsfree_publish_interval_minutes: int = Field(default=120, ge=0, le=1440)
    # Agent 失败后的智能恢复控制。动作白名单和绝对安全上限固定在服务端代码中；
    # 这些值只决定是否启用、预算和全局恢复协调并发。
    recovery_enabled: bool = True
    recovery_max_rounds: int = Field(default=3, ge=0, le=10)
    recovery_same_task_retries: int = Field(default=1, ge=0, le=3)
    recovery_unknown_attempts: int = Field(default=1, ge=0, le=2)
    recovery_max_parallel: int = Field(default=1, ge=1, le=8)
    asset_image_enabled: bool = True
    # 图文模式单条内容的最大生图次数（参考重绘 + 纯生图）；0 表示全部原图直用。
    image_only_gen_budget: int = Field(default=8, ge=0, le=8)
    cover_ai_enabled: bool = True
    cover_style_preset: str = DEFAULT_COVER_STYLE_PRESET
    content_qc_enabled: bool = True
    publish_prepare_enabled: bool = True

    # 成片归档 git 仓库目录（相对当前工作目录或绝对路径）。
    archive_enabled: bool = True
    archive_repo_dir: Path = Path("data/archive-repo")

    @property
    def content_dir(self) -> Path:
        return self.data_dir / "content"

    @property
    def agent_submission_dir(self) -> Path:
        return self.data_dir / "agent-submissions"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_advertise_url(settings: "Settings") -> str:
    """Agent 实际应该连的地址。

    绑定 0.0.0.0 时 `public_base_url` 里的 127.0.0.1 对别的机器没有意义，
    这里解析出本机在局域网里的真实 IP，让管理台生成的接入信息可以直接用。
    显式配置过 GLANCE_PUBLIC_BASE_URL（非默认值）时以配置为准。
    """
    configured = settings.public_base_url.rstrip("/")
    if configured and configured != f"http://127.0.0.1:{settings.port}":
        return configured
    # 绑到具体网卡时直接用它；下面几个值代表"全部网卡"，需要另行解析。
    if settings.host not in {"0.0.0.0", "::", ""}:
        return f"http://{settings.host}:{settings.port}"
    address = _detect_lan_address()
    return f"http://{address}:{settings.port}"


def resolve_remote_advertise_url(settings: "Settings") -> str:
    """生成远程 Agent 默认连接地址，绝不返回 127.0.0.1。"""
    configured = settings.public_base_url.rstrip("/")
    if configured and configured != f"http://127.0.0.1:{settings.port}":
        return configured
    if settings.host not in {"0.0.0.0", "::", "", "127.0.0.1", "localhost", "::1"}:
        return f"http://{settings.host}:{settings.port}"
    return f"http://{_detect_lan_address()}:{settings.port}"


def _detect_lan_address() -> str:
    try:
        # 不会真的发包：连一个外部地址只是让内核选出默认出口网卡。
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("10.255.255.255", 1))
            return str(probe.getsockname()[0])
    except OSError:
        return socket.gethostbyname(socket.gethostname())
