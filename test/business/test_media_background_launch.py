import array
import subprocess
from types import SimpleNamespace

from app.services import video_project
from glance_media import executor


def test_media_run_command_hides_window(monkeypatch, tmp_path):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(executor.subprocess, "run", run)
    executor.run_command(["ffmpeg", "-version"], cwd=tmp_path)

    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_media_probe_hides_window(monkeypatch, tmp_path):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"streams": [], "format": {}}')

    monkeypatch.setattr(executor.subprocess, "run", run)
    assert executor.probe(tmp_path / "video.mp4") == {"streams": [], "format": {}}

    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_media_capability_probe_hides_window(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="..C scale V->V scale\n")

    monkeypatch.setattr(executor, "_ffmpeg_filter_cache", None)
    monkeypatch.setattr(executor.subprocess, "run", run)
    assert executor.available_ffmpeg_filters() == frozenset({"scale"})

    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_final_audio_decode_hides_window(monkeypatch, tmp_path):
    calls = []
    samples = array.array("h", [1000] * 1600).tobytes()
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    media = executor.MediaExecutor(tmp_path / "content")

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=samples)

    monkeypatch.setattr(executor.subprocess, "run", run)
    evidence = media.final_audio_evidence(video)

    assert evidence["signal_status"] == "available"
    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_video_project_tool_version_hides_window(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ffmpeg version test\n")

    monkeypatch.setattr(video_project.subprocess, "run", run)
    assert video_project._tool_version("ffmpeg") == "ffmpeg version test"

    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_video_project_run_hides_window(monkeypatch, tmp_path):
    calls = []

    class Process:
        returncode = 0

        def communicate(self, timeout):
            return "rendered", None

        def poll(self):
            return self.returncode

    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(video_project.subprocess, "Popen", popen)
    project = video_project.VideoProject(tmp_path / "project")

    assert (
        project._run(["ffmpeg", "-version"], tmp_path / "cancel.json", 5) == "rendered"
    )
    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
