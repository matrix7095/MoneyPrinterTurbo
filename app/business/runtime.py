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
from glance_core.runtime_config import RuntimeSettingsError, effective_settings
from glance_core.settings import Settings
from glance_core.task_service import AgentTaskService, TaskConflictError, stable_hash
from glance_core.workflow import WorkflowService, parse_platform_domains
from glance_media.executor import NonRetryableMediaError
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.media_executor import MptMediaExecutor
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
FAILURE_RECOVERY_RECONCILED = "mpt_bundle_failure_recovery_reconciled"
QC_RECOVERY = "mpt_content_qc_recovery_created"
QC_PLAN_REJECTED = "mpt_content_qc_recovery_plan_rejected"
QC_TARGET_REJECTION = "RECOVERY_QC_TARGET_CHANGED"


def _content_qc_target_changed(result):
    return (isinstance(result, dict) and result.get("failure_class") == "BUSINESS_QC"
            and result.get("action") in {"create_repair_task", "reroute_to_role", "repair_task_context"}
            and (result.get("target_task_type") or "short_script") != "short_script")


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
    # UUID order cannot establish which equal-timestamp revision is current.
    if (latest is None or latest.id != task.id
            or any(row.id != task.id and as_utc(row.created_at) == as_utc(task.created_at) for row in revisions)):
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


def _require_idle_bundle_recovery(session, content_id):
    from app.business.content_run import load_run
    from glance_core.models import PublicationRecord

    run = load_run(session)
    if run is not None and run.content_id == content_id and run.state not in {"stopped", "expired"}:
        raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
    if (session.exec(select(AgentTask.id).where(
            AgentTask.content_id == content_id,
            AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
        )).first() or session.exec(select(SchedulerOffer.request_id).where(
            SchedulerOffer.task_id.in_(select(AgentTask.id).where(AgentTask.content_id == content_id)),
            SchedulerOffer.expires_at > utc_now(),
        )).first()):
        raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
    if session.exec(select(PublicationRecord.id).where(PublicationRecord.content_id == content_id)).first():
        raise TaskConflictError("RECOVERY_PUBLISH_RECONCILIATION_REQUIRED")
    _require_reconciled_content(session, content_id)


