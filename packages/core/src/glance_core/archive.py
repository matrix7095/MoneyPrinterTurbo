"""Git-backed metadata archive for packaged videos.

防重的事实源永远是 SQLite；git 仓库只做后验历史、备份与可 diff 的账本。
媒体文件（mp4/mp3/wav/m4a）不进 git。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
from pathlib import Path

from sqlmodel import Session, select

from glance_core.models import (
    AgentTask,
    Artifact,
    AuditEvent,
    Candidate,
    Content,
    PublicationRecord,
    TaskType,
    utc_now,
)
from glance_core.normalize import normalize_title
from glance_core.settings import Settings

_MAX_STAGED_BYTES = 5 * 1024 * 1024
_GIT_IDENTITY = [
    "-c",
    "user.name=glance-archive",
    "-c",
    "user.email=archive@glance.local",
]


class ArchiveError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_file(root: Path, path: Path, artifact: Artifact | None = None) -> Path:
    resolved = path.resolve()
    unsafe = not resolved.is_relative_to(root)
    current = path.absolute()
    while current != root and current.is_relative_to(root):
        if current.is_symlink() or (
            current.exists()
            and getattr(current.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            unsafe = True
        current = current.parent
    if unsafe or not resolved.is_file() or (artifact is not None and (
        resolved.stat().st_size != artifact.size_bytes or _sha256_file(resolved) != artifact.sha256
    )):
        raise ArchiveError("Current artifact failed archive integrity validation")
    return resolved


class ArchiveService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repo_dir = Path(settings.archive_repo_dir).resolve()

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *_GIT_IDENTITY, *args],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            shell=False,
        )
        if completed.returncode:
            raise ArchiveError(
                f"git {' '.join(args[:2])} failed ({completed.returncode}): "
                f"{completed.stderr[-500:]}"
            )
        return completed.stdout.strip()

    def ensure_repo(self) -> Path:
        if not (self.repo_dir / ".git").is_dir():
            self.repo_dir.mkdir(parents=True, exist_ok=True)
            self._git("init")
            (self.repo_dir / ".gitignore").write_text(
                "*.mp4\n*.mp3\n*.wav\n*.m4a\n*.mkv\n*.webm\n",
                encoding="utf-8",
            )
            self._git("add", ".gitignore")
            self._git("commit", "-m", "archive: initialize repository")
        return self.repo_dir

    def _latest_artifact(self, content_id: str, artifact_type: str) -> Artifact | None:
        return self.session.exec(
            select(Artifact)
            .where(Artifact.content_id == content_id, Artifact.artifact_type == artifact_type)
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()

    def snapshot(self, content_id: str) -> Path:
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        content_root = Path(content.content_dir).resolve()
        final_artifact = self._latest_artifact(content_id, "final_video")
        final_video = content_root / (
            final_artifact.relative_path if final_artifact else "render/final.mp4"
        )
        if final_artifact or final_video.exists():
            final_video = _verified_file(content_root, final_video, final_artifact)
        video_sha256 = _sha256_file(final_video) if final_video.is_file() else ""
        legacy = final_video == content_root / "render/final.mp4"
        sources: dict[str, Path] = {}
        qc = final_video.with_name("qc.json")
        if qc.exists() or qc.is_symlink():
            sources["qc.json"] = _verified_file(content_root, qc)
        publish = self._latest_artifact(content_id, "publish_video")
        if publish is not None:
            packaged_video = _verified_file(content_root, content_root / publish.relative_path, publish)
            revision_root = content_root / "publish/package-revisions"
            if publish.sha256 == video_sha256 and (legacy or packaged_video.is_relative_to(revision_root)):
                metadata_path = _verified_file(content_root, packaged_video.with_name("publish.json"))
                raw = metadata_path.read_bytes()
                try:
                    metadata = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ArchiveError("Package metadata failed archive integrity validation") from exc
                if not isinstance(metadata, dict) or metadata.get("content_id") != content.id or (
                    packaged_video.is_relative_to(revision_root)
                    and packaged_video.parent.name != hashlib.sha256(
                        video_sha256.encode("ascii") + b"\0" + raw
                    ).hexdigest()
                ):
                    raise ArchiveError("Package metadata failed archive integrity validation")
                sources["publish.json"] = metadata_path
        elif legacy:
            metadata_path = content_root / "publish/publish.json"
            if metadata_path.exists() or metadata_path.is_symlink():
                sources["publish.json"] = _verified_file(content_root, metadata_path)
        cover = self._latest_artifact(content_id, "cover")
        if cover is not None:
            sources["cover.jpg"] = _verified_file(content_root, content_root / cover.relative_path, cover)
        elif legacy:
            cover_path = content_root / "publish/cover.jpg"
            if cover_path.exists() or cover_path.is_symlink():
                sources["cover.jpg"] = _verified_file(content_root, cover_path)
        candidate = self.session.get(Candidate, content.candidate_id)
        tasks = self.session.exec(
            select(AgentTask)
            .where(AgentTask.content_id == content_id)
            .order_by(AgentTask.created_at)
        ).all()
        publications = self.session.exec(
            select(PublicationRecord)
            .where(PublicationRecord.content_id == content_id)
            .order_by(PublicationRecord.published_at)
        ).all()
        self.ensure_repo()
        target = self.repo_dir / str(content.created_at.year) / content.id
        target.mkdir(parents=True, exist_ok=True)

        metadata = {
            "content": content.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json") if candidate else None,
            "tasks": [
                {
                    "task_id": task.id,
                    "task_type": task.task_type.value,
                    "status": task.status.value,
                    "result_data": task.result_data,
                }
                for task in tasks
            ],
            "publication_records": [record.model_dump(mode="json") for record in publications],
        }
        (target / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        script = next(
            (
                task.result_data
                for task in reversed(tasks)
                if task.task_type == TaskType.SHORT_SCRIPT and task.result_data
            ),
            None,
        )
        if script:
            (target / "script.md").write_text(_script_markdown(content, script), encoding="utf-8")
        plan = next(
            (
                task.result_data
                for task in reversed(tasks)
                if task.task_type == TaskType.VISUAL_PLAN and task.result_data
            ),
            None,
        )
        if plan:
            (target / "render_plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        for name in ("qc.json", "publish.json", "cover.jpg"):
            if name in sources:
                shutil.copy2(sources[name], target / name)
            else:
                # A newer unpackaged revision must not retain the previous snapshot's sidecars.
                (target / name).unlink(missing_ok=True)

        entry = {
            "content_id": content.id,
            "title": content.title,
            "title_fp": normalize_title(content.title),
            "normalized_url": candidate.normalized_url if candidate else "",
            "video_sha256": video_sha256,
            "packaged_at": utc_now().isoformat(),
        }
        with (self.repo_dir / "index.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._git("add", "-A")
        self._assert_no_large_staged_files()
        if self._git("status", "--porcelain"):
            self._git("commit", "-m", f"archive: {content.id} {content.title[:60]}")
        commit = self._git("rev-parse", "HEAD")
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="archive-service",
                entity_type="content",
                entity_id=content.id,
                action="archived",
                metadata_json={"commit": commit, "path": str(target)},
            )
        )
        self.session.commit()
        return target

    def _assert_no_large_staged_files(self) -> None:
        staged = self._git("diff", "--cached", "--name-only")
        for name in filter(None, staged.splitlines()):
            path = self.repo_dir / name
            if path.is_file() and path.stat().st_size > _MAX_STAGED_BYTES:
                raise ArchiveError(
                    f"Refusing to commit {name}: {path.stat().st_size} bytes exceeds 5MB "
                    "(media files must stay out of the archive repo)"
                )


def _script_markdown(content: Content, script: dict) -> str:
    titles = "\n".join(f"- {title}" for title in script.get("title_options", []))
    beats = "\n".join(
        f"- {beat.get('at_sec', 0)}s：{beat.get('beat', '')}"
        for beat in script.get("retention_beats", [])
    )
    cover_title = content.cover_title_override or script.get("cover_title", "")
    return (
        f"# {content.title}\n\n"
        f"## 标题选项\n{titles}\n\n"
        f"## 封面文案\n{cover_title}\n\n"
        f"## 开场类型\n{script.get('hook_type', '')}\n\n"
        f"## 配乐档位\n{script.get('bgm_mood', '')}\n\n"
        f"## 留存节奏\n{beats}\n\n"
        f"## 正文\n{script.get('body', '')}\n\n"
        f"## 互动引导\n{script.get('cta', '')}\n\n"
        f"## 旁白全文\n{script.get('full_narration', '')}\n"
    )
