# Upstream synchronization evidence

## 2026-10-10: migration rehearsal against a fresh upstream commit

- Fork baseline: `b819f7e1213ca2bc444cbf53ad3e0e3080c8a866`.
- Fixed fetched upstream: `7c80ca0b14407966a4fa3cc5bfc428868b186f5f`.
- Independent rehearsal started at
  `412de0c2dd54cc90f4a4136e3a386d1764a0b17a`, replayed the reviewed migration
  commits, and actually merged the fixed upstream SHA. This includes the real
  background-color change in b819f7e and the Redis ownership guard in 7c80ca0;
  it was not an Already-up-to-date result.
- Rehearsal merge: `07a5a48cf0d8f8409ab4fa2c8639e0b0a79ab98d`.
- Migration-branch merge: `f106108c45d5f2425dfb85af537d94f7fae3a2c8`.
  Both have identical tracked tree
  `47cd4df4953b0308e9696d27eec94e6fb7a78ad9`.
- No conflicts or dependency/lock changes. The merged tree differs from the
  previously verified migration tree only in `app/services/state.py` and the
  new Redis ownership test file.

The independent clean worktree repeated the complete business suite: 306 passed
on each of Python 3.11, 3.12 and 3.13. Its separate optional-SDK compatibility
environment ran the complete service suite: 2016 passed, 54 skipped, 10857
subtests passed. Default production installation contained no language-model
SDKs and all portable module paths resolved inside that worktree. Ruff passed.

Independent Sol verification covered 79 focused cases and 12 actual rendered
videos with red/blue hex and named colors, RGB/HSL inputs, and rectangular/round
backgrounds. It also exercised actual AgentClient model contracts and durable
publish-intent retry/UNKNOWN behavior. Logs, media and frame-pixel statistics
are under the rehearsal worktree's `storage/upstream-independent/`.

An isolated 8011 instance proved authenticated 200/anonymous 401, exact worktree
module/data paths, a persisted maintenance tick, a real restart with a different
PID and unchanged tick, then exact stop and port release. The source 8000,
working migration 8010 and UI 8510 instances were preserved. Network proof is
under `storage/upstream-network-smoke/`; the three complete business logs and
`upstream-services-full.log` are under `storage/`.

Limitations: 30 real Redis cases in the focused suite require an isolated Redis
server unavailable on this host; these are not claimed as validated. Other
external integration tests account for the remaining skips in the full suite.
The actual migration business uses SQLite. No Docker build, production cutover,
main fast-forward, push, new-video final QC or external Douyin publication is
claimed by this rehearsal. The retained source and staged-data backups remain
the rollback targets; production release still requires its separate gates.
