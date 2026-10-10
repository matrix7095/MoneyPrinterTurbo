"""MPT-owned storage and consumption boundary around portable Glance services."""

import logging
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from glance_collector.service import CollectionService
from glance_core.database import begin_immediate, build_engine, create_db
from glance_core.models import AgentSubmission, AgentTask, AgentWorker, Artifact, AuditEvent, Content, ContentStatus, SchedulerOffer, Source, TaskStatus, TaskType, as_utc, utc_now
from glance_core.runtime_config import effective_settings
from glance_core.settings import Settings
from glance_core.task_service import AgentTaskService, TaskConflictError, stable_hash
from glance_core.workflow import WorkflowService, parse_platform_domains
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.quarantine import (
    IsolatedTaskService,
    assert_allowed,
    eligible_task_clause,
    filter_task_reads,
    deployment_proof,
)

logger = logging.getLogger(__name__)
CONSUMED = "mpt_discovery_consumed"
AGENT_CONSUMED = "mpt_agent_consumed"
OWNED = "mpt_task_owned"
FAILURE_RECOVERY = "mpt_failure_recovery_created"


def _require_owned_input(session, task, *, consumed=False):
    assert_allowed(session, task_id=task.id)
    receipts = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
        AuditEvent.action == OWNED,
    )).all()
    if stable_hash(task.input_data) != task.input_hash or not receipts or any(
        row.metadata_json.get("input_hash") != task.input_hash for row in receipts
    ):
        raise TaskConflictError("RECOVERY_SOURCE_CHANGED_OR_UNOWNED")
    if consumed:
        receipts = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
            AuditEvent.action == AGENT_CONSUMED,
        )).all()
        if task.status != TaskStatus.COMPLETED or not receipts or any(
            row.metadata_json.get("result_hash") != stable_hash(task.result_data) for row in receipts
        ):
            raise TaskConflictError("RECOVERY_UPSTREAM_NOT_ACCEPTED")


