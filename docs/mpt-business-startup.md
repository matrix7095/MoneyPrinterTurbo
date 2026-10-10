# MPT business service on Windows

Run from this checkout using PowerShell 7:

```powershell
& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -NoProfile -NonInteractive -File .\scripts\start-mpt-business.ps1
# Stop only this recorded process chain and verify port 8010 is free:
& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -NoProfile -NonInteractive -File .\scripts\start-mpt-business.ps1 -Stop
```

The default script uses the locked production `.venv`, binds `127.0.0.1:8010`, and sets
`MPT_BUSINESS_PUBLIC_BASE_URL` to that address. Provider SDK installation fails
its production dependency check. It imports the MPT entrypoint and four editable
workspace packages to verify their paths stay inside this checkout. `--app-dir`
pins the server import directory. The independent SQLite database is
`storage/business-8010/database/app.db`; source Glance databases and port 8000
are not used or stopped by the default invocation.

Explicit `-Port`, `-Host` (alias for `BindAddress`) and `-DataDir` select a separate
instance. DataDir must be inside this checkout's `storage/`; redirected paths
and paths into source Glance are rejected. Its independent database remains
`<DataDir>/database/app.db`, with credentials/state under `<DataDir>/private`.
The default DataDir is `storage/business-<Port>`. The script derives its checkout
from its own location, so production must run the script from the intended main
checkout after its locked environment and migration data are ready.

For the later, separately authorized main cutover, select port 8000 and
`-Host 0.0.0.0` with an explicit independent DataDir. The script refuses a port
owned by any unrecorded process; it does not stop or replace old Glance. This
implementation slice did not start or stop production port 8000.

With `0.0.0.0`, authenticated readiness probes use `127.0.0.1:<Port>` and the
default public base URL is that loopback URL. To advertise LAN URLs, explicitly
set `-PublicBaseUrl http://<actual-local-IPv4>:<Port>`; the address must belong to
this Windows host. The script does not modify firewall rules. A specific bind
address requires the public URL to match that address. The separate 8510 WebUI
and host scheduler deployment are not started by this script.

`storage/business-8010/private/runtime.json` contains independently generated
admin/agent tokens. The private directory and the credential file are restricted
to the current Windows user and SYSTEM. Existing explicit file permissions are
removed before credentials are read or reused, including resume and stop.
The directory is Git-ignored. Never print or commit the tokens. Restart keeps
the same tokens and database. `runtime-public.json` records validated non-secret
configuration and module paths, including the actual effective ASR/TTS/Handsfree
fields. API static files come from `resource/public`;
the separate Streamlit WebUI source is `webui/Main.py`.

Windows venv Python launches a second base-Python process. `server.json` records
both identities: PID, creation time, executable, command line, parent PID, and
workspace, bind address, port and DataDir. The listener must belong to this verified chain. Startup waits up to
30 seconds for the selected listener plus authenticated `GET /api/v1/contents`
returning JSON/200 and anonymous access returning 401. Merely launching Python
does not pass startup. Stop targets the listener first, then the launcher, and
checks both processes and the listener disappear within 10 seconds. Recycled or
changed identities and unrecorded listeners fail closed.

Before reading identities or credentials, changing permissions, writing managed
files or launching processes, the script rejects reparse points on managed paths
and their ancestors. This includes the private directory, credentials, process
state, public provenance proof, both redirected logs, the database directory,
SQLite databases and their journal/WAL/SHM sidecars. These checks also apply to
resume and stop; neither may follow a redirected state file to acquire authority.

Pass the same Host/Port/DataDir for resume or stop. Resume reuses the recorded
processes only when the effective options/public URL match their saved snapshot;
changed options require a deliberate stop and restart. Stop verifies the exact
recorded identity before targeting any PID and verifies the selected port is
free afterward. Legacy 8010 records remain usable only with the original default
isolated address/data directory; no production instance can adopt them.

## Explicit business settings

