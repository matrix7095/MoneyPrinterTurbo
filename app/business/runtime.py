"""MPT-owned storage and consumption boundary around portable Glance services."""

import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from glance_collector.service import CollectionService
from glance_core.database import begin_immediate, build_engine, create_db
from glance_core.models import AgentTask, AuditEvent, Source, TaskStatus, TaskType
from glance_core.settings import Settings
from glance_core.task_service import AgentTaskService, stable_hash
from glance_core.workflow import WorkflowService, parse_platform_domains
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.quarantine import (
    IsolatedTaskService,
    assert_allowed,
    eligible_task_clause,
    filter_task_reads,
)

logger = logging.getLogger(__name__)
CONSUMED = "mpt_discovery_consumed"
AGENT_CONSUMED = "mpt_agent_consumed"
OWNED = "mpt_task_owned"


def settings_from_config(
    configuration: dict, root_dir: str | Path | None = None
) -> Settings:
    root = Path(root_dir or Path(__file__).resolve().parents[2])
    options = dict(configuration.get("business", {}))
    options.pop("enabled", None)
    data_dir = Path(
        os.environ.get(
            "MPT_BUSINESS_DATA_DIR", options.pop("data_dir", "storage/business")
        )
    )
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    data_dir = data_dir.resolve()
    # Never inherit GLANCE_* or its .env: those may point to the live installation.
    return Settings(
        _env_file=None,
        _env_prefix="MPT_BUSINESS_",
        data_dir=data_dir,
        database_url=os.environ.get(
            "MPT_BUSINESS_DATABASE_URL",
            options.pop(
                "database_url", f"sqlite:///{(data_dir / 'database/app.db').as_posix()}"
            ),
        ),
        bootstrap_agent_token=os.environ.get(
            "MPT_BUSINESS_BOOTSTRAP_AGENT_TOKEN",
            options.pop("bootstrap_agent_token", ""),
        ),
        admin_token=os.environ.get(
            "MPT_BUSINESS_ADMIN_TOKEN", options.pop("admin_token", "")
        ),
        huey_database_path=data_dir / "database/huey.db",
        archive_repo_dir=data_dir / "archive-repo",
        **options,
    )


class _ConsumptionSession(Session):
    def commit(self):
        # Collector commits each candidate; keep them inside one recoverable transaction.
        self.flush()


