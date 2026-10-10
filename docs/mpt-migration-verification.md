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
No business self-test task or synthetic completion was created. At that point,
publish and video roles had not been registered; capability declarations are not evidence
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

A read-only comparison with the original Glance publication table matched its
latest external ID to that configured account. All four nonempty historical
source IDs appear in this observed manager response. The fifth source record,
dated 2026-08-17, has neither an external ID nor a URL and has not received fresh
external verification. The comparison and missing-evidence record are saved as
`account-binding-proof.json` and `historical-unmatched-record.json` beside the
preflight evidence. No source record
was changed, and a database PUBLISHED status alone is not external proof.

After that actual preflight, fresh launchers were staged under
`storage/agent-launcher-staging/publish-preflight-20261010/` with the five
demonstrated publish capabilities. Registration authenticated the publish role
and returned VERIFIED without findings, using GPT-6.1 Sol/high and host
capacity two with the same two supported models. Executable `selftest` then
returned READY, valid launcher/skill and pending count zero. The actual
registration and selftest reports are saved in that staging directory. This
raises the registered role count to seven; the video-generation role remains
unregistered because native video generation is unavailable. No publish task,
business selftest, publish intent or external mutation was created by these
deployment checks.

## Remaining delivery gates

The new IKEA episode has approved research, script, voice and visual plan.
The user explicitly authorized local image enlargement to 1080 by 1920. The
second image attempt retained ten native 941 by 1672 outputs and three enlarged
1080 by 1920 files with source/final hashes and actual prompts. The last two
segments still failed the frozen matching-stool geometry requirements, so the
task was truthfully failed with `GENERATED_IMAGE_CONSTRAINT_MISMATCH` and
`retryable=false`. No generated image Artifact was uploaded and no result was
submitted. The terminal fail cleaned its temporary task workspace. Evidence was
recovered from the original tool records into the durable directory
`storage/smoke/native-image-constraint-failure-20261010/`: ten raw files and exact
prompts were preserved, and all three recreated enlargement hashes match their
original records. The terminal-failure recovery handoff passed isolated
acceptance and was used live to create a new visual-plan revision. Its
independent plan QC is pending; the original failed input remains unchanged.
Asset/final QC, rendering, publish review, real external publication ID/URL
and screenshot evidence remain required.

The later actual upstream merge rehearsal and its limits are recorded in
`upstream-sync-log.md`. Final fresh data and configuration transfer, production
release/restart, and external LAN checks remain. This checkpoint does not
satisfy those gates.

## Accepted launcher endpoint and proxy boundary

The launcher now explicitly binds a canonical numeric-loopback HTTP endpoint
at install time, so a final 8000 deployment can use its own credentials instead
of silently contacting 8010. Legacy manifests remain 8010. Endpoint validation
precedes credential reads and validates an entire registration batch first.
MPT launcher execution bypasses inherited and Windows registry proxies;
registration uses a transport with `trust_env=False`. The portable client's
existing default remains compatible.

Focused launcher tests passed 46 on each Python 3.11, 3.12 and 3.13. An
independent review passed 78 focused tests and repeated actual isolated HTTP
proxy negative/direct-only controls using synthetic credentials. Root's fresh
full Python 3.12 business suite passed 343 in 186.21 seconds, recorded in
`storage/business-launcher-endpoint-final-20261010.log`. Proxy evidence is
`storage/endpoint-independent-proxy-fixed-proof.json`. These checks did not
contact production or trigger a publish.

## Accepted terminal-failure recovery

The fail hook preserves the committed failure and separately admits a core
Recovery task. An authenticated admin retry uses the same admission path for
failures recorded before this hook existed. Creation and consumption require
current owned frozen inputs, accepted parent receipts, unchanged native
deployment, current revisions and reconciled content publication intents.
Queued replacements and UNKNOWN or malformed publication intents block repair.
Image constraint failure can target a new visual-plan revision only when the
full frozen script, plan and requests bind to the current accepted lineage.
The original failure and accepted script/voice are preserved.

All 58 new tests passed on Python 3.11, 3.12 and 3.13. Independent acceptance
passed 109 tests plus five isolated runtime adversarial cases; source hashes
still match the reviewed files. The tests exercised assistant HTTP completion
through core visual repair, marker-write replay, concurrent retries, rollback,
ordinary QC recovery after an earlier transient failure, and scene-analysis
lists with separate batch revisions. Ruff, AST and diff checks passed.
Root's fresh full Python 3.12 business suite passed 401 in 207.01 seconds;
the log is `storage/business-failure-recovery-final-20261010.log`.
Evidence is in `storage/failure-recovery-independent-final.log`,
`storage/failure-recovery-independent-adversarial-proof.json` and
`storage/failure-recovery-independent-source-hashes.json`. These were isolated
checks; they did not mutate live services or establish publication success.

