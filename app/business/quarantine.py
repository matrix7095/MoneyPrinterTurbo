"""Server-side isolation of imported, unverified historical work."""

from contextlib import contextmanager
import re

from glance_core.models import AgentTask, AgentWorker, AuditEvent, TaskStatus, utc_now
from glance_core.task_service import AgentTaskService, TaskConflictError
from glance_core.task_service import stable_hash
from sqlalchemy import event, exists, or_
from sqlalchemy.orm import with_loader_criteria
from sqlmodel import select

CONTENT_QUARANTINE = "mpt_migration_quarantined_content"
TASK_QUARANTINE = "mpt_migration_quarantined_task"
DEPLOYMENT_VERIFIED = "mpt_deployment_verified"


def deployment_proof(session, agent_id):
    # A contract-2 fresh Worker inherits the exact audited base deployment.
    base, _, suffix = agent_id.rpartition("-")
    identity = base if re.fullmatch(r"[0-9a-f]{12}", suffix) else agent_id
    return session.exec(
        select(AuditEvent)
        .where(
            AuditEvent.action == DEPLOYMENT_VERIFIED,
            AuditEvent.entity_type == "agent_worker",
            AuditEvent.entity_id == identity,
        )
        .order_by(AuditEvent.created_at.desc())
    ).first()


def checked_runtime_host(session, agent_id, runtime_info):
    proof = deployment_proof(session, agent_id)
    worker = session.get(AgentWorker, agent_id)
    if proof is None:
        if worker and worker.deployment_verified_at:
            raise TaskConflictError("DEPLOYMENT_HOST_UNPINNED: redeployment required")
        return runtime_info
    host = proof.metadata_json.get("device_label")
    if not isinstance(host, str) or not host:
        raise TaskConflictError("DEPLOYMENT_HOST_UNPINNED: redeployment required")
    incoming = (runtime_info or {}).get("device_label")
    if incoming and incoming != host:
        raise TaskConflictError(
            "DEPLOYMENT_HOST_CONFLICT: runtime host differs from verified deployment"
        )
    return {**(runtime_info or {}), "device_label": host}


def eligible_task_clause():
    return ~exists().where(
        or_(
            (AuditEvent.action == TASK_QUARANTINE)
            & (AuditEvent.entity_type == "agent_task")
            & (AuditEvent.entity_id == AgentTask.id),
            (AuditEvent.action == CONTENT_QUARANTINE)
            & (AuditEvent.entity_type == "content")
            & (AuditEvent.entity_id == AgentTask.content_id),
        )
    )


def quarantine_records(session, *, task_id=None, content_id=None):
    if task_id:
        task = session.get(AgentTask, task_id)
        content_id = task.content_id if task is not None else content_id
    conditions = []
    if task_id:
        conditions.append(
            (AuditEvent.action == TASK_QUARANTINE)
            & (AuditEvent.entity_type == "agent_task")
            & (AuditEvent.entity_id == task_id)
        )
    if content_id:
        conditions.append(
            (AuditEvent.action == CONTENT_QUARANTINE)
            & (AuditEvent.entity_type == "content")
            & (AuditEvent.entity_id == content_id)
        )
    if not conditions:
        return []
    return [
        {
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "metadata_json": {
                "verified": False,
                "historical": True,
                "reason": str(
                    row.metadata_json.get("reason") or "Unverified historical import"
                ),
            },
        }
        for row in session.exec(select(AuditEvent).where(or_(*conditions))).all()
    ]


def assert_allowed(session, *, task_id=None, content_id=None):
    if quarantine_records(session, task_id=task_id, content_id=content_id):
        raise TaskConflictError(
            "MPT_MIGRATION_QUARANTINED: historical work is unverified"
        )


def task_projection(session, task):
    quarantine = quarantine_records(session, task_id=task.id)
    if not quarantine:
        return task.model_dump(mode="json", exclude={"lease_token_hash"})
    # Keep imported evidence frozen in storage, but never expose historical
    # instructions or input/result/schema payloads as executable API context.
    metadata = {
        "id",
        "task_type",
        "content_id",
        "status",
        "priority",
        "prompt_name",
        "prompt_version",
        "schema_version",
        "input_hash",
        "required_role",
        "lane_key",
        "attempt",
        "max_attempts",
        "assigned_agent_id",
        "error_code",
        "created_at",
        "started_at",
        "completed_at",
        "lease_expires_at",
        "not_before",
    }
    return {
        **task.model_dump(mode="json", include=metadata),
        "result_hash": stable_hash(task.result_data),
        "migration_quarantine": quarantine,
    }


