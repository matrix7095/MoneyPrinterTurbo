"""One explicit authority change resumes the original image attempt, never its budget."""

import hashlib
import json
from datetime import datetime

from glance_core.models import AgentTask, AuditEvent, ContentStatus, TaskStatus, TaskType, as_utc, utc_now
from glance_core.runtime_config import effective_settings
from glance_core.task_service import TaskConflictError, stable_hash, task_dedup_key
from sqlmodel import select

RESUMED = "mpt_image_authorization_changed"
ERROR = "LOCAL_IMAGE_AUTHORIZATION_MISSING"


def _reject():
    raise TaskConflictError("CONTENT_IMAGE_AUTHORIZATION_RESUME_UNVERIFIED")


def _events(session, entity_id, action):
    return session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == entity_id, AuditEvent.action == action,
    )).all()


def _one(session, entity_id, action):
    rows = _events(session, entity_id, action)
    if len(rows) != 1:
        _reject()
    return rows[0]


def _journal(runtime, old, child_id):
    """Count actual native starts as billable work, including failed/unfinished calls."""
    directory = runtime.settings.data_dir / "private" / "content-runs" / old.run_id
    state_path, events_path = directory / "state.json", directory / "events.jsonl"
    if any(path != path.resolve() for path in (directory, state_path, events_path)):
        _reject()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    fields = ("run_id", "content_id", "generation", "host_epoch", "parent_thread_id", "process_identity")
    if (any(state.get(key) != getattr(old, key) for key in fields)
            or state.get("phase") not in {"CONDUCTOR_STOP_REQUESTED", "CONDUCTOR_RUN_EXPIRED"}):
        _reject()
    started, completed = set(), set()
    digest = hashlib.sha256()
    previous_seq = 0
    with events_path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            event = json.loads(raw)
            if type(event.get("seq")) is not int or event["seq"] <= previous_seq:
                _reject()
            previous_seq = event["seq"]
            if event.get("kind") != "native" or event.get("threadId") != child_id:
                continue
            item = event.get("item") or {}
            if item.get("type") != "imageGeneration":
                continue
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier:
                _reject()
            if event.get("method") == "item/started":
                if identifier in started:
                    _reject()
                started.add(identifier)
            elif event.get("method") == "item/completed":
                if identifier not in started or identifier in completed or item.get("status") != "completed":
                    _reject()
                completed.add(identifier)
    if not started or started != completed:
        _reject()
    return {"native_generations": len(started), "events_sha256": digest.hexdigest(),
            "state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
            "child_id": child_id, "item_ids": sorted(started)}


def _claim(session, old, source):
    from app.business.content_host import CLAIM, offer_receipt, validate_child
    from app.business.requests import NativeChild

    matches = []
    for request_id, intent in old.intents.items():
        worker, offer_hash = offer_receipt(session, request_id)
        if worker.get("task_id") != source.id:
            continue
        child = intent.get("child")
        if (intent.get("state") != "TERMINAL" or not child
                or intent.get("host_epoch") != old.host_epoch
                or worker.get("run_id") != old.run_id or worker.get("run_generation") != old.generation
                or worker.get("content_id") != old.content_id or worker.get("input_hash") != source.input_hash
                or worker.get("task_type") != TaskType.ASSET_GENERATE_IMAGE.value
                or worker.get("host_epoch") != old.host_epoch or worker.get("agent_id") != source.assigned_agent_id
                or intent.get("offer_hash") != offer_hash):
            _reject()
        validate_child(old, worker, NativeChild.model_validate(child), terminal=True)
        claims = [row for row in _events(session, source.id, CLAIM)
                  if row.metadata_json.get("claim_id") == intent.get("claim_id")]
        if len(claims) != 1:
            _reject()
        claim = claims[0]
        data = claim.metadata_json
        expected = {"run_id": old.run_id, "generation": old.generation, "host_epoch": old.host_epoch,
                    "request_id": request_id, "offer_hash": offer_hash, "task_id": source.id,
                    "agent_id": source.assigned_agent_id, "child_id": child["child_id"],
                    "parent_thread_id": old.parent_thread_id,
                    "started_at": as_utc(source.started_at).isoformat() if source.started_at else None}
        if (claim.actor_type != "system" or claim.actor_id != "mpt-content-host"
                or claim.entity_type != "agent_task" or any(data.get(key) != value for key, value in expected.items())):
            _reject()
        confirmed = [row for row in _events(session, source.id, "claim_confirmed")
                     if row.metadata_json.get("lease_token_hash") == data.get("lease_token_hash")]
        if (len(confirmed) != 1 or confirmed[0].actor_type != "agent"
                or confirmed[0].actor_id != source.assigned_agent_id
                or confirmed[0].metadata_json.get("attempt") != source.attempt
                or confirmed[0].metadata_json.get("attempt_started_at") != (
                    as_utc(source.attempt_started_at).isoformat() if source.attempt_started_at else None)):
            _reject()
        matches.append((child["child_id"], claim.id, confirmed[0].id))
    if len(matches) != 1:
        _reject()
    return matches[0]