The accepted code `b18aeee59a688e36b8e0013346674d9d0c033f9e` then restarted the
independent 8010 service: listener PID 22564, launcher PID 4556 and authenticated
/anonymous content reads 200/401. The original 8000 and 8510 listeners remained
PID 19020 and 18684. Admin retry admitted `agt_740daeea825b4f2fb06fe963c7a6cda9`;
the native assistant returned VALID and COMPLETED/CONSUMED. Core created the new
visual plan `agt_48c51962d064498493df1466cf64ee76`, which also returned
COMPLETED/CONSUMED. Independent plan QC `agt_677846b767824d99bae6b65bbc84524e`
then passed with zero errors and four warnings. The failed source task remains
FAILED with its original frozen hash, and the approved narration still has SHA
`8050b68f644208e5fa59ec2225f71969cc24f5a6cfdb9e76dfed432e48bda5dd`.
Actual proofs are under `storage/smoke/image-failure-recovery-20261010/`;
assistant and visual results are separately preserved under
`storage/smoke/episode-recovery-plan-20261010/` and
`storage/smoke/episode-visual-repair-20261010/`. This is recovery progress,
not final acceptance or publication evidence.

Fresh image task `agt_792007f708534648bd88ef87e8f299fc` generated five native
941-by-1672 images. The operator explicitly authorized local enlargement and
continuing independent QC/publication. Only Pillow LANCZOS resizing to
1080-by-1920 was applied locally; semantic revisions used the native image tool.
All five actual uploads returned Artifact IDs, and validation/submission returned
VALID and COMPLETED/CONSUMED. Root verified the physical hashes and dimensions
of all ten selected original/final files against the saved provenance.

Independent asset QC `agt_4f579e80493a47e19c83a80982a48d14` viewed all five
final PNGs and returned PASS, zero errors and three warnings; the server consumed
the result. Enlargement does not establish additional native detail. Final QC
must still inspect actual motion/cropping, subtitle safety and continuous AI
concept disclosure. The last two images show a single plant-display surface
from above, without pretending to prove stool hardware or assembly geometry.
Production evidence is under `storage/smoke/native-image-repaired-20261010/`;
independent evidence is under `storage/smoke/episode-asset-qc-20261010/`.
The real render was requested only after this consumed asset QC and returned
HTTP 200, WAITING_AGENT/content_qc. The final H.264/AAC video is 1080-by-1920,
31.766667 seconds, 4,733,696 bytes, with SHA-256
`cf46f0d3b29dc0b3deefaa451bf8e55ac6fdd3ae8a524e0b0a8b68f1083aec96`.
Technical QC passed. Actual final-track medium/CPU/int8 ASR and PCM evidence is
available; peak amplitude is 0.546997 and full-scale sample fraction is zero.
ASR contains product-name and Chinese homophone ambiguities; it is evidence for
independent assessment, not native-listening proof or automatic approval.
Root viewed the first and last extracted actual frames, including the visible
AI concept label. Evidence is in `storage/smoke/episode-render-20261010/`.

Final semantic QC has not been dispatched: maintenance exposed an existing
post-consumption mutation of the visual-plan result when asset QC binds images.
Its accepted submission and consumed-result hash remain preserved, but the
stored projected plan has a different hash. A consistent private database backup
and the original render/acceptance/audio evidence were retained before repair.
This state boundary must be repaired without rewriting frozen inputs or treating
the old pending acceptance subject as a new one. Final acceptance, publication
review and a new external publication ID remain outstanding.

## Immutable visual-plan repair acceptance, 2026-10-10

The repair keeps accepted results immutable and resolves prepared plans and
QC-approved image bindings separately for rendering and acceptance. The narrow
admin recovery preserves old submissions, receipts and pending-task inputs,
then creates a new acceptance subject and QC offer for the unchanged final video.
It rejects stale/missing parent proof, active content tasks/offers, and unresolved
publication intents. Reused generated images require the same origin proof.

Independent review initially rejected five groups of missing guards. Seven
isolated adversarial cases now reject; normal first consumption and historical
portable context remain valid, including a fresh independent publication process.
Final review approved six fixed files with matching before/after physical SHA.
Its fresh focused run passed 49 tests. The 48 new materialization/recovery tests
also passed on Python 3.11 (16.34 seconds) and 3.13 (16.32 seconds); Python 3.12
is covered by that independent run. Ruff, syntax compilation and diff checks
passed. Evidence is `storage/visual-projection-independent-final-v2-*` and
`storage/visual-projection-py{311,313}-final.log`.

