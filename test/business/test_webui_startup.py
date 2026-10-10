"""Real isolated Windows Streamlit lifecycle and credential boundaries."""

import json
import os
import socket
import subprocess
import tempfile

import requests
import pytest

from test.business.test_business_startup import (
    PWSH, ROOT, free_port, invoke as business, isolated_directory, ps, quote,
)

SCRIPT = ROOT / "scripts/start-mpt-webui.ps1"


def invoke(port, data, api_port, token_file, *extra, env=None):
    assert port not in {8000, 8010, 8510}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
             "-Port", str(port), "-DataDir", str(data), "-ApiUrl", f"http://127.0.0.1:{api_port}",
             "-AdminTokenFile", str(token_file), *map(str, extra)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            timeout=90, check=False, env=env,
        )
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(result.args, result.returncode,
                                           stdout.read().decode("utf-8"), stderr.read().decode("utf-8"))


def test_start_resume_drift_live_unready_exited_recovery_and_stop():
    api, ui = isolated_directory(), isolated_directory()
    api_port, port = free_port(), free_port()
    credential = api / "private/runtime.json"
    state_path = ui / "private/webui.json"
    original = None
    assert business(api_port, api).returncode == 0
    try:
        env = {**os.environ, "MPT_BUSINESS_ADMIN_TOKEN": "inherited-secret-must-not-survive"}
        started = invoke(port, ui, api_port, credential, env=env)
        assert started.returncode == 0, started.stdout + started.stderr
        original = state_path.read_bytes()
        state = json.loads(original)
        assert state["worker"]["id"] > 0
        assert state["worker"]["parent_id"] == state["launcher"]["id"] or state["worker"] == state["launcher"]
        assert requests.get(f"http://127.0.0.1:{port}/_stcore/health", timeout=5).text == "ok"
        resumed = invoke(port, ui, api_port, credential)
        assert resumed.returncode == 0, resumed.stderr
        assert json.loads(state_path.read_bytes())["worker"]["id"] == state["worker"]["id"]
        for token in ("inherited-secret-must-not-survive", json.loads(credential.read_text())["admin_token"]):
            assert token not in original.decode() + started.stdout + started.stderr
            assert token not in "".join(p.read_text(encoding="utf-8") for p in ui.glob("*.log"))
        changed = {**state, "worker": {**state["worker"], "created_ticks": state["worker"]["created_ticks"] - 1}}
        state_path.write_text(json.dumps(changed), encoding="utf-8")
        for extra in ((), ("-Stop",)):
            rejected = invoke(port, ui, api_port, credential, *extra)
            assert rejected.returncode != 0 and "identity changed" in rejected.stderr
        changed = {**state, "worker": {**state["worker"], "id": "1 OR ProcessId > 0"}}
        state_path.write_text(json.dumps(changed), encoding="utf-8")
        rejected = invoke(port, ui, api_port, credential, "-Stop")
        assert rejected.returncode != 0 and "Invalid saved process ID" in rejected.stderr
        state_path.write_bytes(original)
        assert business(api_port, api, "-Stop").returncode == 0
        unready = invoke(port, ui, api_port, credential)
        assert unready.returncode != 0 and "running" in unready.stderr
        assert requests.get(f"http://127.0.0.1:{port}/_stcore/health", timeout=5).status_code == 200
        assert business(api_port, api).returncode == 0
        # Simulate an external exit, retaining identity state for automatic recovery.
        ps(f"""Stop-Process -Id {state['worker']['id']}
            try {{ Stop-Process -Id {state['launcher']['id']} -ErrorAction Stop }}
            catch {{ if ($_.CategoryInfo.Category -ne 'ObjectNotFound') {{ throw }} }}
            if (Get-Process -Id {state['worker']['id']},{state['launcher']['id']} -ErrorAction SilentlyContinue) {{ throw 'Test process did not exit' }}
        """)
        recovered = invoke(port, ui, api_port, credential)
        assert recovered.returncode == 0, recovered.stderr
        original = state_path.read_bytes()
        assert json.loads(original)["worker"]["created_ticks"] != state["worker"]["created_ticks"]
        assert invoke(port, ui, api_port, credential, "-Stop").returncode == 0
        original = None
        assert not state_path.exists()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        if original is not None:
            state_path.write_bytes(original)
            assert invoke(port, ui, api_port, credential, "-Stop").returncode == 0
        assert business(api_port, api, "-Stop").returncode == 0


