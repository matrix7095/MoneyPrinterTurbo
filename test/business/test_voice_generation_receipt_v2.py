"""Signed Kokoro receipt compatibility and sentence/tempo evidence binding."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from glance_core.database import build_engine, create_db
from glance_core.models import AgentTask, Artifact, Candidate, Content, StageRun, StageStatus, TaskStatus, TaskType
from glance_core.settings import Settings
from glance_core.task_service import stable_hash
from glance_core.voice_generation import artifact_binding, bind_voice_generation, verify_voice_generation
from glance_media.executor import MediaExecutor
from sqlmodel import Session

from test.business.test_kokoro_offline_provider import bundle as bundle, fake_runtime as fake_runtime


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def contract(tmp_path, bundle, fake_runtime):
    settings = Settings(_env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{(tmp_path / 'data.db').as_posix()}",
                        admin_token="test-admin", tts_provider="kokoro", tts_voice="kokoro:3", tts_rate="+50%")
    engine = build_engine(settings)
    create_db(engine)
    root = tmp_path / "content"
    root.mkdir()
    narration = "面积2.5平方米。第二句！"
    executor = MediaExecutor(root)
    speech = executor.synthesize_voice(narration, provider="kokoro", model_dir=str(bundle[0]), voice="kokoro:3", rate="+50%")
    with Session(engine) as session:
        candidate = Candidate(source_type="fixture", source_id="voice", source_url="https://example.org/voice", original_title="voice")
        session.add(candidate)
        session.commit()
        content = Content(candidate_id=candidate.id, title="voice", content_dir=str(root))
        session.add(content)
        session.commit()
        script = AgentTask(content_id=content.id, task_type=TaskType.SHORT_SCRIPT, status=TaskStatus.COMPLETED,
                           prompt_name="fixture", instructions="fixture", input_data={}, input_hash=stable_hash({}),
                           result_data={"full_narration": narration})
        stage = StageRun(content_id=content.id, stage="voice", status=StageStatus.SUCCESS, input_hash="voice-input")
        voice = Artifact(content_id=content.id, **executor.artifact_metadata(speech.audio_path, "voice", "voice"))
        receipt = Artifact(content_id=content.id, **executor.artifact_metadata(
            speech.audio_path.with_name("tts_generation_receipt.json"), "tts_generation_receipt", "voice"))
        session.add_all([script, stage, voice, receipt])
        session.commit()
        yield session, settings, content, script, stage, voice, receipt, deepcopy(speech.provenance)
    engine.dispose()


def store_receipt(contract, data):
    session, _, content, _, _, _, receipt, _ = contract
    raw = json.dumps(data, ensure_ascii=False).encode()
    (Path(content.content_dir) / receipt.relative_path).write_bytes(raw)
    receipt.sha256, receipt.size_bytes = sha(raw), len(raw)
    session.add(receipt)
    session.commit()


def bind_and_verify(contract):
    session, settings, content, script, stage, voice, receipt, _ = contract
    _, summary = bind_voice_generation(session, settings, content, voice, receipt, script, stage, operation="test")
    session.commit()
    actual, retained = verify_voice_generation(session, settings, content, artifact_binding(voice), summary, script)
    assert actual == summary and retained.id == receipt.id
    return summary


def test_schema2_round_trip_binds_actual_sentence_and_tempo_strategy(contract):
    summary = bind_and_verify(contract)
    data = contract[-1]
    assert summary["receipt_schema_version"] == 2
    assert summary["synthesis"] == data["synthesis"]
    assert summary["conversion"] == data["conversion"]
    assert summary["parameters"]["model_speed"] == 1.0
    assert summary["parameters"]["requested_speed"] == summary["parameters"]["tempo_factor"] == 1.5


def legacy_receipt(contract):
    data = deepcopy(contract[-1])
    data["schema_version"] = 1
    data["parameters"] = {"rate": "+50%", "speed": 1.5, "volume": "+0%", "gain": 1.0}
    data["audio"].pop("pcm_stage")
    data.pop("synthesis")
    data.pop("conversion")
    return data


def test_schema1_round_trip_keeps_the_existing_summary_shape(contract):
    data = legacy_receipt(contract)
    store_receipt(contract, data)
    summary = bind_and_verify(contract)
    assert summary["parameters"] == data["parameters"]
    assert summary["audio"] == data["audio"]
    assert not {"receipt_schema_version", "synthesis", "conversion"} & summary.keys()
    assert set(summary) == {
        "binding_event_id", "receipt_artifact", "provider", "actual_provider_verified", "generated_at", "model_id",
        "model_manifest_sha256", "runtime", "voice", "narration_sha256", "script", "stage_id", "voice_input_hash",
        "parameters", "audio", "licenses", "supporting_license_documents", "rights_summary", "publisher_rights_summary",
        "provenance_limits", "limitation",
    }


@pytest.mark.parametrize("path,value", [
    ("parameters.requested_speed", 1.2), ("parameters.model_speed", 1.5), ("parameters.tempo_factor", 1.2),
    ("parameters.rate", "+20%"), ("parameters.requested_speed", True), ("parameters.gain", 0.5),
    ("audio.pcm_stage", "post_tempo"), ("audio.encoded_duration_sec", 10),
    ("synthesis.strategy", "native-fast"), ("synthesis.sentence_count", 1),
    ("synthesis.sentences.0.text_start_char", 1), ("synthesis.sentences.1.text_end_char", 3),
    ("synthesis.sentences.0.text_sha256", "0" * 64), ("synthesis.sentences.1.pcm_start_sample", 2),
    ("synthesis.sentences.1.pcm_end_sample", 100), ("synthesis.sentences.0.pcm16_sha256", "invalid"),
    ("conversion.tempo_filter", "atempo=1.2"), ("conversion.command.6", "atempo=1.2"),
    ("conversion.command.10", "9"),
    ("conversion.ffmpeg_version", ""),
])
def test_schema2_rejects_inconsistent_semantic_evidence_before_signing(contract, path, value):
    data = deepcopy(contract[-1])
    parts = path.split(".")
    target = data
    for part in parts[:-1]:
        target = target[int(part) if isinstance(target, list) else part]
    target[int(parts[-1]) if isinstance(target, list) else parts[-1]] = value
    store_receipt(contract, data)
    session, settings, content, script, stage, voice, receipt, _ = contract
    with pytest.raises(ValueError):
        bind_voice_generation(session, settings, content, voice, receipt, script, stage, operation="test")


@pytest.mark.parametrize("schema", [1, 2])
def test_both_schemas_preserve_signed_summary_tamper_rejection(contract, schema):
    if schema == 1:
        store_receipt(contract, legacy_receipt(contract))
    summary = bind_and_verify(contract)
    summary["parameters"] = {**summary["parameters"], "rate": "+20%"}
    session, settings, content, script, _, voice, _, _ = contract
    with pytest.raises(ValueError, match="summary changed"):
        verify_voice_generation(session, settings, content, artifact_binding(voice), summary, script)