`-BusinessSettingsFile` reads one JSON object inside this MPT checkout; redirected
paths or paths into another checkout are rejected before reading. Only the following fields are accepted:

- `asr_model`, `asr_device`, `asr_compute_type`
- `tts_provider` (currently `edge`), `tts_voice`, `tts_rate`, `tts_volume`
- `handsfree_enabled`, `handsfree_auto_topic_enabled`, `handsfree_auto_review_enabled`, `handsfree_auto_publish_douyin_enabled`
- `handsfree_min_candidate_score`, `handsfree_daily_publish_limit`, `handsfree_publish_interval_minutes`
- `bgm_enabled`, `bgm_track_id`, `bgm_volume`

Unknown/private fields, token/database/path overrides, strings in place of booleans,
booleans in place of integers, and values outside the Settings constraints fail
before credential creation or launch. Installed Edge TTS constructors validate
voice/rate/volume without a network request. ASR accepts the installed
faster-whisper model names, an existing local model directory containing
`model.bin` and `config.json`, or an empty model to explicitly disable ASR.
Devices are `cpu`, `cuda` or `auto`; compute types must be supported by the
installed CTranslate2 runtime on the selected device, or its `auto`/`default`
selectors. This checks configuration capability without loading a model or
claiming audio evidence. Diagnostics contain field names and error types only.
No arbitrary environment mapping is accepted. Inherited
`MPT_BUSINESS_*` values are cleared during startup and restored in the caller
afterward; they cannot silently enable Handsfree or redirect storage. Default
Handsfree remains false. Root prepares any production Handsfree configuration
only after the maintenance implementation and release gates pass.

Example for an independent manual instance:

```json
{"handsfree_enabled": false, "asr_model": "medium", "asr_device": "cpu", "asr_compute_type": "int8", "tts_voice": "zh-CN-YunxiNeural", "tts_rate": "+20%"}
```

Tokens stay exclusively in `private/runtime.json`, which accepts only the two
independent token fields. Do not transfer source credentials. Selecting a BGM
track does not establish its license; use only an independently migrated,
verified catalog.

The default local ASR is `medium`, `cpu`, `int8`, and default Edge TTS uses
`zh-CN-YunxiNeural` at `+20%`. An explicit allowlisted configuration may change
them; the script strictly validates it and proves actual effective values through
MPT's settings factory before starting. ASR model loading and audio-quality evidence are later pipeline gates;
service readiness does not prove those gates, video generation or publication.
There is no Hugging Face fake/offline opt-in or transfer of old service secrets.

## Validation of this delivery

`test/business/test_business_startup.py`: 29 tests passed with the current locked
Windows environment. An independent ephemeral-port service proved authenticated
200/anonymous 401, actual settings/module/data provenance, resumption of the
same recorded process, refusal after creation-time tampering, exact stop and
port release. Other tests proved unowned listener rejection, an actual junction
to a temporary external directory rejected before credential creation, outside
configuration rejection, and strict/allowlisted JSON failures without exposing
values. Real private junctions and individual proof/state/credential/log/database
file links were rejected before outside writes. A running service retained its
listener while resume/stop rejected a redirected identity file. Existing
credentials with an explicit Everyone-read permission were restricted to the
current SID and SYSTEM on start and again on resume. Invalid Edge parameters and
ASR model/device/compute types failed before credential creation.
Tests did not start or stop ports 8000, 8010 or 8510 or enable Handsfree.
Independent Sol verification on 2026-10-10 repeated all 29 tests successfully
and rejected 16 start/stop attempts across eight dangling managed-file links,
including SQLite sidecars. A separate real temporary service proved that Stop
removed an explicit Everyone-read credential permission without changing its
bytes and released its port. Evidence is recorded in
`storage/startup-independent-recheck-20261010.log` and
`storage/startup-independent-extra-proof-20261010.json`.
Production cutover, external LAN reachability, actual Agent deployment and
publication still need their own acceptance evidence.
