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
