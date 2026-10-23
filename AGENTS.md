# Local task files and cleanup

These instructions supplement the inherited workspace instructions.

- Keep render and diagnostic logs in the task's own output directory with explicit names. Do not use `GetTempFileName()` merely to discard subprocess stdout or stderr; capture the output or retain named task logs.
- Before cleanup, verify each exact absolute path belongs to the intended task, is the expected file type and size, and is not a reparse point. For ordinary files, use PowerShell 7 `Remove-Item -LiteralPath` without `-Force` or `-Recurse`, then verify absence. This is a cleanup method, not authorization to delete unrelated data.
- If a cleanup guard checks `CreationTimeUtc` or `LastWriteTimeUtc`, compare it with UTC timestamps. Do not append `Z` to a local wall-clock time.
- Distinguish a pre-execution `CreateProcess ... Rejected ... blocked by policy` from a running command's filesystem permission error. Preserve the exact error; do not label it Auto-review without evidence. Check the relevant command rules before changing configuration. Do not conceal a rejected operation through another shell or deletion API, or weaken global safeguards.
