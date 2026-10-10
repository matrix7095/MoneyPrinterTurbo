# MPT deployment entrypoints

Windows uses `scripts/start-mpt-business.ps1` from the actual target checkout's
locked `.venv`. Defaults remain the independent `127.0.0.1:8010` instance.
Explicit Host/Port/DataDir and an allowlisted business-settings JSON support the
later main/LAN cutover without sharing source Glance data or credentials.
See [Windows startup and identity boundaries](mpt-business-startup.md) for exact
parameters, configuration fields and authenticated readiness/stop checks.
Managed files and their ancestors reject Windows reparse points on start,
resume and stop. Existing credential-file ACLs are restricted before reuse;
strict settings and installed Edge/ASR parameter validation precede creation.
The script does not stop an unrelated service occupying port 8000, manage
Windows services/firewall rules, start the separate Streamlit UI, or install
global Agent launchers/automation. Production cutover and publication still
require their own evidence and an authorized safe window.

## Docker

`docker-compose.yml` builds the default production image from `Dockerfile`.
`docker-compose.gpu.yml` overlays that entrypoint with `Dockerfile.gpu`. Both
install the root `requirements.txt` and its four editable workspace packages;
the default requirements exclude optional upstream model SDKs, and the default
Compose path leaves `MPT_UPSTREAM_COMPATIBILITY` unset.

`docker-compose.release.yml` pulls the published GHCR image. It is not a build
of the current checkout, so a mutable `latest` tag does not prove that it
contains this migration until the updated image has been published.

`docker-compose.claude.yml` is an explicit upstream MPT compatibility variant.
It builds `Dockerfile.claude` from the mutable GHCR `latest` base, installs this
checkout's `requirements.txt` and editable packages at `/MoneyPrinterTurbo`,
then adds the Claude Code CLI. Compose mounts the checkout at that same path and
sets `MPT_UPSTREAM_COMPATIBILITY=1`; the required OAuth token supplies the CLI
credential. The local requirements still exclude optional model SDKs, but the
only provider added by this image is `claude_code`; other providers that require
SDKs are outside this dependency closure. The inherited remote base may contain
packages or system state not covered by the default production image's model
SDK checks. Treat this as the explicit upstream provider compatibility path,
not as proof that the separate MPT business production deployment is running.
The mutable base is neither pinned nor proof that it contains the current
migration.