def _require_current_revision(session, task):
    revisions = session.exec(select(AgentTask).where(
        AgentTask.content_id == task.content_id, AgentTask.task_type == task.task_type,
        AgentTask.status != TaskStatus.CANCELLED,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).all()
    if task.task_type == TaskType.SCENE_ANALYZE:
        # Core scene builders version each numbered batch independently.
        revisions = [row for row in revisions if (row.input_data.get("batch_index") or 1)
                     == (task.input_data.get("batch_index") or 1)]
    latest = revisions[0] if revisions else None
    if latest is None or latest.id != task.id:
        raise TaskConflictError("RECOVERY_SOURCE_SUPERSEDED")


def _failure_hash(task):
    return stable_hash({"code": task.error_code, "message": task.error_message,
                        "agent_id": task.assigned_agent_id, "result": task.result_data})


def _require_reconciled_content(session, content_id):
    # Intent keys span revisions/artifacts. A different task may already have clicked.
    latest = {}
    for event in session.exec(select(AuditEvent).where(AuditEvent.entity_type == "publish_intent")).all():
        key = (AgentTaskService._intent_sequence(event), as_utc(event.created_at), event.id)
        if event.entity_id not in latest or key > latest[event.entity_id][0]:
            latest[event.entity_id] = (key, event)
    for _, event in latest.values():
        data = event.metadata_json
        if not isinstance(data, dict) or not isinstance(data.get("content_id"), str) or not data["content_id"]:
            raise TaskConflictError("RECOVERY_PUBLISH_RECONCILIATION_REQUIRED")
        if data["content_id"] != content_id:
            continue
        fields = ("platform", "account_id", "content_id", "video_artifact_id", "video_sha256")
        if (any(not isinstance(data.get(field), str) for field in fields)
                or not data["platform"] or not data["account_id"]
                or not (data["video_artifact_id"] or data["video_sha256"])
                or data.get("intent_key") != event.entity_id
                or stable_hash({field: data[field] for field in fields}) != event.entity_id
                or AgentTaskService._intent_sequence(event) < 0
                or data.get("status") != "FAILED"):
            raise TaskConflictError("RECOVERY_PUBLISH_RECONCILIATION_REQUIRED")


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

    def _require_native_deployment(self, session, task):
        from glance_core.agent_roles import primary_role_for
        from app.business.requests import DeploymentReportRequest
        from app.business.scheduler import deployment_files
        from app.business.bundles import sha256_file

        proof = deployment_proof(session, task.assigned_agent_id or "")
        worker = session.get(AgentWorker, proof.entity_id) if proof else None
        if worker is None or not worker.deployment_verified_at or worker.role_id != primary_role_for(task.task_type).value:
            raise TaskConflictError("RECOVERY_DEPLOYMENT_UNVERIFIED")
        try:
            launcher, _, _ = deployment_files(self, DeploymentReportRequest(
                agent_id=worker.agent_id, role_id=worker.role_id, launcher_path=worker.launcher_path,
                skill={"name": worker.skill_name, "path": proof.metadata_json["skill_path"],
                       "content_hash": worker.skill_content_hash}, protocol_version=worker.protocol_version,
            ))
            if (not proof.metadata_json.get("device_label")
                    or sha256_file(launcher) != proof.metadata_json["launcher_sha256"]
                    or sha256_file(launcher.with_suffix(".json")) != proof.metadata_json["manifest_sha256"]):
                raise ValueError("Deployment changed")
        except (OSError, ValueError, KeyError, TypeError):
            raise TaskConflictError("RECOVERY_DEPLOYMENT_CHANGED") from None

    def _failure_source(self, session, task_id):
        task = session.get(AgentTask, task_id)
        if task is None:
            raise KeyError(task_id)
        _require_owned_input(session, task)
        if task.status != TaskStatus.FAILED or task.task_type == TaskType.RECOVERY:
            raise TaskConflictError("RECOVERY_REQUIRES_TERMINAL_SOURCE_FAILURE")
        if task.task_type not in self.supported_task_types:
            raise TaskConflictError("RECOVERY_CONSUMER_UNAVAILABLE")
        failure = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
            AuditEvent.action == "failed",
        ).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())).first()
        if (failure is None or failure.actor_type != "agent" or task.attempt < 1
                or failure.actor_id != task.assigned_agent_id
                or failure.metadata_json.get("late_report")
                or failure.metadata_json.get("next_status") != TaskStatus.FAILED.value
                or failure.metadata_json.get("error_code") != task.error_code
                or failure.metadata_json.get("message") != (task.error_message or "")[:200]):
            raise TaskConflictError("RECOVERY_FAILURE_EVIDENCE_MISSING")
        self._require_native_deployment(session, task)
        content = session.get(Content, task.content_id) if task.content_id else None
        if task.content_id and (content is None or content.business_status in {
            ContentStatus.ARCHIVED, ContentStatus.PUBLISHED, ContentStatus.REJECTED,
        }):
            raise TaskConflictError("RECOVERY_CONTENT_NOT_CURRENT")
        if task.task_type == TaskType.DOUYIN_PUBLISH and self.task_service(session).publish_intent_for_task(task):
            raise TaskConflictError("RECOVERY_PUBLISH_RECONCILIATION_REQUIRED")
        if task.content_id:
            _require_reconciled_content(session, task.content_id)
            _require_current_revision(session, task)
        upstream_types = {"research": TaskType.CONTENT_RESEARCH, "script": TaskType.SHORT_SCRIPT,
                          "script_qc": TaskType.SCRIPT_QC, "visual_plan": TaskType.VISUAL_PLAN,
                          "visual_plan_qc": TaskType.VISUAL_PLAN_QC, "scene_analysis": TaskType.SCENE_ANALYZE,
                          "asset_generate_image": TaskType.ASSET_GENERATE_IMAGE,
                          "content_qc": TaskType.CONTENT_QC, "publish_prepare": TaskType.PUBLISH_PREPARE}
        for name, reference in (task.input_data.get("upstream_task_ids") or {}).items():
            if name not in upstream_types:
                continue  # Historical previous_plan and Artifact references are not current task parents.
            refs = reference if name == "scene_analysis" and isinstance(reference, list) else [reference]
            if not refs or any(not isinstance(ref, str) or not ref for ref in refs):
                raise TaskConflictError("RECOVERY_UPSTREAM_NOT_ACCEPTED")
            for upstream_id in refs:
                upstream = session.get(AgentTask, upstream_id)
                if upstream is None or upstream.content_id != task.content_id or upstream.task_type != upstream_types[name]:
                    raise TaskConflictError("RECOVERY_UPSTREAM_NOT_ACCEPTED")
                _require_owned_input(session, upstream, consumed=True)
                _require_current_revision(session, upstream)
        return task, failure.metadata_json.get("retryable") is True

    def _failure_visual_target(self, session, flow, task):
        from app.business.pipeline import _current_visual_plan_for_asset_task
        from glance_core.content_acceptance import prepared_visual_plan

        plan = _current_visual_plan_for_asset_task(session, flow, task)
        script = flow.completed_task_for(task.content_id or "", TaskType.SHORT_SCRIPT)
        if plan is None or script is None:
            raise TaskConflictError("RECOVERY_VISUAL_LINEAGE_STALE")
        _require_owned_input(session, plan, consumed=True)
        _require_owned_input(session, script, consumed=True)
        _require_current_revision(session, plan)
        _require_current_revision(session, script)
        declared = task.input_data.get("upstream_task_ids") or {}
        frozen_plan = task.input_data.get("visual_plan")
        current_plan = prepared_visual_plan(session, plan)
        requests = [{"segment_index": index, "asset_request": segment["asset_request"]}
                    for index, segment in enumerate(current_plan.get("segments", []))
                    if segment.get("asset_request")]
        actual = [{"segment_index": item.get("segment_index"), "asset_request": item.get("asset_request")}
                  for item in task.input_data.get("requests", [])]
        if (plan.input_data.get("upstream_task_ids", {}).get("script") != script.id
                or task.input_data.get("script") != script.result_data
                or actual != requests or not requests
                or (frozen_plan is not None and frozen_plan != current_plan)
                or (not declared.get("visual_plan") and frozen_plan is None)):
            raise TaskConflictError("RECOVERY_VISUAL_LINEAGE_STALE")
        return plan

    def schedule_failure_recovery(self, task_id):
        from app.business.pipeline import workflow

        with Session(self.engine) as session:
            task = session.get(AgentTask, task_id)
            if task is None:
                raise KeyError(task_id)
            key = task.content_id or task.id
        with content_operation_lock(self.settings, key):
            with _ConsumptionSession(self.engine) as session:
                begin_immediate(session)
                try:
                    task, retryable = self._failure_source(session, task_id)
                    current = effective_settings(session, self.settings)
                    flow = workflow(session, current)
                    flow.tasks = self.task_service(session)
                    existing = session.exec(select(AgentTask).where(
                        AgentTask.task_type == TaskType.RECOVERY,
                    )).all()
                    for recovery in existing:
                        failure = recovery.input_data.get("failure") or {}
                        if failure.get("source_task_id") == task.id and failure.get("source_attempt") == task.attempt:
                            _require_owned_input(session, recovery)
                            if failure.get("code") != task.error_code:
                                raise TaskConflictError("RECOVERY_FAILURE_CHANGED")
                            return {"recovery_status": recovery.status.value, "recovery_task_id": recovery.id}
                    if task.content_id and session.exec(select(AgentTask.id).where(
                        AgentTask.content_id == task.content_id,
                        AgentTask.status.in_([TaskStatus.LEASED, TaskStatus.SUBMITTED]),
                    )).first():
                        raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
                    target = None
                    if task.task_type == TaskType.ASSET_GENERATE_IMAGE:
                        target = self._failure_visual_target(session, flow, task)
                    owned = select(AuditEvent.entity_id).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.action == OWNED,
                        AuditEvent.metadata_json["input_hash"].as_string() == AgentTask.input_hash,
                    )
                    with filter_task_reads(session, eligible_task_clause() & (
                        (AgentTask.task_type != TaskType.RECOVERY) | AgentTask.id.in_(owned)
                    )):
                        recovery = flow.create_recovery_task(
                            task, code=task.error_code, message=task.error_message, retryable=retryable,
                            failure_class="BUSINESS_QC" if target is not None and task.error_code
                            == "GENERATED_IMAGE_CONSTRAINT_MISMATCH" else None,
                            repair_target_task_id=target.id if target is not None and task.error_code
                            == "GENERATED_IMAGE_CONSTRAINT_MISMATCH" else None,
                            actor_id="mpt-agent-failure",
                        )
                    if recovery is not None:
                        if recovery.id in {row.id for row in existing}:
                            _require_owned_input(session, recovery)
                        else:
                            session.add(AuditEvent(
                                actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                entity_id=recovery.id, action=OWNED, metadata_json={"input_hash": recovery.input_hash},
                            ))
                            session.add(AuditEvent(
                                actor_type="system", actor_id="mpt-agent-failure", entity_type="agent_task",
                                entity_id=recovery.id, action=FAILURE_RECOVERY,
                                metadata_json={"source_task_id": task.id, "source_input_hash": task.input_hash,
                                               "source_attempt": task.attempt, "source_failure_hash": _failure_hash(task),
                                               "recovery_input_hash": recovery.input_hash,
                                               "target_hashes": {parent.id: {"input": parent.input_hash,
                                                                           "result": stable_hash(parent.result_data)}
                                                                 for parent in (target, flow.completed_task_for(
                                                                     task.content_id, TaskType.SHORT_SCRIPT))}
                                               if target is not None else {}},
                            ))
                    Session.commit(session)
                    return {"recovery_status": recovery.status.value if recovery else
                            "DEFERRED" if flow.recovery_capacity_deferred else "BLOCKED",
                            "recovery_task_id": recovery.id if recovery else None}
                finally:
                    session.rollback()

    def _failure_recovery_needs_apply(self, session, recovery):
        receipt = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
            AuditEvent.action == FAILURE_RECOVERY,
        )).first()
        if receipt is None:
            source = session.get(AgentTask, (recovery.input_data.get("failure") or {}).get("source_task_id"))
            origin = session.exec(select(AuditEvent.id).where(
                AuditEvent.entity_id == recovery.id, AuditEvent.action == "recovery_task_created",
                AuditEvent.actor_id == "mpt-agent-failure",
            )).first()
            if origin or (source is not None and source.status == TaskStatus.FAILED):
                raise TaskConflictError("RECOVERY_FAILURE_RECEIPT_MISSING")
            return True  # Existing QC-origin Recovery keeps its portable consumer contract.
        _require_owned_input(session, recovery)
        self._require_native_deployment(session, recovery)
        source = session.get(AgentTask, receipt.metadata_json.get("source_task_id"))
        if source is None:
            raise TaskConflictError("RECOVERY_SOURCE_MISSING")
        _require_owned_input(session, source)
        if (source.input_hash != receipt.metadata_json.get("source_input_hash")
                or source.attempt != receipt.metadata_json.get("source_attempt")
                or _failure_hash(source) != receipt.metadata_json.get("source_failure_hash")
                or recovery.input_hash != receipt.metadata_json.get("recovery_input_hash")
                or (recovery.input_data.get("failure") or {}).get("source_task_id") != source.id
                or recovery.content_id != source.content_id):
            raise TaskConflictError("RECOVERY_SOURCE_CHANGED")
        _require_reconciled_content(session, source.content_id)
        for target_id, hashes in receipt.metadata_json.get("target_hashes", {}).items():
            target = session.get(AgentTask, target_id)
            if target is None:
                raise TaskConflictError("RECOVERY_VISUAL_LINEAGE_STALE")
            _require_owned_input(session, target)
            accepted = session.exec(select(AuditEvent).where(
                AuditEvent.action == AGENT_CONSUMED, AuditEvent.entity_id == target.id,
            )).all()
            if target.input_hash != hashes["input"] or stable_hash(target.result_data) != hashes["result"]:
                raise TaskConflictError("RECOVERY_VISUAL_LINEAGE_STALE")
            if not accepted or any(event.metadata_json.get("result_hash") != hashes["result"] for event in accepted):
                raise TaskConflictError("RECOVERY_UPSTREAM_NOT_ACCEPTED")
        # A marker-write failure must not repeat an already executed portable action.
        retried = session.exec(select(AuditEvent.id).where(
            AuditEvent.action == "recovery_same_task_retried", AuditEvent.entity_id == source.id,
            AuditEvent.metadata_json["recovery_task_id"].as_string() == recovery.id,
        )).first()
        children = session.exec(select(AgentTask).where(
            AgentTask.content_id == recovery.content_id,
            AgentTask.input_data["recovery_task_id"].as_string() == recovery.id,
        )).all()
        if retried or children:
            if any(stable_hash(child.input_data) != child.input_hash for child in children):
                raise TaskConflictError("RECOVERY_REPAIR_INPUT_CHANGED")
            return False
        source, _ = self._failure_source(session, source.id)
        if session.exec(select(AgentTask.id).where(
            AgentTask.content_id == recovery.content_id, AgentTask.id != recovery.id,
            AgentTask.status.in_([TaskStatus.LEASED, TaskStatus.SUBMITTED]),
        )).first():
            raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
        if source.task_type == TaskType.ASSET_GENERATE_IMAGE:
            flow = self.workflow_service(session)
            target = self._failure_visual_target(session, flow, source)
            lineage = recovery.input_data.get("lineage") or {}
            if source.error_code == "GENERATED_IMAGE_CONSTRAINT_MISMATCH" and (
                lineage.get("repair_target_task_id") != target.id
                or recovery.input_data.get("failure", {}).get("failure_class") != "BUSINESS_QC"
            ):
                raise TaskConflictError("RECOVERY_VISUAL_LINEAGE_STALE")
        return True

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
                needs_apply = kind != TaskType.RECOVERY or self._failure_recovery_needs_apply(session, task)
            action = pipeline.advance_after_agent(self.engine, self.settings, task_id) if needs_apply else None
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
                if action == "render":
                    operation(self.engine, self.settings, task.content_id, consuming_task_id=task_id)
                else:
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

    def restore_consumed_visual_plan(self, content_id: str) -> dict:
        """Restore ONLY a proven historical asset backfill, superseding an unexecuted QC offer."""
        from glance_core.content_acceptance import acceptance_subject, resolved_visual_plan, source_context
        from glance_media.executor import sha256_file

        with content_operation_lock(self.settings, content_id):
            with _ConsumptionSession(self.engine) as session:
                begin_immediate(session)
                try:
                    assert_allowed(session, content_id=content_id)
                    content = session.get(Content, content_id)
                    if content is None:
                        raise KeyError(content_id)
                    if (content.business_status, content.current_stage) != (ContentStatus.WAITING_AGENT, "content_qc"):
                        raise ValueError("Visual restoration requires an unexecuted content QC offer")
                    _require_reconciled_content(session, content_id)
                    if session.exec(select(AgentTask.id).where(
                        AgentTask.content_id == content_id,
                        AgentTask.status.in_([TaskStatus.LEASED, TaskStatus.SUBMITTED]),
                    )).first() or session.exec(select(SchedulerOffer.request_id).where(
                        SchedulerOffer.task_id.in_(select(AgentTask.id).where(AgentTask.content_id == content_id)),
                        SchedulerOffer.expires_at > utc_now(),
                    )).first():
                        raise ValueError("Visual restoration content work is active or offered")
                    if session.exec(select(AuditEvent.id).where(
                        AuditEvent.entity_type == "publish_intent",
                        AuditEvent.metadata_json["content_id"].as_string() == content_id,
                    )).first():
                        raise ValueError("Visual restoration cannot cross a publication intent")
                    flow = self.workflow_service(session)
                    plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
                    if plan is None:
                        raise ValueError("Visual restoration plan missing")
                    _require_owned_input(session, plan)
                    _require_current_revision(session, plan)
                    self._require_native_deployment(session, plan)
                    submissions = session.exec(select(AgentSubmission).where(
                        AgentSubmission.task_id == plan.id, AgentSubmission.attempt == plan.attempt,
                        AgentSubmission.validation_status == "ACCEPTED",
                    )).all()
                    if len(submissions) != 1:
                        raise ValueError("Visual restoration requires one accepted submission")
                    submission = submissions[0]
                    expected_path = (self.settings.agent_submission_dir / plan.id / str(plan.attempt) / "result.json").resolve()
                    if (not expected_path.is_relative_to(self.settings.agent_submission_dir.resolve())
                            or Path(submission.raw_result_path).resolve() != expected_path):
                        raise ValueError("Visual restoration submission path changed")
                    original = json.loads(expected_path.read_text(encoding="utf-8"))
                    events = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == plan.id,
                        AuditEvent.action.in_(["completed", AGENT_CONSUMED]),
                    )).all()
                    completed = [event for event in events if event.action == "completed"]
                    consumed = [event for event in events if event.action == AGENT_CONSUMED]
                    if (not isinstance(original, dict) or stable_hash(original) != submission.result_hash
                            or len(completed) != 1 or completed[0].actor_type != "agent"
                            or completed[0].actor_id != submission.agent_id
                            or submission.agent_id != plan.assigned_agent_id
                            or completed[0].metadata_json.get("result_hash") != submission.result_hash
                            or not consumed or any(event.metadata_json.get("result_hash") != submission.result_hash for event in consumed)):
                        raise ValueError("Visual restoration accepted/consumed proof changed")
                    if plan.result_data == original:
                        resolved_visual_plan(session, plan)
                        return {"restored": False, "visual_plan_task_id": plan.id}
                    before = deepcopy(plan.result_data)
                    detached = plan.model_copy(update={"result_data": original})
                    if resolved_visual_plan(session, detached) != before:
                        raise ValueError("Visual restoration is not an exact approved asset backfill")
                    offers = session.exec(select(AgentTask).where(
                        AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_QC,
                        AgentTask.status != TaskStatus.CANCELLED,
                    )).all()
                    if len(offers) != 1:
                        raise ValueError("Visual restoration requires one unexecuted QC offer")
                    offer = offers[0]
                    assert_allowed(session, task_id=offer.id)
                    if (offer.status != TaskStatus.PENDING or offer.attempt != 0 or offer.assigned_agent_id
                            or offer.lease_token_hash or offer.started_at or offer.result_data
                            or offer.schema_version != "2" or stable_hash(offer.input_data) != offer.input_hash
                            or offer.input_data.get("acceptance_contract_version") != 2
                            or offer.input_data.get("source_context_version") != 1
                            or offer.input_data.get("audio_evidence_version") != 1):
                        raise ValueError("Visual restoration QC offer has already executed or changed")
                    if session.exec(select(AgentTask.id).where(
                        AgentTask.content_id == content_id,
                        AgentTask.task_type.in_([TaskType.FINAL_REVIEW_ASSIST, TaskType.PUBLISH_PREPARE,
                                               TaskType.PUBLISH_REVIEW_ASSIST, TaskType.DOUYIN_PUBLISH]),
                        AgentTask.status != TaskStatus.CANCELLED,
                    )).first():
                        raise ValueError("Visual restoration downstream work already exists")
                    frozen = deepcopy(offer.input_data)
                    root = Path(content.content_dir).resolve()
                    if not root.is_relative_to(self.settings.content_dir.resolve()):
                        raise ValueError("Visual restoration content root outside runtime")
                    receipt_path = root / "render" / "acceptance-input.json"
                    receipts = session.exec(select(Artifact).where(
                        Artifact.content_id == content_id, Artifact.artifact_type == "acceptance_input",
                        Artifact.relative_path == "render/acceptance-input.json",
                    )).all()
                    if (not receipt_path.resolve().is_relative_to(root) or not receipt_path.is_file()
                            or sum(artifact.sha256 == sha256_file(receipt_path)
                                   and artifact.size_bytes == receipt_path.stat().st_size for artifact in receipts) != 1):
                        raise ValueError("Visual restoration original render receipt changed")
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    without_audio = deepcopy(frozen)
                    without_audio.pop("audio_evidence")
                    without_audio.pop("audio_evidence_version")
                    without_audio["acceptance_subject"].pop("audio_evidence_sha256")
                    if receipt != without_audio:
                        raise ValueError("Visual restoration QC/render receipt mismatch")
                    audio = frozen["audio_evidence"]
                    audio_path = root / "render" / f"audio-evidence-{audio.get('video_sha256')}.json"
                    if (not audio_path.resolve().is_relative_to(root) or not audio_path.is_file()
                            or json.loads(audio_path.read_text(encoding="utf-8")) != audio):
                        raise ValueError("Visual restoration independent audio evidence changed")
                    refs = {**frozen["upstream_task_ids"], "video_artifact_id": frozen["video_artifact_id"]}
                    if refs.get("visual_plan") != plan.id:
                        raise ValueError("Visual restoration QC plan lineage changed")
                    plan.result_data = original
                    session.add(plan)
                    current = acceptance_subject(session, content_id, refs, source_context_version=1, audio_evidence=audio)
                    expected = {**frozen["acceptance_subject"], "visual_plan_sha256": submission.result_hash}
                    context = source_context(session, content_id, refs)
                    if current != expected or context != frozen["source_context"] or context.get("evidence_gaps"):
                        raise ValueError("Visual restoration current acceptance materials changed")
                    artifact = Artifact(content_id=content_id, artifact_type="acceptance_input", relative_path="",
                                        sha256="", size_bytes=0, mime_type="application/json",
                                        producer_stage="visual_plan_restoration")
                    new_input = {**frozen, "acceptance_subject": current, "previous_acceptance_task_id": offer.id,
                                 "acceptance_receipt_artifact_id": artifact.id}
                    relative = f"render/acceptance-input-restored-{stable_hash(new_input)}.json"
                    new_path = root / relative
                    encoded = json.dumps(new_input, ensure_ascii=False).encode("utf-8")
                    if not new_path.resolve().is_relative_to(root) or (new_path.exists() and new_path.read_bytes() != encoded):
                        raise ValueError("Visual restoration new receipt path changed")
                    temporary = new_path.with_suffix(".tmp")
                    if not temporary.resolve().is_relative_to(root):
                        raise ValueError("Visual restoration temporary receipt path changed")
                    temporary.write_bytes(encoded)
                    temporary.replace(new_path)
                    artifact.relative_path, artifact.sha256, artifact.size_bytes = relative, sha256_file(new_path), len(encoded)
                    session.add(artifact)
                    flow.tasks.cancel(offer.id, actor_id="admin-api", actor_type="human")
                    replacement = flow.create_agent_task(content_id, TaskType.CONTENT_QC, new_input, priority=60,
                                                         allow_multiple=True, schema_version="2")
                    session.add(AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content",
                                           entity_id=content_id, action="mpt_visual_plan_restored",
                                           metadata_json={"visual_plan_task_id": plan.id, "original_result_hash": submission.result_hash,
                                                          "backfilled_result_hash": stable_hash(before), "cancelled_task_id": offer.id,
                                                          "replacement_task_id": replacement.id, "receipt_artifact_id": artifact.id,
                                                          "video_artifact_id": refs["video_artifact_id"], "video_sha256": current["video_sha256"]}))
                    Session.commit(session)
                    return {"restored": True, "visual_plan_task_id": plan.id, "content_qc_task_id": replacement.id,
                            "receipt_artifact_id": artifact.id}
                finally:
                    session.rollback()

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
