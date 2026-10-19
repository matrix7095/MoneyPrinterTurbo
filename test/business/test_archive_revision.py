"""Archive the verified current revision without trusting the old render alias."""

import hashlib
import json
from datetime import timedelta

import pytest
from glance_core.archive import ArchiveError, ArchiveService
from glance_core.database import build_engine, create_db
from glance_core.models import Artifact, Candidate, Content, utc_now
from glance_core.settings import Settings
from sqlmodel import Session


@pytest.fixture
def archive_contract(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        archive_repo_dir=tmp_path / "archive",
    )
    engine = build_engine(settings)
    create_db(engine)
    with Session(engine) as session:
        candidate = Candidate(source_type="agent", source_id="archive", original_title="Furniture",
                              source_url="https://example.com/furniture")
        session.add(candidate)
        session.commit()
        root = settings.content_dir / "episode"
        (root / "render").mkdir(parents=True)
        (root / "render/final.mp4").write_bytes(b"old alias")
        content = Content(candidate_id=candidate.id, title="Furniture", content_dir=str(root))
        session.add(content)
        session.commit()
        yield session, settings, content, root


def register_final(session, content, root, relative, data, *, created_at=None):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    artifact = Artifact(
        content_id=content.id,
        artifact_type="final_video",
        relative_path=relative,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        mime_type="video/mp4",
        producer_stage="render",
        created_at=created_at or utc_now(),
    )
    session.add(artifact)
    session.commit()
    return artifact


@pytest.mark.parametrize("registered", [False, True])
def test_archive_indexes_current_revision_and_preserves_legacy(archive_contract, registered):
    session, settings, content, root = archive_contract
    expected = b"old alias"
    if registered:
        register_final(session, content, root, "render/previous.mp4", b"previous",
                       created_at=utc_now() - timedelta(minutes=1))
        expected = b"current revision"
        register_final(session, content, root, "render/revisions/new/final.mp4", expected)
    ArchiveService(session, settings).snapshot(content.id)
    entry = json.loads((settings.archive_repo_dir / "index.jsonl").read_text().splitlines()[-1])
    assert entry["video_sha256"] == hashlib.sha256(expected).hexdigest()
    assert (root / "render/final.mp4").read_bytes() == b"old alias"
    assert not list(settings.archive_repo_dir.rglob("*.mp4"))


@pytest.mark.parametrize("tamper", ["missing", "bytes", "size", "outside"])
def test_latest_bad_final_never_falls_back_or_commits_archive(archive_contract, tamper):
    session, settings, content, root = archive_contract
    artifact = register_final(session, content, root, "render/revisions/new/final.mp4", b"new")
    path = root / artifact.relative_path
    if tamper == "missing":
        path.unlink()
    elif tamper == "bytes":
        path.write_bytes(b"bad")
    elif tamper == "size":
        artifact.size_bytes += 1
    else:
        outside = root.parent / "outside.mp4"
        outside.write_bytes(b"new")
        artifact.relative_path = str(outside)
    session.add(artifact)
    session.commit()
    with pytest.raises(ArchiveError, match="integrity"):
        ArchiveService(session, settings).snapshot(content.id)
    assert not settings.archive_repo_dir.exists()
    assert (root / "render/final.mp4").read_bytes() == b"old alias"


def register_asset(session, content, root, relative, data, artifact_type):
    artifact = register_final(session, content, root, relative, data)
    artifact.artifact_type = artifact_type
    session.add(artifact)
    session.commit()
    return artifact


def register_package(session, content, root, data=b"current revision", *, metadata=None):
    metadata = metadata if metadata is not None else {"content_id": content.id, "title": "current"}
    raw = json.dumps(metadata).encode()
    revision = hashlib.sha256(hashlib.sha256(data).hexdigest().encode() + b"\0" + raw).hexdigest()
    relative = f"publish/package-revisions/{revision}/final.mp4"
    artifact = register_asset(session, content, root, relative, data, "publish_video")
    (root / relative).with_name("publish.json").write_bytes(raw)
    return artifact


def write_legacy_sidecars(root):
    for relative, data in (("render/qc.json", b'{"old": "qc"}'),
                           ("publish/publish.json", b'{"old": "metadata"}'),
                           ("publish/cover.jpg", b"old cover")):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_archive_uses_registered_revision_sidecars(archive_contract):
    session, settings, content, root = archive_contract
    write_legacy_sidecars(root)
    final = register_final(session, content, root, "render/revisions/new/final.mp4", b"current revision")
    (root / final.relative_path).with_name("qc.json").write_bytes(b'{"new": "qc"}')
    package = register_package(session, content, root)
    register_asset(session, content, root, "publish/cover-revisions/new/cover.jpg", b"new cover", "cover")
    target = ArchiveService(session, settings).snapshot(content.id)
    assert (target / "qc.json").read_bytes() == b'{"new": "qc"}'
    assert (target / "publish.json").read_bytes() == (root / package.relative_path).with_name("publish.json").read_bytes()
    assert (target / "cover.jpg").read_bytes() == b"new cover"
    assert not list(target.rglob("*.mp4"))
    assert (root / "publish/publish.json").read_bytes() == b'{"old": "metadata"}'


def test_legacy_sidecars_remain_supported(archive_contract):
    session, settings, content, root = archive_contract
    write_legacy_sidecars(root)
    target = ArchiveService(session, settings).snapshot(content.id)
    assert (target / "qc.json").read_bytes() == b'{"old": "qc"}'
    assert (target / "publish.json").read_bytes() == b'{"old": "metadata"}'
    assert (target / "cover.jpg").read_bytes() == b"old cover"


