# Glance portable packages and MPT pipeline integration

## Provenance and scope

Imported on 2026-10-10 from the tracked files in
`C:\Users\Administrator\Documents\Glance` at commit
`369d4c109412c1cea1e6f4cb0be957af66b4d20c` (source package files were clean).
The initial three-package import contained 75 files; the approved deterministic-media
extension adds 14 files, for **89 tracked package files**. The unrelated source
untracked `=` file, credentials, cookies, `.env`, databases, generated media and
service/deployment files were excluded. Source inspection and tests did not cut over
or write the production Glance database.

Original distribution/import names and installable `src/` layouts are preserved:

| Distribution | Python import | Direct dependency boundary |
| --- | --- | --- |
| glance-core | glance_core | jsonschema, pydantic, pydantic-settings, sqlmodel |
| glance-agent | glance_agent | httpx, jsonschema, platformdirs, pydantic, pydantic-settings, rich, typer |
| glance-collector | glance_collector | glance-core, beautifulsoup4, feedparser, httpx |
| glance-media | glance_media | glance-core, edge-tts, faster-whisper, httpx, pysubs2, scenedetect, yt-dlp |

SQLAlchemy is supplied by sqlmodel. Core retains the complete dependency closure:
models, SQLite/settings/security, roles/capabilities, schemas/task service, workflow,
rights/acceptance, prompts, configuration, research/archive and scheduler profiles.
It does not import the old server, worker or media packages. Agent CLI role skills,
manifests and shared protocol resources are bundled unchanged. These four packages
contain no model-provider SDK; independent CPU ASR is a media quality dependency.
Root dependencies, lockfile and interpreter matrix remain owned by root integration.

No Glance project license file or package license declaration was found at the fixed
source commit. This import does not invent a license or assert MPT relicenses Glance.
The media font assets retain the original complete `fonts/OFL.txt` and both bundled
NotoSansSC OTF files, including their wheel artifact declarations.

## Recorded local differences

A fresh byte comparison on 2026-10-10 found exactly seven package paths differing
from the 89 source files; the other 82 package files retain source bytes:

- Four `pyproject.toml` files widen source `>=3.12,<3.13` to `>=3.11`, preserving
  MPT's supported range. UTC/StrEnum need Python 3.11; no compatibility shims were added.
- `core/src/glance_core/workflow.py`: publish `account_name` changes from `default`
  to `local-default`, matching the configured SAU session filename. `account_id`
  remains `local-default`; publish intent identity and historical dedup are unchanged.
- `core/src/glance_core/task_service.py`: retain only the accepted attempt's lease
  token hash after completion and verify it before any successful idempotent retry.
  Wrong token/worker cannot replay accepted consumption; COMPLETED still cannot
  heartbeat, obtain bundles or upload. Plain tokens are never stored. Historical
  completed rows without a retained hash cannot use this new retry path.
- `core/src/glance_core/content_acceptance.py`: explicit human
  `manual_candidate_selected` audit grants manual production ownership separately
  from the original Handsfree ownership audit. Acceptance origin must match actual
  ownership; identical hash/lineage/file/evidence checks apply. Manual content never
  receives a fabricated Handsfree audit or automatic media/publish authorization.

Carry each local difference and its regression tests into future Glance updates.
Do not overwrite these paths wholesale from a newer source tree.

## Application pipeline and rendering boundary

`app/business/pipeline.py` is adapted from the same commit's tracked
`apps/worker/src/glance_worker/pipeline.py`, preserving mature acquisition, scene
analysis, timing, TTS, generated-asset provenance, recovery and packaging functions.
No Huey worker, old HTTP server or old deployment runtime was imported. The plan's
original no-media-import phase boundary was deliberately expanded to retain selected
portable media helpers and this plain-function pipeline; root migration planning must
reflect this approved scope change.

Its MPT-local changes are explicit:

- `SUPPORTED_TASK_TYPES` enumerates installed consumers. Retained schemas alone do
  not authorize execution; `ASSET_GENERATE_VIDEO` has no installed consumer.
- Rights and latest passed QC, exact upstream task IDs and immutable input hashes
  are gates before TTS/render/package/publish. Manual failed QC no longer becomes
  a warning followed by approval. Publication requires schema v2, source context v1
  and independent final-audio evidence v1, bound to the latest actual final-video hash.
  The legacy optional semantic-QC switch cannot bypass the new release boundary.
- Both explicit manual selection and Handsfree render issue genuine v2 acceptance;
  absent ASR/signal evidence leaves `audio_evidence_waiting`. Receipt-based resume
  verifies artifact size/hash/current subject and never substitutes a synthetic pass.
  Manual review remains explicit; automatic voice/render/package followups require
  real Handsfree ownership and effective auto-review authorization.
- Accepted-task replay preserves later persisted media stages. Runtime centrally owns
  the per-content process lock and the final consumption marker; pipeline returns
  `voice`, `render`, `package` or `prepare` only when a real next action is pending.
  Pipeline functions open their own sessions: never wrap them in a long outer SQLite
  write transaction. A crash inside unfinished IO can rerun that unfinished operation;
  the completed-marker and stage checks prevent routine repeated completed work.
- Publish metadata completion schedules review even when content is already PACKAGED;
  actual publish results go through original intent/evidence/external-ID reconciliation.
  UNKNOWN/upload completion never becomes PUBLISHED.

