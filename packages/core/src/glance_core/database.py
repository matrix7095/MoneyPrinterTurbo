from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from glance_core.settings import DEFAULT_COSYVOICE_ENDPOINT, Settings

# 模型必须在 metadata.create_all 之前注册。
from glance_core import models as _models  # noqa: F401  isort:skip


def build_engine(settings: Settings) -> Engine:
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def create_db(engine: Engine) -> None:
    """Create the current schema once at process startup.

    SQLite upgrades are additive and idempotent; immutable historical inputs are not backfilled.
    """
    SQLModel.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        # Add nullable identities only: legacy immutable task inputs/results are untouched.
        for table, column in (("content", "current_revision_id"),
                              ("agent_task", "revision_id"), ("artifact", "revision_id"),
                              ("stage_run", "revision_id"), ("publication_record", "revision_id"),
                              ("content_revision", "request_key")):
            if column not in {item["name"] for item in inspect(engine).get_columns(table)}:
                with engine.begin() as connection:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR")
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"
                )
        columns = {column["name"] for column in inspect(engine).get_columns("candidate")}
        if "video_url" not in columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE candidate ADD COLUMN video_url VARCHAR NOT NULL DEFAULT ''"
                )
        if "video_source_page_url" not in columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE candidate ADD COLUMN video_source_page_url "
                    "VARCHAR NOT NULL DEFAULT ''"
                )
        run_columns = {column["name"] for column in inspect(engine).get_columns("collection_run")}
        if "error_code" not in run_columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE collection_run ADD COLUMN error_code VARCHAR"
                )
        signal_columns = {
            column["name"] for column in inspect(engine).get_columns("candidate_signal")
        }
        if "collection_run_id" not in signal_columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE candidate_signal ADD COLUMN collection_run_id VARCHAR"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_candidate_signal_collection_run_id "
                    "ON candidate_signal (collection_run_id)"
                )
        content_columns = {column["name"] for column in inspect(engine).get_columns("content")}
        if "cover_title_override" not in content_columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE content ADD COLUMN cover_title_override "
                    "VARCHAR NOT NULL DEFAULT ''"
                )
        if "title_zh" not in content_columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE content ADD COLUMN title_zh VARCHAR NOT NULL DEFAULT ''"
                )
        if "production_mode" not in content_columns:
            with engine.begin() as connection:
                # SQLAlchemy 的枚举列按**名称**存储（与 task_type 一致），
                # 默认值必须写 FOOTAGE；写成小写值会让读取时抛 LookupError。
                connection.exec_driver_sql(
                    "ALTER TABLE content ADD COLUMN production_mode "
                    "VARCHAR NOT NULL DEFAULT 'FOOTAGE'"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_content_production_mode "
                    "ON content (production_mode)"
                )
        else:
            # 幂等修正：早期迁移曾把枚举值（小写）写进该列，读取会直接报错。
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE content SET production_mode = UPPER(production_mode) "
                    "WHERE production_mode <> UPPER(production_mode)"
                )
        publication_columns = {
            column["name"] for column in inspect(engine).get_columns("publication_record")
        }
        if "status" not in publication_columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE publication_record ADD COLUMN status "
                    "VARCHAR NOT NULL DEFAULT 'RECORDED'"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_publication_record_status "
                    "ON publication_record (status)"
                )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_task_dedup_key "
            "ON agent_task (dedup_key) WHERE dedup_key IS NOT NULL"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_task_active_lane "
            "ON agent_task (lane_key) "
            "WHERE lane_key IS NOT NULL "
            "AND task_type = 'SOURCE_DISCOVERY' "
            "AND status IN ('PENDING', 'LEASED')"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_agent_task_dispatch "
            "ON agent_task (status, task_type, priority, created_at)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_agent_task_agent_lease "
            "ON agent_task (assigned_agent_id, status, lease_expires_at)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_agent_task_not_before ON agent_task (not_before)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_audit_event_entity_created "
            "ON audit_event (entity_id, created_at)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_audit_event_action_created "
            "ON audit_event (action, created_at)"
        )
        connection.exec_driver_sql("DROP INDEX IF EXISTS ux_publication_record_content_platform")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_publication_record_legacy_content_platform "
            "ON publication_record (content_id, platform) WHERE revision_id IS NULL"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_publication_record_revision_platform "
            "ON publication_record (content_id, revision_id, platform) WHERE revision_id IS NOT NULL"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_content_revision_number "
            "ON content_revision (content_id, revision_no)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_content_revision_request "
            "ON content_revision (content_id, request_key) WHERE request_key IS NOT NULL"
        )
        if engine.dialect.name == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS content_revision_immutable_{operation.lower()} "
                    f"BEFORE {operation} ON content_revision BEGIN "
                    "SELECT RAISE(ABORT, 'ContentRevision is immutable'); END"
                )
    from glance_core.capabilities import ensure_builtin_providers

    with Session(engine) as session:
        ensure_builtin_providers(session)
        _ensure_voice_profiles(session)
        _backfill_content_title_zh(session)
        _cancel_legacy_single_image_tasks(session)