The earlier full business run passed 448 before the last first-generation
consumption case was added. Root's fresh full run of the exact approved revision
passed 449 tests in 221.11 seconds; the log is
`storage/business-visual-projection-exact-final-20261010.log`. These code checks
do not establish live restoration, final QC approval or external publication.

The exact approved repair was committed as `7d7bea0` and deployed only to the
isolated 8010 runtime. Authenticated restoration returned 200; anonymous access
returned 401. The original visual-plan result again matches its accepted and
consumed hash. The old, unexecuted QC was cancelled with its input hash unchanged;
a separate receipt and QC `agt_ba00d61131904cd084906a5266fc723d` were created.
The physical final-video and original voice hashes remained unchanged. The source
8000 service and 8510 UI were retained. Actual restoration proof is under
`storage/smoke/visual-plan-restoration-20261010/`.

That fresh independent CONTENT_QC completed and was consumed with `passed=false`.
It reviewed all 15 frozen frames plus 64 frames across the full timeline, decoded
all 953 video frames, and reproduced the frozen final-track PCM hash. Seven hard
checks passed; audio and subtitle consistency did not. The product-name reading
at 6.10–7.56 seconds had low ASR confidence without independent disambiguation.
This is an evidence gap, not proof of a physical mispronunciation. The original
failed result and complete review evidence are retained under
`storage/smoke/episode-final-qc-20261010/`.

A separate verifier ran cached small-model full-track ASR and small/medium-model
English recognition of the brand interval, without script, subtitles, initial
prompt or hotwords. Neither model resolved the product name reliably. Both the
final video and newly decoded PCM hashes match the frozen QC input. No native
listening or successful publication is claimed. Independent evidence is under
`storage/smoke/episode-audio-disambiguation-20261010/`. A new script/voice revision
through the existing budgeted recovery and QC chain remains necessary.

The render-history repair received independent approval for four fixed files.
Initial review reproduced two real interruption failures: a partial canonical
file after interrupted copying, and an old request receipt paired with another
current VideoProject revision. Atomic same-filesystem promotion and an explicit
revision binding closed both. Independent real process termination before/after
promotion leaves either no canonical file or complete recoverable bytes. The
red/blue revision-mismatch reproduction now returns the requested red picture.
The final independent run passed 31 tests without skips, including actual MPT
rendering, complete video decoding, subtitles, audio, review frames and archive
checks; before/after hashes of all four reviewed files matched. Evidence is under
`storage/render-media-independent-review/`.

Root also ran all 16 new media/archive tests on Python 3.11 and 3.13: both passed
without skips (6.36 and 7.03 seconds). The native QC Recovery and revised receipt
callers separately passed 75 new tests on both versions and 253 related tests on
Python 3.12. Final independent caller/Recovery review approved all five fixed
files after reproducing and fixing the optional null recovery target. Its 135
tests and actual native claim/confirm/complete/consume probe passed. Root then
passed the exact final full business suite: 542 tests, 2903 warnings, 259.22
seconds, with all nine reviewed file hashes unchanged. Evidence is in
`storage/content-qc-recovery-independent-review-final.md` and
`storage/business-qc-recovery-render-exact-final-v2-20261010.log`. These checks
do not approve the failed video or prove a new publication.


The real content-QC recovery admission returned HTTP 200 and created
`agt_49fa345654e34ea3a0a7d71fc69f86c1`. Its independent assistant completed and
was consumed through the budgeted core path, creating fresh owned SHORT_SCRIPT
`agt_e69f733fc3964ae3af1dd365871830c9`. The independent script changed narration
to the verified Chinese brand/category while retaining the full formal identity
in metadata, c1/c3/c5, conceptual-image disclosure and the five registered images.
Original failed CONTENT_QC input/result and original final-video hash remain
unchanged. Public evidence is in `storage/smoke/content-qc-budget-recovery-20261010/`
and `storage/smoke/episode-clear-brand-script-20261010/`.

The new independent manual SCRIPT_QC `agt_66fb3d848acf4583ae30e90542b5d443`
correctly returned false under its frozen rule requiring the foreign formal name
in narration; it was accepted/consumed and created budgeted Recovery
`agt_ea720de7683848a2898880e74be20c58`. The readable-identity policy already
permits verified category narration plus exact full identity in opening and at
least one title, but was only appended for Handsfree. This manual/Handsfree
contract mismatch requires sharing that existing policy across the same five
stages. No historical QC result was changed and no new video or publication was
approved. Evidence is in `storage/smoke/episode-clear-brand-script-qc-20261010/`.


