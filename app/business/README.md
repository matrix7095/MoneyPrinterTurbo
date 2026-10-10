# MoneyPrinterTurbo business boundary

This directory attaches portable Glance contracts in `packages/{core,agent,collector,media}` to MPT. It does not import the old server, read its `.env`, or depend on its checkout. Pipeline stage functions reuse upstream services; MPT's media executor connects rendering to VideoProject and MPT video services.

## Application attachment and configuration

`settings_from_config(config._cfg, config.root_dir)` reads a separate `[business]` section and `MPT_BUSINESS_*`. `BusinessRuntime(settings)` exposes `engine`, `settings`, `task_service(session)`, `workflow_service(session)`, `initialize()` and `close()`. MPT lifespan initializes it, assigns `app.state.business_runtime`, and closes it in `finally`. Controller modules `agent`, `discovery`, `content` and `deployment` export routers with `/api/v1` prefixes. Imports do not initialize databases.

```toml
[business]
enabled = true
data_dir = "storage/business"
public_base_url = "http://127.0.0.1:8010"
admin_token = "your-independent-admin-token"
bootstrap_agent_token = "your-independent-agent-root-token"
```

Set `MPT_BUSINESS_PUBLIC_BASE_URL` to the actual listener. Bundle/context URLs resolve against the AgentClient's explicit API URL; absolute business URLs use this setting. The upstream default is port 8000 and must not silently address the existing Glance server.

SQLite defaults to `storage/business/database/app.db`, submissions to `storage/business/agent-submissions`, content to `storage/business/content`. Database paths must stay inside `data_dir`. Startup creates/migrates this isolated schema and seeds providers: never point it at live Glance data or place the production DB in this startup path. Missing/default tokens fail startup. `GLANCE_*` and the old `.env` are ignored; credentials are not copied.

## HTTP and review contracts

Admin bearer authentication protects discovery signing, candidate/collection queries, content selection/actions, publication queueing and Artifact downloads. Discovery search accepts upstream `SearchTaskRequest` fields. Stable sources per direction let `CollectionService.collect_source` enforce lanes and supply a real source to `ingest_agent_result`; upstream one-off console signing uses an empty source ID that cannot be directly consumed.

AgentClient supports `whoami`, `claim`, `dispatch`, `bundle`, `confirm-claim`, `heartbeat`, `similar`, `complete`, `fail`, context download, Artifact upload and `publish-intent`. Dispatch reuses the portable claim service with a short reservation, exact task/type filtering and the original skill prompt. `AT_CAPACITY` includes active task IDs without claiming another task. Confirmation converts the reservation to a formal lease. Bootstrap/derived role tokens, runtime capability checks, strict schemas, worker registration, lease ownership, max-inflight and accepted JSON persistence remain portable core contracts. Claim/dispatch capabilities intersect `pipeline.SUPPORTED_TASK_TYPES`; unsupported consumers cannot dequeue work.

Content actions reuse plain pipeline functions and WorkflowService. Authenticated manual selection records actual human selection; it does not fabricate Handsfree ownership. Manual approvals remain explicit; automatic follow-ups require real Handsfree ownership and enabled review policy. Publish queueing calls `verify_publish_ready` before the core single-account queue. PACKAGED alone does not prove current QC, rights or file integrity. Account targets come from signed core tasks, not adapter inference. A publish Agent must register its intent and submit a validated external result/evidence before publication is recorded.

No unrestricted task-creation endpoint exists. Direct dispatch with an explicitly configured AgentClient is available.

## Deployment and scheduler contract 2

`POST /api/v1/agent/deployment` requires the exact derived role token, a fresh authenticated whoami, explicit host `device_label`, integer `max_concurrency` (1..16) and `supported_models`. The accepted MPT models are `gpt-6.1-sol` and `gpt-6-luna`, with medium/high/xhigh/max effort. It verifies the staged role manifest, skill version/protocol/hash and the installer's fixed PowerShell wrapper under this workspace's `storage/agent-launcher-staging`. The runtime credential file is checked for ownership/existence without reading its secrets. Publish capabilities must be explicitly staged. `request_selftest=false` is required because AGENT_SELFTEST has no installed consumer; a valid registration does not manufacture a selftest result. The original CLI install report lacks the explicit capacity/model list and therefore cannot establish this deployment contract by itself.

