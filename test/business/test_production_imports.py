"""Run in the actual no-extra uv environment, never fake missing SDK modules."""

import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL_DISTRIBUTIONS = (
    "openai", "litellm", "dashscope", "google-genai", "twelvelabs",
)


def test_production_install_and_entrypoints_have_no_model_sdks():
    for name in MODEL_DISTRIBUTIONS:
        with pytest.raises(PackageNotFoundError):
            version(name)
    for name in ("glance-core", "glance-agent", "glance-collector", "glance-media"):
        assert version(name)

    script = """
import importlib
import sys
from fastapi.testclient import TestClient
from app.asgi import app as application
import app.services.task
import cli
import main
for module in ('openai', 'litellm', 'dashscope', 'google.genai', 'twelvelabs'):
    try:
        importlib.import_module(module)
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError('Model SDK installed: ' + module)
with TestClient(application) as client:
    assert client.get('/ping').status_code == 200
    assert '/api/v1/agent/tasks/claim' in application.openapi()['paths']
from streamlit.testing.v1 import AppTest
ui = AppTest.from_file('webui/Main.py', default_timeout=45).run()
assert not ui.exception, [str(item.value) for item in ui.exception]
assert not any(
    name == sdk or name.startswith(sdk + '.')
    for name in sys.modules
    for sdk in ('openai', 'litellm', 'dashscope', 'google.genai', 'twelvelabs', 'faster_whisper')
)
"""
    env = dict(os.environ, MPT_BUSINESS_ENABLED="0")
    env.pop("MPT_UPSTREAM_COMPATIBILITY", None)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("source", [
    "wavespeed", "volcengine_seedance", "ofox", "metaso_minimax",
    "muapi", "loomloom", "openai_image",
])
def test_production_pipeline_rejects_generated_material_before_execution(monkeypatch, source):
    from app.models.schema import VideoParams
    from app.services import state, task

    monkeypatch.delenv("MPT_UPSTREAM_COMPATIBILITY", raising=False)
    monkeypatch.setattr(task, "_run_pipeline", lambda *a, **k: pytest.fail("provider executed"))
    task_id = f"production-reject-{source}"
    try:
        task.start(task_id, VideoParams(video_subject="topic", video_source=source))
        result = state.state.get_task(task_id)
        assert result["failed_stage"] == "preflight"
        assert "AgentTask" in result["error"]
    finally:
        state.state.delete_task(task_id)


def test_production_semantic_and_direct_generation_fail_closed(monkeypatch):
    from app.services import llm, material, twelvelabs, voice

    monkeypatch.delenv("MPT_UPSTREAM_COMPATIBILITY", raising=False)
    with pytest.raises(RuntimeError, match="MPT business workbench"):
        llm._generate_response("test")
    with pytest.raises(RuntimeError, match="social-metadata"):
        llm.generate_social_metadata("topic")
    for call in (
        lambda: material.download_videos("task", ["topic"], source="openai_image"),
        lambda: material.generate_images_openai("topic", 5),
        lambda: material.generate_videos_wavespeed("topic", 5),
        lambda: voice.gemini_tts("text", "Zephyr", 1, "unused.mp3"),
        lambda: voice.mimo_tts("text", "default", 1, "unused.mp3"),
        twelvelabs._client,
    ):
        with pytest.raises(RuntimeError, match="AgentTask"):
            call()


@pytest.mark.parametrize("source", ["local", "pexels", "pixabay", "coverr"])
def test_production_boundary_preserves_deterministic_render_entry(monkeypatch, source):
    from app.models.schema import VideoParams
    from app.services import task

    monkeypatch.delenv("MPT_UPSTREAM_COMPATIBILITY", raising=False)
    monkeypatch.setattr(task, "_run_pipeline", lambda *a, **k: "render-entry")
    monkeypatch.setattr(task.upload_post.upload_post_service, "_account_setting", lambda *a: False)
    assert task.start("render", VideoParams(video_subject="topic", video_source=source, bgm_type="")) == "render-entry"


def test_production_boundary_rejects_music_and_automatic_publishing(monkeypatch):
    from app.models.schema import VideoParams
    from app.services import state, task

    monkeypatch.delenv("MPT_UPSTREAM_COMPATIBILITY", raising=False)
    monkeypatch.setattr(task, "_run_pipeline", lambda *a, **k: pytest.fail("provider executed"))
    cases = [("sonilo", "Generated music"), ("elevenlabs", "Generated music")]
    for music, error in cases:
        try:
            result = task.start("music-boundary", VideoParams(video_subject="topic", video_source="local", bgm_type=music, bgm_volume=1))
            assert result["failed_stage"] == "preflight"
            assert error in result["error"]
        finally:
            state.state.delete_task("music-boundary")
    monkeypatch.setattr(task.upload_post.upload_post_service, "_account_setting", lambda *a: True)
    monkeypatch.setitem(task.config.app, "upload_post_auto_upload", True)
    try:
        result = task.start("publish-boundary", VideoParams(video_subject="topic", video_source="local", bgm_type=""))
        assert result["failed_stage"] == "preflight"
        assert "server-issued publish task" in result["error"]
    finally:
        state.state.delete_task("publish-boundary")


def test_asgi_lifespan_initializes_and_closes_independent_business_storage(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.asgi import app

    monkeypatch.setenv("MPT_BUSINESS_ENABLED", "1")
    monkeypatch.setenv("MPT_BUSINESS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MPT_BUSINESS_DATABASE_URL", f"sqlite:///{(tmp_path / 'database/app.db').as_posix()}")
    monkeypatch.setenv("MPT_BUSINESS_ADMIN_TOKEN", "test-only-admin")
    monkeypatch.setenv("MPT_BUSINESS_BOOTSTRAP_AGENT_TOKEN", "test-only-agent")
    monkeypatch.setenv("MPT_BUSINESS_PUBLIC_BASE_URL", "http://127.0.0.1:8010")
    monkeypatch.setenv("MPT_BUSINESS_ASR_MODEL", "medium")
    monkeypatch.setenv("MPT_BUSINESS_ASR_DEVICE", "cpu")
    monkeypatch.setenv("MPT_BUSINESS_ASR_COMPUTE_TYPE", "int8")
    with TestClient(app) as client:
        runtime = app.state.business_runtime
        assert runtime.settings.data_dir == tmp_path
        assert runtime.settings.public_base_url == "http://127.0.0.1:8010"
        assert (runtime.settings.asr_model, runtime.settings.asr_device, runtime.settings.asr_compute_type) == ("medium", "cpu", "int8")
        assert client.get("/api/v1/contents").status_code == 401
        response = client.get("/api/v1/contents", headers={"Authorization": "Bearer test-only-admin"})
        assert response.status_code == 200
        assert response.json() == []
        assert (tmp_path / "database/app.db").is_file()
    assert app.state.business_runtime is None