The shared pronunciation/identity repair extracts the existing policy unchanged
from the Handsfree fact block and applies it to the same five stages in both modes.
It does not enable Handsfree or rewrite any completed task. Its 97 new tests pass
on Python 3.11 and 3.13 (9.79/9.96 seconds). Root's exact final business regression
passes 639 tests (2938 warnings, 271.25 seconds), with both fixed source hashes
unchanged. Logs are `storage/business-pronunciation-exact-final-20261010.log` and
`storage/pronunciation-py311-final-20261010.log` /
`storage/pronunciation-py313-final-20261010.log`. Independent code acceptance
approved both fixed files: 97 new cases, 175 related cases and 504 resolver
equivalence cases, plus real leased HTTP immutable/quarantine probes. The report
is `storage/manual-pronunciation-independent-review-final.md`. Root owns the
separate deployment, fresh budgeted repair and actual publication gates.

Revision review deduplication now uses the existing complete input/contract hash
at the three migrated script-review and package call sites. Nine regression
cases reproduce old pending/leased/completed task reuse before the fix and pass
after it, preserving old rows and identical-input deduplication. Independent
acceptance reports zero issues with 111 distinct connected/native cases in
`storage/revision-review-dedup-independent-review.md`. Root's exact business
suite passes 648 tests (2938 existing warnings, 281.43 seconds), recorded in
`storage/business-review-dedup-exact-final-20261010.log`. The nine new cases also
pass on Python 3.11 and 3.13, together with two actual-render cases.
The existing filtered maintenance domain scan created current script review
`agt_bb03b32fbd574c84974f02f6f7db4cfc`; it did not replay its consumed QC or
alter the 20 previous task rows. A repeat scan created no additional task.
Public recovery evidence is in
`storage/smoke/episode-current-review-recovery-20261010/`. These checks do not
approve final media or establish a new publication.

The visual-image transport repair independently passes 248 connected cases and
45 cases after the controller resolution was hoisted outside its manifest loop.
Real loopback TCP probes download all five PNGs for current and prepared-plan
shapes, enforce authentication/lease/root/size/hash boundaries, and preserve
frozen input and previous result bytes. Resolution falls from seven whole-image
passes to three per five-image bundle. The 26 new cases pass on Python 3.11 and
3.13. Evidence: `storage/visual-context-transport-independent-review.md` and
`storage/visual-context-py311-20261010.log` /
`storage/visual-context-py313-20261010.log`. Live plan QC still requires a fresh
leased attempt with the actual supplied pixels; transport checks are not image
or publication approval.
A full transport regression exposed a real equal-timestamp recovery bug:
`new_qc` intermittently selected the old QC by random UUID order (673 other
cases passed). Read-only persisted-state diagnosis is recorded in
`storage/qc-revision-failure-diagnosis.md`. The shared current-version guard now
rejects ambiguous same-time siblings after the existing scene-batch filter.
Deterministic pre-fix evidence has seven failing tie cases; the repaired QC and
failure suites pass 139 tests. The 13 new cases also pass on Python 3.11 and 3.13.
Independent acceptance passes 187 connected cases and real TCP API probes:
both ID directions and pending/completed siblings return 409 with persisted
state and content bytes unchanged; strictly older/cancelled siblings retain
idempotent recovery. Reports: `storage/qc-revision-tie-independent-review.md`
and `storage/qc-revision-tie-{py311,py313}-20261010.log`. No historical task
input, timestamp, result or budget was rewritten.
Root's exact final combined business suite passes 687 tests (3120 warnings,
298.30 seconds) with all six accepted source hashes unchanged. Log:
`storage/business-visual-context-tie-exact-final-20261010.log`. This closes the
code regression gate; deployment and fresh actual-media publication are separate.

Manual reuse of the five owned AI images exposed a missing fresh current-plan
asset-QC task when `original_reuse` was absent or false. The shared factory now
uses the existing full-QC branch for these owned plans. Independent acceptance
passes 143 connected tests and actual TCP plan-QC to asset-QC consumption with
five decoded, hash-verified PNGs. Historical inputs/results remain unchanged;
denials preserve business state, while corrupt bundle rejection retains its
existing `bundle_served` audit side effect. Mixed source/generated reuse remains
fail closed. Report: `storage/manual-reused-asset-qc-independent-review.md`.
Thirteen selected cases pass on Python 3.11 and 3.13. Root's frozen final full
business suite passes 703 tests (3120 warnings, 295.55 seconds), recorded in
`storage/business-manual-reused-asset-qc-exact-final-20261010.log`.

