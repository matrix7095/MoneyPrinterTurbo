"""Real Windows launch/identity/port tests on independent ephemeral MPT instances."""

import json
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
PWSH = "C:/Program Files/PowerShell/7/pwsh.exe"
SCRIPT = ROOT / "scripts/start-mpt-business.ps1"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def invoke(port, data, *extra):
    assert port not in {8000, 8010, 8510}
    # Hidden Windows children can inherit pipe handles beyond pwsh's lifetime.
    # Files let us wait for the exact launcher without waiting for pipe EOF.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
             "-Port", str(port), "-DataDir", str(data), *map(str, extra)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            timeout=90, check=False,
        )
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(result.args, result.returncode,
                                           stdout.read().decode("utf-8"), stderr.read().decode("utf-8"))


def isolated_directory():
    return ROOT / "storage/startup-tests" / uuid.uuid4().hex


def test_actual_start_resume_identity_rejection_and_exact_stop():
    data, port = isolated_directory(), free_port()
    data.mkdir(parents=True)
    options = data / "business-settings.json"
    options.write_text(json.dumps({"asr_model": "small", "tts_rate": "+10%", "handsfree_enabled": False}), encoding="utf-8")
    args = ("-BusinessSettingsFile", options)
    original = None
    state_path = data / "private/server.json"
    try:
        started = invoke(port, data, *args)
        assert started.returncode == 0, started.stdout + started.stderr
        original = state_path.read_bytes()
        state = json.loads(original)
        assert state["port"] == port and state["data_dir"] == str(data)
        assert state["worker"]["id"] > 0 and state["worker"]["created_ticks"] >= state["launcher"]["created_ticks"]
        credentials = json.loads((data / "private/runtime.json").read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{port}/api/v1/contents"
        assert requests.get(url, timeout=5).status_code == 401
        assert requests.get(url, headers={"Authorization": f"Bearer {credentials['admin_token']}"}, timeout=5).status_code == 200
        proof = json.loads((data / "private/runtime-public.json").read_text(encoding="utf-8"))
        assert proof["business_settings"]["asr_model"] == "small"
        assert proof["business_settings"]["tts_rate"] == "+10%"
        assert proof["business_settings"]["handsfree_enabled"] is False
        assert proof["database_url"] == f"sqlite:///{data.as_posix()}/database/app.db"
        resumed = invoke(port, data, *args)
        assert resumed.returncode == 0, resumed.stderr
        assert json.loads(state_path.read_bytes())["worker"]["id"] == state["worker"]["id"]
        changed = {**state, "worker": {**state["worker"], "created_ticks": state["worker"]["created_ticks"] - 1}}
        state_path.write_text(json.dumps(changed), encoding="utf-8")
        rejected = invoke(port, data, "-Stop")
        assert rejected.returncode != 0
        assert "identity changed" in rejected.stderr
        assert requests.get(url, timeout=5).status_code == 401
        state_path.write_bytes(original)
        stopped = invoke(port, data, "-Stop")
        assert stopped.returncode == 0, stopped.stderr
        assert not state_path.exists()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        original = None
    finally:
        if original is not None:
            state_path.write_bytes(original)
            result = invoke(port, data, "-Stop")
            assert result.returncode == 0, result.stderr


def test_unowned_listener_conflict_does_not_stop_it_or_create_credentials():
    data = isolated_directory()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        for extra in ((), ("-Stop",)):
            result = invoke(port, data, *extra)
            assert result.returncode != 0
            assert "another process" in result.stderr or "unrecorded process" in result.stderr
            assert listener.getsockname()[1] == port
        assert not data.exists()


@pytest.mark.parametrize("settings", [
    {"admin_token": "forbidden-secret"}, {"unexpected": "forbidden-secret"},
    {"handsfree_enabled": "true"}, {"handsfree_daily_publish_limit": True},
    {"handsfree_daily_publish_limit": 21}, {"tts_provider": "model-provider"},
    {"tts_rate": "not-a-rate"}, {"asr_device": "not-a-device"},
    {"tts_volume": "not-a-volume"}, {"tts_voice": "not-a-voice"},
    {"tts_provider": "cosyvoice3"},
    {"tts_voice": "not-a-voice", "tts_provider": "kokoro"},
    {"kokoro_model_dir": "not-a-model", "tts_provider": "kokoro", "tts_voice": "kokoro:58"},
    {"tts_provider_endpoint": "http://127.0.0.1:8765", "tts_provider": "kokoro", "tts_voice": "kokoro:58"},
    {"asr_compute_type": "not-a-compute-type"}, {"asr_model": "not-a-model"},
])
def test_settings_allowlist_strict_types_and_ranges_fail_before_launch(settings):
    data = isolated_directory()
    data.mkdir(parents=True)
    path = data / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    result = invoke(free_port(), data, "-BusinessSettingsFile", path)
    try:
        assert result.returncode != 0
        assert not (data / "private/server.json").exists()
        assert not (data / "private/runtime.json").exists()
        assert "forbidden-secret" not in result.stdout + result.stderr
        assert not any(value in result.stdout + result.stderr for value in settings.values()
                       if isinstance(value, str) and value.startswith("not-a-"))
        assert next(iter(settings)) in result.stderr
    finally:
        if result.returncode == 0:
            saved = json.loads((data / "private/server.json").read_text(encoding="utf-8"))
            assert invoke(saved["port"], data, "-Stop").returncode == 0


def test_data_directory_cannot_point_outside_mpt_storage(tmp_path):
    result = invoke(free_port(), tmp_path)
    assert result.returncode != 0
    assert "inside this MPT workspace storage" in result.stderr


def test_actual_junction_cannot_redirect_private_data_outside_workspace(tmp_path):
    parent = isolated_directory()
    parent.mkdir(parents=True)
    link = parent / "redirect"
    quoted_link = "'" + str(link).replace("'", "''") + "'"
    quoted_target = "'" + str(tmp_path).replace("'", "''") + "'"
    created = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         f"$ErrorActionPreference='Stop'; $null = New-Item -ItemType Junction -Path {quoted_link} -Value {quoted_target}"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert created.returncode == 0, created.stderr
    try:
        rejected = invoke(free_port(), link / "business")
        assert rejected.returncode != 0
        assert "Redirected storage" in rejected.stderr
        assert not (tmp_path / "business").exists()
    finally:
        removed = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f"$ErrorActionPreference='Stop'; if ((Get-Item -LiteralPath {quoted_link}).LinkType -ne 'Junction') {{ throw 'Link identity changed' }}; Remove-Item -LiteralPath {quoted_link} -Force"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        assert removed.returncode == 0, removed.stderr


def test_settings_file_cannot_read_another_checkout(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"admin_token":"forbidden-secret"}', encoding="utf-8")
    data = isolated_directory()
    result = invoke(free_port(), data, "-BusinessSettingsFile", path)
    assert result.returncode != 0
    assert "belong to this MPT workspace" in result.stderr
    assert not data.exists()
    assert "forbidden-secret" not in result.stdout + result.stderr


def ps(command):
    result = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                             "$ErrorActionPreference='Stop'; " + command],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