@contextmanager
def filter_task_reads(session, clause):
    # Core claim/reaper own their transaction/query shape. Apply a scoped SQL
    # predicate instead of copying them or filtering after a lease is written.
    def restrict(state):
        if state.is_select:
            state.statement = state.statement.options(
                with_loader_criteria(AgentTask, clause, include_aliases=True)
            )

    event.listen(session, "do_orm_execute", restrict)
    try:
        yield
    finally:
        event.remove(session, "do_orm_execute", restrict)


class IsolatedTaskService(AgentTaskService):
    def _authorize_claim(self, *, agent_id, task_id, role_id, runtime_info, authorization_context):
        from app.business.content_run import authorize_claim

        authorize_claim(
            self.session, agent_id=agent_id, task_id=task_id, role_id=role_id,
            runtime_info=runtime_info, scope=authorization_context,
        )

    def create_task(self, **kwargs):
        assert_allowed(self.session, content_id=kwargs.get("content_id"))
        return super().create_task(**kwargs)

    def claim(self, **kwargs):
        from app.business.content_run import scoped_offer_clause

        kwargs["runtime_info"] = checked_runtime_host(
            self.session, kwargs["agent_id"], kwargs.get("runtime_info")
        )
        if kwargs.get("task_id"):
            assert_allowed(self.session, task_id=kwargs["task_id"])
        owned = select(AuditEvent.entity_id).where(
            AuditEvent.action == "mpt_task_owned",
            AuditEvent.metadata_json["input_hash"].as_string() == AgentTask.input_hash,
        )
        # Upstream claim can directly take an expired lease. Only this runtime's
        # signed ownership permits that recovery, including explicit task claims.
        recoverable = (
            (AgentTask.status != TaskStatus.LEASED)
            | (AgentTask.lease_expires_at >= utc_now())
            | AgentTask.id.in_(owned)
        )
        if (
            kwargs.get("task_id")
            and self.session.exec(
                select(AgentTask.id).where(
                    AgentTask.id == kwargs["task_id"],
                    eligible_task_clause(),
                    recoverable,
                )
            ).first()
            is None
        ):
            # A cached identity-map object makes Session.get bypass loader SQL
            # criteria. Validate the persisted row with an explicit scalar query.
            return None
        selectable = eligible_task_clause() & recoverable
        if not kwargs.get("authorization_context"):
            selectable &= (AgentTask.status != TaskStatus.PENDING) | ~scoped_offer_clause()
        with filter_task_reads(self.session, selectable):
            return super().claim(**kwargs)

    def heartbeat(self, task_id, lease_token, runtime_info=None):
        task = self.session.get(AgentTask, task_id)
        if task and task.assigned_agent_id:
            runtime_info = checked_runtime_host(
                self.session, task.assigned_agent_id, runtime_info
            )
        # Validate before core require_lease can implicitly confirm/renew/commit.
        return super().heartbeat(task_id, lease_token, runtime_info)

    def _touch_worker(self, agent_id, **kwargs):
        kwargs["runtime_info"] = checked_runtime_host(
            self.session, agent_id, kwargs.get("runtime_info")
        )
        return super()._touch_worker(agent_id, **kwargs)

    def require_lease(self, task_id, *args, **kwargs):
        assert_allowed(self.session, task_id=task_id)
        return super().require_lease(task_id, *args, **kwargs)

    def complete(self, *, task_id, **kwargs):
        assert_allowed(self.session, task_id=task_id)
        return super().complete(task_id=task_id, **kwargs)

    def fail(self, *, task_id, **kwargs):
        # Core accepts some late failures after a lease conflict; guard first.
        assert_allowed(self.session, task_id=task_id)
        return super().fail(task_id=task_id, **kwargs)

    def confirm_claim(self, *, task_id, **kwargs):
        assert_allowed(self.session, task_id=task_id)
        return super().confirm_claim(task_id=task_id, **kwargs)

    def _latest_publish_intent(self, intent_key):
        existing = super()._latest_publish_intent(intent_key)
        context = getattr(self, "_mpt_publish_policy_context", None)
        if existing is None and context is not None:
            from app.business.publish_policy import enforce_auto_intent

            task_id, settings = context
            task = self.session.get(AgentTask, task_id)
            # Core calls this after BEGIN IMMEDIATE and before writing AUTHORIZED.
            # Its rollback/finally retains the original lease and retry semantics.
            enforce_auto_intent(self.session, task, settings)
        return existing

    def register_publish_intent(self, *, task_id, settings=None, **kwargs):
        assert_allowed(self.session, task_id=task_id)
        self._mpt_publish_policy_context = (task_id, settings)
        try:
            return super().register_publish_intent(task_id=task_id, **kwargs)
        finally:
            self._mpt_publish_policy_context = None
