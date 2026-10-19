import json

import pytest

from glance_agent.workspace import prepare_workspace, task_directory


_MISSING = object()


def make_bundle(*, context_manifest=_MISSING):
    bundle = {
        "task": {"task_id": "agt_context_manifest"},
        "input": {
            "context_files": ["context/brief.md"],
            "allowed_asset_ids": ["asset-7"],
            "script": {"title": "保留原始输入"},
        },
        "result_schema": {"type": "object"},
        "instructions": "Use the supplied context.",
    }
    if context_manifest is not _MISSING:
        bundle["context_manifest"] = context_manifest
    return bundle


def prepare(root, *, context_manifest=_MISSING):
    return prepare_workspace(
        root,
        bundle=make_bundle(context_manifest=context_manifest),
        lease_token="lease",
        api_url="http://127.0.0.1:8000",
        agent_id="context-manifest-test",
    )


def test_persists_context_manifest_separately_and_preserves_input(tmp_path):
    manifest = [
        {
            "index": 0,
            "name": "scene-一.png",
            "size_bytes": 42,
            "sha256": "a" * 64,
            "input_ref": "context/brief.md#scene-1",
        }
    ]
    expected_input = make_bundle()["input"]

    directory = prepare(tmp_path, context_manifest=manifest)

    assert json.loads((directory / "context_manifest.json").read_text(encoding="utf-8")) == manifest
    saved_input = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    assert saved_input == expected_input
    assert "context_manifest" not in saved_input


@pytest.mark.parametrize("manifest_state", ["missing", "none"])
def test_reprepare_without_manifest_clears_stale_manifest(tmp_path, manifest_state):
    directory = prepare(tmp_path, context_manifest=[{"index": 99, "name": "stale.png"}])
    bundle = make_bundle()
    if manifest_state == "none":
        bundle["context_manifest"] = None

    prepare_workspace(
        tmp_path,
        bundle=bundle,
        lease_token="lease",
        api_url="http://127.0.0.1:8000",
        agent_id="context-manifest-test",
    )

    assert json.loads((directory / "context_manifest.json").read_text(encoding="utf-8")) == []


def test_task_directory_accepts_legacy_workspace_without_context_manifest(tmp_path):
    directory = prepare(tmp_path)
    (directory / "context_manifest.json").unlink(missing_ok=True)

    assert task_directory(tmp_path, "agt_context_manifest") == directory
