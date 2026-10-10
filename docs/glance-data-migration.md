# Glance business-data migration: isolated merge rehearsal

`app.business.migration` prepares an independent staging directory, merges historical
business rows transactionally, and rehearses restoring the pre-import baseline. It
has no live cutover command and never accepts a writable live database destination.
Source and baseline databases are opened with SQLite `mode=ro`; `create_db` is used
only for the new staging database. Do not point an application or scheduler at this
directory until the separate cutover gates have been accepted.

## Snapshot and import boundaries

SQLite's backup API first captures a consistent database, including committed WAL
rows, in memory. A business-only projection is then persisted. This deliberately
avoids persisting a raw backup containing runtime/provider credentials. The exact
known column set is checked before projection; unexpected schemas stop preparation.
Persisted business snapshots are pinned by SHA-256 and verified again on import.
Staging format v2 also pins both preflight inventories and the rollback backup.
Older v1 directories are retained for inspection but cannot be imported or restored
with the hardened tool; create a fresh independent directory.

Included tables, in relation order: `source`, `collection_run`, `candidate`,
`candidate_signal`, `content`, `agent_task`, `stage_run`, `artifact`,
`agent_submission`, `publication_record`, `content_issue`, `content_revision`,
`publication_preview`, `audit_event`. Original IDs, relationships, frozen task
input/result JSON, hashes, account/intent keys, URLs, states and audit history remain
intact. Only `content.content_dir` and `agent_submission.raw_result_path` are rebased
to staging roots. An explicitly quarantined submission outside allowed roots gets
an absent staging placeholder; its original path remains in the business snapshot.

Excluded tables include runtime settings, provider configuration, workers, executor
nodes, scheduler offers, lease policies, task prompts and voice profiles. No `.env`,
cookies, private deployment configuration or runtime secrets are copied. Sensitive
business JSON stops projection before it is persisted. Plural/nested credential
fields (`api_keys`, `tokens`, `credentials`, and their
singular forms) are rejected; diagnostics contain identities and field names only.
Referenced credential files and sensitive JSON files are rejected without reporting their values. This is a
bounded safeguard for known data shapes, not a general content redaction system.

Files are limited to Artifact paths, submission result files, direct task
`context_files`/`materialized_context` references and frozen voice timing sidecars. No project-wide or drive-wide scan
is performed. Exact allowed content/submission roots must be supplied; archive
roots outside them require a separately reviewed explicit root. Each Artifact is
checked against its recorded size and SHA-256; submission JSON is checked against
its semantic result hash. File copies are verified again before and after copying.
A path is checked separately against each reference's expected hash/size. An old
failed reference cannot poison a newer verified Artifact or accepted submission at
the same path. A context reference to a declared Artifact path requires at least
one matching verified Artifact; dropping the expected hash cannot rescue a path
with no valid Artifact. Physical file facts are cached, not failed expectations.
Absolute frozen context paths require handoff; payloads are not rewritten.

For `visual_timing_evidence`, the frozen `voice_sha256` selects an actually verified
voice Artifact of the same content. Its adjacent `word_boundaries.json` is copied
only after matching the frozen `boundaries_sha256`. This works for both legacy
`audio/narration.mp3` and immutable `audio/voice-<unique>/narration.mp3`, in source
and baseline inventories. There is no hardcoded legacy sidecar, invented past
audio, or regenerated timing evidence.

Source and baseline file inventories are combined before copying, with database
backup overhead and a 256 MiB reserve. File collisions reject preparation. Row IDs
and unique keys are checked during the single merge transaction: an identical row
is skipped, a conflicting row rejects and rolls back the whole merge. Existing
baseline episodes therefore survive both a rejected merge and restoration.

Every task's frozen `input_hash` must equal the core `stable_hash(input_data)`
contract. A mismatch is reported without repairing either field. Nonterminal or
baseline mismatches block preparation; terminal source history requires explicit
quarantine. There is no implicit allowance for an unverified legacy hash format.

Import checks both source and baseline snapshots, both inventories, rollback
lineage and every staged source/baseline file before merging and again before
transaction commit. Under the same SQLite write lock, the target's complete schema
and all table rows must equal either the exact prepared baseline or the independently
constructed expected merge (pinned rollback plus rebased source and deterministic
audit rows). After insertion it must equal the expected merge. External edits to
baseline rows, new unexpected target rows, schema or files reject both first and
repeated imports. A crash after a valid commit but before report writing can retry
because all expected rows are reconstructed, rather than trusting a success receipt.
Restore rehearsal also checks baseline media; matching database rows alone cannot
produce a successful restore report for damaged baseline files.

