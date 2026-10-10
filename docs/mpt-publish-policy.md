# MPT Handsfree publication policy

Automatic publishing requires genuine `handsfree_candidate_selected` system
ownership, effective MPT runtime settings, current rights and QC-v2 acceptance,
independent final-audio evidence, intact current video/cover files and a fresh
high-confidence `APPROVE`/`READY` publication review. The package video must have
the same digest as the accepted final video. Changed source/script/preparation
snapshots or artifact revisions require another review; an already issued task
cannot authorize its stale revision.

The migrated daily limit preserves the original host's actual **Asia/Shanghai
local calendar day**, rather than changing the policy to UTC or depending on the
new host's timezone. Every actual Douyin `PUBLISHED` record counts, including
manual and quarantined historical publications. The interval uses the latest
actual publication instant. `RECORDED` entries retain the upstream distinction
and are not treated as verified publications.

Queueing is an advisory check. The final publish-intent boundary repeats the
checks with the request's MPT base settings plus persisted runtime overrides,
inside the existing core SQLite `BEGIN IMMEDIATE` transaction. No new transaction
is nested and no external IO or click happens in this transaction. Core's
durable `AUTHORIZED` event is the reservation: concurrent HTTP requests/processes
cannot both obtain an automatic click authorization. An outstanding automatic or
manual Douyin intent blocks new automatic authorizations until its outcome is
resolved. `UNKNOWN`, `INTERRUPTED`, `NEEDS_USER` and other unresolved states require
reconciliation; no timeout or new calendar day guesses that a click did not happen.
An explicit `FAILED` outcome releases this reservation. A `PUBLISHED` intent
without its publication record yet still counts conservatively. A retry of an
existing intent is always reconciliation-only and never grants another click.

Explicit operator publication retains its manual origin and independent manual
contract; enabling Handsfree or setting its quota does not convert manual content
into automatic production. Workers cannot change a task's signed policy or report
their own publication count. Automatic service calls without effective MPT
settings fail closed.

The portable `WorkflowService.build_publish_review_input` is also the shared
review-policy boundary. The scanner passes its explicit base `settings` into
each item's builder. Handsfree metadata is included only for that item's real
Handsfree selection audit and current effective enabled/auto-publication opt-in;
a mixed batch leaves manual review inputs manual. A persisted opt-out applies
even when the base settings enable automation. Both methods keep the optional
keyword signature compatible with callers lacking a settings argument, but
those callers cannot infer automatic authorization or load global `.env`
settings. Existing frozen tasks are retained as issued; the final intent gate
still checks the current opt-in before authorizing any click.

Recognized persisted overrides are validated together against the complete
explicit base snapshot. Malformed booleans/numbers, invalid ranges or an invalid
combination raise `RuntimeSettingsError` with the affected **field names only**;
they never return an enabled base configuration or silently discard an opt-out.
Validation does not invoke the Settings constructor or reread global GLANCE
environment/credentials. Affected builder/stage calls stop, automatic queueing
returns `BLOCKED`, and claim/dispatch/planning return an explicit 503 diagnostic.
The automatic intent gate returns 409 and creates no new click authorization.
Correcting the persisted configuration permits the existing work to resume;
existing frozen tasks/results and previously registered intents are retained.

`try_auto_publish(engine, settings, content_id, now=...)` owns the per-content OS
lock. Accepted publication-review consumption uses its locked-session helper.
Quota/interval waits leave the content `PACKAGED`, create no duplicate task, and
return a bounded five-minute `retry_at`/`retry_after_sec`. Maintenance invokes this
interface for at most 20 eligible nonquarantined Handsfree packaged items per
persisted five-minute cycle. Terminal/uncertain prior publication attempts are
never automatically retried. Quarantine guards apply before all queueing.

Verification uses temporary SQLite databases and real loopback TestClient /
AgentClient requests. The valid fixture encodes a small video/audio track with
ffmpeg, decodes independent PCM evidence, supplies a controlled ASR observation,
and submits schema-v2 QC plus publication review through the actual HTTP
acceptance path. It proves the policy boundary and artifact/lineage rejection;
it is not evidence of a production publication or external platform result.