## Actual publication and production cutover (2026-10-10)

The new IKEA KOMPISHÄNG episode, content
`content_92b4df321fcf4212ae8adbc435386acf`, passed fresh script, visual-plan,
asset and final-media QC before the existing operator publish gate. The user
explicitly authorized local upscaling of the newly generated 941×1672 concept
images to 1080×1920; the five resulting images received independent full QC.
The final 31.566667-second 1080×1920 H.264/AAC file is 4,669,000 bytes with SHA-256
`46e54530013990f44603e5bdfe6887009976c661b345f92c6be3eb7e6e69d3e3`.

Actual Douyin publication is **7695010253280234779**:
https://www.douyin.com/video/7695010253280234779. The platform creation time is
2026-10-10 20:09:57 Asia/Shanghai. Creator management, the public video page,
the complete description, AI disclosure and the saved platform cover were
independently checked. The core publication record
`pub_60a1ee1557b742b98851ea4e4d7dbba4` remains PUBLISHED with its original
recording timestamp; it was not rewritten to imitate platform time.
Task `agt_335a8ceffcf04585afbfe5428350b6a3` is COMPLETED and CONSUMED.
Evidence artifact `asset_c4fcf991b4624f1c9d7468caf10fae1c` has SHA-256
`df3e261c921b6b0be5ea5c4505e91ab3628e361ce765813b110e12fbc271f4b5`.
Public proof is retained in the migration worktree's
`storage/smoke/episode-real-publish-20261010/`, including `final-cover-readonly/`.
There was one final publish click, with no retry after an unknown outcome.

The installed social-auto-upload helper inserts tags but ignores the supplied
description, and its default 3:4 cover crop clips this 9:16 cover title. Its real
pre-click CLI failure permitted the existing browser fallback. The final live
page was checked for the complete description and platform AI declaration;
the crop was adjusted with the normal UI and its saved result verified after
publication. These helper limitations remain: an unattended CLI success must
not replace checking the actual description, declaration and saved cover.
Neither the helper source nor business receipts were patched to fabricate
success. The images and cover are disclosed as AI concepts, not product photos.

The main checkout is `C:\Users\Administrator\Documents\MoneyPrinterTurbo`.
The production API binds `0.0.0.0:8000`, with public URL
`http://10.10.10.200:8000` and independent data directory
`storage/business-8000/data`. Actual imports resolve app, agent, collector, core
and media modules inside this checkout. LAN requests to the business API return
401 anonymously and 200 with the protected administrator credential. Seven
native roles were registered and each returned READY from its actual launcher
self-test. Scheduler `mpt-codex` has capacity 2: discovery uses GPT-6 Luna/max;
quality and publishing use GPT-6.1 Sol/high. Unsupported native video generation
is disabled. Handsfree is enabled with a daily publication limit of 1; ASR is
medium/cpu/int8 and TTS is Edge Yunxi (+20%, +0%). Private credentials and
original Glance runtime configuration are not committed.

The frozen source plus the newly published MPT baseline imported 132 contents,
6 publication records and 12,025 referenced files (4,894,428,172 bytes). Every
reference was checked for size and SHA-256. Repeating the import inserted zero
rows in all 14 imported tables. SQLite integrity and foreign keys passed; the
restore rehearsal reproduced the frozen rollback database. The 356 quarantine
items and four explicitly frozen historical PENDING tasks remain preserved,
without current OWNED credentials or replay eligibility. Existing incomplete
historical publication IDs were retained, not invented.

Both archive Git histories are retained at production archive merge
`a88d92b51a55552b89ebc22d7e2f023f23e4d35b`, with parents
`d0b1d2ab26201b5efd640423f334662bd3b61973` (new MPT publication baseline) and
`8aa8c61f675f024610cd9b211a6780041f902e25` (Glance). Strict Git fsck, both
ancestry checks, 127 original object records and all 11 referenced archive
commits passed. The combined index preserves source bytes followed by baseline
bytes. Original absolute audit paths still refer to the preserved Glance backup.

Glance's six verified owned server/worker processes were stopped. Its watchdog
is Disabled; the exported task XML and unchanged source database remain available
for rollback. The original checkout and migration worktree are retained because
they contain rollback material and publication evidence. The two existing app
automations now target MoneyPrinterTurbo with updated launcher/maintenance paths;
their schedules, models, notification preferences and auto-archive behavior were
preserved, and both remain PAUSED. Final cutover evidence and independent reports
are under the main checkout's `storage/smoke/final-cutover-20261010/`.