Admin GET/PUT `/api/v1/agent/scheduler/config` uses the portable validated configuration. Only the existing core ID `codex` and installer ID `mpt-codex` are accepted. An unconfigured MPT scheduler remains DISABLED. GET `/api/v1/agent/scheduler/plan?scheduler_id=...&contract_version=2` requires a token derived for that exact scheduler identity and rejects other versions. It calls the portable planner inside a SQLite write transaction, counts leased tasks and unexpired SchedulerOffers, applies installed-consumer/quarantine filters and checks staged fingerprints plus deployment capacity. READY returns fresh request/agent identities, title, role, skill, model, effort, launcher and prompt. Offers expire after ten minutes; the CLI freezes a shorter nine-minute one-use handoff through its existing validate/materialize/consume functions. The service never spawns native Workers or writes host automation.

Deployment capacity is shared by all roles with the same verified `device_label`: their leases and unexpired offers occupy one host budget. Host identity and capacity are pinned in the deployment review AuditEvent, not mutable AgentWorker registry fields. Conflicting reported capacities use the lower limit. Baseline and fresh scheduler identities inherit that proof; claim/dispatch/heartbeat reject conflicting runtime labels before any lease confirmation or renewal commit, and the shared worker update boundary preserves the pinned host. Direct role Workers without an offer conservatively occupy their configured role host. A verification record lacking the host pin cannot authorize new claims or planning and requires a new deployment review.

WAITING_UPSTREAM requires both an actual RUNNING media StageRun on a PRODUCING content and a currently held MPT content OS lock. An orphaned stage row alone returns NO_TASK. Lock permission/IO errors return BLOCKED with `MEDIA_LOCK_IO_ERROR` and the exception type; private paths are not exposed. Agent registrations and synchronous media activity are not fabricated ExecutorNode online counts. Anonymous health/readiness and live deployment acceptance remain separate work.

## Files and recovery

Generated image/video and publication evidence uploads preserve task, content, MIME/signature and size boundaries. Files are content addressed inside the isolated root; changed bytes at an existing address are rejected. Discovery submits schema JSON through AgentSubmission/result.json and rejects Artifact upload rather than fabricating Artifact rows.

Uploads check the actual byte stream against the signed task's size limit before retaining each chunk. An over-limit request receives 413 without reading subsequent chunks; a declared over-limit Content-Length is rejected before reading the stream.

Bundle and download share effective file indices without changing the signed input/hash. Review, publish and video-generation tasks receive required videos; planning tasks use frames. Signed video/publish/cover Artifact IDs resolve to lease-authenticated context URLs instead of the unmounted old `/media/artifacts` route. Ownership, root, size and SHA256 are checked before serving. Manifests and downloaded basenames agree.

Completion durably accepts validated results first. Consumption errors return `advanced: PENDING` and retain accepted results for replay. `CONSUMED` means consumption, not external publication. Idempotent completion retries still require the original lease token.

Terminal Agent failure commits the original `FAILED` state before requesting durable Recovery. The fail response adds `recovery_status` and `recovery_task_id`: a task ID accompanies an actual core Recovery status; `DEFERRED` records core capacity deferral, `BLOCKED` preserves core policy/budget refusal, and `PENDING` with no ID means recovery admission needs retry. Retryable failures keep the original core backoff without creating Recovery. Authenticated admin `POST /api/v1/agent/tasks/{id}/retry-recovery` retries admission for an already failed source and deduplicates its current attempt. It accepts no agent-selected classification, target or action override.

Admission requires current MPT ownership and frozen input, a terminal authenticated failure audit, unchanged verified native staging, current content and parent revisions, and no other leased/submitted task on that content. Current scene-analysis references retain the portable single-ID/list and per-batch revision contracts. Content-wide durable publication intents must be reconciled; unknown, outstanding, published or malformed targets cannot enter failure recovery. Imported/quarantined sources remain isolated. Core classification, round limits, capacity and action whitelist remain authoritative.

`GENERATED_IMAGE_CONSTRAINT_MISMATCH` may use server-classified `BUSINESS_QC` visual-plan repair only when the current accepted owned plan and script match the failed image task's full frozen script, plan and requests. Legacy image inputs without explicit upstream IDs need that full plan binding. This produces a new visual-plan revision through the core Recovery consumer and preserves the accepted script/voice; it does not fabricate generated assets. Consumption rechecks source/failure/envelope fingerprints, native deployment, current parent inputs/results/acceptance receipts, content publication intents and active leases. A stale accepted Recovery result stays `COMPLETED` with consumption pending and creates no repair. Existing completed-QC-origin Recovery keeps its portable behavior even if that QC had an earlier retryable failure.

Discovery uses `_ConsumptionSession` and SQLite `BEGIN IMMEDIATE`: collector's candidate commits become flushes until candidates, source schedule, run statistics and result-hash marker commit atomically. This serializes independent runtimes/processes and avoids partial batches or statistical drift. Exceptions roll back and close sessions.