## History, publication evidence and quarantine

Source `PENDING`, `LEASED` and `SUBMITTED` tasks block migration by default. Preparation
does not expire leases, cancel tasks or silently hand off pending work. The explicit
paused staging handoff below permits pinned `PENDING` preservation only. A final
coordinated source freeze and explicit task handoff are required. Unsupported task
schemas or video-generation tasks are reported for manual handoff. Legacy QC is
preserved as history; it is never converted to passing QC v2.

Historical completed tasks receive deterministic `mpt_migration_handled_task`
audit events plus the actual existing consumer marker: `mpt_discovery_consumed`
for `SOURCE_DISCOVERY`, `mpt_agent_consumed` for other task types. Metadata records
the historical result hash and snapshot SHA. No `mpt_task_owned` event is fabricated.
Original accepted lease-token hashes, including historical `NULL`, remain unchanged.
Completed historical payloads must not be replayed by either discovery or pipeline
startup. Historical published/UNKNOWN records are preserved without requeueing.
Unresolved publication states require reconciliation; terminal history can only
be imported with explicit quarantine, and nonterminal cases remain blocked.

For a `PUBLISHED` record, the report checks a nonempty external ID and URL, a matching
completed publish task result, and a same-content `publish_evidence` Artifact. This
proves stored record linkage; it does not prove that a historical screenshot shows
that ID or that the platform still hosts the publication. File verification and
quarantine are reported separately. New production publication still needs the
normal runtime QC, rights, review and external-result gates.

By default, missing/corrupt files or incomplete publication linkage block import.
`--include-quarantined-history` permits metadata preservation only for terminal
`ARCHIVED`/`PUBLISHED`/`REJECTED` content, or terminal tasks without content. Invalid
nonterminal content still blocks the whole operation unless the bounded historical
reference exception in an explicit paused handoff applies. Unverified versions are omitted;
rows and their original states are preserved. Reports say `QUARANTINED_STAGING`,
not verified production. Multiple reasons can quarantine the same content.

The required runtime/API gate contract is:

| Field | Content quarantine | Task quarantine |
| --- | --- | --- |
| `action` | `mpt_migration_quarantined_content` | `mpt_migration_quarantined_task` |
| `entity_type` | `content` | `agent_task` |
| `entity_id` | Original content ID | Original task ID |
| `actor_type` / `actor_id` | `system` / `offline-migration` | Same |

`metadata_json` is `{verified: false, historical: true, snapshot_sha256, identity,
reason}`. The deterministic event ID makes repeated imports stable. **Any such
audit must deny all mutating stages, consumption, publishing, repair and recovery
for the subject; GET remains available.** Enforcing that gate is a separate runtime
change. Do not activate quarantined staging without its verified deployment.

This preserves inspectable historical metadata instead of deleting uncertain
publications or treating missing evidence as verified. Resolving a quarantine is
an explicit future evidence/handoff operation, not automatic replay.

## Explicit paused staging handoff

`prepare --paused-handoff C:/reviewed/paused-handoff.json` accepts exactly this
manifest shape; unknown fields, duplicate tasks, an incorrect snapshot pin or any
task other than the exact pinned `PENDING` input reject preparation:

```json
{
  "version": 1,
  "source_snapshot_sha256": "<SHA-256 of the business-only source snapshot>",
  "reason": "Preserve the reviewed queued work paused in independent staging",
  "tasks": [
    {"task_id": "<original task ID>", "content_id": "<original content ID>", "input_hash": "<original stable_hash>"}
  ]
}
```

The manifest is stored and SHA-pinned in the staging marker, then verified again
against the immutable source snapshot before import and before commit. Original
task status, frozen JSON/hash, attempts and lease fields remain unchanged. For
each listed task, import emits both content and task quarantine events (task-only
for a task without content). Metadata adds `paused: true`, `task_id`, `content_id`,
`input_hash` and `cutover_authorized: false` to the contract above. There is no
`mpt_task_owned`, handled or consumed receipt for these pending tasks. They are
paused preserved work, **not completed work**, and the runtime gate must deny
claim/consume/stage/publish/recovery for their quarantined subjects.

