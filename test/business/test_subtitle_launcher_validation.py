"""Run the actual startup settings blocks without process or listener operations."""
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import ctranslate2
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/start-mpt-business.ps1'
PWSH = 'C:/Program Files/PowerShell/7/pwsh.exe'
FIELD = 'subtitle_alignment_fallback_model_dir'


def shell_settings(tmp_path, options):
    path = tmp_path / 'business-settings.json'
    path.write_text(json.dumps(options), encoding='utf-8')
    source = SCRIPT.read_text(encoding='utf-8')
    settings = '$businessOptions = ' + source.split('$businessOptions = ', 1)[1].split('\nif (-not $Stop) {', 1)[0]
    environment = '    foreach ($key in $businessOptions.Keys)' + source.split(
        '    foreach ($key in $businessOptions.Keys)', 1)[1].split('    foreach ($name in $environment.Keys)', 1)[0]
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    command = ("$ErrorActionPreference='Stop'; $Stop=$false; "
               f'$workspace={quote(tmp_path)}; $BusinessSettingsFile={quote(path)}; '
               + settings + '\n$environment=@{}\n' + environment
               + '\n@{options=$businessOptions;environment=$environment}|ConvertTo-Json -Depth 4 -Compress')
    return subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command],
        capture_output=True, text=True, encoding='utf-8', timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def validate(monkeypatch, options):
    code = SCRIPT.read_text(encoding='utf-8').split("$validationCode = @'\n", 1)[1].split("\n'@", 1)[0]
    monkeypatch.setattr(ctranslate2, 'get_supported_compute_types', lambda _: {'int8'})
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(options)))
    exec(compile(code, str(SCRIPT) + ' validation', 'exec'), {})


def local_model(tmp_path):
    model = tmp_path / 'local-subtitle-model'
    model.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json'):
        (model / name).write_bytes(b'{}')
    return model


def test_real_launcher_schema_accepts_and_passes_local_subtitle_directory(tmp_path, monkeypatch):
    from app.business.runtime import settings_from_config

    model = local_model(tmp_path)
    result = shell_settings(tmp_path, {FIELD: str(model)})
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual['options'][FIELD] == str(model)
    assert actual['environment']['MPT_BUSINESS_' + FIELD.upper()] == str(model)
    validate(monkeypatch, actual['options'])
    for key, value in actual['environment'].items():
        monkeypatch.setenv(key, value)
    settings = settings_from_config({}, ROOT)
    assert settings.subtitle_alignment_fallback_model_dir == str(model)
    assert settings.asr_model == 'medium'


def test_real_launcher_defaults_subtitle_fallback_to_disabled(tmp_path, monkeypatch):
    result = shell_settings(tmp_path, {})
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual['options'][FIELD] == ''
    assert actual['environment']['MPT_BUSINESS_' + FIELD.upper()] == ''
    validate(monkeypatch, actual['options'])


@pytest.mark.parametrize('key', ['unknown_option', 'admin_token', 'bootstrap_agent_token'])
def test_real_launcher_keeps_unknown_and_private_keys_forbidden(tmp_path, key):
    result = shell_settings(tmp_path, {FIELD: '', key: 'private-sentinel-do-not-log'})
    assert result.returncode != 0
    assert 'Unknown or private business settings fields are forbidden' in result.stderr
    assert key in result.stderr
    assert 'private-sentinel-do-not-log' not in result.stdout + result.stderr


@pytest.mark.parametrize('value', ['large-v3', 'org/model', 'https://example.org/model',
                                  r'\\server\model', '//server/model', 123, True, None])
def test_launcher_rejects_remote_or_wrongly_typed_subtitle_model(tmp_path, monkeypatch, capsys, value):
    actual = shell_settings(tmp_path, {}).stdout
    options = json.loads(actual)['options']
    options[FIELD] = value
    with pytest.raises(SystemExit) as stopped:
        validate(monkeypatch, options)
    assert stopped.value.code == 2
    diagnostic = capsys.readouterr().out
    assert FIELD in diagnostic
    assert 'example.org' not in diagnostic


@pytest.mark.parametrize('missing', ['model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json'])
def test_launcher_rejects_incomplete_local_subtitle_model(tmp_path, monkeypatch, capsys, missing):
    model = local_model(tmp_path)
    (model / missing).unlink()
    options = json.loads(shell_settings(tmp_path, {}).stdout)['options']
    options[FIELD] = str(model)
    with pytest.raises(SystemExit) as stopped:
        validate(monkeypatch, options)
    assert stopped.value.code == 2
    assert FIELD in capsys.readouterr().out


@pytest.mark.parametrize('empty', ['model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json'])
def test_launcher_rejects_empty_local_subtitle_model_files(tmp_path, monkeypatch, capsys, empty):
    model = local_model(tmp_path)
    (model / empty).write_bytes(b'')
    options = json.loads(shell_settings(tmp_path, {}).stdout)['options']
    options[FIELD] = str(model)
    with pytest.raises(SystemExit) as stopped:
        validate(monkeypatch, options)
    assert stopped.value.code == 2
    assert FIELD in capsys.readouterr().out


def test_launcher_checks_model_structure_without_reading_or_hashing_model(tmp_path, monkeypatch):
    from glance_media import executor

    model = local_model(tmp_path)
    options = json.loads(shell_settings(tmp_path, {FIELD: str(model)}).stdout)['options']

    def forbidden(*args, **kwargs):
        pytest.fail('Startup must not read or hash model contents')

    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.parent == model:
            forbidden()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(executor, '_subtitle_model_manifest', forbidden)
    monkeypatch.setattr(executor, 'sha256_file', forbidden)
    monkeypatch.setattr(hashlib, 'file_digest', forbidden)
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    monkeypatch.setattr(Path, 'open', guarded_open)
    validate(monkeypatch, options)