def prepare_resume(runtime, session, content, old, execution, permission):
    """Validate without writes; caller owns the SQLite write transaction and commit."""
    from app.business.content_host import child_work_unresolved, host_live
    from app.business.pipeline import _has_manual_script_approval, _require_script_assist, _verify_rights
    from app.business.runtime import (
        FAILURE_RECOVERY, _failure_hash, _require_current_revision, _require_idle_bundle_recovery,
    )
    from glance_core.content_acceptance import _visual_parent_chain, _unchanged_material_task, is_handsfree_content

    if (permission is not True or runtime is None or execution is None or old is None
            or old.content_id != content.id or old.allow_local_image_adaptation is not False
            or old.state not in {"stopped", "expired"} or not old.stop_requested
            or host_live(old) or child_work_unresolved(old)
            or execution.content_id != content.id or execution.run_id != old.run_id
            or execution.generation != old.generation or execution.state != "FINISHED"
            or execution.exit_code != 0 or not execution.exited_at
            or execution.allow_local_image_adaptation is not False
            or content.business_status != ContentStatus.FAILED or content.current_stage != "recovery_blocked"):
        _reject()
    if as_utc(datetime.fromisoformat(execution.exited_at)) > utc_now():
        _reject()
    _require_idle_bundle_recovery(session, content.id)
    sources = session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
    )).all()
    if len(sources) != 1 or sources[0].status != TaskStatus.FAILED:
        _reject()
    source = sources[0]
    source, retryable = runtime._failure_source(session, source.id)
    if (source.error_code != ERROR or retryable or source.attempt != 1 or source.attempt >= source.max_attempts
            or source.lease_token_hash is not None or source.lease_expires_at is not None
            or _events(session, source.id, RESUMED)):
        _reject()
    child_id, claim_id, confirmed_id = _claim(session, old, source)
    failure = sorted(_events(session, source.id, "failed"), key=lambda row: (as_utc(row.created_at), row.id))[-1]
    confirmed = session.get(AuditEvent, confirmed_id)
    if as_utc(failure.created_at) < as_utc(confirmed.created_at):
        _reject()
    flow = runtime.workflow_service(session)
    plan = runtime._failure_visual_target(session, flow, source)
    _visual_parent_chain(session, plan, owned=True)
    parents = [plan]
    for kind in (TaskType.CONTENT_RESEARCH, TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC, TaskType.VISUAL_PLAN_QC):
        parent = flow.completed_task_for(content.id, kind)
        if parent is None:
            _reject()
        runtime._require_accepted_native_task(session, parent, consumed=True)
        _require_current_revision(session, parent)
        parents.append(parent)
    runtime._require_accepted_native_task(session, plan, consumed=True)
    qc = parents[-1]
    if qc.result_data.get("passed") is not True or qc.input_data.get("upstream_task_ids", {}).get("visual_plan") != plan.id:
        _reject()
    _verify_rights(session, content)
    if is_handsfree_content(session, content.id):
        _require_script_assist(session, runtime.settings, content.id, automatic=True, allow_manual_approval=True)
    elif not _has_manual_script_approval(session, runtime.settings, content.id):
        _reject()
    recoveries = session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.RECOVERY,
    )).all()
    matches = [row for row in recoveries if row.input_data.get("failure", {}).get("source_task_id") == source.id
               and row.input_data.get("failure", {}).get("source_attempt") == source.attempt]
    if len(matches) != 1:
        _reject()
    recovery = matches[0]
    runtime._require_accepted_native_task(session, recovery, consumed=True)
    _unchanged_material_task(session, recovery, owned=True, consumed=True)
    envelope, lineage, policy = (recovery.input_data.get(key) or {} for key in ("failure", "lineage", "policy"))
    signature = flow._root_cause_signature(envelope.get("failure_class"), ERROR)
    related = [row for row in recoveries if row.input_data.get("lineage", {}).get("root_cause_signature") == signature]
    rounds, maximum = lineage.get("round"), policy.get("max_rounds")
    if (recovery.status != TaskStatus.COMPLETED or recovery.result_data.get("action") != "mark_recovery_blocked"
            or envelope.get("source_task_id") != source.id or envelope.get("source_task_type") != source.task_type.value
            or envelope.get("source_attempt") != source.attempt or envelope.get("code") != ERROR
            or envelope.get("message") != source.error_message or envelope.get("retryable") is not False
            or lineage.get("content_id") != content.id or lineage.get("parent_task_id") != source.id
            or lineage.get("repair_target_task_id") != source.id or lineage.get("source_attempt") != source.attempt
            or lineage.get("repair_fingerprint") != flow._repair_fingerprint(source, failure_class=envelope.get("failure_class"), code=ERROR)
            or lineage.get("root_cause_signature") != signature
            or type(rounds) is not int or type(maximum) is not int or not 0 < rounds < maximum
            or rounds != len(related) or rounds >= flow.recovery_max_rounds
            or any(row.input_data.get("lineage", {}).get("round", 0) > rounds for row in related)):
        _reject()
    origin = _one(session, recovery.id, FAILURE_RECOVERY)
    if (origin.actor_type != "system" or origin.actor_id != "mpt-agent-failure"
            or origin.entity_type != "agent_task"
            or origin.metadata_json != runtime._failure_recovery_metadata(source, recovery, targets=(plan, flow.completed_task_for(content.id, TaskType.SHORT_SCRIPT)))):
        _reject()
    created = _one(session, recovery.id, "recovery_task_created")
    if (created.actor_type != "system" or created.actor_id != "mpt-agent-failure"
            or created.metadata_json != {"source_task_id": source.id, "failure_class": envelope.get("failure_class"),
                                         "code": ERROR, "root_cause_signature": signature, "round": rounds,
                                         "target_task_id": source.id}):
        _reject()
    blocked = _one(session, recovery.id, "recovery_blocked")
    if (blocked.actor_type != "system" or blocked.actor_id != "recovery-coordinator"
            or blocked.metadata_json.get("action") != "mark_recovery_blocked"):
        _reject()
    generation = _journal(runtime, old, child_id)
    requests = source.input_data.get("requests") or []
    segments = [item.get("segment_index") for item in requests]
    maximum_images = plan.input_data.get("image_only_gen_budget")
    current = effective_settings(session, runtime.settings)
    if (type(maximum_images) is not int or maximum_images <= 0
            or not current.asset_image_enabled or maximum_images > current.image_only_gen_budget
            or not segments or any(type(index) is not int or index < 0 for index in segments)
            or len(set(segments)) != len(segments)
            or generation["native_generations"] + len(requests) > maximum_images):
        _reject()
    return source, {"source_task_id": source.id, "source_attempt": source.attempt,
                    "authority_change": "local_image_adaptation_false_to_true",
                    "source_input_hash": source.input_hash, "source_result_hash": stable_hash(source.result_data),
                    "source_max_attempts": source.max_attempts,
                    "source_failure_hash": _failure_hash(source), "original_error_code": source.error_code,
                    "original_error_message": source.error_message, "source_agent_id": source.assigned_agent_id,
                    "failure_event_id": failure.id, "claim_event_id": claim_id, "confirmation_event_id": confirmed_id,
                    "recovery_task_id": recovery.id, "recovery_input_hash": recovery.input_hash,
                    "recovery_result_hash": stable_hash(recovery.result_data), "recovery_round": rounds,
                    "old_run": old.model_dump(), "old_execution": execution.model_dump(),
                    "upstream": {parent.id: {"input_hash": parent.input_hash, "result_hash": stable_hash(parent.result_data)} for parent in parents},
                    "generation_evidence": generation, "image_only_gen_budget": maximum_images,
                    "max_new_native_generations": len(requests), "segment_indices": segments}