Only independently identified superseded historical references of the listed
content may be retained as unverified metadata: an older Artifact replaced by a
newer verified Artifact at the same path/type; an older rejected submission
replaced by a newer accepted submission for the same task/attempt/path, whose
actual JSON matches both accepted result hash and completed task result; or a
terminal older timing task referencing a superseded voice at that shared path,
with a newer listed pending task's actual voice and boundary hashes verified.
All old records and states remain present; unavailable old bytes are not restored.
Current corruption, missing current timing, root escapes, credentials, other
unlisted pending tasks, unsupported inputs and unresolved nonterminal publication
states still block preparation. A manifest never authorizes source freeze,
production cutover, automatic continuation, fabricated QC or publication proof.

## Invocation and outputs

Run from the target worktree using its existing virtual environment. All paths are
explicit; the staging directory must not exist or overlap any source/baseline
database or allowed file root.

```powershell
& ./.venv/Scripts/python.exe -m app.business.migration prepare `
  --staging C:/independent-staging/new-migration `
  --source-db C:/source/data/database/app.db --source-project C:/source `
  --content-root C:/source/data/content `
  --submission-root C:/source/data/agent-submissions `
  --baseline-db C:/target/storage/business-8010/database/app.db `
  --baseline-project C:/target `
  --baseline-content-root C:/target/storage/business-8010/content `
  --baseline-submission-root C:/target/storage/business-8010/agent-submissions
```

Only after `preflight.json` says `READY`, the explicit isolated operation is:

```powershell
& ./.venv/Scripts/python.exe -m app.business.migration apply --staging C:/independent-staging/new-migration
& ./.venv/Scripts/python.exe -m app.business.migration apply --staging C:/independent-staging/new-migration
& ./.venv/Scripts/python.exe -m app.business.migration restore-rehearsal --staging C:/independent-staging/new-migration
```

Preparation produces `stage.json`, `source-business.db`, optional
`baseline-business.db`, their preflight inventories, and, only when ready, copied
files plus `target.db` and `rollback.db`, plus a pinned `paused-handoff.json` when
explicitly supplied. Import produces `audit-report.json` with
counts, foreign-key validation, historical external IDs, publication bindings,
quarantine and verified-file counts. Every report has `cutover_authorized: false`.
Repeating import must insert zero business rows and preserve the logical database
hash. Restoration writes a new `rollback-rehearsal.db`, never replaces the staging
target, source or live baseline; its complete logical rows must match `rollback.db`.

## Actual read-only preflight, 2026-10-10

Evidence directory, relative to the migration worktree:
`storage/data-migration-preflight-38ab573b5dea/`.

The source was `C:/Users/Administrator/Documents/Glance/data/database/app.db`;
the baseline was the separate MPT `storage/business-8010/database/app.db`.
Both were captured through read-only consistent backup. This baseline is a
point-in-time snapshot during ongoing work, not a final freeze. It contains
`content_92b4df321fcf4212ae8adbc435386acf` and research task
`agt_0605e3b1bfbe416891ae79226599c352`.

| Inventory | Source | Baseline |
| --- | ---: | ---: |
| Candidates | 338 | 2 |
| Content | 131 | 1 |
| Agent tasks | 2,775 | 3 |
| Artifacts | 1,017 | 0 |
| Agent submissions | 2,781 | 2 |
| Publication records | 5 | 0 |

Source snapshot SHA-256:
`7fddeea1345a07bfe7183ce792705204687abd3b17a2844d9ce665045a8da202`.
Baseline snapshot SHA-256:
`d61b5e4966bc36a9e70c622b1b6d9c2ddeb95a4074eb1630810998c4d91bd745`.

This preflight is **BLOCKED**: two source pending tasks, two nonterminal Artifact
size/hash mismatches and one nonterminal submission semantic-hash mismatch. Its
explicit terminal-history quarantine inventory has 347 issues across 93 content:
230 submission mismatches, 115 Artifact mismatches and two incomplete publication
bindings. These counts are issues, not deleted rows or independently verified
publications. The initial inventory selected 4,862,048,850 bytes of source files
and 10,172 baseline bytes, with 28,484,337,664 bytes free; later code also rejects
context references to already rejected files, so a fresh final inventory is required.
No `target.db` was created, media copied, historical import applied or live cutover
performed for this directory. Exact blocker IDs and referenced paths are in its
`preflight.json`; no source/private settings were persisted.

After independent review, the v1 directory remains an initial, unaccepted artifact.
A field-name-only scan using the broadened detector found zero sensitive fields in
each of its two business snapshots; no credential values were printed or copied.

