"""Workbench controls use the real HTTP application; only transport is in-process."""

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from glance_core.models import Content, ContentStatus, RightsStatus
from streamlit.testing.v1 import AppTest
from sqlmodel import Session

from test.business.test_content_http import seed_candidate
from test.business.test_discovery_http import make_runtime
from test.business.test_semantic_http import semantic_app
from webui.business import (
    BusinessAPIError,
    BusinessConnection,
    available_actions,
    connection_defaults,
    publication_view,
)


def test_configuration_uses_only_explicit_private_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("MPT_BUSINESS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("MPT_BUSINESS_ADMIN_TOKEN_FILE", raising=False)
    monkeypatch.setenv("GLANCE_ADMIN_TOKEN", "old-secret")
    assert connection_defaults({}, tmp_path)[1] == ""
    path = tmp_path / "private.json"
    path.write_text(json.dumps({"admin_token": "test-admin"}), encoding="utf-8")
    assert connection_defaults({"business": {"admin_token_file": "private.json"}}, tmp_path)[1] == "test-admin"
    assert "test-admin" not in repr(BusinessConnection("http://127.0.0.1:8010", "test-admin"))
    with pytest.raises(BusinessAPIError, match="不可用"):
        connection_defaults({"business": {"admin_token_file": "missing.json"}}, tmp_path)


def test_requests_do_not_forward_credentials_through_redirects(monkeypatch):
    def redirected(method, url, **options):
        assert options["allow_redirects"] is False
        assert options["timeout"] == (3, 30)
        return requests.Response()  # Missing success status must fail closed.

    monkeypatch.setattr(requests, "request", redirected)
    with pytest.raises(BusinessAPIError, match="服务拒绝"):
        BusinessConnection("http://127.0.0.1:8010", "private-token").request("GET", "/api/v1/contents")
    with pytest.raises(BusinessAPIError):
        BusinessConnection("http://user:secret@host", "token")


def test_audio_resume_and_unverified_publication_are_distinct():
    assert available_actions("WAITING_AGENT", "short_script") == ()
    assert available_actions("WAITING_AGENT", "audio_evidence_waiting") == ("resume-acceptance",)
    assert "publications/douyin/publish" not in available_actions("FINAL_REVIEW")
    record = {"platform": "douyin", "status": "UNKNOWN", "external_id": "unverified", "external_url": "https://example.org/item"}
    assert publication_view(record)["external_url"] is None
    assert publication_view({**record, "status": "PUBLISHED"})["verified"]
    assert not publication_view({**record, "status": "PUBLISHED", "external_id": ""})["verified"]


def test_media_actions_have_bounded_long_reads_and_honest_timeout(monkeypatch):
    def timed_out(method, url, **options):
        assert options["timeout"] == (3, 600)
        raise requests.Timeout("private transport details must not be exposed")

    monkeypatch.setattr(requests, "request", timed_out)
    connection = BusinessConnection("http://127.0.0.1:8010", "private-token")
    for action in ("prepare", "approve-script", "render", "approve-final", "resume-acceptance"):
        with pytest.raises(BusinessAPIError, match="操作可能仍在服务端执行") as error:
            connection.request("POST", f"/api/v1/contents/content-test/{action}")
        assert "private" not in str(error.value)


def test_workbench_issues_real_discovery_selects_content_and_reads_pending_tasks(tmp_path, monkeypatch):
    monkeypatch.delenv("MPT_UPSTREAM_COMPATIBILITY", raising=False)
    monkeypatch.setenv("MPT_BUSINESS_ADMIN_TOKEN", "test-admin")
    monkeypatch.delenv("MPT_BUSINESS_ADMIN_TOKEN_FILE", raising=False)
    runtime = make_runtime(tmp_path)
    with TestClient(semantic_app(runtime)) as http:
        seed_candidate(runtime)

        def through_asgi(method, url, **options):
            options.pop("timeout")
            options["follow_redirects"] = options.pop("allow_redirects")
            return http.request(method, urlsplit(url).path, **options)

        monkeypatch.setattr(requests, "request", through_asgi)
        page = AppTest.from_string("from webui.business import render_business_page\nrender_business_page({}, '.')", default_timeout=30).run()
        assert not page.exception
        page.text_input(key="business_direction").set_value("海外设备")
        page.button(key="business_search").click().run()
        assert not page.exception
        task_id = page.session_state["business_search_task_id"]
        assert http.get(f"/api/v1/discovery/tasks/{task_id}", headers={"Authorization": "Bearer test-admin"}).json()["status"] == "PENDING"
        page.selectbox(key="business_production_mode").select("image_only")
        page.button(key="business_select_candidate").click().run()
        assert not page.exception
        content_id = page.session_state["business_selected_content"]
        assert page.button(key="business_scripts")
        assert not any(button.key == "business_action_approve-script" for button in page.button)
        page.button(key="business_scripts").click().run()
        assert not page.exception
        assert any("尚未完成消费" in element.value for element in page.info)
        content = http.get(f"/api/v1/contents/{content_id}", headers={"Authorization": "Bearer test-admin"}).json()
        assert content["tasks"][0]["task_type"] == "content_research"
        assert content["content"]["business_status"] == "WAITING_AGENT"


def test_workbench_refresh_preserves_selection_and_reads_latest_status(tmp_path, monkeypatch):
    monkeypatch.delenv("MPT_UPSTREAM_COMPATIBILITY", raising=False)
    monkeypatch.setenv("MPT_BUSINESS_ADMIN_TOKEN", "test-admin")
    monkeypatch.delenv("MPT_BUSINESS_ADMIN_TOKEN_FILE", raising=False)
    runtime = make_runtime(tmp_path)
    with TestClient(semantic_app(runtime)) as http:
        candidate_ids = []
        with Session(runtime.engine) as session:
            for suffix in ("first", "second"):
                candidate, _ = runtime.workflow_service(session).ingest_candidate(
                    source_type="agent",
                    source_id=f"refresh-project-{suffix}",
                    source_url=f"https://example.org/refresh-{suffix}",
                    original_title=f"Refresh Project {suffix}",
                    rights_status=RightsStatus.EXPLICIT_LICENSE,
                )
                candidate_ids.append(candidate.id)

        for candidate_id in candidate_ids:
            response = http.post(
                f"/api/v1/candidates/{candidate_id}/select?production_mode=image_only",
                headers={"Authorization": "Bearer test-admin"},
            )
            assert response.status_code == 200, response.text
        content_rows = http.get(
            "/api/v1/contents", headers={"Authorization": "Bearer test-admin"}
        ).json()
        selected_id = content_rows[-1]["id"]

        calls = []

        def through_asgi(method, url, **options):
            calls.append((method, urlsplit(url).path))
            options.pop("timeout")
            options["follow_redirects"] = options.pop("allow_redirects")
            return http.request(method, urlsplit(url).path, **options)

        monkeypatch.setattr(requests, "request", through_asgi)
        page = AppTest.from_string(
            "from webui.business import render_business_page\nrender_business_page({}, '.')",
            default_timeout=30,
        ).run()
        assert not page.exception
        page.selectbox(key="business_selected_content").select(selected_id).run()
        assert not page.exception
        assert page.session_state["business_selected_content"] == selected_id

        with Session(runtime.engine) as session:
            content = session.get(Content, selected_id)
            content.business_status = ContentStatus.FAILED
            content.current_stage = "recovery_blocked"
            session.add(content)
            session.commit()

        calls.clear()
        page.button(key="business_refresh").click().run()

        assert not page.exception
        assert page.session_state["business_selected_content"] == selected_id
        assert any(
            element.value == "状态：FAILED · 阶段：recovery_blocked"
            for element in page.markdown
        )
        assert any(method == "GET" for method, _ in calls)
        assert all(method != "POST" for method, _ in calls)


def test_main_production_entry_does_not_recover_or_write_legacy_tasks(tmp_path, monkeypatch):
    from app.config import config
    from app.services import task

    monkeypatch.delenv("MPT_UPSTREAM_COMPATIBILITY", raising=False)
    monkeypatch.setenv("MPT_BUSINESS_ADMIN_TOKEN", "test-admin")
    monkeypatch.delenv("MPT_BUSINESS_ADMIN_TOKEN_FILE", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Business entry must not mutate legacy task/config state")

    monkeypatch.setattr(task, "recover_interrupted_cross_posts", forbidden)
    monkeypatch.setattr(config, "save_config", forbidden)
    runtime = make_runtime(tmp_path)
    with TestClient(semantic_app(runtime)) as http:
        def through_asgi(method, url, **options):
            options.pop("timeout")
            options["follow_redirects"] = options.pop("allow_redirects")
            return http.request(method, urlsplit(url).path, **options)

        monkeypatch.setattr(requests, "request", through_asgi)
        main = Path(__file__).resolve().parents[2] / "webui/Main.py"
        page = AppTest.from_file(str(main), default_timeout=30).run()
        assert not page.exception
        assert page.title[0].value == "MoneyPrinterTurbo 业务工作台"
        assert not any(button.key == "auto_generate_script" for button in page.button)