def apply_resume(session, source, proof, run):
    """All mutations commit with the new run; never call generic requeue/Recovery."""
    proof = {**proof, "run_id": run.run_id, "run_generation": run.generation, "content_id": run.content_id}
    key = stable_hash({key: proof[key] for key in ("source_task_id", "source_attempt", "authority_change")})
    if session.get(AuditEvent, "aud_" + key) is not None:
        _reject()
    session.add(AuditEvent(id="aud_" + key, actor_type="admin", actor_id="mpt-admin-api",
                           entity_type="agent_task", entity_id=source.id, action=RESUMED, metadata_json=proof))
    source.status = TaskStatus.PENDING
    source.error_code = source.error_message = source.assigned_agent_id = None
    source.not_before = None
    source.dedup_key = task_dedup_key(task_type=source.task_type, content_id=source.content_id,
                                    input_hash=source.input_hash, instructions=source.instructions,
                                    prompt_version=source.prompt_version, schema_version=source.schema_version)
    session.add(source)


def receipt_for_run(session, run):
    rows = session.exec(select(AuditEvent).where(
        AuditEvent.action == RESUMED, AuditEvent.metadata_json["run_id"].as_string() == run.run_id,
    )).all()
    if len(rows) > 1:
        _reject()
    return rows[0] if rows else None