Fresh v2 read-only evidence is in
`storage/data-migration-preflight-v2-d431013423a8/`. The source snapshot SHA is
unchanged. Its current baseline snapshot SHA is
`b8653b9ce91f783a050f8aa104f447b6a668bcc6a18d2014dbd494db0abc1659`;
that point-in-time baseline contains the same current episode/research IDs,
eight tasks, seven submissions, one stage run, one Artifact and 59 audit events.
Its source frozen-input hash mismatch count is zero.

The new inventory is **BLOCKED** with seven issue entries: two source pending tasks,
one direct Artifact size/hash failure, one direct submission hash failure and three
additional references to already rejected files. It records 624 terminal quarantine
issue entries across the same 93 content: 179 direct submission hash failures,
67 direct Artifact failures, 376 further rejected-file references, and two incomplete
publication bindings. Counts rose because subsequent references to a rejected path
are now explicitly rejected, rather than allowing context references to copy it.
These are reference-level issue counts, not independent files or new source changes.
Combined selected file size is 4,597,299,606 bytes, with 28,262,244,352 bytes free
at inventory time; this includes baseline files and excludes rejected-context files.
No target database, media copy, import or cutover was performed in the v2 directory.
Independent acceptance of the hardened implementation is still required; these
read-only evidence artifacts do not authorize production cutover.

### Fresh paused inventory, 2026-10-10

`storage/data-migration-paused-preflight-aaaab9482cb8/` contains fresh consistent
business-only source and 8010 baseline snapshots, both inventories, the explicit
two-task paused manifest and `inventory-provenance.json`. It is **read-only
inventory**, not a prepared/imported staging deployment: no `stage.json`, target
database, media copy or import was created. Source and baseline reference plans
both report `READY` with zero blockers under the explicit paused/history policy;
this does not prove source freeze, final baseline freshness or production cutover.

The source snapshot SHA remains
`7fddeea1345a07bfe7183ce792705204687abd3b17a2844d9ce665045a8da202`;
the baseline snapshot SHA is
`89228dfbf6d1d123e2ca3c0ff2d301faed2506f913c730f14c1913c39be3035d`.
The source inventory still has 131 contents, 2,775 tasks, 1,017 Artifacts, 2,781
submissions and five publication records. The baseline includes the current new
episode, eight tasks, one voice Artifact and seven submissions at backup time.
The pinned pending tasks are `agt_bfd2b09e5d8a4982932b8a1098ce2aba`
(content `content_4916957a1812471bbe4b5bed8339b0b1`) and
`agt_c55378040e9548328973db3dd9468cc1`
(content `content_eb718d1f514d462dae3901ad3a1b3770`). Both remain original pending
work requiring separately coordinated freeze/handoff before any activation.

354 reference-level quarantine entries include terminal historical gaps, the
two explicit pauses, two overwritten old voice Artifacts, one overwritten rejected
raw result and two cancelled old timing references. They do not describe 354
independent lost files. The current Xerdeck voice is actually verified against
SHA `7b0a0502651557bdb322c7916964e39e50d7bbbe9d55d638eb79367d6a3750a6`;
its current timing sidecar is selected against frozen SHA
`9ae8ae9c219732cf92456b27613d63d7fc0f61dde532b9d9c0a99b3ed8575fee`.
The old unavailable voice/raw/timing versions remain unverified metadata; neither
the manifest nor a current valid alias recovers those original bytes. Source
inventory selects 11,947 files / 4,862,050,588 bytes, plus nine baseline files.
Independent review and real isolated import/restore remain separate future gates.

## Verification and remaining cutover work

Target Python 3.12: the original 40 focused migration tests cover baseline preservation,
atomic collision rollback, repeat import, restoration, active leases, WAL snapshot
consistency, missing/corrupt media, terminal versus nonterminal quarantine, escaped
submission paths, singular/plural nested sensitive JSON, UNKNOWN publication,
snapshot/file tampering, frozen input hashes and combined capacity. Review regressions
first reproduced 18 failures, then passed after fixes. Added drift cases cover both
first and repeated import, baseline files/snapshot/rollback/rows, unexpected target
rows, file change during the merge transaction, and corrupt baseline restore media.
Ruff and compile checks passed. Independent root verification ran exactly these
40 migration tests in separate `.venv-py311` and `.venv-py313` environments using
`pytest test/business/test_migration.py -q -p no:cacheprovider`: Python 3.11 passed
40 tests in 11.13 s, Python 3.13 passed 40 in 11.27 s. This is focused migration
coverage, separate from the older business/media matrix; it is not a new whole-suite claim.

