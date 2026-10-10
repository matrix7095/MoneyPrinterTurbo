from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def diagnostic_snapshot(database: Path, *, limit: int = 50) -> dict[str, Any]:
    """Read the Glance control-plane log tables without taking a write lock."""
    target = database.expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"Glance database not found: {target}")
    connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        issues: list[dict[str, Any]] = []
        issues.extend(
            _rows(
                connection,
                """
                SELECT 'content_failed' AS kind, id AS entity_id, error_code AS code,
                       error_message AS message, updated_at AS occurred_at
                FROM content
                WHERE business_status = 'FAILED'
                ORDER BY updated_at DESC LIMIT ?
                """,
                limit,
            )
        )
        issues.extend(
            _rows(
                connection,
                """
                SELECT 'waiting_agent_without_task' AS kind, c.id AS entity_id,
                       'NO_ACTIVE_AGENT_TASK' AS code,
                       'Content is WAITING_AGENT but has no active Agent task' AS message,
                       c.updated_at AS occurred_at
                FROM content c
                WHERE c.business_status = 'WAITING_AGENT'
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_task t
                      WHERE t.content_id = c.id
                        AND t.status IN ('PENDING', 'LEASED', 'SUBMITTED')
                  )
                ORDER BY c.updated_at DESC LIMIT ?
                """,
                limit,
            )
        )
        issues.extend(
            _rows(
                connection,
                """
                SELECT 'lease_expired_unreaped' AS kind, id AS entity_id,
                       'LEASE_EXPIRED' AS code,
                       'Agent lease expired but task is still LEASED' AS message,
                       lease_expires_at AS occurred_at
                FROM agent_task
                WHERE status = 'LEASED'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < CURRENT_TIMESTAMP
                ORDER BY lease_expires_at DESC LIMIT ?
                """,
                limit,
            )
        )
        issues.extend(
            _rows(
                connection,
                """
                SELECT 'stale_media_stage' AS kind, content_id AS entity_id,
                       'MEDIA_STAGE_STALE' AS code,
                       'Media stage has been RUNNING for more than 60 minutes: ' || stage
                       AS message,
                       started_at AS occurred_at
                FROM stage_run
                WHERE status = 'RUNNING'
                  AND started_at IS NOT NULL
                  AND started_at < datetime('now', '-60 minutes')
                  AND stage IN ('acquire', 'transcribe', 'scene_detect', 'acquire_images',
                                'voice', 'render', 'content_qc_frames', 'package')
                ORDER BY started_at ASC LIMIT ?
                """,
                limit,
            )
        )
        issues.extend(
            _rows(
                connection,
                """
                SELECT 'stage_failed' AS kind, content_id AS entity_id, error_code AS code,
                       error_message AS message, completed_at AS occurred_at
                FROM stage_run
                WHERE status = 'FAILED'
                  AND NOT EXISTS (
                    SELECT 1 FROM stage_run newer
                    WHERE newer.content_id = stage_run.content_id
                      AND newer.stage = stage_run.stage
                      AND newer.status = 'SUCCESS'
                      AND newer.started_at > stage_run.started_at
                  )
                ORDER BY COALESCE(completed_at, started_at) DESC LIMIT ?
                """,
                limit,
            )
        )
        issues.extend(
            _rows(
                connection,
                """
                SELECT 'agent_task_failed' AS kind, id AS entity_id, error_code AS code,
                       error_message AS message, completed_at AS occurred_at
                FROM agent_task
                WHERE status IN ('FAILED', 'REJECTED')
                ORDER BY created_at DESC LIMIT ?
                """,
                limit,
            )
        )
        issues.extend(
            _rows(
                connection,
                """
                SELECT 'pipeline_advance_failed' AS kind, entity_id, action AS code,
                       json_extract(metadata_json, '$.error') AS message,
                       created_at AS occurred_at
                FROM audit_event
                WHERE action = 'advance_failed'
                  AND NOT EXISTS (
                    SELECT 1 FROM audit_event recovered
                    WHERE recovered.entity_id = audit_event.entity_id
                      AND recovered.action = 'advance_recovered'
                      AND recovered.created_at > audit_event.created_at
                  )
                ORDER BY created_at DESC LIMIT ?
                """,
                limit,
            )
        )
        issues.sort(key=lambda row: str(row.get("occurred_at") or ""), reverse=True)
        recent_events = _rows(
            connection,
            """
            SELECT entity_type, entity_id, action, actor_type, actor_id, created_at
            FROM audit_event ORDER BY created_at DESC LIMIT ?
            """,
            limit,
        )
        return {
            "status": "ISSUES_FOUND" if issues else "HEALTHY",
            "issue_count": len(issues),
            "issues": issues[:limit],
            "recent_events": recent_events,
        }
    finally:
        connection.close()


def _rows(connection: sqlite3.Connection, sql: str, limit: int) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in connection.execute(sql, (limit,)).fetchall()]
    except sqlite3.OperationalError as exc:
        # 旧库缺表/字段时也要给排障 Agent 一份可读诊断，而不是裸 traceback。
        return [
            {
                "kind": "diagnostic_query_failed",
                "entity_id": "database",
                "code": "SCHEMA_MISMATCH",
                "message": str(exc),
                "occurred_at": None,
            }
        ]