def retry_unstarted_run(session, run, execution, permission):
    """A proven pre-spawn failure can retry this permit, without consuming it twice."""
    from app.business.content_run import save_run

    receipt = receipt_for_run(session, run)
    if receipt is None:
        return False
    source = session.get(AgentTask, receipt.entity_id)
    proof = receipt.metadata_json
    if (permission is not True or execution is None or execution.run_id != run.run_id
            or execution.generation != run.generation or execution.state != "FAILED"
            or execution.error_code != "CONTENT_EXECUTION_START_FAILED" or execution.pid is not None
            or run.host_epoch or run.intents or run.state != "stopped"
            or source is None or source.status != TaskStatus.PENDING or source.attempt != proof.get("source_attempt")
            or source.input_hash != proof.get("source_input_hash") or stable_hash(source.input_data) != source.input_hash
            or proof.get("source_result_hash") != stable_hash(source.result_data)
            or proof.get("run_generation") != run.generation or source.attempt >= source.max_attempts):
        _reject()
    run.state, run.stop_requested, run.stop_reason = "awaiting_host", False, ""
    save_run(session, run, "mpt_image_authorization_launch_retry")
    return True


def has_unfinished_resume(session, content_id):
    receipts = session.exec(select(AuditEvent).where(
        AuditEvent.action == RESUMED,
        AuditEvent.metadata_json["content_id"].as_string() == content_id,
    )).all()
    return any((task := session.get(AgentTask, row.entity_id)) is None
               or task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED} for row in receipts)


def continuation_prompt(session, run, task):
    receipt = receipt_for_run(session, run)
    if receipt is None or receipt.entity_id != task.id:
        return ""
    proof = receipt.metadata_json
    if (receipt.actor_type != "admin" or receipt.actor_id != "mpt-admin-api"
            or proof["run_generation"] != run.generation or proof["source_input_hash"] != task.input_hash
            or proof["source_attempt"] != task.attempt or task.status != TaskStatus.PENDING
            or proof["source_max_attempts"] != task.max_attempts
            or run.allow_local_image_adaptation is not True):
        _reject()
    return ("\n本次授权变更恢复的固定生图预算（服务端原生事件计数，不采用 Worker 自报）："
            + json.dumps({"receipt_id": receipt.id, "run_id": run.run_id, "task_id": task.id,
                          "next_attempt": task.attempt + 1,
                          "historical_native_generations": proof["generation_evidence"]["native_generations"],
                          "frozen_total": proof["image_only_gen_budget"],
                          "max_new_native_generations": proof["max_new_native_generations"],
                          "segment_indices": proof["segment_indices"]}, ensure_ascii=False, sort_keys=True)
            + "\n每个指定 segment 最多原生生图一次；尺寸准备必须使用已生成原图，禁止为适配尺寸重新生成。"
            "原生生图或本机准备失败立即报告，不做隐式重试、替换请求或预算外生成；保留原图和处理记录，"
            "冻结最低1080×1920、概念图限制、独立质检、版权与发布门禁全部仍有效。")