def _require_bundle_recovery_subject(session, source):
    from glance_core.content_acceptance import acceptance_subject

    content = session.get(Content, source.content_id)
    if content is None or (content.business_status, content.current_stage) != (ContentStatus.WAITING_AGENT, "recovery"):
        raise TaskConflictError("RECOVERY_CONTENT_NOT_CURRENT")
    inputs = source.input_data
    refs = {**inputs.get("upstream_task_ids", {}), "video_artifact_id": inputs.get("video_artifact_id")}
    if acceptance_subject(session, source.content_id, refs,
                          source_context_version=inputs.get("source_context_version"),
                          audio_evidence=inputs.get("audio_evidence"),
                          visual_evidence=inputs.get("visual_evidence")) != inputs.get("acceptance_subject"):
        raise TaskConflictError("RECOVERY_SOURCE_SUBJECT_CHANGED")


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

    def maintain_content_dispatch(self, run_id):
        from app.business.content_run import require_run

        with Session(self.engine) as session:
            target_id = require_run(session, run_id).content_id
        self.recover_agent_results(lock_timeout_seconds=0, content_id=target_id)
        owned = select(AuditEvent.entity_id).where(
            AuditEvent.action == OWNED,
            AuditEvent.metadata_json["input_hash"].as_string() == AgentTask.input_hash,
        )
        with Session(self.engine) as session:
            begin_immediate(session)
            require_run(session, run_id)
            with filter_task_reads(session, eligible_task_clause() & AgentTask.id.in_(owned) & (AgentTask.content_id == target_id)):
                count = self.task_service(session).reap_expired_leases()
            session.commit()
            return count

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

    def _failure_recovery_metadata(self, task, recovery, *, targets=()):
        return {"source_task_id": task.id, "source_input_hash": task.input_hash,
                "source_attempt": task.attempt, "source_failure_hash": _failure_hash(task),
                "recovery_input_hash": recovery.input_hash,
                "target_hashes": {parent.id: {"input": parent.input_hash,
                                              "result": stable_hash(parent.result_data)} for parent in targets}}

    def _reconcile_bundle_failure(self, session, source, recovery):
        """Adopt only a proven native preflight chain, without rewriting its frozen records."""
        from glance_core.task_schemas import RecoveryPlan

        _require_owned_input(session, recovery)
        _require_current_revision(session, recovery)
        self._require_accepted_native_task(session, recovery, consumed=False)
        failure = recovery.input_data.get("failure") or {}
        lineage = recovery.input_data.get("lineage") or {}
        flow = self.workflow_service(session)
        expected_class = flow.classify_recovery_failure(source, code=source.error_code, retryable=False)
        signature = flow._root_cause_signature(expected_class, source.error_code[:120])
        origins = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
            AuditEvent.action == "recovery_task_created",
        )).all()
        if (source.task_type != TaskType.CONTENT_QC or source.schema_version not in {"2", "3"}
                or source.error_code not in {"ENVIRONMENT_MISSING_INPUT", "ENVIRONMENT_MISSING_CONTEXT"}
                or recovery.task_type != TaskType.RECOVERY or recovery.status != TaskStatus.COMPLETED
                or recovery.attempt < 1 or recovery.content_id != source.content_id
                or failure.get("source_task_id") != source.id
                or failure.get("source_task_type") != source.task_type.value
                or failure.get("source_attempt") != source.attempt or failure.get("retryable") is not False
                or failure.get("code") != source.error_code or failure.get("message") != source.error_message
                or failure.get("failure_class") != expected_class
                or lineage.get("parent_task_id") != source.id or lineage.get("repair_target_task_id") != source.id
                or lineage.get("content_id") != source.content_id or lineage.get("source_attempt") != source.attempt
                or lineage.get("target_task_type") != source.task_type.value
                or lineage.get("repair_fingerprint") != flow._repair_fingerprint(
                    source, failure_class=expected_class, code=source.error_code)
                or lineage.get("root_cause_signature") != signature
                or len(origins) != 1 or origins[0].actor_type != "system" or origins[0].actor_id != "bundle-preflight"
                or origins[0].metadata_json != {
                    "source_task_id": source.id, "failure_class": expected_class, "code": source.error_code,
                    "root_cause_signature": signature, "round": lineage.get("round"), "target_task_id": source.id,
                }):
            raise TaskConflictError("RECOVERY_BUNDLE_ORIGIN_UNVERIFIED")
        errors = failure.get("validation_errors")
        if (not isinstance(errors, list) or not errors or any(not isinstance(error, dict) for error in errors)
                or "；".join(f"{'.'.join(str(part) for part in error.get('path', []))}: {error.get('message', '')}"
                            for error in errors[:30])[:4000] != source.error_message):
            raise TaskConflictError("RECOVERY_BUNDLE_ORIGIN_UNVERIFIED")
        RecoveryPlan.model_validate(recovery.result_data)
        _require_bundle_recovery_subject(session, source)
        _require_idle_bundle_recovery(session, source.content_id)
        if session.exec(select(AuditEvent.id).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
            AuditEvent.action.in_([FAILURE_RECOVERY, FAILURE_RECOVERY_RECONCILED, AGENT_CONSUMED,
                                  "recovery_blocked", "recovery_escalated"]),
        )).first() or session.exec(select(AgentTask.id).where(
            AgentTask.input_data["recovery_task_id"].as_string() == recovery.id,
        )).first():
            raise TaskConflictError("RECOVERY_BUNDLE_ALREADY_APPLIED")
        origin = origins[0]
        metadata = {**self._failure_recovery_metadata(source, recovery),
                    "origin_event_id": origin.id, "origin_metadata_hash": stable_hash(origin.metadata_json),
                    "accepted_result_hash": stable_hash(recovery.result_data)}
        session.add(AuditEvent(actor_type="admin", actor_id="mpt-admin-api", entity_type="agent_task",
                               entity_id=recovery.id, action=FAILURE_RECOVERY_RECONCILED, metadata_json=metadata))

    def schedule_failure_recovery(self, task_id, *, validation_errors=None, reconcile_bundle_preflight=False):
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
                            receipts = session.exec(select(AuditEvent).where(
                                AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
                                AuditEvent.action.in_([FAILURE_RECOVERY, FAILURE_RECOVERY_RECONCILED]),
                            )).all()
                            if not receipts:
                                if not reconcile_bundle_preflight:
                                    raise TaskConflictError("RECOVERY_FAILURE_RECEIPT_MISSING")
                                try:
                                    self._reconcile_bundle_failure(session, task, recovery)
                                except (OSError, ValueError, KeyError, TypeError) as exc:
                                    raise TaskConflictError(str(exc)) from exc
                                Session.commit(session)
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
                            validation_errors=validation_errors,
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
                                metadata_json=self._failure_recovery_metadata(task, recovery, targets=(
                                    target, flow.completed_task_for(task.content_id, TaskType.SHORT_SCRIPT)
                                ) if target is not None else ()),
                            ))
                    Session.commit(session)
                    return {"recovery_status": recovery.status.value if recovery else
                            "DEFERRED" if flow.recovery_capacity_deferred else "BLOCKED",
                            "recovery_task_id": recovery.id if recovery else None}
                finally:
                    session.rollback()

    def _require_accepted_native_task(self, session, task, *, consumed):
        from glance_core.content_acceptance import _unchanged_material_task

        _require_owned_input(session, task, consumed=consumed)
        _unchanged_material_task(session, task, owned=True, consumed=consumed)
        self._require_native_deployment(session, task)
        submission = session.exec(select(AgentSubmission).where(
            AgentSubmission.task_id == task.id, AgentSubmission.attempt == task.attempt,
            AgentSubmission.validation_status == "ACCEPTED",
        )).one()
        raw = (self.settings.agent_submission_dir / task.id / str(task.attempt) / "result.json").resolve()
        if (Path(submission.raw_result_path).resolve() != raw
                or not raw.is_relative_to(self.settings.agent_submission_dir.resolve())
                or stable_hash(json.loads(raw.read_text(encoding="utf-8"))) != stable_hash(task.result_data)):
            raise TaskConflictError("RECOVERY_ACCEPTED_RESULT_CHANGED")

    def _content_qc_source(self, session, task_id, *, consumed=True, recovery_id=None, archived_receipt=False):
        from glance_core.content_acceptance import verify_acceptance
        from glance_core.models import PublicationRecord
        from glance_core.task_schemas import result_model_for
        from app.business.pipeline import _render_voice_path
        from app.business.bundles import sha256_file

        task = session.get(AgentTask, task_id)
        if task is None:
            raise KeyError(task_id)
        if (task.task_type != TaskType.CONTENT_QC or task.schema_version not in {"2", "3"}
                or not isinstance(task.input_data, dict)
                or task.status != TaskStatus.COMPLETED or not isinstance(task.result_data, dict)
                or task.result_data.get("passed") is not False
                or task.input_data.get("source_context_version") != 1
                or task.input_data.get("audio_evidence_version") != 1):
            raise TaskConflictError("RECOVERY_REQUIRES_FAILED_V2_CONTENT_QC")
        result_model_for(TaskType.CONTENT_QC, task.schema_version).model_validate(task.result_data)
        self._require_accepted_native_task(session, task, consumed=consumed)
        _require_current_revision(session, task)
        _require_reconciled_content(session, task.content_id)
        if session.exec(select(PublicationRecord.id).where(PublicationRecord.content_id == task.content_id)).first():
            raise TaskConflictError("RECOVERY_PUBLISH_RECONCILIATION_REQUIRED")
        content = session.get(Content, task.content_id)
        if content is None or (content.business_status == ContentStatus.ARCHIVED and not archived_receipt) or content.business_status in {
            ContentStatus.PUBLISHED, ContentStatus.REJECTED, ContentStatus.PACKAGED,
        }:
            raise TaskConflictError("RECOVERY_CONTENT_NOT_CURRENT")
        if session.exec(select(AgentTask.id).where(
            AgentTask.content_id == task.content_id, AgentTask.id != recovery_id,
            AgentTask.status.in_([TaskStatus.LEASED, TaskStatus.SUBMITTED]),
        )).first() or session.exec(select(SchedulerOffer.request_id).where(
            SchedulerOffer.task_id.in_(select(AgentTask.id).where(AgentTask.content_id == task.content_id)),
            SchedulerOffer.task_id != recovery_id, SchedulerOffer.expires_at > utc_now(),
        )).first():
            raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
        root = Path(content.content_dir).resolve()
        if not root.is_relative_to(self.settings.content_dir.resolve()):
            raise TaskConflictError("RECOVERY_CONTENT_STORAGE_CHANGED")
        verify_acceptance(session, task, task.result_data, _archived_revalidation=archived_receipt)
        script = session.get(AgentTask, task.input_data["upstream_task_ids"]["script"])
        plan = session.get(AgentTask, task.input_data["upstream_task_ids"]["visual_plan"])
        for parent in (script, plan):
            _require_owned_input(session, parent, consumed=True)
            _require_current_revision(session, parent)
        _render_voice_path(session, content, plan.input_data.get("visual_timing_evidence"),
                           plan.input_data.get("voice_artifact"))
        frozen = deepcopy(task.input_data)
        receipt_id = frozen.get("acceptance_receipt_artifact_id")
        receipts = session.exec(select(Artifact).where(
            Artifact.content_id == content.id, Artifact.artifact_type == "acceptance_input",
            Artifact.id == receipt_id if receipt_id else Artifact.relative_path == "render/acceptance-input.json",
        )).all()
        if len(receipts) != 1:
            raise TaskConflictError("RECOVERY_ACCEPTANCE_RECEIPT_MISSING")
        receipt = receipts[0]
        path = (root / receipt.relative_path).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or path.stat().st_size != receipt.size_bytes or sha256_file(path) != receipt.sha256):
            raise TaskConflictError("RECOVERY_ACCEPTANCE_RECEIPT_CHANGED")
        original_receipt = json.loads(path.read_text(encoding="utf-8"))
        if "audio_evidence" not in original_receipt:
            frozen.pop("audio_evidence_version")
            frozen.pop("audio_evidence")
            frozen["acceptance_subject"].pop("audio_evidence_sha256")
        if original_receipt != frozen:
            raise TaskConflictError("RECOVERY_ACCEPTANCE_RECEIPT_CHANGED")
        audio = task.input_data["audio_evidence"]
        path = (root / "render" / f"audio-evidence-{audio.get('video_sha256')}.json").resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or json.loads(path.read_text(encoding="utf-8")) != audio):
            raise TaskConflictError("RECOVERY_AUDIO_EVIDENCE_CHANGED")
        return task, script

    def _create_content_qc_recovery(self, session, task):
        from app.business.pipeline import create_content_qc_recovery, workflow

        current = effective_settings(session, self.settings)
        flow = workflow(session, current)
        flow.tasks = self.task_service(session)
        script = session.get(AgentTask, task.input_data["upstream_task_ids"]["script"])
        before = set(session.exec(select(AgentTask.id).where(AgentTask.task_type == TaskType.RECOVERY)).all())
        owned = select(AuditEvent.entity_id).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.action == OWNED,
            AuditEvent.metadata_json["input_hash"].as_string() == AgentTask.input_hash,
        )
        with filter_task_reads(session, eligible_task_clause() & (
            (AgentTask.task_type != TaskType.RECOVERY) | AgentTask.id.in_(owned)
        )):
            recovery = create_content_qc_recovery(flow, task, script)
        if recovery is not None:
            if recovery.id in before:
                self._check_content_qc_recovery_receipt(session, recovery, task, script)
            else:
                session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                       entity_id=recovery.id, action=OWNED,
                                       metadata_json={"input_hash": recovery.input_hash}))
                session.add(AuditEvent(actor_type="system", actor_id="mpt-content-qc", entity_type="agent_task",
                                       entity_id=recovery.id, action=QC_RECOVERY, metadata_json={
                                           "source_task_id": task.id, "source_input_hash": task.input_hash,
                                           "source_result_hash": stable_hash(task.result_data), "source_attempt": task.attempt,
                                           "recovery_input_hash": recovery.input_hash, "target_task_id": script.id,
                                           "target_input_hash": script.input_hash, "target_result_hash": stable_hash(script.result_data),
                                       }))
        return {"recovery_status": recovery.status.value if recovery else
                "DEFERRED" if flow.recovery_capacity_deferred else "BLOCKED",
                "recovery_task_id": recovery.id if recovery else None}

    def _check_content_qc_recovery_receipt(self, session, recovery, source, script):
        _require_owned_input(session, recovery)
        receipts = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
            AuditEvent.action == QC_RECOVERY,
        )).all()
        expected = {"source_task_id": source.id, "source_input_hash": source.input_hash,
                    "source_result_hash": stable_hash(source.result_data), "source_attempt": source.attempt,
                    "recovery_input_hash": recovery.input_hash, "target_task_id": script.id,
                    "target_input_hash": script.input_hash, "target_result_hash": stable_hash(script.result_data)}
        if (len(receipts) != 1 or receipts[0].metadata_json != expected or recovery.content_id != source.content_id
                or recovery.input_data.get("failure", {}).get("source_task_id") != source.id
                or recovery.input_data.get("failure", {}).get("source_attempt") != source.attempt
                or recovery.input_data.get("failure", {}).get("failure_class") != "BUSINESS_QC"
                or recovery.input_data.get("lineage", {}).get("repair_target_task_id") != script.id):
            raise TaskConflictError("RECOVERY_QC_RECEIPT_CHANGED")

    def _content_qc_rejection_metadata(self, recovery, source, script):
        from glance_core.task_schemas import RecoveryPlan

        plan = RecoveryPlan.model_validate(recovery.result_data)
        if not _content_qc_target_changed(recovery.result_data):
            raise TaskConflictError("RECOVERY_QC_REJECTION_RECEIPT_CHANGED")
        return {"code": QC_TARGET_REJECTION, "action": plan.action, "target_task_type": plan.target_task_type,
                "recovery_input_hash": recovery.input_hash, "recovery_result_hash": stable_hash(recovery.result_data),
                "source_task_id": source.id, "source_input_hash": source.input_hash,
                "source_result_hash": stable_hash(source.result_data), "source_attempt": source.attempt,
                "target_task_id": script.id, "target_input_hash": script.input_hash,
                "target_result_hash": stable_hash(script.result_data)}

    def _check_content_qc_rejection_receipt(self, session, recovery):
        from glance_core.models import PublicationRecord

        receipts = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
            AuditEvent.action == QC_PLAN_REJECTED,
        )).all()
        if (len(receipts) != 1 or receipts[0].actor_type != "system"
                or receipts[0].actor_id != "mpt-content-qc"):
            raise TaskConflictError("RECOVERY_QC_REJECTION_RECEIPT_CHANGED")
        self._require_accepted_native_task(session, recovery, consumed=True)
        _require_current_revision(session, recovery)
        source_id = recovery.input_data.get("failure", {}).get("source_task_id")
        content = session.get(Content, recovery.content_id)
        archived = content is not None and content.business_status == ContentStatus.ARCHIVED
        if archived:
            # Only its committed terminal receipt permits read-only acceptance revalidation after archival.
            archives = session.exec(select(AuditEvent).where(
                AuditEvent.entity_type == "content", AuditEvent.entity_id == content.id,
                AuditEvent.action == "handsfree_content_skipped",
            )).all()
            expected = {"candidate_id": content.candidate_id, "recovery_task_id": recovery.id,
                        "result_hash": stable_hash(recovery.result_data), "policy": "consumed_terminal_recovery",
                        "reason": QC_TARGET_REJECTION,
                        "previous": {"business_status": "SCRIPT_REVIEW", "current_stage": "recovery_blocked",
                                     "error_code": "RECOVERY_BLOCKED"}}
            if (len(archives) != 1 or archives[0].actor_type != "system"
                    or archives[0].actor_id != "handsfree-coordinator" or archives[0].metadata_json != expected
                    or content.current_stage != "archived" or content.error_code != "RECOVERY_BLOCKED"
                    or content.error_message != QC_TARGET_REJECTION):
                raise TaskConflictError("RECOVERY_QC_REJECTION_RECEIPT_CHANGED")
            tasks = session.exec(select(AgentTask).where(AgentTask.content_id == content.id)).all()
            if (any(row.status in {TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED} for row in tasks)
                    or session.exec(select(SchedulerOffer.request_id).where(
                        SchedulerOffer.task_id.in_([row.id for row in tasks]), SchedulerOffer.expires_at > utc_now(),
                    )).first()):
                raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
            if session.exec(select(PublicationRecord.id).where(PublicationRecord.content_id == content.id)).first():
                raise TaskConflictError("RECOVERY_PUBLISH_RECONCILIATION_REQUIRED")
            _require_reconciled_content(session, content.id)
        try:
            source, script = self._content_qc_source(session, source_id, recovery_id=recovery.id, archived_receipt=archived)
        except (OSError, ValueError, KeyError, TypeError, NonRetryableMediaError) as exc:
            raise TaskConflictError(str(exc)) from exc
        self._check_content_qc_recovery_receipt(session, recovery, source, script)
        if receipts[0].metadata_json != self._content_qc_rejection_metadata(recovery, source, script):
            raise TaskConflictError("RECOVERY_QC_REJECTION_RECEIPT_CHANGED")

    def schedule_content_qc_recovery(self, task_id, *, consuming=False):
        with Session(self.engine) as session:
            task = session.get(AgentTask, task_id)
            if task is None:
                raise KeyError(task_id)
            key = task.content_id or task.id
        with content_operation_lock(self.settings, key):
            with _ConsumptionSession(self.engine) as session:
                begin_immediate(session)
                try:
                    task, _ = self._content_qc_source(session, task_id, consumed=not consuming)
                    marker = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_id == task.id, AuditEvent.action == AGENT_CONSUMED,
                    )).first()
                    if consuming and marker is not None:
                        if marker.metadata_json.get("result_hash") != stable_hash(task.result_data):
                            raise TaskConflictError("RECOVERY_SOURCE_RESULT_CHANGED")
                        return False
                    result = self._create_content_qc_recovery(session, task)
                    if consuming:
                        session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                               entity_id=task.id, action=AGENT_CONSUMED,
                                               metadata_json={"result_hash": stable_hash(task.result_data)}))
                    Session.commit(session)
                    return True if consuming else result
                except RuntimeSettingsError:
                    raise
                except (OSError, ValueError, KeyError, TypeError, NonRetryableMediaError) as exc:
                    if isinstance(exc, KeyError) and str(exc).strip("'") == task_id:
                        raise
                    raise TaskConflictError(str(exc)) from exc
                finally:
                    session.rollback()

    def _consume_content_qc_recovery(self, task_id):
        with Session(self.engine) as session:
            task = session.get(AgentTask, task_id)
            key = task.content_id or task.id
        with content_operation_lock(self.settings, key):
            with _ConsumptionSession(self.engine) as session:
                begin_immediate(session)
                try:
                    recovery = session.get(AgentTask, task_id)
                    marker = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_id == task_id, AuditEvent.action == AGENT_CONSUMED,
                    )).first()
                    if marker is not None:
                        if marker.metadata_json.get("result_hash") != stable_hash(recovery.result_data):
                            raise TaskConflictError("RECOVERY_SOURCE_RESULT_CHANGED")
                        if _content_qc_target_changed(recovery.result_data):
                            self._check_content_qc_rejection_receipt(session, recovery)
                        return False
                    self._require_accepted_native_task(session, recovery, consumed=False)
                    _require_current_revision(session, recovery)
                    source, script = self._content_qc_source(session, recovery.input_data.get("failure", {}).get("source_task_id"),
                                                             recovery_id=recovery.id)
                    self._check_content_qc_recovery_receipt(session, recovery, source, script)
                    result = recovery.result_data or {}
                    from app.business.pipeline import workflow
                    flow = workflow(session, effective_settings(session, self.settings))
                    flow.tasks = self.task_service(session)
                    if (result.get("action") in {"create_repair_task", "reroute_to_role", "repair_task_context"}
                            and (result.get("target_task_type") or "short_script") != "short_script"):
                        metadata = self._content_qc_rejection_metadata(recovery, source, script)
                        if session.exec(select(AuditEvent.id).where(
                            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
                            AuditEvent.action.in_([QC_PLAN_REJECTED, "recovery_blocked", "recovery_escalated"]),
                        )).first():
                            raise TaskConflictError("RECOVERY_QC_REJECTION_RECEIPT_CHANGED")
                        session.add(AuditEvent(actor_type="system", actor_id="mpt-content-qc", entity_type="agent_task",
                                               entity_id=recovery.id, action=QC_PLAN_REJECTED, metadata_json=metadata))
                        flow._mark_recovery_blocked(recovery, reason=QC_TARGET_REJECTION, action="mark_recovery_blocked")
                        repair = None
                    else:
                        repair = flow.apply_recovery_plan(recovery)
                    if repair is not None:
                        session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                               entity_id=repair.id, action=OWNED,
                                               metadata_json={"input_hash": repair.input_hash}))
                    session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                           entity_id=recovery.id, action=AGENT_CONSUMED,
                                           metadata_json={"result_hash": stable_hash(recovery.result_data)}))
                    Session.commit(session)
                    return True
                except RuntimeSettingsError:
                    raise
                except (OSError, ValueError, KeyError, TypeError, NonRetryableMediaError) as exc:
                    raise TaskConflictError(str(exc)) from exc
                finally:
                    session.rollback()

    def _failure_recovery_needs_apply(self, session, recovery):
        receipts = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
            AuditEvent.action.in_([FAILURE_RECOVERY, FAILURE_RECOVERY_RECONCILED]),
        )).all()
        if len(receipts) > 1:
            raise TaskConflictError("RECOVERY_FAILURE_RECEIPT_CHANGED")
        receipt = receipts[0] if receipts else None
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
        if receipt.action == FAILURE_RECOVERY_RECONCILED:
            _require_current_revision(session, recovery)
            _require_idle_bundle_recovery(session, recovery.content_id)
            self._require_accepted_native_task(session, recovery, consumed=False)
            origin = session.get(AuditEvent, receipt.metadata_json.get("origin_event_id"))
            if (receipt.actor_type != "admin" or receipt.actor_id != "mpt-admin-api"
                    or origin is None or origin.entity_id != recovery.id or origin.entity_type != "agent_task"
                    or origin.action != "recovery_task_created" or origin.actor_type != "system"
                    or origin.actor_id != "bundle-preflight"
                    or stable_hash(origin.metadata_json) != receipt.metadata_json.get("origin_metadata_hash")
                    or stable_hash(recovery.result_data) != receipt.metadata_json.get("accepted_result_hash")):
                raise TaskConflictError("RECOVERY_FAILURE_RECEIPT_CHANGED")
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
        if receipt.action == FAILURE_RECOVERY_RECONCILED:
            _require_bundle_recovery_subject(session, source)
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

    def _consume_reconciled_bundle_recovery(self, task_id):
        """The caller holds the content lock; portable commits flush inside this one transaction."""
        from app.business.pipeline import workflow

        with _ConsumptionSession(self.engine) as session:
            begin_immediate(session)
            try:
                recovery = session.get(AgentTask, task_id)
                if recovery is None or recovery.task_type != TaskType.RECOVERY or recovery.status != TaskStatus.COMPLETED:
                    raise TaskConflictError("Agent result is not accepted yet")
                _require_owned_input(session, recovery)
                digest = stable_hash(recovery.result_data)
                marker = session.exec(select(AuditEvent).where(
                    AuditEvent.entity_id == task_id, AuditEvent.action == AGENT_CONSUMED,
                )).first()
                if marker is not None:
                    if marker.metadata_json.get("result_hash") != digest:
                        raise TaskConflictError("RECOVERY_SOURCE_RESULT_CHANGED")
                    return False
                if not session.exec(select(AuditEvent.id).where(
                    AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task_id,
                    AuditEvent.action == FAILURE_RECOVERY_RECONCILED,
                )).first():
                    raise TaskConflictError("RECOVERY_FAILURE_RECEIPT_MISSING")
                if self._failure_recovery_needs_apply(session, recovery):
                    flow = workflow(session, effective_settings(session, self.settings))
                    flow.tasks = self.task_service(session)
                    repair = flow.apply_recovery_plan(recovery)
                    if repair is not None:
                        session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                               entity_id=repair.id, action=OWNED,
                                               metadata_json={"input_hash": repair.input_hash}))
                session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                       entity_id=task_id, action=AGENT_CONSUMED,
                                       metadata_json={"result_hash": digest}))
                Session.commit(session)
                return True
            except RuntimeSettingsError:
                raise
            except (OSError, ValueError, KeyError, TypeError, NonRetryableMediaError) as exc:
                raise TaskConflictError(str(exc)) from exc
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
                if kind == TaskType.RECOVERY:
                    _require_owned_input(session, task)
                if kind == TaskType.RECOVERY and (
                    (_content_qc_target_changed(task.result_data)
                     and (task.input_data or {}).get("failure", {}).get("source_task_type") == TaskType.CONTENT_QC.value)
                    or session.exec(select(AuditEvent.id).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
                        AuditEvent.action == QC_PLAN_REJECTED,
                    )).first()
                ):
                    self._check_content_qc_rejection_receipt(session, task)
                return False
        if kind == TaskType.CONTENT_QC and task.schema_version in {"2", "3"} and (task.result_data or {}).get("passed") is False:
            return self.schedule_content_qc_recovery(task_id, consuming=True)
        if kind == TaskType.RECOVERY:
            with Session(self.engine) as session:
                native_qc = session.exec(select(AuditEvent.id).where(
                    AuditEvent.entity_id == task_id, AuditEvent.action.in_([QC_RECOVERY, "recovery_task_created"]),
                    AuditEvent.actor_id == "mpt-content-qc",
                )).first()
            if native_qc:
                return self._consume_content_qc_recovery(task_id)
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
                reconciled_bundle = kind == TaskType.RECOVERY and session.exec(select(AuditEvent.id).where(
                    AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task_id,
                    AuditEvent.action == FAILURE_RECOVERY_RECONCILED,
                )).first() is not None
                needs_apply = not reconciled_bundle and (
                    kind != TaskType.RECOVERY or self._failure_recovery_needs_apply(session, task)
                )
            if reconciled_bundle:
                return self._consume_reconciled_bundle_recovery(task_id)
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
                    operation(self.engine, self.settings, task.content_id, consuming_task_id=task_id, automatic=True)
                elif action in {"voice", "package"}:
                    operation(self.engine, self.settings, task.content_id, automatic=True)
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

    def recover_agent_results(self, *, lock_timeout_seconds: float = 30, content_id: str | None = None):
        from app.business.pipeline import VoiceRetryExhausted

        with Session(self.engine) as session:
            tasks = session.exec(
                select(AgentTask.id).where(
                    AgentTask.status == TaskStatus.COMPLETED,
                    *([AgentTask.content_id == content_id] if content_id is not None else []),
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
            except VoiceRetryExhausted:
                # The immutable approved result stays pending until a new
                # authorized media run. Repeated maintenance performs no IO.
                pass
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
                            or offer.schema_version not in {"2", "3"} or stable_hash(offer.input_data) != offer.input_hash
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
                        Artifact.id == frozen.get("acceptance_receipt_artifact_id") if offer.schema_version == "3"
                        else Artifact.relative_path == "render/acceptance-input.json",
                    )).all()
                    if offer.schema_version == "3" and len(receipts) == 1:
                        receipt_path = root / receipts[0].relative_path
                    if (not receipt_path.resolve().is_relative_to(root) or not receipt_path.is_file()
                            or sum(artifact.sha256 == sha256_file(receipt_path)
                                   and artifact.size_bytes == receipt_path.stat().st_size for artifact in receipts) != 1):
                        raise ValueError("Visual restoration original render receipt changed")
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    without_audio = deepcopy(frozen)
                    if "audio_evidence" not in receipt:
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
                    visual = None
                    if offer.schema_version == "3":
                        from app.business.pipeline import _collect_visual_evidence

                        visual = _collect_visual_evidence(session, content, refs)
                    current = acceptance_subject(session, content_id, refs, source_context_version=1,
                                                 audio_evidence=audio, visual_evidence=visual)
                    expected = {**frozen["acceptance_subject"], "visual_plan_sha256": submission.result_hash}
                    if visual is not None:
                        expected["visual_evidence_sha256"] = stable_hash(visual)
                    context = source_context(session, content_id, refs)
                    if current != expected or context != frozen["source_context"] or context.get("evidence_gaps"):
                        raise ValueError("Visual restoration current acceptance materials changed")
                    artifact = Artifact(content_id=content_id, artifact_type="acceptance_input", relative_path="",
                                        sha256="", size_bytes=0, mime_type="application/json",
                                        producer_stage="visual_plan_restoration")
                    new_input = {**frozen, "acceptance_subject": current, "previous_acceptance_task_id": offer.id,
                                 "acceptance_receipt_artifact_id": artifact.id}
                    if visual is not None:
                        old_root = (root / frozen["visual_evidence"]["manifest_path"]).parent
                        new_root = (root / visual["manifest_path"]).parent
                        new_input["visual_evidence"] = visual
                        new_input["context_files"] = [
                            path for path in frozen.get("context_files", [])
                            if not (root / path).resolve().is_relative_to(old_root.resolve())
                        ] + [visual["manifest_path"], *[
                            (new_root / item["path"]).relative_to(root).as_posix()
                            for item in [*visual["manifest"]["pages"], *visual["manifest"]["frames"],
                                         visual["manifest"]["subtitles"]]]]
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
                                                         allow_multiple=True, schema_version=offer.schema_version)
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
            base_settings=settings,
            opening_identity_overlay=getattr(MptMediaExecutor, "OPENING_IDENTITY_OVERLAY", False),
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
