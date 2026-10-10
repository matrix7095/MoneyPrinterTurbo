# MPT polling and Handsfree maintenance

`BusinessRuntime.start_maintenance()` starts one MPT-owned daemon thread after ASGI
initialization. `close()` requests stop, joins for at most 30 seconds, and refuses to
dispose an engine still in use. Tests and offline migration initialize the runtime
without starting the thread. The loop checks every 30 seconds; the durable business
`runtime_setting` key `mpt_maintenance_last_run` limits control-plane work to one pass
per five minutes. Agent dispatch and scheduler-plan call the same maintenance pass.
An OS lock in the independent data directory serializes all participating processes;
SQLite `BEGIN IMMEDIATE` plus deferred service commits keeps new tasks and their
`mpt_task_owned` input-hash audit atomic. No Glance server, Worker, Huey, node heartbeat,
or model SDK is needed.

## Policy carried from the actual source

The source is Glance commit `369d4c109412c1cea1e6f4cb0be957af66b4d20c`, specifically
`apps/worker/src/glance_worker/tasks.py` (`collector.due.v1` and `handsfree.cycle.v1`,
both every five minutes) and the topic/refill helpers of `glance_worker/handsfree.py`.
`app/business/topic_policy.py` retains only the candidate selection and low-water
predicates. It does not copy the worker's media queue, archive/recovery coordinator,
or external publishing code. MPT's existing result consumers advance verified
research, script, visual, asset and final-review results through its media pipeline.

- Enabled sources whose `next_poll_at` is due poll regardless of Handsfree opt-in.
  A pass handles at most 20 due sources, ordered by due time and ID. Collector keeps
  the original direction-lane in-flight guard, failure scheduling and empty-result
  backoff. An unexpected exception outside Collector's own failure handler produces a genuine
  failed collection record and an error-type audit, and moves the next retry no earlier
  than five minutes without shortening an existing longer cooldown. Updating the due
  order keeps a permanently failing first batch from starving later healthy sources.
  No direct task completion or successful collection record is fabricated.
- Only the independent MPT `effective_settings(session, runtime.settings)` enables
  Handsfree. No plain `Settings()` or `.env` read can inherit Glance configuration.
- Topic selection retains score threshold, rights and production blockers, high or
  medium `SELECT` verdict, declared image-only capability/budget and duplicate gates.
  Selection is capped at one item per cycle and two nonterminal Handsfree items.
  Existing content cannot acquire a new Handsfree audit, including manual content.
- Low-water mark is one available candidate. Supplements keep daily cap three,
  persisted 60-minute cooldown, exhausted cooldown and UTC-day rollover. A normal
  due poll bypasses the supplement budget; empty-result backoff applies to both.
- Preparation of one new, MPT-selected item runs outside the SQLite policy
  transaction through the existing per-content operation lock. A crash before it
  starts leaves the item genuinely pending for the next cycle.
- Up to 20 genuinely Handsfree, non-isolated packaged items are rechecked every
  five minutes through `publish_policy.try_auto_publish`; its queue and intent gates
  own daily quota, interval, current QC, rights and external-identity checks. Waiting
  items keep `PACKAGED`. Maintenance does not click publish or alter policy receipts.

## Migration and isolation

A migrated completed candidate-assist result cannot automatically select a topic:
maintenance checks raw database columns inside its write transaction before installing
ORM filters. Frozen input must match its stable hash and MPT ownership hash; every
actual consumption receipt must match the current result hash. Discovery uses its
real mpt_discovery_consumed receipt; other Agent consumers use mpt_agent_consumed.
This retains verified new discovery history for the next search. It never updates an
old receipt to authorize changed input or output.
The session filters task quarantine, content quarantine and every candidate linked
to a quarantined content before batch scheduling/selection. A paused historical
`PENDING` task is not consumed, cancelled, reaped, adopted or given MPT ownership.
Only a new MPT selection companion audit authorizes the preparation retry path.

Preserve the following non-secret source operating state exactly during the separately
reviewed cutover: source ID, enabled flag, poll interval, `next_poll_at`, and
`config_json.handsfree_low_water_supplement` (`status`, UTC `day`, `count`, `daily_limit`,
`cooldown_minutes`, `last_scheduled_at`, `next_retry_at`). Also preserve the collector's
`empty_result_backoff` state. Do not reset a same-day exhausted budget just because
runtime ownership or task isolation changes. These fields are already source data;
this module never reads or writes the old live database or copies credentials.

The safe operating configuration allowlist (Handsfree flags/threshold/daily limit/
interval and approved TTS/ASR values) is migrated independently by the cutover owner.
Do not bulk-copy runtime settings, tokens, worker identities, leases, host/tool proofs,
provider endpoints or credentials. Historical publication records remain available
to the publishing policy's quota/dedup checks; they are not proof of a new publish.

## Validation

`test/business/test_maintenance.py` uses temporary independent databases and AGENT
sources that perform no network request. It covers due polling with Handsfree off,
concurrent/repeated calls, real task/ownership creation, real image-only research
preparation, inherited exhausted quota, normal-due bypass, empty-result backoff,
opt-in/topic disable, WIP bounds, historical/unconsumed/quarantined assist rejection,
manual ownership, frozen paused pending tasks, consumed-result/input/ownership
hash tampering, 20 persistent failures followed by a healthy source, and bounded
shutdown/engine protection.
Live source cutover and external publication require separate acceptance evidence.