Other pipeline stages create their own sessions and perform media IO outside that SQLite transaction. A per-content OS file lock serializes advance and admin actions; its filename hashes the content ID and process crashes release it. Persisted pipeline stage/input guards handle an advance whose marker failed; the separate consumption audit pins the accepted result hash. Failures leave accepted work pending.

For a temporary ASR/audio-evidence failure, authenticated `POST /api/v1/contents/{id}/resume-acceptance` resumes `WAITING_AGENT/audio_evidence_waiting` or an orphaned `PRODUCING/audio_evidence_running`. It takes the same content OS lock as live collectors, verifies the registered v2 render receipt, explicit manual/Handsfree production ownership and current file/lineage hashes, then collects independent final-track evidence without rendering again. Process crashes release the lock; other running media stages are not recovered through this endpoint. Invalid receipts/materials close the snapshot; concurrent retries issue at most one QC v2 task. This endpoint does not approve that QC decision.

Rendering uses the explicitly requested encoder once. The default remains libx264; an explicit hardware encoder may fail. The original failure is recorded in StageRun rather than silently retrying an unrelated RuntimeError with software encoding.

Startup replays only completed tasks with an MPT ownership audit matching the signed input hash. Historical imported completed tasks do not become new work merely because their MPT marker is absent. Dispatch/planning first recover accepted owned results, then reap owned expired leases in a separate SQLite transaction. Unconfirmed reservations preserve the attempt budget; confirmed expiry preserves core backoff/exhaustion and publication intent reconciliation. Imported unowned expired leases are neither reaped nor directly reclaimed.

Existence of `mpt_migration_quarantined_content` or `mpt_migration_quarantined_task` AuditEvents blocks the corresponding task/content writes regardless of metadata or its original terminal status. Shared task/runtime guards protect claim, dispatch, lease actions, uploads, completion/failure, consumption/recovery, approval and publishing. Explicit task claims query persisted eligibility/ownership so a cached Session object cannot bypass the historical lease boundary. A historical task without a content ID is also isolated. Admin GETs preserve original records/status and expose quarantined tasks only as metadata, input/result hashes and concise isolation reasons; instructions, input/result/schema payloads remain frozen in the database. Normal current tasks keep their full context. Importers must retain these markers until an explicitly reviewed migration proves the work can be handled.

## Upstream sync and validation

Handsfree automatic publication additionally uses the shared policy in
`publish_policy.py`: Shanghai local-day limits, latest-publication interval,
current QC/review/artifact lineage, and durable outstanding-intent reservations.
The final intent gate uses effective MPT settings inside core's existing SQLite
authorization transaction; an issued task cannot bypass a later quota change.
Quota waits retain PACKAGED and return a five-minute bounded retry. Explicit
manual publishing keeps its manual origin. See `docs/mpt-publish-policy.md` and
`test/business/test_publish_policy.py` for the contract and real temporary HTTP /
two-process reservation tests. When syncing upstream, preserve the source
Handsfree `_published_limits_allow` day/record semantics and the core intent
transaction/reconciliation behavior together.

Publication-review creation applies the same identity distinction at the shared
portable builder: scanner batches pass explicit MPT base settings, then each
item requires real Handsfree ownership and current effective auto-publish opt-in
before receiving Handsfree review metadata. Missing settings preserve the manual
contract without reading process-global settings; mixed batches cannot assign a
manual item somebody else's automatic authorization.

Follow `packages/MIGRATION.md` and `docs/glance-upstream-sync.md`. Review request models, AgentTaskService, CollectionService, bundle/materialization contracts and pipeline consumers together. Keep adapter differences centralized: isolated configuration/auth, source-backed signing, supported consumers, transport/index/hash validation, content serialization and result recovery.

Real AgentClient HTTP tests in `test/business/test_discovery_http.py`, `test/business/test_content_http.py` and `test/business/test_deployment_http.py` use temporary SQLite/filesystem state. They cover discovery rollback/restart/concurrent consumers; dispatch exact-task reservation/capacity/confirmation; research marker-failure replay; actual advance/admin serialization; OS-lock crash release; historical quarantine/recovery exclusion; scoped deployment/scheduler authentication; concurrent plans and expired offers; owned lease reaping; exact model/effort and one-use handoff; changed staged proof; raw header/role/schema/lease denial; upload/context integrity; and lease-authenticated video/cover downloads resolving on port 8010. These fixtures prove transport and persisted-state behavior. They do not prove a live deployment, native Worker creation or a new external publication ID/evidence; those require separate acceptance.