@pytest.mark.parametrize("kind", ["private", "public-proof"])
def test_managed_private_and_public_proof_links_rejected_before_writes(tmp_path, kind):
    data, port = isolated_directory(), free_port()
    data.mkdir(parents=True)
    if kind == "private":
        link, target, link_type = data / "private", tmp_path, "Junction"
    else:
        (data / "private").mkdir()
        link, target, link_type = data / "private/runtime-public.json", tmp_path / "proof.json", "SymbolicLink"
        target.write_text("outside-sentinel", encoding="utf-8")
    ps(f"$null = New-Item -ItemType {link_type} -Path {quote(link)} -Value {quote(target)}")
    result = None
    try:
        result = invoke(port, data)
        assert result.returncode != 0
        assert "Redirected" in result.stderr
        assert not (tmp_path / "runtime.json").exists()
        if kind == "public-proof":
            assert target.read_text(encoding="utf-8") == "outside-sentinel"
    finally:
        if result is not None and result.returncode == 0:
            assert invoke(port, data, "-Stop").returncode == 0
        ps(f"if ((Get-Item -LiteralPath {quote(link)} -Force).LinkType -ne '{link_type}') {{ throw 'Link identity changed' }}; Remove-Item -LiteralPath {quote(link)} -Force")


def test_existing_credential_acl_is_restricted_on_start_and_resume():
    data, port = isolated_directory(), free_port()
    private = data / "private"
    private.mkdir(parents=True)
    credentials = private / "runtime.json"
    credentials.write_text(json.dumps({"admin_token": "fixture-admin", "bootstrap_agent_token": "fixture-agent"}), encoding="utf-8")
    grant = (
        f"$p={quote(credentials)}; $a=Get-Acl -LiteralPath $p; "
        "$a.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new("
        "[Security.Principal.SecurityIdentifier]::new('S-1-1-0'),'Read','Allow')); Set-Acl -LiteralPath $p -AclObject $a"
    )
    inspect_acl = (
        f"$a=Get-Acl -LiteralPath {quote(credentials)}; "
        "@($a.Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }) | ConvertTo-Json -Compress"
    )
    ps(grant)
    allowed = {"S-1-5-18", ps("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").strip()}
    started = None
    try:
        started = invoke(port, data)
        assert started.returncode == 0, started.stderr
        assert set(json.loads(ps(inspect_acl))) == allowed
        ps(grant)
        resumed = invoke(port, data)
        assert resumed.returncode == 0, resumed.stderr
        assert set(json.loads(ps(inspect_acl))) == allowed
    finally:
        if started is not None and started.returncode == 0:
            assert invoke(port, data, "-Stop").returncode == 0