class BusinessRuntime:
    def __init__(self, settings: Settings, *, workspace_root: Path | None = None):
        self.settings = settings
        self.workspace_root = (
            workspace_root or Path(__file__).resolve().parents[2]
        ).resolve()
        if not settings.database_url.startswith("sqlite:///"):
            raise ValueError("MPT business currently requires its own SQLite database")
        database = Path(settings.database_url.removeprefix("sqlite:///")).resolve()
        if not database.is_relative_to(settings.data_dir.resolve()):
            raise ValueError("Business database must be inside business data_dir")
        for secret in (settings.admin_token, settings.bootstrap_agent_token):
            if (
                not secret.get_secret_value()
                or secret.get_secret_value() == "change-me-before-network-use"
            ):
                raise ValueError(
                    "Configure independent MPT business admin and agent tokens"
                )
        self.engine = build_engine(settings)
        from app.business.maintenance import Maintenance

        self.maintenance = Maintenance(self)

    def initialize(self):
        self.settings.agent_submission_dir.mkdir(parents=True, exist_ok=True)
        create_db(self.engine)
        self.recover_discovery()
        self.recover_agent_results()

    def close(self):
        self.maintenance.close()
        self.engine.dispose()

    def start_maintenance(self):
        self.maintenance.start()

    @property
    def supported_task_types(self):
        from app.business.pipeline import SUPPORTED_TASK_TYPES

        return SUPPORTED_TASK_TYPES

    def run_content_stage(self, content_id: str, operation):
        with content_operation_lock(self.settings, content_id):
            with Session(self.engine) as session:
                assert_allowed(session, content_id=content_id)
            return operation(self.engine, self.settings, content_id)

    def maintain_dispatch(self):
        self.maintenance.run()
        # Each phase uses an independent session: never nest BEGIN IMMEDIATE
        # around pipeline sessions, or reap imported/unknown historical leases.
        self.recover_discovery()
        self.recover_agent_results(lock_timeout_seconds=0)
        owned = select(AuditEvent.entity_id).where(
            AuditEvent.action == OWNED,
            AuditEvent.metadata_json["input_hash"].as_string() == AgentTask.input_hash,
        )
        with Session(self.engine) as session:
            begin_immediate(session)
            try:
                with filter_task_reads(
                    session, eligible_task_clause() & AgentTask.id.in_(owned)
                ):
                    count = self.task_service(session).reap_expired_leases()
                session.commit()
                return count
            finally:
                session.rollback()

    def consume_agent(self, task_id: str, *, lock_timeout_seconds: float = 30) -> bool:
        with Session(self.engine) as session:
            assert_allowed(session, task_id=task_id)
            task = session.get(AgentTask, task_id)
            if task is None or task.status != TaskStatus.COMPLETED:
                raise ValueError("Agent result is not accepted yet")
            kind, key = task.task_type, task.content_id or task.id
            marker = session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == AGENT_CONSUMED,
                    AuditEvent.entity_id == task_id,
                )
            ).first()
            if marker is not None:
                if marker.metadata_json.get("result_hash") != stable_hash(
                    task.result_data
                ):
                    raise ValueError("Consumed Agent result changed")
                return False
        if kind == TaskType.SOURCE_DISCOVERY:
            return self.consume_discovery(task_id)
        if kind not in self.supported_task_types:
            raise ValueError("Agent task has no installed consumer")
        from app.business import pipeline

        # Pipeline functions open their own sessions and perform media IO: never hold
        # the discovery SQLite write transaction around this stage dispatcher.
        with content_operation_lock(
            self.settings, key, timeout_seconds=lock_timeout_seconds
        ):
            with Session(self.engine) as session:
                assert_allowed(session, task_id=task_id)
                task = session.get(AgentTask, task_id)
                digest = stable_hash(task.result_data)
                marker = session.exec(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == task_id,
                        AuditEvent.action == AGENT_CONSUMED,
                    )
                ).first()
                if marker is not None:
                    if marker.metadata_json.get("result_hash") != digest:
                        raise ValueError("Consumed Agent result changed")
                    return False
            action = pipeline.advance_after_agent(self.engine, self.settings, task_id)
            operations = {
                "voice": pipeline.approve_script,
                "render": pipeline.render_content,
                "package": pipeline.approve_final,
                "prepare": pipeline.prepare_content,
            }
            if action is not None:
                operation = operations.get(action)
                if operation is None or not task.content_id:
                    raise ValueError(f"Unsupported pipeline follow-up: {action}")
                operation(self.engine, self.settings, task.content_id)
            with Session(self.engine) as session:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="mpt-business",
                        entity_type="agent_task",
                        entity_id=task_id,
                        action=AGENT_CONSUMED,
                        metadata_json={"result_hash": digest},
                    )
                )
                session.commit()
            return True

    def recover_agent_results(self, *, lock_timeout_seconds: float = 30):
        with Session(self.engine) as session:
            tasks = session.exec(
                select(AgentTask.id).where(
                    AgentTask.status == TaskStatus.COMPLETED,
                    eligible_task_clause(),
                    AgentTask.task_type != TaskType.SOURCE_DISCOVERY,
                    AgentTask.task_type.in_(self.supported_task_types),
                    AgentTask.id.in_(
                        select(AuditEvent.entity_id).where(
                            AuditEvent.action == OWNED,
                            AuditEvent.metadata_json["input_hash"].as_string()
                            == AgentTask.input_hash,
                        )
                    ),
                )
            ).all()
        for task_id in tasks:
            try:
                self.consume_agent(task_id, lock_timeout_seconds=lock_timeout_seconds)
            except Exception:
                logger.exception("Agent consumption pending for %s", task_id)

    def task_service(self, session: Session) -> AgentTaskService:
        return IsolatedTaskService(
            session,
            self.settings.agent_submission_dir,
            lease_minutes=self.settings.agent_lease_minutes,
            max_inflight=self.settings.agent_max_inflight,
        )

    def workflow_service(self, session: Session) -> WorkflowService:
        settings = self.settings
        return WorkflowService(
            session,
            data_dir=settings.data_dir,
            submission_root=settings.agent_submission_dir,
            lease_minutes=settings.agent_lease_minutes,
            video_platform_domains=parse_platform_domains(
                settings.video_platform_domains
            ),
            recovery_enabled=settings.recovery_enabled,
            recovery_max_rounds=settings.recovery_max_rounds,
            recovery_same_task_retries=settings.recovery_same_task_retries,
            recovery_unknown_attempts=settings.recovery_unknown_attempts,
            recovery_max_parallel=settings.recovery_max_parallel,
        )

    def consume_discovery(self, task_id: str) -> bool:
        # SQLite's write lock serializes consumers across processes, not only HTTP threads.
        with _ConsumptionSession(self.engine) as session:
            begin_immediate(session)
            try:
                assert_allowed(session, task_id=task_id)
                task = session.get(AgentTask, task_id)
                if (
                    task is None
                    or task.task_type != TaskType.SOURCE_DISCOVERY
                    or task.status != TaskStatus.COMPLETED
                ):
                    return False
                digest = stable_hash(task.result_data)
                marker = session.exec(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == task.id,
                        AuditEvent.action == CONSUMED,
                    )
                ).first()
                if marker is not None:
                    if marker.metadata_json.get("result_hash") != digest:
                        raise ValueError("Consumed discovery result changed")
                    return False
                if (
                    session.get(Source, str(task.input_data.get("source_id") or ""))
                    is None
                ):
                    raise ValueError(
                        "Discovery source is missing; result remains pending consumption"
                    )
                CollectionService(session, self.settings).ingest_agent_result(task)
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="mpt-business",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action=CONSUMED,
                        metadata_json={"result_hash": digest},
                    )
                )
                Session.commit(session)
                return True
            finally:
                session.rollback()

    def recover_discovery(self):
        with Session(self.engine) as session:
            tasks = session.exec(
                select(AgentTask.id).where(
                    AgentTask.task_type == TaskType.SOURCE_DISCOVERY,
                    eligible_task_clause(),
                    AgentTask.status == TaskStatus.COMPLETED,
                    AgentTask.id.in_(
                        select(AuditEvent.entity_id).where(
                            AuditEvent.action == OWNED,
                            AuditEvent.metadata_json["input_hash"].as_string()
                            == AgentTask.input_hash,
                        )
                    ),
                )
            ).all()
        for task_id in tasks:
            try:
                self.consume_discovery(task_id)
            except Exception:
                # The accepted task stays durable; restart/admin retry can consume it later.
                logger.exception("Discovery consumption pending for %s", task_id)


def get_runtime(request: Request) -> BusinessRuntime:
    runtime = getattr(request.app.state, "business_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=503, detail="Business runtime is not configured"
        )
    return runtime


def get_session(request: Request):
    with Session(get_runtime(request).engine) as session:
        yield session


def task_service(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> AgentTaskService:
    return get_runtime(request).task_service(session)