def _ensure_voice_profiles(session: Session) -> None:
    """Seed explicit provider profiles without overwriting administrator choices."""
    from sqlmodel import select

    from glance_core.models import VoiceProfile

    profiles = session.exec(select(VoiceProfile)).all()
    changed = False
    if not profiles:
        session.add(
            VoiceProfile(
                id="voice-edge-yunxi",
                name="Edge · 云希",
                provider="edge",
                voice="zh-CN-YunxiNeural",
                speed=1.0,
                rate="+20%",
                volume="+0%",
                enabled=True,
                is_default=True,
            )
        )
        session.add(
            VoiceProfile(
                id="voice-edge-xiaoxiao",
                name="Edge · 晓晓",
                provider="edge",
                voice="zh-CN-XiaoxiaoNeural",
                speed=1.0,
                rate="+20%",
                volume="+0%",
                enabled=True,
                is_default=False,
            )
        )
        profiles = []
        changed = True

    if not any(profile.provider.strip().casefold() == "cosyvoice3" for profile in profiles):
        session.add(
            VoiceProfile(
                id="voice-cosyvoice3-default",
                name="CosyVoice3 · 本机默认",
                provider="cosyvoice3",
                voice="cosyvoice3:default",
                speed=1.0,
                rate="+0%",
                volume="+0%",
                endpoint=DEFAULT_COSYVOICE_ENDPOINT,
                enabled=False,
                is_default=False,
            )
        )
        changed = True
    if changed:
        session.commit()


def _backfill_content_title_zh(session: Session) -> None:
    """幂等回填：title_zh 为空的 Content 从关联 Candidate 的 title_zh 补齐。"""
    from sqlmodel import select

    from glance_core.models import Candidate, Content

    rows = session.exec(
        select(Content, Candidate).where(
            Content.candidate_id == Candidate.id,
            Content.title_zh == "",
            Candidate.title_zh != "",
        )
    ).all()
    for content, candidate in rows:
        content.title_zh = candidate.title_zh
        session.add(content)
    if rows:
        session.commit()


def _cancel_legacy_single_image_tasks(session: Session) -> None:
    """V8 升级步骤：取消存量单图格式（input 带 segment_index）的未结束生图任务；
    合并任务由 advance_after_agent 按新格式重新派发。"""
    from sqlmodel import select

    from glance_core.models import AgentTask, AuditEvent, TaskStatus, TaskType

    stale = session.exec(
        select(AgentTask).where(
            AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
            AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
        )
    ).all()
    touched = False
    for task in stale:
        if "requests" in task.input_data or "segment_index" not in task.input_data:
            continue
        task.status = TaskStatus.CANCELLED
        task.dedup_key = None
        task.lease_token_hash = None
        task.lease_expires_at = None
        task.not_before = None
        session.add(task)
        session.add(
            AuditEvent(
                actor_type="system",
                actor_id="v8-upgrade",
                entity_type="agent_task",
                entity_id=task.id,
                action="cancelled",
                metadata_json={"reason": "legacy_single_image_task"},
            )
        )
        touched = True
    if touched:
        session.commit()


def begin_immediate(session: Session) -> None:
    """Acquire SQLite's write lock only for the atomic claim path."""
    session.rollback()
    session.exec(text("BEGIN IMMEDIATE"))


def session_for(engine: Engine) -> Session:
    return Session(engine)