@pytest.mark.parametrize("relative", [
    "database", "database/app.db", "database/app.db-wal", "database/huey.db",
    "private/server.json", "private/runtime.json", "server.stdout.log", "server.stderr.log",
])
def test_all_managed_links_rejected_on_start_and_stop(tmp_path, relative):
    data, port = isolated_directory(), free_port()
    link = data / relative
    link.parent.mkdir(parents=True)
    is_directory = relative == "database"
    target = tmp_path if is_directory else tmp_path / "outside-file"
    if not is_directory:
        target.write_text("outside-sentinel", encoding="utf-8")
    link_type = "Junction" if is_directory else "SymbolicLink"
    ps(f"$null = New-Item -ItemType {link_type} -Path {quote(link)} -Value {quote(target)}")
    try:
        for extra in ((), ("-Stop",)):
            rejected = invoke(port, data, *extra)
            assert rejected.returncode != 0
            assert "Redirected managed" in rejected.stderr
        assert not (data / "private/runtime-public.json").exists()
        if not is_directory:
            assert target.read_text(encoding="utf-8") == "outside-sentinel"
        else:
            assert not list(tmp_path.iterdir())
    finally:
        ps(f"if ((Get-Item -LiteralPath {quote(link)} -Force).LinkType -ne '{link_type}') {{ throw 'Link identity changed' }}; Remove-Item -LiteralPath {quote(link)} -Force")


def test_live_resume_and_stop_reject_redirected_identity_file(tmp_path):
    data, port = isolated_directory(), free_port()
    state_path = data / "private/server.json"
    original = None
    linked = False
    try:
        started = invoke(port, data)
        assert started.returncode == 0, started.stderr
        original = state_path.read_bytes()
        target = tmp_path / "server.json"
        target.write_bytes(original)
        state_path.unlink()
        ps(f"$null = New-Item -ItemType SymbolicLink -Path {quote(state_path)} -Value {quote(target)}")
        linked = True
        for extra in ((), ("-Stop",)):
            rejected = invoke(port, data, *extra)
            assert rejected.returncode != 0
            assert "Redirected managed" in rejected.stderr
            assert requests.get(f"http://127.0.0.1:{port}/api/v1/contents", timeout=5).status_code == 401
        assert target.read_bytes() == original
    finally:
        if linked:
            ps(f"if ((Get-Item -LiteralPath {quote(state_path)} -Force).LinkType -ne 'SymbolicLink') {{ throw 'Link identity changed' }}; Remove-Item -LiteralPath {quote(state_path)} -Force")
        if original is not None:
            state_path.write_bytes(original)
            assert invoke(port, data, "-Stop").returncode == 0
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0