def test_new_unpackaged_revision_removes_previous_archived_metadata(archive_contract):
    session, settings, content, root = archive_contract
    write_legacy_sidecars(root)
    register_final(session, content, root, "render/revisions/one/final.mp4", b"current revision")
    register_package(session, content, root)
    service = ArchiveService(session, settings)
    target = service.snapshot(content.id)
    assert (target / "publish.json").is_file()
    register_final(session, content, root, "render/revisions/two/final.mp4", b"not packaged")
    service.snapshot(content.id)
    assert not (target / "publish.json").exists()
    assert not (target / "qc.json").exists()
    assert not (target / "cover.jpg").exists()


@pytest.mark.parametrize("kind", ["publish_video", "cover"])
@pytest.mark.parametrize("tamper", ["missing", "bytes", "size", "outside"])
def test_bad_registered_sidecar_never_falls_back(archive_contract, kind, tamper):
    session, settings, content, root = archive_contract
    write_legacy_sidecars(root)
    register_final(session, content, root, "render/revisions/new/final.mp4", b"current revision")
    artifact = register_package(session, content, root) if kind == "publish_video" else register_asset(
        session, content, root, "publish/cover-revisions/new/cover.jpg", b"new cover", "cover")
    path = root / artifact.relative_path
    if tamper == "missing":
        path.unlink()
    elif tamper == "bytes":
        path.write_bytes(b"wrong")
    elif tamper == "size":
        artifact.size_bytes += 1
    else:
        outside = root.parent / "outside-sidecar"
        outside.write_bytes(path.read_bytes())
        artifact.relative_path = str(outside)
    session.add(artifact)
    session.commit()
    with pytest.raises(ArchiveError, match="integrity"):
        ArchiveService(session, settings).snapshot(content.id)
    assert not settings.archive_repo_dir.exists()


@pytest.mark.parametrize("tamper", ["bytes", "content", "missing", "outside", "invalid_json"])
def test_revision_metadata_is_bound_to_current_package(archive_contract, tamper):
    session, settings, content, root = archive_contract
    register_final(session, content, root, "render/revisions/new/final.mp4", b"current revision")
    package = register_package(session, content, root,
                               metadata={"content_id": "other"} if tamper == "content" else None)
    path = (root / package.relative_path).with_name("publish.json")
    if tamper == "bytes":
        path.write_bytes(json.dumps({"content_id": content.id, "changed": True}).encode())
    elif tamper == "missing":
        path.unlink()
    elif tamper == "outside":
        outside = root.parent / "outside.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    elif tamper == "invalid_json":
        path.write_bytes(b"not JSON")
    with pytest.raises(ArchiveError, match="integrity"):
        ArchiveService(session, settings).snapshot(content.id)
    assert not settings.archive_repo_dir.exists()


def test_registered_legacy_package_and_cover_remain_supported(archive_contract):
    session, settings, content, root = archive_contract
    write_legacy_sidecars(root)
    register_final(session, content, root, "render/final.mp4", b"legacy video")
    register_asset(session, content, root, "publish/final.mp4", b"legacy video", "publish_video")
    metadata = {"content_id": content.id, "title": "legacy package"}
    (root / "publish/publish.json").write_text(json.dumps(metadata), encoding="utf-8")
    register_asset(session, content, root, "publish/cover.jpg", b"registered legacy cover", "cover")
    target = ArchiveService(session, settings).snapshot(content.id)
    assert json.loads((target / "publish.json").read_text()) == metadata
    assert (target / "cover.jpg").read_bytes() == b"registered legacy cover"
    assert (target / "qc.json").read_bytes() == b'{"old": "qc"}'


def test_new_revision_without_registered_package_ignores_fixed_sidecars(archive_contract):
    session, settings, content, root = archive_contract
    write_legacy_sidecars(root)
    register_final(session, content, root, "render/revisions/new/final.mp4", b"new revision")
    target = ArchiveService(session, settings).snapshot(content.id)
    assert not any((target / name).exists() for name in ("qc.json", "publish.json", "cover.jpg"))


def test_revision_final_cannot_reuse_same_sha_legacy_package_metadata(archive_contract):
    session, settings, content, root = archive_contract
    register_final(session, content, root, "render/revisions/new/final.mp4", b"same video")
    register_asset(session, content, root, "publish/final.mp4", b"same video", "publish_video")
    stale = {"content_id": content.id, "title": "STALE LEGACY"}
    (root / "publish/publish.json").write_text(json.dumps(stale), encoding="utf-8")
    target = ArchiveService(session, settings).snapshot(content.id)
    assert not (target / "publish.json").exists()
    assert json.loads((root / "publish/publish.json").read_text()) == stale


def test_revision_qc_cannot_escape_content_root(archive_contract):
    session, settings, content, root = archive_contract
    final = register_final(session, content, root, "render/revisions/new/final.mp4", b"new revision")
    outside = root.parent / "outside-qc.json"
    outside.write_bytes(b'{"passed": true}')
    (root / final.relative_path).with_name("qc.json").symlink_to(outside)
    with pytest.raises(ArchiveError, match="integrity"):
        ArchiveService(session, settings).snapshot(content.id)
    assert not settings.archive_repo_dir.exists()
