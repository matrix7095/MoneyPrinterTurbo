"""MPT-owned storage and consumption boundary around portable Glance services."""

from glance_core.content_revision import current_revision_clause

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
QC_REINSPECTION = "mpt_content_qc_reinspection_created"
VISUAL_REPAIR_CONTEXT = "mpt_visual_repair_context_reconciled"
VISUAL_REVIEW_CONTEXT = "mpt_visual_plan_review_context_reconciled"
# Version 1 recognizes this audited legacy derivation, never an arbitrary failed rights review.
VISUAL_REVIEW_LEGACY_SOURCE = "ca87d146dbc700990f54e2460332c36b233cb96eb897a7f10003efbef3cb6a3c"
QC_REINSPECTION_CHECKS = frozenset({
    "CONTENT_QC_SAME_SUBJECT_FULL_REVIEW_V1", "CONTENT_QC_ALL_NINE_HARD_GATES",
})


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
        current_revision_clause(AgentTask), AgentTask.content_id == task.content_id, AgentTask.task_type == task.task_type,
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
    if (content := session.get(Content, content_id)) is not None and content.current_revision_id:
        from glance_core.content_revision import require_reconciled_publish_intents

        require_reconciled_publish_intents(session, content_id)
        return
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
            current_revision_clause(AgentTask), AgentTask.content_id == content_id,
            AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
        )).first() or session.exec(select(SchedulerOffer.request_id).where(
            SchedulerOffer.task_id.in_(select(AgentTask.id).where(current_revision_clause(AgentTask), AgentTask.content_id == content_id)),
            SchedulerOffer.expires_at > utc_now(),
        )).first()):
        raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
    if session.exec(select(PublicationRecord.id).where(current_revision_clause(PublicationRecord), PublicationRecord.content_id == content_id)).first():
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
                        current_revision_clause(AgentTask), AgentTask.content_id == task.content_id,
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
        if session.exec(select(PublicationRecord.id).where(current_revision_clause(PublicationRecord),
                                                         PublicationRecord.content_id == task.content_id)).first():
            raise TaskConflictError("RECOVERY_PUBLISH_RECONCILIATION_REQUIRED")
        content = session.get(Content, task.content_id)
        if content is None or (content.business_status == ContentStatus.ARCHIVED and not archived_receipt) or content.business_status in {
            ContentStatus.PUBLISHED, ContentStatus.REJECTED, ContentStatus.PACKAGED,
        }:
            raise TaskConflictError("RECOVERY_CONTENT_NOT_CURRENT")
        if session.exec(select(AgentTask.id).where(
            current_revision_clause(AgentTask), AgentTask.content_id == task.content_id, AgentTask.id != recovery_id,
            AgentTask.status.in_([TaskStatus.LEASED, TaskStatus.SUBMITTED]),
        )).first() or session.exec(select(SchedulerOffer.request_id).where(
            SchedulerOffer.task_id.in_(select(AgentTask.id).where(current_revision_clause(AgentTask), AgentTask.content_id == task.content_id)),
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
            current_revision_clause(Artifact), Artifact.content_id == content.id, Artifact.artifact_type == "acceptance_input",
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
        path = MptMediaExecutor(root).final_audio_evidence_path(audio).resolve()
        if not path.is_file():
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
            tasks = session.exec(select(AgentTask).where(current_revision_clause(AgentTask), AgentTask.content_id == content.id)).all()
            if (any(row.status in {TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED} for row in tasks)
                    or session.exec(select(SchedulerOffer.request_id).where(
                        SchedulerOffer.task_id.in_([row.id for row in tasks]), SchedulerOffer.expires_at > utc_now(),
                    )).first()):
                raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
            if session.exec(select(PublicationRecord.id).where(current_revision_clause(PublicationRecord), PublicationRecord.content_id == content.id)).first():
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
                        if session.exec(select(AuditEvent.id).where(
                            AuditEvent.entity_id == task_id, AuditEvent.action == QC_REINSPECTION,
                        )).first():
                            self._check_content_qc_reinspection_receipt(session, recovery)
                        elif _content_qc_target_changed(recovery.result_data):
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
                    if (result.get("action") == "create_repair_task" and result.get("target_task_type") == "content_qc"
                            and QC_REINSPECTION_CHECKS.issubset(result.get("acceptance_checks", []))):
                        repair = self._create_content_qc_reinspection(session, recovery, source, script, flow)
                    elif (result.get("action") in {"create_repair_task", "reroute_to_role", "repair_task_context"}
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

    @staticmethod
    def _content_qc_reinspection_metadata(recovery, source, script, child):
        return {"recovery_input_hash": recovery.input_hash, "recovery_result_hash": stable_hash(recovery.result_data),
                "source_task_id": source.id, "source_input_hash": source.input_hash,
                "source_result_hash": stable_hash(source.result_data), "source_attempt": source.attempt,
                "legacy_target_task_id": script.id, "legacy_target_input_hash": script.input_hash,
                "legacy_target_result_hash": stable_hash(script.result_data),
                "lineage": deepcopy(recovery.input_data["lineage"]), "child_task_id": child.id,
                "child_input_hash": child.input_hash, "subject": deepcopy(source.input_data["acceptance_subject"]),
                "manifest_sha256": source.input_data["visual_evidence"]["manifest_sha256"],
                "acceptance_receipt_artifact_id": child.input_data["acceptance_receipt_artifact_id"]}

    def _check_content_qc_reinspection_receipt(self, session, recovery):
        self._require_accepted_native_task(session, recovery, consumed=True)
        receipts = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
            AuditEvent.action == QC_REINSPECTION,
        )).all()
        children = session.exec(select(AgentTask).where(
            AgentTask.input_data["recovery_task_id"].as_string() == recovery.id,
        )).all()
        source = session.get(AgentTask, recovery.input_data["failure"]["source_task_id"])
        script = session.get(AgentTask, recovery.input_data["lineage"]["repair_target_task_id"])
        if (len(receipts) != 1 or receipts[0].actor_type != "system" or receipts[0].actor_id != "mpt-content-qc"
                or len(children) != 1 or source is None or script is None):
            raise TaskConflictError("RECOVERY_QC_REINSPECTION_RECEIPT_CHANGED")
        child = children[0]
        if (child.task_type != TaskType.CONTENT_QC or child.schema_version != "3"
                or child.content_id != recovery.content_id or stable_hash(child.input_data) != child.input_hash
                or receipts[0].metadata_json != self._content_qc_reinspection_metadata(recovery, source, script, child)
                or session.exec(select(AuditEvent.id).where(
                    AuditEvent.entity_id == recovery.id, AuditEvent.action == QC_PLAN_REJECTED,
                )).first()):
            raise TaskConflictError("RECOVERY_QC_REINSPECTION_RECEIPT_CHANGED")
        _require_owned_input(session, child)
        self._require_accepted_native_task(session, source, consumed=True)
        self._require_accepted_native_task(session, script, consumed=True)
        self._require_accepted_native_task(session, session.get(AgentTask, source.input_data["upstream_task_ids"]["visual_plan"]), consumed=True)
        self._check_content_qc_recovery_receipt(session, recovery, source, script)
        from app.business.bundles import sha256_file
        content = session.get(Content, child.content_id)
        receipt = session.get(Artifact, child.input_data.get("acceptance_receipt_artifact_id"))
        root = Path(content.content_dir).resolve() if content else None
        path = (root / receipt.relative_path).resolve() if root and receipt else None
        if (root is None or not root.is_relative_to(self.settings.content_dir.resolve())
                or receipt is None or receipt.content_id != child.content_id or receipt.artifact_type != "acceptance_input"
                or not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != receipt.size_bytes
                or sha256_file(path) != receipt.sha256 or json.loads(path.read_text(encoding="utf-8")) != child.input_data):
            raise TaskConflictError("RECOVERY_QC_REINSPECTION_RECEIPT_CHANGED")

    def _create_content_qc_reinspection(self, session, recovery, source, script, flow):
        from glance_core.content_acceptance import is_handsfree_content
        from glance_core.task_schemas import RecoveryPlan
        from glance_media.executor import MediaExecutor
        from app.business.pipeline import _write_acceptance_receipt

        plan = RecoveryPlan.model_validate(recovery.result_data)
        self._require_accepted_native_task(session, script, consumed=True)
        self._require_accepted_native_task(session, session.get(AgentTask, source.input_data["upstream_task_ids"]["visual_plan"]), consumed=True)
        result, inputs = source.result_data, source.input_data
        lineage, failure = recovery.input_data["lineage"], recovery.input_data["failure"]
        policy = recovery.input_data.get("policy") or {}
        current = effective_settings(session, self.settings)
        content = session.get(Content, source.content_id)
        signature = flow._root_cause_signature("BUSINESS_QC", "CONTENT_QC_FAILED")
        rounds = [item for item in flow._recovery_history(source.content_id)
                  if item["root_cause_signature"] == signature]
        origins = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
            AuditEvent.action == "recovery_task_created",
        )).all()
        expected_origin = {"source_task_id": source.id, "failure_class": "BUSINESS_QC", "code": "CONTENT_QC_FAILED",
                           "root_cause_signature": signature, "round": lineage.get("round"), "target_task_id": script.id}
        limits = [lineage.get("max_rounds"), policy.get("max_rounds"), current.recovery_max_rounds]
        if (plan.failure_class != "BUSINESS_QC" or plan.action != "create_repair_task"
                or plan.target_task_type != "content_qc" or source.id not in plan.evidence_refs
                or not QC_REINSPECTION_CHECKS.issubset(plan.acceptance_checks)
                or not plan.must_change or not plan.must_preserve or not plan.must_not_do
                or any("create_repair_task" not in actions for actions in
                       (recovery.input_data.get("allowed_actions", []), policy.get("allowed_actions", [])))
                or policy.get("enabled") is not True or not current.recovery_enabled
                or any(type(limit) is not int or limit < 1 for limit in limits)
                or type(lineage.get("round")) is not int or not 1 <= lineage["round"] <= min(limits)
                or lineage["round"] != len(rounds) or lineage.get("root_cause_signature") != signature
                or lineage.get("root_task_id") != (inputs.get("lineage") or {}).get("root_task_id", source.id)
                or lineage.get("parent_task_id") != source.id or lineage.get("content_id") != source.content_id
                or lineage.get("source_attempt") != source.attempt
                or lineage.get("target_task_type") != "short_script" or lineage.get("same_task_retries") != 0
                or lineage.get("repair_fingerprint") != flow._repair_fingerprint(
                    source, failure_class="BUSINESS_QC", code="CONTENT_QC_FAILED")
                or failure.get("code") != "CONTENT_QC_FAILED" or failure.get("retryable") is not False
                or failure.get("validation_errors") != result.get("issues")
                or len(origins) != 1 or origins[0].actor_type != "system" or origins[0].actor_id != "mpt-content-qc"
                or origins[0].metadata_json != expected_origin
                or content is None or (content.business_status, content.current_stage) != (ContentStatus.WAITING_AGENT, "recovery")):
            raise TaskConflictError("RECOVERY_QC_REINSPECTION_NOT_ALLOWED")
        issues, review = result.get("issues", []), result.get("visual_review") or {}
        visual = inputs.get("visual_evidence") or {}
        manifest = visual.get("manifest") or {}
        pages, observations = manifest.get("pages", []), review.get("pages", [])
        # This proves missing coverage, not the absence of a physical defect; all nine checks run anew.
        if (source.schema_version != "3" or len(issues) != 1 or issues[0].get("severity") != "error"
                or issues[0].get("field") != "visual_review" or issues[0].get("segment_index") is not None
                or any(result.get("checks", {}).get(key, {}).get("passed") is not True for key in ("facts", "technical"))
                or review.get("method") != "native_contact_pages_asr_pcm"
                or review.get("video_sha256") != inputs["acceptance_subject"]["video_sha256"]
                or review.get("manifest_sha256") != visual.get("manifest_sha256")
                or review.get("realtime_motion_required") is not False or review.get("realtime_motion_observed") is not False
                or not pages or len(observations) != len(pages)
                or any(type(row.get("observed")) is not bool or any(row.get(key) != page.get(key)
                       for key in ("page_index", "sha256", "frame_indices"))
                       for row, page in zip(observations, pages, strict=True))
                or not 0 < sum(row["observed"] for row in observations) < len(pages)):
            raise TaskConflictError("RECOVERY_QC_REINSPECTION_NOT_ALLOWED")
        if (session.exec(select(AgentTask.id).where(
                current_revision_clause(AgentTask), AgentTask.content_id == source.content_id, AgentTask.id != recovery.id,
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
            )).first() or session.exec(select(AgentTask.id).where(
                AgentTask.input_data["recovery_task_id"].as_string() == recovery.id,
            )).first() or session.exec(select(AuditEvent.id).where(
                AuditEvent.entity_id == recovery.id, AuditEvent.action.in_([QC_REINSPECTION, QC_PLAN_REJECTED,
                                                                           "recovery_blocked", "recovery_escalated"]),
            )).first()):
            raise TaskConflictError("RECOVERY_CONTENT_IN_FLIGHT")
        if session.exec(select(AuditEvent.id).where(
            AuditEvent.entity_type == "publish_intent",
            AuditEvent.metadata_json["content_id"].as_string() == source.content_id,
        )).first():
            raise TaskConflictError("RECOVERY_PUBLISH_RECONCILIATION_REQUIRED")
        new_input = deepcopy(inputs)
        new_input.update(recovery_task_id=recovery.id, previous_acceptance_task_id=source.id,
                         lineage=deepcopy(lineage), recovery_plan=plan.model_dump(mode="json"))
        _write_acceptance_receipt(session, content, MediaExecutor(Path(content.content_dir)), new_input)
        child = flow.create_agent_task(source.content_id, TaskType.CONTENT_QC, new_input, priority=60,
                                       allow_multiple=True, schema_version="3",
                                       handsfree=is_handsfree_content(session, source.content_id))
        session.add(AuditEvent(actor_type="system", actor_id="mpt-content-qc", entity_type="agent_task",
                               entity_id=recovery.id, action=QC_REINSPECTION,
                               metadata_json=self._content_qc_reinspection_metadata(recovery, source, script, child)))
        content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "content_qc"
        content.error_code, content.error_message = None, None
        content.updated_at = utc_now()
        session.add(content)
        return child

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
            current_revision_clause(AgentTask), AgentTask.content_id == recovery.content_id,
            AgentTask.input_data["recovery_task_id"].as_string() == recovery.id,
        )).all()
        if retried or children:
            if any(stable_hash(child.input_data) != child.input_hash for child in children):
                raise TaskConflictError("RECOVERY_REPAIR_INPUT_CHANGED")
            return False
        source, _ = self._failure_source(session, source.id)
        if session.exec(select(AgentTask.id).where(
            current_revision_clause(AgentTask), AgentTask.content_id == recovery.content_id, AgentTask.id != recovery.id,
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
                reinspection = kind == TaskType.RECOVERY and session.exec(select(AuditEvent.id).where(
                    AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
                    AuditEvent.action == QC_REINSPECTION,
                )).first()
                if reinspection:
                    self._check_content_qc_reinspection_receipt(session, task)
                elif kind == TaskType.RECOVERY and (
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
                    current_revision_clause(AgentTask), AgentTask.status == TaskStatus.COMPLETED,
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

    def _visual_repair_history(self, session, task, content_id):
        from glance_core.content_acceptance import _unchanged_material_task, completed_image_origin

        if task is None or task.content_id != content_id or not completed_image_origin(session, task):
            raise TaskConflictError("VISUAL_REPAIR_HISTORY_NOT_ACCEPTED")
        _require_owned_input(session, task)
        _unchanged_material_task(session, task, owned=True, consumed=True)
        submissions = session.exec(select(AgentSubmission).where(
            AgentSubmission.task_id == task.id, AgentSubmission.attempt == task.attempt,
            AgentSubmission.validation_status == "ACCEPTED",
        )).all()
        raw = (self.settings.agent_submission_dir / task.id / str(task.attempt) / "result.json").resolve()
        if (len(submissions) != 1 or not raw.is_relative_to(self.settings.agent_submission_dir.resolve())
                or Path(submissions[0].raw_result_path).resolve() != raw
                or stable_hash(json.loads(raw.read_text(encoding="utf-8"))) != stable_hash(task.result_data)):
            raise TaskConflictError("VISUAL_REPAIR_HISTORY_RESULT_CHANGED")
        cancellations = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
            AuditEvent.action == "cancelled",
        )).all()
        if cancellations and (task.status != TaskStatus.CANCELLED or any(
            row.actor_type != "system" or row.actor_id != "recovery-visual-repair"
            or row.metadata_json != {"reason": "recovery-visual-repair"}
            for row in cancellations
        )):
            raise TaskConflictError("VISUAL_REPAIR_HISTORY_CANCELLATION_CHANGED")

    def request_pending_render_revision(self, content_id: str, expected: dict) -> dict:
        from app.business.pending_render_revision import request_rerender
        from app.business.requests import PendingRenderRevisionRequest

        expected = PendingRenderRevisionRequest.model_validate(expected).model_dump()
        with content_operation_lock(self.settings, content_id):
            with _ConsumptionSession(self.engine) as session:
                begin_immediate(session)
                try:
                    response = request_rerender(self, session, content_id, expected)
                    Session.commit(session)
                    return response
                finally:
                    session.rollback()

    def start_published_revision(self, content_id: str, expected: dict) -> dict:
        from app.business.published_revision import start_published_revision
        from app.business.requests import PublishedRevisionRequest

        payload = PublishedRevisionRequest.model_validate(expected)
        with content_operation_lock(self.settings, content_id):
            with Session(self.engine) as session:
                begin_immediate(session)
                try:
                    response = start_published_revision(session, content_id, payload)
                    session.commit()
                    return response
                finally:
                    session.rollback()

    def reconcile_packaged_visual_context(self, content_id: str, expected: dict) -> dict:
        from app.business.requests import PackagedVisualContextRequest
        from app.business.visual_plan_context import reconcile_packaged_visual_context

        expected = PackagedVisualContextRequest.model_validate(expected).model_dump()
        with content_operation_lock(self.settings, content_id):
            with _ConsumptionSession(self.engine) as session:
                begin_immediate(session)
                try:
                    response = reconcile_packaged_visual_context(self, session, content_id, expected)
                    Session.commit(session)
                    return response
                finally:
                    session.rollback()

    def reconcile_visual_repair_context(self, content_id: str, expected: dict) -> dict:
        """Supersede only the proven indexed-continuity context defect, without a new Recovery."""
        from glance_core.content_acceptance import generated_image_material
        from glance_core.models import ProductionMode, PublicationRecord
        from glance_core.task_schemas import RecoveryFailureEnvelope, RecoveryPlan
        from glance_core.task_service import _normalize_content_task_input
        from glance_core.workflow import _failed_segment_indexes
        from app.business.content_host import child_work_unresolved, host_live
        from app.business.content_run import load_run
        from app.business.pipeline import _ensure_image_only_asset_requests

        with content_operation_lock(self.settings, content_id):
            with _ConsumptionSession(self.engine) as session:
                begin_immediate(session)
                try:
                    assert_allowed(session, content_id=content_id)
                    content = session.get(Content, content_id)
                    if content is None:
                        raise KeyError(content_id)
                    bound = {}
                    for name, kind in (("recovery", TaskType.RECOVERY), ("plan", TaskType.VISUAL_PLAN),
                                       ("qc", TaskType.VISUAL_PLAN_QC)):
                        binding = expected[name]
                        task = session.get(AgentTask, binding["task_id"])
                        if (task is None or task.content_id != content_id or task.task_type != kind
                                or task.input_hash != binding["input_hash"]
                                or stable_hash(task.input_data) != binding["input_hash"]
                                or (name != "qc" and stable_hash(task.result_data) != binding["result_hash"])):
                            raise TaskConflictError("VISUAL_REPAIR_EXPECTED_BINDING_CHANGED")
                        bound[name] = task
                    recovery, bad, qc = (bound[name] for name in ("recovery", "plan", "qc"))
                    key = stable_hash({"content_id": content_id, "recovery_id": recovery.id,
                                       "superseded_plan_id": bad.id, "version": 1})
                    receipts = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "content", AuditEvent.entity_id == content_id,
                        AuditEvent.action == VISUAL_REPAIR_CONTEXT,
                    )).all()
                    matches = [row for row in receipts if row.metadata_json.get("key") == key]
                    if matches:
                        if len(matches) != 1 or matches[0].metadata_json.get("expected") != expected:
                            raise TaskConflictError("VISUAL_REPAIR_REPLAY_BINDING_CHANGED")
                        receipt = matches[0]
                        replacement = session.get(AgentTask, receipt.metadata_json.get("replacement_task_id"))
                        if (receipt.actor_type != "admin" or receipt.actor_id != "mpt-admin-api"
                                or replacement is None or replacement.content_id != content_id
                                or replacement.task_type != TaskType.VISUAL_PLAN
                                or replacement.input_hash != receipt.metadata_json.get("replacement_input_hash")
                                or stable_hash(replacement.input_data) != replacement.input_hash
                                or replacement.input_data.get("recovery_task_id") != recovery.id
                                or replacement.input_data.get("supersedes_task_id") != bad.id
                                or replacement.input_data.get("visual_repair_context_version") != 1):
                            raise TaskConflictError("VISUAL_REPAIR_REPLAY_CHANGED")
                        _require_owned_input(session, replacement)
                        return {**receipt.metadata_json["response"], "replayed": True}
                    run = load_run(session)
                    if (run is None or run.content_id != content_id
                            or {field: getattr(run, field) for field in expected["run"]} != expected["run"]
                            or run.state != "stopped" or not run.stop_requested
                            or host_live(run) or child_work_unresolved(run)):
                        raise TaskConflictError("VISUAL_REPAIR_RUN_NOT_DRAINED_OR_CHANGED")
                    if (content.production_mode != ProductionMode.IMAGE_ONLY
                            or (content.business_status, content.current_stage)
                            != (ContentStatus.WAITING_AGENT, "visual_plan_qc")):
                        raise TaskConflictError("VISUAL_REPAIR_CONTENT_NOT_CURRENT")
                    tasks = session.exec(select(AgentTask).where(current_revision_clause(AgentTask), AgentTask.content_id == content_id)).all()
                    if (any(task.status in {TaskStatus.LEASED, TaskStatus.SUBMITTED}
                            or (task.status == TaskStatus.PENDING and task.id != qc.id) for task in tasks)
                            or session.exec(select(SchedulerOffer.request_id).where(
                                SchedulerOffer.task_id.in_([task.id for task in tasks]),
                                SchedulerOffer.expires_at > utc_now(),
                            )).first()):
                        raise TaskConflictError("VISUAL_REPAIR_CONTENT_IN_FLIGHT")
                    if (session.exec(select(PublicationRecord.id).where(current_revision_clause(PublicationRecord), PublicationRecord.content_id == content_id)).first()
                            or session.exec(select(AuditEvent.id).where(
                                AuditEvent.entity_type == "publish_intent",
                                AuditEvent.metadata_json["content_id"].as_string() == content_id,
                            )).first()):
                        raise TaskConflictError("VISUAL_REPAIR_PUBLISH_HISTORY_PRESENT")
                    _require_reconciled_content(session, content_id)
                    _require_current_revision(session, bad)
                    if (recovery.status != TaskStatus.COMPLETED or bad.status != TaskStatus.COMPLETED
                            or any(task.task_type == TaskType.RECOVERY and task.id != recovery.id
                                   and as_utc(task.created_at) >= as_utc(recovery.created_at) for task in tasks)
                            or [task.id for task in tasks if task.input_data.get("recovery_task_id") == recovery.id
                                and task.task_type == TaskType.VISUAL_PLAN] != [bad.id]):
                        raise TaskConflictError("VISUAL_REPAIR_RECOVERY_SUPERSEDED")
                    assert_allowed(session, task_id=qc.id)
                    qc_events = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == qc.id,
                    )).all()
                    created = [row for row in qc_events if row.action == "created"]
                    if (len(created) != 1 or created[0].actor_type != "system"
                            or created[0].actor_id != "task-service" or created[0].metadata_json != {}
                            or any(row.action not in {"created", OWNED} for row in qc_events)):
                        raise TaskConflictError("VISUAL_REPAIR_QC_CREATION_OR_EXECUTION_CHANGED")
                    if any(row.action == OWNED for row in qc_events):
                        _require_owned_input(session, qc)
                    if (qc.status != TaskStatus.PENDING or qc.attempt != 0 or qc.assigned_agent_id
                            or qc.lease_token_hash or qc.lease_expires_at or qc.started_at or qc.completed_at
                            or qc.attempt_started_at or qc.last_heartbeat_at or qc.result_data
                            or session.exec(select(AgentSubmission.id).where(AgentSubmission.task_id == qc.id)).first()
                            or qc.input_data.get("upstream_task_ids", {}).get("visual_plan") != bad.id):
                        raise TaskConflictError("VISUAL_REPAIR_QC_ALREADY_EXECUTED_OR_CHANGED")
                    descendants = {bad.id, qc.id}
                    for task in tasks:
                        if task.id in descendants:
                            continue
                        references = task.input_data.get("upstream_task_ids", {})
                        ids = {item for value in references.values() for item in
                               (value if isinstance(value, list) else [value]) if isinstance(item, str)}
                        if ids & descendants or task.input_data.get("previous_plan_task_id") == bad.id:
                            raise TaskConflictError("VISUAL_REPAIR_DESCENDANT_ALREADY_CREATED")
                    envelope = RecoveryFailureEnvelope.model_validate(recovery.input_data.get("failure"))
                    plan = RecoveryPlan.model_validate(recovery.result_data)
                    source = session.get(AgentTask, envelope.source_task_id)
                    lineage = recovery.input_data.get("lineage") or {}
                    previous = session.get(AgentTask, lineage.get("repair_target_task_id"))
                    if (source is None or source.task_type != TaskType.ASSET_QC
                            or previous is None or previous.task_type != TaskType.VISUAL_PLAN
                            or envelope.failure_class != plan.failure_class or plan.failure_class != "BUSINESS_QC"
                            or plan.action != "create_repair_task" or plan.target_task_type != "visual_plan"
                            or envelope.source_task_type != "asset_qc" or envelope.source_attempt != source.attempt
                            or envelope.code != "ASSET_QC_FAILED" or envelope.retryable
                            or envelope.validation_errors != (source.result_data or {}).get("issues")
                            or (source.result_data or {}).get("passed") is not False
                            or len(envelope.validation_errors) != 1
                            or envelope.validation_errors[0].get("field") != "continuity"
                            or envelope.validation_errors[0].get("severity") != "error"
                            or bad.input_data.get("previous_plan_task_id") != previous.id
                            or bad.input_data.get("recovery_task_id") != recovery.id
                            or lineage.get("parent_task_id") != source.id or lineage.get("content_id") != content_id
                            or lineage.get("source_attempt") != source.attempt
                            or lineage.get("root_task_id") != source.input_data.get("lineage", {}).get("root_task_id", source.id)
                            or type(lineage.get("same_task_retries")) is not int or lineage["same_task_retries"] != 0
                            or lineage.get("target_task_type") != "visual_plan"):
                        raise TaskConflictError("VISUAL_REPAIR_RECOVERY_INTENT_CHANGED")
                    flow = self.workflow_service(session)
                    signature = flow._root_cause_signature("BUSINESS_QC", envelope.code)
                    policy = recovery.input_data.get("policy") or {}
                    current = effective_settings(session, self.settings)
                    limits = [lineage.get("max_rounds"), policy.get("max_rounds"), current.recovery_max_rounds]
                    if (policy.get("enabled") is not True or not current.recovery_enabled
                            or any(type(limit) is not int or limit < 1 for limit in limits)
                            or type(lineage.get("round")) is not int or not 1 <= lineage["round"] <= min(limits)
                            or lineage["round"] != sum(item["root_cause_signature"] == signature
                                                       for item in flow._recovery_history(content_id))
                            or lineage.get("root_cause_signature") != signature
                            or lineage.get("repair_fingerprint") != flow._repair_fingerprint(
                                source, failure_class="BUSINESS_QC", code=envelope.code)
                            or any("create_repair_task" not in actions for actions in (
                                policy.get("allowed_actions", []), recovery.input_data.get("allowed_actions", [])))):
                        raise TaskConflictError("VISUAL_REPAIR_RECOVERY_AUTHORITY_CHANGED")
                    origins = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
                        AuditEvent.action == "recovery_task_created",
                    )).all()
                    origin = {"source_task_id": source.id, "failure_class": "BUSINESS_QC", "code": envelope.code,
                              "root_cause_signature": signature, "round": lineage["round"], "target_task_id": previous.id}
                    if len(origins) != 1 or origins[0].actor_type != "system" or origins[0].metadata_json != origin:
                        raise TaskConflictError("VISUAL_REPAIR_RECOVERY_ORIGIN_CHANGED")
                    generation = session.get(AgentTask, source.input_data.get("upstream_task_ids", {}).get("asset_generate_image"))
                    if (generation is None or generation.task_type != TaskType.ASSET_GENERATE_IMAGE
                            or source.input_data.get("upstream_task_ids") != {
                                "visual_plan": previous.id, "asset_generate_image": generation.id}
                            or generation.input_data.get("upstream_task_ids") != {"visual_plan": previous.id}):
                        raise TaskConflictError("VISUAL_REPAIR_ORIGINAL_LINEAGE_CHANGED")
                    script = session.get(AgentTask, previous.input_data.get("upstream_task_ids", {}).get("script"))
                    if (script is None or script.task_type != TaskType.SHORT_SCRIPT or script.status != TaskStatus.COMPLETED
                            or bad.input_data.get("upstream_task_ids") != previous.input_data.get("upstream_task_ids")
                            or qc.input_data.get("upstream_task_ids", {}).get("script") != script.id):
                        raise TaskConflictError("VISUAL_REPAIR_SCRIPT_LINEAGE_CHANGED")
                    _require_current_revision(session, script)
                    for task in (recovery, bad, previous, source, generation, script):
                        self._visual_repair_history(session, task, content_id)
                    prepared, _ = _ensure_image_only_asset_requests(
                        content=content, settings=self.settings, plan_task=bad, script=script.result_data,
                    )
                    expected_qc_input = {
                        "upstream_task_ids": {"visual_plan": bad.id, "script": script.id},
                        "scenes": bad.input_data.get("scenes", []),
                        "voice_duration_sec": bad.input_data.get("voice_duration_sec"),
                        "allowed_layouts": bad.input_data.get("allowed_layouts", []),
                        "allowed_asset_ids": bad.input_data.get("allowed_asset_ids", []),
                        "context_files": bad.input_data.get("context_files", []),
                    }
                    if prepared != bad.result_data:
                        expected_qc_input.update(prepared_visual_plan=prepared,
                                                 visual_plan_result_sha256=stable_hash(bad.result_data))
                    expected_qc_input = flow._visual_qc_generation_policy(content_id, expected_qc_input)
                    expected_qc_input.pop("opening_identity_overlay", None)
                    overlay = flow.opening_identity_input(content_id, TaskType.VISUAL_PLAN_QC, expected_qc_input)
                    if overlay:
                        expected_qc_input["opening_identity_overlay"] = overlay
                    expected_qc_input, _ = _normalize_content_task_input(
                        session, TaskType.VISUAL_PLAN_QC, content_id, expected_qc_input,
                    )
                    if qc.input_data != expected_qc_input:
                        raise TaskConflictError("VISUAL_REPAIR_QC_NOT_EXACT_DERIVED_INPUT")
                    corrected = flow.visual_repair_input(previous, recovery, plan, source=source)
                    segments = previous.result_data.get("segments", [])
                    indexes = _failed_segment_indexes(envelope.validation_errors, len(segments))
                    assets = source.input_data.get("assets", [])
                    materials = [generated_image_material(session, content, row["artifact_id"]) for row in assets]
                    good_ids = {row["artifact_id"] for row in assets if row.get("segment_index") not in (indexes or set())}
                    failed_ids = {row["artifact_id"] for row in assets if row.get("segment_index") in (indexes or set())}
                    if (indexes is None or len(indexes) != 1 or len(segments) < 2 or len(assets) != len(segments)
                            or {row.get("segment_index") for row in assets} != set(range(len(segments)))
                            or any(type(row.get("segment_index")) is not int for row in assets)
                            or any(material["generation_task_id"] != generation.id
                                   or material["generation_segment_index"] != row["segment_index"]
                                   or material["request"] != row.get("request")
                                   or material["generation"] != row.get("generation")
                                   for material, row in zip(materials, assets, strict=True))
                            or corrected["reusable_assets_evidence_gaps"] or not corrected["original_reuse"]
                            or len(good_ids) != len(segments) - 1 or len(failed_ids) != 1
                            or {row["artifact_id"] for row in corrected["reusable_generated_assets"]} != good_ids
                            or set(corrected["allowed_asset_ids"]) != good_ids
                            or not failed_ids.issubset(corrected["excluded_generated_asset_ids"])):
                        raise TaskConflictError("VISUAL_REPAIR_EXACT_REUSE_NOT_PROVEN")
                    legacy = deepcopy(corrected)
                    legacy.update(reusable_generated_assets=[], original_reuse=False,
                                  reusable_assets_evidence_gaps=["reuse: failed segment has no exact index"],
                                  excluded_generated_asset_ids=sorted(
                                      item for item in previous.input_data.get("excluded_generated_asset_ids", [])
                                      if isinstance(item, str)),
                                  allowed_asset_ids=[], context_files=list(dict.fromkeys(previous.input_data.get("context_files", []))))
                    for field in ("qc_issues", "previous_qc_task_id"):
                        if field in previous.input_data:
                            legacy[field] = deepcopy(previous.input_data[field])
                        else:
                            legacy.pop(field, None)
                    if (legacy != bad.input_data or bad.input_data.get("previous_qc_task_id") == source.id
                            or bad.input_data.get("qc_issues") == envelope.validation_errors):
                        raise TaskConflictError("VISUAL_REPAIR_NOT_KNOWN_CONTEXT_DEFECT")
                    corrected.update(supersedes_task_id=bad.id, visual_repair_context_version=1)
                    replacement = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, corrected,
                                                         priority=52, allow_multiple=True)
                    session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                           entity_id=replacement.id, action=OWNED,
                                           metadata_json={"input_hash": replacement.input_hash}))
                    flow.tasks.cancel(qc.id, actor_id="visual-repair-context-reconciliation", actor_type="system")
                    bad.status, bad.dedup_key = TaskStatus.CANCELLED, None
                    session.add(bad)
                    session.add(AuditEvent(actor_type="system", actor_id="visual-repair-context-reconciliation",
                                           entity_type="agent_task", entity_id=bad.id, action="cancelled",
                                           metadata_json={"superseded_by": replacement.id, "version": 1}))
                    content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "visual_plan"
                    content.error_code, content.error_message = "VISUAL_REPAIR_CONTEXT_RECONCILED", "已从原始证据重建视觉修复上下文"
                    content.updated_at = utc_now()
                    session.add(content)
                    response = {"reconciled": True, "replayed": False, "visual_plan_task_id": replacement.id,
                                "superseded_visual_plan_task_id": bad.id, "cancelled_qc_task_id": qc.id}
                    session.add(AuditEvent(actor_type="admin", actor_id="mpt-admin-api", entity_type="content",
                                           entity_id=content_id, action=VISUAL_REPAIR_CONTEXT, metadata_json={
                                               "key": key, "version": 1, "expected": deepcopy(expected),
                                               "replacement_task_id": replacement.id, "replacement_input_hash": replacement.input_hash,
                                               "source_task_id": source.id, "source_input_hash": source.input_hash,
                                               "source_result_hash": stable_hash(source.result_data),
                                               "original_plan_id": previous.id, "original_plan_input_hash": previous.input_hash,
                                               "original_plan_result_hash": stable_hash(previous.result_data),
                                               "generation_id": generation.id, "generation_input_hash": generation.input_hash,
                                               "generation_result_hash": stable_hash(generation.result_data),
                                               "response": response,
                                           }))
                    Session.commit(session)
                    return response
                finally:
                    session.rollback()

    def reconcile_visual_plan_review_context(self, content_id: str, expected: dict) -> dict:
        """Issue one fresh review after a proved pre-review self-approval defect; never pass QC."""
        from datetime import datetime
        from glance_core.content_acceptance import _unchanged_material_task, completed_image_origin, source_context, visual_review_source_context
        from glance_core.models import Candidate, ProductionMode, PublicationRecord, RightsStatus
        from glance_core.task_schemas import RecoveryFailureEnvelope, RecoveryPlan
        from app.business.bundles import sha256_file
        from app.business.content_host import CLAIM, child_work_unresolved, host_live, offer_receipt
        from app.business.content_run import load_run

        with content_operation_lock(self.settings, content_id):
            with _ConsumptionSession(self.engine) as session:
                begin_immediate(session)
                try:
                    assert_allowed(session, content_id=content_id)
                    content = session.get(Content, content_id)
                    if content is None:
                        raise KeyError(content_id)
                    if (expected.get("version") != 1
                            or expected.get("evidence_mode") != "accepted_failure_plus_deployed_derivation_v1"):
                        raise TaskConflictError("VISUAL_REVIEW_CONTEXT_VERSION_CHANGED")
                    bound = {}
                    for name, kind in (("plan", TaskType.VISUAL_PLAN), ("qc", TaskType.VISUAL_PLAN_QC),
                                       ("recovery", TaskType.RECOVERY)):
                        binding = expected[name]
                        task = session.get(AgentTask, binding["task_id"])
                        if (task is None or task.content_id != content_id or task.task_type != kind
                                or task.input_hash != binding["input_hash"]
                                or stable_hash(task.input_data) != task.input_hash
                                or stable_hash(task.result_data) != binding["result_hash"]
                                or task.attempt != binding["attempt"]):
                            raise TaskConflictError("VISUAL_REVIEW_EXPECTED_BINDING_CHANGED")
                        self._require_accepted_native_task(session, task, consumed=True)
                        raw = self.settings.agent_submission_dir / task.id / str(task.attempt) / "result.json"
                        if sha256_file(raw) != binding["raw_file_sha256"]:
                            raise TaskConflictError("VISUAL_REVIEW_ACCEPTED_RAW_CHANGED")
                        bound[name] = task
                    plan, qc, recovery = (bound[name] for name in ("plan", "qc", "recovery"))
                    key = stable_hash({"content_id": content_id, "qc_id": qc.id, "recovery_id": recovery.id, "version": 1})
                    receipts = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "content", AuditEvent.entity_id == content_id,
                        AuditEvent.action == VISUAL_REVIEW_CONTEXT,
                    )).all()
                    matches = [row for row in receipts if row.metadata_json.get("key") == key]
                    if matches:
                        if len(matches) != 1:
                            raise TaskConflictError("VISUAL_REVIEW_REPLAY_AMBIGUOUS")
                        receipt = matches[0]
                        data = receipt.metadata_json
                        replacement = session.get(AgentTask, data.get("replacement_task_id"))
                        if (receipt.actor_type != "admin" or receipt.actor_id != "mpt-admin-api"
                                or data.get("expected") != expected or replacement is None
                                or replacement.content_id != content_id or replacement.task_type != TaskType.VISUAL_PLAN_QC
                                or replacement.input_hash != data.get("replacement_input_hash")
                                or stable_hash(replacement.input_data) != replacement.input_hash
                                or replacement.input_data.get("review_context_compensation") != data.get("compensation")):
                            raise TaskConflictError("VISUAL_REVIEW_REPLAY_CHANGED")
                        _require_owned_input(session, replacement)
                        return {**data["response"], "replayed": True}
                    run = load_run(session)
                    if (run is None or run.content_id != content_id
                            or {field: getattr(run, field) for field in expected["run"]} != expected["run"]
                            or run.state != "stopped" or not run.stop_requested
                            or host_live(run) or child_work_unresolved(run)):
                        raise TaskConflictError("VISUAL_REVIEW_RUN_NOT_DRAINED_OR_CHANGED")
                    if (content.production_mode != ProductionMode.IMAGE_ONLY
                            or (content.business_status, content.current_stage) != (ContentStatus.FAILED, "recovery_blocked")
                            or content.error_code != "RECOVERY_BLOCKED"
                            or content.error_message != "角色路由动作与失败分类不匹配"):
                        raise TaskConflictError("VISUAL_REVIEW_CONTENT_NOT_CURRENT")
                    _require_idle_bundle_recovery(session, content_id)
                    tasks = session.exec(select(AgentTask).where(current_revision_clause(AgentTask), AgentTask.content_id == content_id)).all()
                    for task in (plan, qc, recovery):
                        _require_current_revision(session, task)
                    if (any("visual_review_context_version" in task.input_data for task in (qc, plan))
                            or "source_context_version" in qc.input_data or qc.schema_version != "1"
                            or qc.input_data.get("upstream_task_ids") != {"visual_plan": plan.id,
                                "script": plan.input_data.get("upstream_task_ids", {}).get("script")}):
                        raise TaskConflictError("VISUAL_REVIEW_NOT_LEGACY_PRE_REVIEW")
                    if (session.exec(select(AuditEvent.id).where(
                            AuditEvent.entity_type == "publish_intent",
                            AuditEvent.metadata_json["content_id"].as_string() == content_id,
                        )).first() or session.exec(select(PublicationRecord.id).where(
                            current_revision_clause(PublicationRecord), PublicationRecord.content_id == content_id)).first()):
                        raise TaskConflictError("VISUAL_REVIEW_PUBLISH_HISTORY_PRESENT")
                    for task in tasks:
                        refs = task.input_data.get("upstream_task_ids", {})
                        if task.id not in {qc.id, recovery.id} and (refs.get("visual_plan") == plan.id
                                or task.input_data.get("supersedes_qc_id") == qc.id):
                            raise TaskConflictError("VISUAL_REVIEW_DESCENDANT_PRESENT")
                    claims = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == qc.id,
                        AuditEvent.action == CLAIM,
                    )).all()
                    served = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == qc.id,
                        AuditEvent.action == "bundle_served",
                    )).all()
                    if len(claims) != 1 or len(served) != 1:
                        raise TaskConflictError("VISUAL_REVIEW_NATIVE_DELIVERY_UNPROVEN")
                    claim, delivery = claims[0], served[0]
                    claim_data = claim.metadata_json
                    worker, offer_hash = offer_receipt(session, claim_data["request_id"])
                    offer_event = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "scheduler_offer", AuditEvent.entity_id == claim_data["request_id"],
                        AuditEvent.action == "mpt_content_run_offer",
                    )).one()
                    intent = run.intents.get(claim_data["request_id"], {})
                    if (claim.actor_type != "system" or claim.actor_id != "mpt-content-host"
                            or delivery.actor_type != "agent" or delivery.actor_id != qc.assigned_agent_id
                            or delivery.metadata_json != {"served_prompt_hash": stable_hash(qc.instructions),
                                                          "schema_version": qc.schema_version}
                            or qc.started_at is None or qc.completed_at is None
                            or not as_utc(qc.started_at) <= as_utc(claim.created_at) <= as_utc(delivery.created_at) <= as_utc(qc.completed_at)
                            or claim_data.get("task_id") != qc.id or claim_data.get("agent_id") != qc.assigned_agent_id
                            or claim_data.get("lease_token_hash") != qc.lease_token_hash
                            or claim_data.get("started_at") != as_utc(qc.started_at).isoformat()
                            or claim_data.get("run_id") != run.run_id or claim_data.get("generation") != run.generation
                            or claim_data.get("host_epoch") != run.host_epoch
                            or claim_data.get("parent_thread_id") != run.parent_thread_id
                            or worker.get("task_id") != qc.id or worker.get("agent_id") != qc.assigned_agent_id
                            or worker.get("input_hash") != qc.input_hash or worker.get("content_id") != content_id
                            or worker.get("task_type") != qc.task_type.value or worker.get("role_id") != "visual"
                            or offer_event.actor_type != "scheduler" or offer_event.actor_id != worker.get("scheduler_id")
                            or worker.get("run_id") != run.run_id or worker.get("run_generation") != run.generation
                            or worker.get("host_epoch") != run.host_epoch or claim_data.get("offer_hash") != offer_hash
                            or intent.get("state") != "TERMINAL" or intent.get("claim_id") != claim_data.get("claim_id")
                            or intent.get("offer_hash") != offer_hash
                            or (intent.get("child") or {}).get("child_id") != claim_data.get("child_id")):
                        raise TaskConflictError("VISUAL_REVIEW_NATIVE_DELIVERY_CHANGED")
                    deployment = expected["historical_deployment"]
                    relative = Path(deployment["relative_path"])
                    proof = (self.workspace_root / relative).resolve()
                    smoke = (self.workspace_root / "storage" / "smoke").resolve()
                    if relative.is_absolute() or ".." in relative.parts or not proof.is_relative_to(smoke) or sha256_file(proof) != deployment["sha256"]:
                        raise TaskConflictError("VISUAL_REVIEW_DEPLOYMENT_EVIDENCE_CHANGED")
                    historical = json.loads(proof.read_text(encoding="utf-8"))
                    source_sha = historical.get("source_sha256", {}).get("packages/core/src/glance_core/content_acceptance.py")
                    if (historical.get("phase") != "after" or historical.get("head") != deployment["revision"]
                            or historical.get("target_commit") != deployment["revision"]
                            or historical.get("database_media_and_negatives_unchanged") is not True
                            or source_sha != VISUAL_REVIEW_LEGACY_SOURCE
                            or datetime.fromisoformat(historical["at"]).tzinfo is None
                            or as_utc(datetime.fromisoformat(historical["at"])) >= as_utc(delivery.created_at)):
                        raise TaskConflictError("VISUAL_REVIEW_DEPLOYED_DERIVATION_UNPROVEN")
                    refs = qc.input_data["upstream_task_ids"]
                    legacy = source_context(session, content_id, refs)
                    if (legacy.get("evidence_gaps") != ["Current VISUAL_PLAN_QC must be completed and passed"]
                            or (qc.result_data or {}).get("passed") is not False):
                        raise TaskConflictError("VISUAL_REVIEW_NOT_KNOWN_CIRCULAR_DEFECT")
                    facts = visual_review_source_context(session, content_id, refs, review_task=qc, include_references=True)
                    segments = facts.get("generation_provenance", {}).get("segments", [])
                    selected = qc.input_data.get("prepared_visual_plan", plan.result_data).get("segments", [])
                    asset_ids = [segment.get("asset_id") for segment in selected]
                    errors = [issue for issue in qc.result_data.get("issues", []) if issue.get("severity") == "error"]
                    message = errors[0].get("message", "") if len(errors) == 1 else ""
                    positions = [message.find(asset_id) for asset_id in asset_ids if isinstance(asset_id, str)]
                    if (len(asset_ids) != 7 or len(set(asset_ids)) != 7 or len(segments) != 7
                            or any(segment.get("asset_request") or segment.get("scene_id") for segment in selected)
                            or [row.get("artifact_id") for row in segments] != asset_ids
                            or [row.get("segment_index") for row in segments] != list(range(7))
                            or facts.get("scope") != "visual_plan_pre_review" or facts.get("evidence_gaps")
                            or facts.get("generation_intents")
                            or facts.get("generation_provenance", {}).get("license_assessed") is not False
                            or facts.get("generation_provenance", {}).get("composition_qc_assessed") is not False
                            or stable_hash(facts) != expected["corrected_context_sha256"]
                            or len(errors) != 1 or errors[0].get("field") != "rights"
                            or "source_context_read_only.evidence_gaps" not in message
                            or "visual material accepted/consumed proof missing" not in message
                            or len(positions) != 7 or any(pos < 0 for pos in positions) or positions != sorted(positions)
                            or any(issue.get("severity") not in {"error", "warning"} for issue in qc.result_data.get("issues", []))):
                        raise TaskConflictError("VISUAL_REVIEW_EXACT_PREEXISTING_FACTS_UNPROVEN")
                    candidate = session.get(Candidate, content.candidate_id)
                    if (candidate is None or candidate.rights_status == RightsStatus.REJECTED
                            or as_utc(candidate.updated_at) > as_utc(delivery.created_at)):
                        raise TaskConflictError("VISUAL_REVIEW_REGISTRATION_CHANGED")
                    voice_data = plan.input_data.get("voice_artifact", {})
                    voice = session.get(Artifact, voice_data.get("id"))
                    latest_voice = session.exec(select(Artifact).where(
                        current_revision_clause(Artifact), Artifact.content_id == content_id, Artifact.artifact_type == "voice",
                    ).order_by(Artifact.created_at.desc(), Artifact.id.desc())).first()
                    root = Path(content.content_dir).resolve()
                    if (voice is None or voice.content_id != content_id or voice.artifact_type != "voice"
                            or voice != latest_voice
                            or {key: getattr(voice, key) for key in ("id", "relative_path", "sha256", "size_bytes")} != voice_data):
                        raise TaskConflictError("VISUAL_REVIEW_VOICE_CHANGED")
                    proof_tasks = {plan.id, refs["script"], facts["research_task_id"]}
                    script_qc = self.workflow_service(session).completed_task_for(content_id, TaskType.SCRIPT_QC)
                    proof_tasks.add(script_qc.id)
                    for row in segments:
                        proof_tasks.update((row["generation_task_id"], row["generation_plan_id"]))
                        proof_tasks.update(item["task_id"] for item in row.get("prior_asset_qc", []))
                    for task_id in proof_tasks:
                        task = session.get(AgentTask, task_id)
                        if task is None or task.content_id != content_id or not completed_image_origin(session, task):
                            raise TaskConflictError("VISUAL_REVIEW_HISTORY_NOT_ACCEPTED")
                        _require_owned_input(session, task)
                        _unchanged_material_task(session, task, owned=True, consumed=True)
                        accepted = session.exec(select(AgentSubmission).where(
                            AgentSubmission.task_id == task.id, AgentSubmission.attempt == task.attempt,
                            AgentSubmission.validation_status == "ACCEPTED",
                        )).one()
                        raw = (self.settings.agent_submission_dir / task.id / str(task.attempt) / "result.json").resolve()
                        cancellations = session.exec(select(AuditEvent).where(
                            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
                            AuditEvent.action == "cancelled",
                        )).all()
                        if (not raw.is_relative_to(self.settings.agent_submission_dir.resolve())
                                or Path(accepted.raw_result_path).resolve() != raw
                                or stable_hash(json.loads(raw.read_text(encoding="utf-8"))) != accepted.result_hash
                                or any(row.actor_type != "system" or row.actor_id not in {"voice_redub", "recovery-visual-repair"}
                                       or row.metadata_json != {"reason": row.actor_id} for row in cancellations)):
                            raise TaskConflictError("VISUAL_REVIEW_HISTORY_CHANGED")
                        events = session.exec(select(AuditEvent).where(
                            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task_id,
                            AuditEvent.action.in_(["completed", AGENT_CONSUMED, OWNED]),
                        )).all()
                        submissions = session.exec(select(AgentSubmission).where(
                            AgentSubmission.task_id == task_id, AgentSubmission.attempt == task.attempt,
                            AgentSubmission.validation_status == "ACCEPTED",
                        )).all()
                        if (task.completed_at is None or as_utc(task.created_at) > as_utc(delivery.created_at)
                                or as_utc(task.completed_at) > as_utc(delivery.created_at)
                                or any(as_utc(event.created_at) > as_utc(delivery.created_at) for event in events)
                                or any(as_utc(sub.created_at) > as_utc(delivery.created_at) for sub in submissions)):
                            raise TaskConflictError("VISUAL_REVIEW_FACTS_POSTDATE_DELIVERY")
                    for artifact_id in [*asset_ids, voice.id]:
                        artifact = session.get(Artifact, artifact_id)
                        path = (root / artifact.relative_path).resolve()
                        if (as_utc(artifact.created_at) > as_utc(delivery.created_at)
                                or not path.is_relative_to(root) or not path.is_file()
                                or path.stat().st_size != artifact.size_bytes or sha256_file(path) != artifact.sha256):
                            raise TaskConflictError("VISUAL_REVIEW_ARTIFACT_CHANGED_OR_LATE")
                    envelope = RecoveryFailureEnvelope.model_validate(recovery.input_data.get("failure"))
                    recovery_plan = RecoveryPlan.model_validate(recovery.result_data)
                    lineage = recovery.input_data.get("lineage") or {}
                    policy = recovery.input_data.get("policy") or {}
                    flow = self.workflow_service(session)
                    signature = flow._root_cause_signature("BUSINESS_QC", "VISUAL_PLAN_QC_FAILED")
                    current = effective_settings(session, self.settings)
                    if (envelope.source_task_id != qc.id or envelope.source_task_type != qc.task_type.value
                            or envelope.source_attempt != qc.attempt or envelope.failure_class != "BUSINESS_QC"
                            or envelope.code != "VISUAL_PLAN_QC_FAILED" or envelope.retryable
                            or envelope.validation_errors != qc.result_data["issues"]
                            or recovery_plan.failure_class != "BUSINESS_QC" or recovery_plan.action != "reroute_to_role"
                            or recovery_plan.target_task_type != "content_research"
                            or lineage.get("parent_task_id") != qc.id or lineage.get("repair_target_task_id") != plan.id
                            or lineage.get("content_id") != content_id or lineage.get("source_attempt") != qc.attempt
                            or lineage.get("root_task_id") != qc.input_data.get("lineage", {}).get("root_task_id", qc.id)
                            or lineage.get("root_cause_signature") != signature
                            or lineage.get("repair_fingerprint") != flow._repair_fingerprint(qc, failure_class="BUSINESS_QC", code=envelope.code)
                            or type(lineage.get("round")) is not int or lineage["round"] != 1
                            or sum(item["root_cause_signature"] == signature for item in flow._recovery_history(content_id)) != 1
                            or not current.recovery_enabled or policy.get("enabled") is not True
                            or any(type(limit) is not int or limit <= lineage["round"] for limit in
                                   (lineage.get("max_rounds"), policy.get("max_rounds"), current.recovery_max_rounds))
                            or any(task.input_data.get("recovery_task_id") == recovery.id for task in tasks)):
                        raise TaskConflictError("VISUAL_REVIEW_ORIGINAL_RECOVERY_CHANGED")
                    blocked = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
                        AuditEvent.action.in_(["recovery_blocked", "recovery_escalated"]),
                    )).all()
                    origins = session.exec(select(AuditEvent).where(
                        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == recovery.id,
                        AuditEvent.action == "recovery_task_created",
                    )).all()
                    if (len(blocked) != 1 or blocked[0].actor_type != "system" or blocked[0].actor_id != "recovery-coordinator"
                            or blocked[0].action != "recovery_escalated"
                            or blocked[0].metadata_json != {"reason": "角色路由动作与失败分类不匹配", "action": "reroute_to_role"}
                            or len(origins) != 1 or origins[0].actor_type != "system"
                            or origins[0].metadata_json != {"source_task_id": qc.id, "failure_class": "BUSINESS_QC",
                                "code": envelope.code, "root_cause_signature": signature, "round": 1, "target_task_id": plan.id}):
                        raise TaskConflictError("VISUAL_REVIEW_RECOVERY_RECEIPT_CHANGED")
                    compensation = {"version": 1, "supersedes_qc_task_id": qc.id,
                        "previous_qc_input_hash": qc.input_hash, "previous_qc_result_hash": stable_hash(qc.result_data),
                        "blocked_recovery_task_id": recovery.id, "blocked_recovery_input_hash": recovery.input_hash,
                        "blocked_recovery_result_hash": stable_hash(recovery.result_data),
                        "corrected_context_hash": stable_hash(facts)}
                    inputs = {**deepcopy(qc.input_data), "supersedes_qc_id": qc.id, "visual_review_context_version": 1,
                        "original_recovery_task_id": recovery.id, "original_recovery_lineage": deepcopy(lineage),
                        "original_recovery_policy": deepcopy(policy), "lineage": {"root_task_id": lineage["root_task_id"]},
                        "review_context_compensation": compensation, "visual_review_source_context": facts,
                        "visual_review_source_context_sha256": stable_hash(facts)}
                    replacement = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN_QC, inputs, priority=55, allow_multiple=True)
                    session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                           entity_id=replacement.id, action=OWNED, metadata_json={"input_hash": replacement.input_hash}))
                    content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "visual_plan_qc"
                    content.error_code, content.error_message = "VISUAL_REVIEW_CONTEXT_RECONCILED", "已补充可核验的历史生成来源，等待新的独立视觉计划质检"
                    content.updated_at = utc_now()
                    session.add(content)
                    response = {"reconciled": True, "replayed": False, "visual_plan_qc_task_id": replacement.id,
                                "superseded_qc_task_id": qc.id}
                    session.add(AuditEvent(actor_type="admin", actor_id="mpt-admin-api", entity_type="content", entity_id=content_id,
                        action=VISUAL_REVIEW_CONTEXT, metadata_json={"key": key, "expected": deepcopy(expected),
                            "replacement_task_id": replacement.id, "replacement_input_hash": replacement.input_hash,
                            "compensation": compensation, "evidence_mode": expected["evidence_mode"],
                            "original_bundle_retained": False, "delivery_body_verified": False,
                            "historical_deployment_scope": "operator_deployment_evidence",
                            "historical_source_sha256": source_sha, "historical_delivery_event_id": delivery.id,
                            "historical_claim_event_id": claim.id, "corrected_context_hash": stable_hash(facts),
                            "asset_selection_hash": stable_hash(asset_ids), "voice_artifact": deepcopy(voice_data),
                            "original_recovery_lineage": deepcopy(lineage), "original_recovery_policy": deepcopy(policy),
                            "response": response}))
                    Session.commit(session)
                    return response
                finally:
                    session.rollback()

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
                        current_revision_clause(AgentTask), AgentTask.content_id == content_id,
                        AgentTask.status.in_([TaskStatus.LEASED, TaskStatus.SUBMITTED]),
                    )).first() or session.exec(select(SchedulerOffer.request_id).where(
                        SchedulerOffer.task_id.in_(select(AgentTask.id).where(current_revision_clause(AgentTask), AgentTask.content_id == content_id)),
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
                        current_revision_clause(AgentTask), AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_QC,
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
                        current_revision_clause(AgentTask), AgentTask.content_id == content_id,
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
                        current_revision_clause(Artifact), Artifact.content_id == content_id, Artifact.artifact_type == "acceptance_input",
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
                    audio_path = MptMediaExecutor(root).final_audio_evidence_path(audio)
                    if not audio_path.is_file():
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
                    artifact = Artifact(content_id=content_id, revision_id=content.current_revision_id,
                                        artifact_type="acceptance_input", relative_path="",
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
