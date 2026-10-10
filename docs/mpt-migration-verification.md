# Migration verification checkpoint, 2026-10-10

This checkpoint covers the migration worktree, not production cutover or a
successful Douyin publication. The source Glance process on port 8000 remains
unchanged. No new external publication has been verified.

## Code and isolated data checks

- Full `test/business`: 306 passed on Python 3.11, 3.12 and 3.13.
- Full `test/services` in the separate locked `upstream-providers`,
  `azure-speech`, `asr` compatibility environment: 2013 passed, 32 skipped,
  10857 subtests passed. External integration tests were disabled.
- The default production environment contains none of `openai`, `litellm`,
  `dashscope`, `google-genai`, or `anthropic`. Compatibility SDKs were not
  installed there.
- Ruff passed for business code, new controllers, portable packages, scripts
  and business tests. Independent startup and publish-policy reviews passed.
- Invalid persisted configuration now rejects authorization explicitly.
  Independent real-media/QCv2/HTTP reproduction returned 409 with zero new
  publish intents, including a saved automatic-publish opt-out plus invalid
  quota. Manual content never receives Handsfree authorization metadata.
- Actual isolated import, repeat-import idempotency, paused-task preservation,
  media hashes and restoration are recorded in `glance-data-migration.md`.
  That dataset remains quarantined staging with cutover authorization false.

Logs are under `storage/`: `mpt-business-failclosed-final-20261010.log`,
`mpt-business-final-py311-20261010.log`,
`mpt-business-final-py313-20261010.log`, and
`mpt-services-final-verification-20261010.log`.

## Actual staging restart and role registration

The reviewed startup script stopped the exact recorded 8010 process chain after
confirming no leased/submitted business tasks. Restart produced listener PID
9684 and launcher PID 17472. An authenticated content read returned 200 and an
anonymous read returned 401. Actual module/data/config paths and explicit
manual-mode settings are in `storage/smoke/live-8010-restart-20261010/proof.json`.
The maintenance tick was persisted after startup. The 8000 listener PID 19020
and 8510 UI listener PID 18684 remained unchanged.

Nine local launchers passed executable staging checks in
`storage/agent-launcher-staging/live-20261010-1253/`. Actual fresh whoami and
deployment requests returned VERIFIED for assistant, research, script, visual,
image and discovery. Host capacity is two; supported models are GPT-6.1 Sol and
GPT-6 Luna. Discovery uses Luna/max; the other registered roles use Sol/high.
No business self-test task or synthetic completion was created. Publish and
video roles have not been registered; capability declarations are not evidence
of actual generation or publication.

After the verified upstream merge, 8010 was restarted again from code commit
`f106108c45d5f2425dfb85af537d94f7fae3a2c8`: listener PID 14352, launcher PID
16452. Authentication remained 200/401, all six roles were freshly VERIFIED,
and the image task stayed PENDING with zero publications for the new episode.
The later proof is `storage/smoke/live-8010-upstream-20261010/proof.json`.

## Actual Douyin read-only evidence preflight

The existing SAU `local-default` account passed a fresh `douyin check`. A
read-only Chrome session using that account opened the actual content manager,
saved its screenshot, and correlated six visible descriptions with the
platform's observed `/janus/douyin/creator/pc/work_list` response. Both the
`items.id` and `aweme_list.aweme_id` must agree before recording an ID. No
upload, click, login challenge handling, or cookie write was performed; the
cookie file hash remained unchanged.

Proof, screenshot, visible text and whitelisted content IDs are under
`storage/smoke/douyin-readonly-preflight-20261010/`. The latest observed item
is the older Waydoo video, ID `7684644113731325235`, displayed as published on
2026-09-12 at 21:44. These historical items prove the read-only evidence path,
not a new publication. The probe consumes only the observed work-list response
and stores no cookies, request headers, authentication query strings or raw
API responses. For a future publish, uniquely match the frozen new description
and publication time, recheck its visible platform status, and upload that
specific screenshot as an Artifact. Missing or ambiguous evidence remains
UNKNOWN and never permits another upload or final click.

## Remaining delivery gates

The new IKEA episode has approved research, script, voice and visual plan, but
its image task remains pending. Two native image outputs were 941 by 1672,
below the frozen minimum of 1080 by 1920. Local image enlargement requires the
user's explicit exception to the image-editing tool rule; it has not been
performed. Asset/final QC, rendering, publish review, real external publication
ID/URL and screenshot evidence remain required.

The later actual upstream merge rehearsal and its limits are recorded in
`upstream-sync-log.md`. Final fresh data and configuration transfer, production
release/restart, and external LAN checks remain. This checkpoint does not
satisfy those gates.
