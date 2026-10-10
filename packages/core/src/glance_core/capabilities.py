from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from glance_core.models import (
    CapabilityProvider,
    ExecutorNode,
    ProviderKind,
    ProviderStatus,
    utc_now,
)

BUILTIN_PROVIDERS = (
    ("download.local", "media.download.v1", "本地下载执行器", "1"),
    ("asr.whisper", "media.transcribe.v1", "faster-whisper 转写执行器", "1"),
    ("tts.edge", "tts.edge", "edge-tts 配音执行器", "1"),
    ("subtitle.local", "subtitle.compose.v1", "本地字幕执行器", "1"),
    ("render.ffmpeg", "video.render.v1", "FFmpeg 渲染执行器", "1"),
    ("qc.ffprobe", "media.technical_qc.v1", "媒体技术质检执行器", "1"),
    ("archive.git", "content.archive.v1", "Git 归档执行器", "1"),
)

AGENT_PROVIDERS = (
    ("agent.image", "image.generate.v1", "生图 Agent", "image"),
    ("agent.video", "video.generate.v1", "视频 Agent", "video"),
    ("agent.publish", "content.publish.v1", "发布 Agent", "publish"),
)


def ensure_builtin_providers(session: Session) -> None:
    for provider_id, capability_id, name, version in BUILTIN_PROVIDERS:
        if session.get(CapabilityProvider, provider_id) is None:
            session.add(
                CapabilityProvider(
                    id=provider_id,
                    capability_id=capability_id,
                    name=name,
                    kind=ProviderKind.EXECUTOR,
                    version=version,
                    priority=100,
                )
            )
    for provider_id, capability_id, name, role_id in AGENT_PROVIDERS:
        if session.get(CapabilityProvider, provider_id) is None:
            session.add(
                CapabilityProvider(
                    id=provider_id,
                    capability_id=capability_id,
                    name=name,
                    kind=ProviderKind.AGENT,
                    role_id=role_id,
                    version="1",
                    priority=100,
                )
            )
    # 发布由本机发布 Agent 执行；服务器不保存平台 Cookie 或发布凭据。
    legacy_placeholder = session.get(CapabilityProvider, "publish.unconfigured")
    if legacy_placeholder is not None:
        session.delete(legacy_placeholder)
    session.commit()


class CapabilityService:
    def __init__(self, session: Session, lease_minutes: int = 30) -> None:
        self.session = session
        self.lease_minutes = lease_minutes

    def report_node(
        self,
        *,
        node_id: str,
        display_name: str,
        version: str,
        capabilities: list[str],
        runtime: dict[str, Any],
        max_concurrency: int,
        active_jobs: int,
    ) -> ExecutorNode:
        node = self.session.get(ExecutorNode, node_id) or ExecutorNode(node_id=node_id)
        node.display_name = display_name
        node.version = version
        node.capabilities = sorted(set(capabilities))
        node.runtime_json = runtime
        node.max_concurrency = max(1, max_concurrency)
        node.active_jobs = max(0, active_jobs)
        node.last_seen_at = utc_now()
        self.session.add(node)
        for capability_id in node.capabilities:
            existing = self.session.exec(
                select(CapabilityProvider).where(
                    CapabilityProvider.capability_id == capability_id,
                    CapabilityProvider.kind == ProviderKind.EXECUTOR,
                )
            ).first()
            if existing is None and capability_id.startswith("tts."):
                self.session.add(
                    CapabilityProvider(
                        id=f"executor.{capability_id}",
                        capability_id=capability_id,
                        name=f"{capability_id} 配音执行器",
                        kind=ProviderKind.EXECUTOR,
                        version=version,
                        priority=100,
                    )
                )
        self.session.flush()
        for provider in self.session.exec(
            select(CapabilityProvider).where(
                CapabilityProvider.kind == ProviderKind.EXECUTOR,
                CapabilityProvider.capability_id.in_(node.capabilities),
            )
        ).all():
            provider.status = ProviderStatus.HEALTHY
            provider.last_seen_at = node.last_seen_at
            self.session.add(provider)
        self.session.commit()
        self.session.refresh(node)
        return node

    def route(self, capability_id: str) -> CapabilityProvider | None:
        return self.session.exec(
            select(CapabilityProvider)
            .where(
                CapabilityProvider.capability_id == capability_id,
                CapabilityProvider.enabled == True,  # noqa: E712
                CapabilityProvider.status.in_([ProviderStatus.HEALTHY, ProviderStatus.UNKNOWN]),
            )
            .order_by(CapabilityProvider.priority, CapabilityProvider.id)
        ).first()