def test_unowned_listener_rejected_without_creating_state():
    ui, api = isolated_directory(), isolated_directory()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        for extra in ((), ("-Stop",)):
            result = invoke(listener.getsockname()[1], ui, free_port(), api / "private/runtime.json", *extra)
            assert result.returncode != 0 and "process" in result.stderr
        assert not ui.exists()


def test_redirected_credentials_and_logs_rejected(tmp_path):
    api, ui = isolated_directory(), isolated_directory()
    (api / "private").mkdir(parents=True)
    credential = api / "private/runtime.json"
    target = tmp_path / "runtime.json"
    target.write_text('{"admin_token":"outside-secret"}', encoding="utf-8")
    ps(f"$null = New-Item -ItemType SymbolicLink -Path {quote(credential)} -Value {quote(target)}")
    try:
        result = invoke(free_port(), ui, free_port(), credential)
        assert result.returncode != 0 and "Redirected" in result.stderr
        assert "outside-secret" not in result.stdout + result.stderr
        assert not ui.exists()
    finally:
        ps(f"Remove-Item -LiteralPath {quote(credential)} -Force")
    ui.mkdir(parents=True)
    log = ui / "webui.stdout.log"
    ps(f"$null = New-Item -ItemType SymbolicLink -Path {quote(log)} -Value {quote(target)}")
    try:
        assert "Redirected" in invoke(free_port(), ui, free_port(), credential, "-Stop").stderr
        assert target.read_text(encoding="utf-8") == '{"admin_token":"outside-secret"}'
    finally:
        ps(f"Remove-Item -LiteralPath {quote(log)} -Force")


def test_credentials_must_belong_to_workspace_storage(tmp_path):
    result = invoke(free_port(), isolated_directory(), free_port(), tmp_path / "private/runtime.json")
    assert result.returncode != 0 and "storage" in result.stderr


def test_unprotected_credentials_rejected_without_launch_or_secret_output():
    api, ui = isolated_directory(), isolated_directory()
    credential = api / "private/runtime.json"
    credential.parent.mkdir(parents=True)
    credential.write_text('{"admin_token":"unprotected-secret"}', encoding="utf-8")
    result = invoke(free_port(), ui, free_port(), credential)
    assert result.returncode != 0 and "unsafe ACL" in result.stderr
    assert "unprotected-secret" not in result.stdout + result.stderr
    assert not ui.exists()


@pytest.mark.parametrize("relative", ["private", "private/webui.json", "webui.stdout.log", "webui.stderr.log"])
def test_all_ui_managed_links_rejected_before_start_and_stop(tmp_path, relative):
    ui, api = isolated_directory(), isolated_directory()
    link = ui / relative
    link.parent.mkdir(parents=True)
    directory = relative == "private"
    target = tmp_path if directory else tmp_path / "outside-file"
    kind = "Junction" if directory else "SymbolicLink"
    if not directory:
        target.write_text("outside-sentinel", encoding="utf-8")
    ps(f"$null = New-Item -ItemType {kind} -Path {quote(link)} -Value {quote(target)}")
    try:
        for extra in ((), ("-Stop",)):
            rejected = invoke(free_port(), ui, free_port(), api / "private/runtime.json", *extra)
            assert rejected.returncode != 0 and "Redirected" in rejected.stderr
        if directory:
            assert not list(tmp_path.iterdir())
        else:
            assert target.read_text(encoding="utf-8") == "outside-sentinel"
    finally:
        ps(f"Remove-Item -LiteralPath {quote(link)} -Force")