The paused-handoff extension subsequently passed 60 focused tests in Python 3.12
in 15.39 s, with Ruff and compile checks passing. Additional coverage includes
shared-path order independence, exact pending/snapshot/hash pins, disallowed
LEASED/SUBMITTED/UNKNOWN handoffs, unrelated blockers, original pending fields,
dual quarantine events without fake consumption, repeated isolated import,
paired immutable voice sidecars in source/baseline, manifest tampering, and lost
cancelled timing references requiring a verified newer pending voice. The complete 60-test suite also passed independent root runs in Python 3.11
(17.67 s) and Python 3.13 (16.75 s). These remain focused migration checks.

Before a real cutover: resolve/handoff source active tasks and nonterminal corruption,
review quarantine evidence, deploy and prove the runtime quarantine gate, take a
fresh frozen baseline containing all current episodes, repeat isolated import and
restoration, and review operating configuration separately. Runtime settings cannot
be blindly copied: a reviewed allowlist must explicitly retain the user's existing
Handsfree authorization and chosen ASR/TTS policy (previously observed Handsfree
enabled, ASR medium and Edge rate +20), while excluding provider secrets and dynamic
scheduler/tool state. Those values are prior operator evidence, not values read or
applied by this tool. Manual content must retain its real manual authorization;
configuration migration must not fabricate Handsfree provenance or bypass QC v2.
Voice profiles, custom prompts/provider mappings, enabled source polling and any
out-of-root historical fixtures also need a reviewed configuration/path step.


## Actual isolated merge and restore rehearsal, 2026-10-10

For the later main deployment, keep the selected staging root permanently at
`main/storage/business-8000` and use its `data` child as the runtime DataDir.
The migration tool already writes content and submission paths under
`<staging>/data`. After a fresh reviewed merge, use SQLite backup to put
`<staging>/target.db` at `<staging>/data/database/app.db`; do not move the media
directory or rewrite frozen task payloads. The 8010 database/media must be the
baseline, not the historical source, so its genuine MPT ownership audits survive.
This directory contract is statically compatible, but the fully published new
episode baseline still needs fresh preflight, repeat import, restore and an
actual isolated start/stop at that DataDir before main cutover. Completed baseline
tasks are also checked for context/hash/media blockers. Successful stop removes
the exact instance's `private/server.json`, permitting a later port change;
unsuccessful stop keeps the guard. Operating credentials are created independently.

`storage/data-migration-rehearsal-20261010-a/` was freshly prepared from read-only
source and MPT baseline snapshots with the reviewed pinned paused handoff. Both
inventories were READY with zero blockers. This was not a source freeze or cutover.

The first merge inserted 131 historical contents, 2,775 tasks, 1,017 Artifacts,
2,781 submissions and five historical publication records. Combined target counts
include the independent current episode: 132 contents, 2,783 tasks and 1,018
Artifacts. All 11,956 selected source/baseline files were verified, with zero foreign
key violations. The 354 quarantine references cover 93 historical subjects and the
two explicitly paused current subjects. Historical publications remain history;
these records do not prove publication of the newly produced MPT episode.

A repeat import inserted zero rows and retained logical SHA-256
`915954ac60c6d7e3ab5f555c45138150d5b46747efe74b804eb913ce31ea5f66`.
Restore rehearsal independently reproduced the pre-import baseline (one content,
eight tasks, seven submissions, zero publications), with logical SHA-256
`20e5508b8a48045bd0f6eed6144efc9372df945582778235a876350c4052a6f6`.

Root compared every frozen field of both paused task rows against the source
business snapshot: each remained PENDING and identical. Their task audits contain
paused, unverified quarantine; no ownership, handled or consumed receipt was added.
The installed runtime `assert_allowed` gate rejected both task IDs with
`TaskConflictError` against this actual isolated target. The source and live MPT
databases and their running services were not switched. Every rehearsal report
continues to declare `cutover_authorized: false`; final fresh freeze, operating
configuration, complete runtime acceptance and new publication proof remain gates.


An additional HTTP smoke used a consistent SQLite backup of the real isolated
merged target, without initializing or starting maintenance. For both paused
contents, authenticated GET returned 200 and metadata-only task projections;
prepare, approve-script, render, approve-final and resume-acceptance each returned
409. Anonymous list access returned 401. Proof is in
`storage/smoke/migration-quarantine-http-20261010/proof.json`; the prepared target,
source and live MPT databases were not mutated by these HTTP calls.