`app/business/media_executor.py` subclasses portable media helpers but overrides
`render`; it **does not call the old Glance renderer**. It validates approved source
intervals and exact continuous narration timing, prepares local clips/audio and original
ASS subtitle styles (including AI-concept disclosure), then uses MPT's actual
`VideoProject.create_revision/render` and `video.generate_video`. The frozen manifest,
asset hashes, stage/cache status, local logs and output promotion remain real MPT data.
It compares prepared-file hashes before reusing a revision; replacement assets cannot
silently reuse an old render. MoviePy adds the full original voice and approved BGM;
requested BGM mix failure blocks promotion. The validated final and preview live under
content `render/`; downstream QC reads these actual files.

Preparing clips, MPT scene rendering and MPT final audio/BGM composition introduce
additional encoding passes (at least three for final footage), plus preview encoding.
This is a correctness integration, not a speed/quality-loss improvement claim. Future
optimization must preserve exact RenderPlan timing, subtitles, audio and manifests.
MPT's randomized legacy combine path is unsuitable for this exact approved timeline.

One minimal upstream MPT fix is required in `app/services/video_project.py`: open a
finished artifact `r+b` before `os.fsync`. Windows CRT rejects fsync on the old read-only
`rb` descriptor with EBADF. The two other fsync sites already use writable descriptors.
Keep this bounded upstream touch separate from portable-package synchronization.

## Persistence and HTTP contracts

- Explicit isolated database/data paths and `_env_file=None` are required for tests.
  `create_db` writes/seeds/backfills/cancels legacy tasks: never treat it as read-only
  inspection of the live Glance database. SQLite enables WAL, foreign keys and timeout.
- Task service owns role claims, token-hash leases, heartbeat, expiry/attempt bounds,
  validation/submission/audit persistence and authenticated accepted-result retry.
  HTTP must authenticate role and worker identity before invoking it.
- Collection polls skip an active direction; ingestion deduplicates external ID and
  normalized URL while preserving user rights/status. Per-item commits mean partial
  consumption can leave a prefix: serialized replay completes the rest without another
  candidate. A durable completed marker prevents ordinary re-ingestion/stat rewriting.
- The MPT HTTP integration owns authenticated manual-selection audit, leases, artifact
  upload/download hash checks, materialized bundles, owned-task recovery and consumption
  locks/markers. It does not automatically replay imported historical COMPLETED tasks.
- Bundles materialize current Artifact bytes through authenticated lease-context URLs;
  old signed task inputs can retain `/media/artifacts/...` identifiers without implying
  that an old Glance server/media endpoint was imported.
- Having schemas, local tests or a packaged video does not prove production cutover or
  actual publication. Publishing still requires authorized review, rights, QC, submit
  intent, verified external ID/URL and actual screenshot evidence.

## Verification evidence, 2026-10-10

- Four portable package source trees plus both MPT adapters/tests compile on local
  Python 3.11.15 and the installed target Python 3.12.13.
- New target `.venv`: **17 portable contracts passed**, including cross-engine SQLite
  persistence, role/token/expiry/schema gates, partial-ingest replay, accepted-result
  retry authentication and configured SAU account identity.
- New target `.venv`: **14 media/pipeline contracts passed**, covering false/mismatched/
  changed/not-latest QC rejection before TTS, legacy-QC publish refusal, explicit manual
  v2 ownership/origin/hash binding, later-stage replay and Handsfree followup actions.
- The actual 1.2-second MPT smoke renders two approved image scenes, never calls source
  `MediaExecutor.render`, fully decodes final and preview with FFmpeg, checks actual
  red/blue decoded frame order, final 1080x1920 dimensions and audio presence, and checks
  decoded AAC audio RMS and 440-Hz FFT peak against the prepared source tone. A second
  adapter render retains the same revision and all MPT stages report `reused=True`.
  This synthetic smoke is renderer evidence, not a new production video or publication.
- Existing `test/services/test_video_project.py`: **41 passed**, including real media,
  interruption/cancellation, cache corruption and storage lifecycle; combined with the
  earlier 12 media contracts, the run was **53 passed** before two extra narrow contracts.
- Root Ruff rules pass all four packages, adapters, contract tests and the bounded MPT
  fix. Source-rule exceptions stay restricted to imported paths; new adapters/tests do
  not receive source-code exclusions. Pysubs2 emits existing alignment deprecation
  warnings; these do not skip render or decode validation.
- Clean dependency/runtime matrix on 3.11 and 3.13 is separately owned by root and is
  still pending at this record. Syntax success is not claimed as runtime matrix proof.

Focused commands in the isolated target checkout:

```powershell
& .\.venv\Scripts\python.exe -m compileall -q packages/core/src packages/agent/src packages/collector/src packages/media/src app/business
& .\.venv\Scripts\python.exe -m pytest -q test/business/test_portable_contracts.py test/business/test_media_pipeline.py test/services/test_video_project.py
& .\.venv\Scripts\python.exe -m ruff check packages app/business/pipeline.py app/business/media_executor.py test/business/test_portable_contracts.py test/business/test_media_pipeline.py app/services/video_project.py
```

Compare future Glance changes against the fixed source SHA independently of MPT
upstream merges. Preserve original package notices and resources, keep application
routes/auth/consumption/render adaptation in `app/`, and keep root dependency changes
single-writer. Production database migration, actual new-video review and actual Douyin
publication remain separate release evidence; this document does not claim them done.
