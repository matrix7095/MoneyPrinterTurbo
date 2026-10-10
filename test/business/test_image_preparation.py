import builtins
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from glance_agent import cli
from glance_agent.image_preparation import prepare_image
from glance_agent.settings import AgentSettings
from glance_agent.workspace import prepare_workspace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    config = AgentSettings(token="isolated", agent_id="image-test", role_id="image", workspace_dir=tmp_path / "tasks")
    directory = prepare_workspace(
        config.workspace_dir, lease_token="isolated-lease", api_url="http://127.0.0.1:9", agent_id=config.agent_id,
        bundle={"task": {"task_id": "agt_image_prepare", "task_type": "asset_generate_image"},
                "input": {"requests": [{"segment_index": 3, "asset_request": {
                    "min_width": 1080, "min_height": 1920, "aspect_ratio": "9:16"}}]},
                "result_schema": {}, "instructions": "isolated"},
    )
    source = tmp_path / "native.png"
    Image.new("RGB", (941, 1672), "red").save(source)
    monkeypatch.setattr(cli, "settings", lambda: config)
    return directory, source, config


def run_prepare(workspace):
    directory, source, config = workspace
    return prepare_image(directory, "agt_image_prepare", config.agent_id, source, 3)


def test_prepares_actual_dimensions_keeps_original_and_reuses(workspace):
    directory, source, _ = workspace
    originals = {path: path.read_bytes() for path in [source, *(directory / name for name in ("task.json", "input.json", ".lease.json"))]}
    result = run_prepare(workspace)
    target = Path(result["path"])
    assert target.is_relative_to(directory)
    with Image.open(target) as actual:
        assert actual.size == (1080, 1920)
        assert actual.getpixel((540, 960)) == (255, 0, 0)
    assert (result["source_width"], result["source_height"]) == (941, 1672)
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert Path(result["source_path"]).read_bytes() == originals[source]
    modified = target.stat().st_mtime_ns
    assert run_prepare(workspace) == result
    assert target.stat().st_mtime_ns == modified
    assert all(path.read_bytes() == value for path, value in originals.items())


@pytest.mark.parametrize("field,value", [("task_type", "douyin_publish"), ("task_id", "agt_other")])
def test_refuses_other_task(workspace, field, value):
    directory, _, _ = workspace
    task_path = directory / "task.json"
    task = json.loads(task_path.read_text())
    task[field] = value
    task_path.write_text(json.dumps(task))
    with pytest.raises(ValueError, match="exact asset_generate_image"):
        run_prepare(workspace)
    assert not (directory / "prepared-images").exists()


def test_refuses_other_agent_lease(workspace):
    directory, source, _ = workspace
    with pytest.raises(ValueError, match="local task lease"):
        prepare_image(directory, "agt_image_prepare", "other-agent", source, 3)


@pytest.mark.parametrize("input_data", [
    {}, {"requests": []}, {"requests": [{"segment_index": "3"}]},
    {"requests": [{"segment_index": 3}, {"segment_index": 3}]},
    {"requests": [{"segment_index": 4, "asset_request": {}}]},
    {"requests": [{"segment_index": 3, "asset_request": {"min_width": True, "min_height": 1920, "aspect_ratio": "9:16"}}]},
    {"requests": [{"segment_index": 3, "asset_request": {"min_width": 1080, "min_height": 1920, "aspect_ratio": "9:0"}}]},
    {"requests": [{"segment_index": 3, "asset_request": {"min_width": 1080, "min_height": 1920, "aspect_ratio": "NaN:16"}}]},
    {"requests": [{"segment_index": 3, "asset_request": {"min_width": 16384, "min_height": 16384, "aspect_ratio": "1:1"}}]},
])
def test_refuses_invalid_frozen_contract(workspace, input_data):
    directory, _, _ = workspace
    (directory / "input.json").write_text(json.dumps(input_data))
    with pytest.raises(ValueError):
        run_prepare(workspace)
    assert not (directory / "prepared-images").exists()


@pytest.mark.parametrize("size", [(1920, 1080), (1000, 1600)])
def test_refuses_wrong_aspect_instead_of_stretching(workspace, size):
    directory, source, _ = workspace
    Image.new("RGB", size).save(source)
    with pytest.raises(ValueError, match="regenerate"):
        run_prepare(workspace)
    assert not (directory / "prepared-images").exists()


def test_preserves_alpha_and_exact_nondefault_ratio(workspace):
    directory, source, _ = workspace
    request = {"requests": [{"segment_index": 3, "asset_request": {"min_width": 101, "min_height": 130, "aspect_ratio": "3:4"}}]}
    (directory / "input.json").write_text(json.dumps(request))
    Image.new("RGBA", (75, 100), (255, 0, 0, 0)).save(source)
    result = run_prepare(workspace)
    assert (result["width"], result["height"]) == (102, 136)
    with Image.open(result["path"]) as image:
        assert image.mode == "RGBA" and image.getpixel((50, 50))[3] == 0


def test_refuses_to_overwrite_changed_or_incomplete_preparation(workspace):
    result = run_prepare(workspace)
    target = Path(result["path"])
    target.write_bytes(b"changed artifact")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        run_prepare(workspace)
    assert target.read_bytes() == b"changed artifact"
    target.unlink()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        run_prepare(workspace)
    assert not target.exists()


def test_refuses_changed_retained_original(workspace):
    result = run_prepare(workspace)
    raw = Path(result["source_path"])
    raw.write_bytes(b"changed original")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        run_prepare(workspace)
    assert raw.read_bytes() == b"changed original"


def test_cli_is_local_and_returns_receipt(workspace, monkeypatch):
    _, source, _ = workspace
    monkeypatch.setattr(cli, "AgentClient", lambda *a, **kw: pytest.fail("prepare-image must not call API"))
    result = CliRunner().invoke(cli.app, ["prepare-image", "agt_image_prepare", str(source), "--segment-index", "3"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "PREPARED"


@pytest.mark.parametrize("arguments", [["../outside"], ["agt_unrelated"], ["agt_image_prepare", "--segment-index", "4"]])
def test_cli_rejects_invalid_tasks_and_segments(workspace, arguments):
    _, source, _ = workspace
    args = ["prepare-image", arguments[0], str(source), *(arguments[1:] or ["--segment-index", "3"])]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["status"] in {"NO_WORKSPACE", "REJECTED"}


def test_missing_pillow_has_diagnostic_cli_result(workspace, monkeypatch):
    _, source, _ = workspace
    original_import = builtins.__import__
    def import_without_pillow(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("isolated missing Pillow")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", import_without_pillow)
    result = CliRunner().invoke(cli.app, ["prepare-image", "agt_image_prepare", str(source), "--segment-index", "3"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["status"] == "PREPARATION_UNAVAILABLE"


def test_rejects_redirected_output_directory(workspace, tmp_path):
    directory, _, _ = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (directory / "prepared-images").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit test symlinks")
    with pytest.raises(ValueError, match="redirected"):
        run_prepare(workspace)
    assert list(outside.iterdir()) == []


def test_invalid_image_is_rejected_without_workspace_error(workspace):
    _, source, _ = workspace
    source.write_bytes(b"invalid image")
    result = CliRunner().invoke(cli.app, ["prepare-image", "agt_image_prepare", str(source), "--segment-index", "3"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["status"] == "REJECTED"
