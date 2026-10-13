"""Workbench execution buttons issue authenticated server operations."""

from urllib.parse import urlsplit

import requests
from streamlit.testing.v1 import AppTest

from test.business.test_content_execution_http import own_host

pytest_plugins = ["test.business.test_content_execution_http"]


def page_for(http, content_id, monkeypatch):
    calls = []

    def transport(method, url, **options):
        calls.append((method, urlsplit(url).path))
        if method == "POST" and urlsplit(url).path.endswith("/run/execute"):
            calls.append(("PERMISSION", options["json"]))
        options.pop("timeout")
        options["follow_redirects"] = options.pop("allow_redirects")
        return http.request(method, urlsplit(url).path, **options)

    monkeypatch.setattr(requests, "request", transport)
    source = (
        "import streamlit as st\n"
        "from webui.business import BusinessConnection, _execution\n"
        f"_execution(st, BusinessConnection('http://127.0.0.1:8010', 'test-admin'), '{content_id}', 'WAITING_AGENT')\n"
    )
    return AppTest.from_string(source, default_timeout=10).run(), calls


def test_workbench_start_status_and_cooperative_stop(execution, monkeypatch):
    http, runtime, content_id, processes = execution
    page, calls = page_for(http, content_id, monkeypatch)
    assert not page.exception
    assert page.checkbox[0].value is False
    page.button(key="business_execution_start").click().run()
    assert not page.exception
    assert ("POST", f"/api/v1/contents/{content_id}/run/execute") in calls
    assert ("PERMISSION", {"allow_local_image_adaptation": False}) in calls
    assert len(processes) == 1
    assert any("正在启动" in item.value for item in page.info)
    assert page.button(key="business_execution_start").disabled
    assert page.checkbox[0].disabled
    own_host(http, runtime, content_id)
    page.button(key="business_execution_refresh").click().run()
    assert not page.exception
    assert any("正在执行" in item.value for item in page.info)
    page.button(key="business_execution_stop").click().run()
    assert not page.exception
    assert ("POST", f"/api/v1/contents/{content_id}/execution/stop") in calls
    assert any("停止" in item.value for item in page.info)
    assert not processes[0].done.is_set()
    assert "test-admin" not in str(page.info)


def test_workbench_permission_is_explicit_for_one_new_run(execution, monkeypatch):
    http, _, content_id, processes = execution
    page, calls = page_for(http, content_id, monkeypatch)
    assert page.checkbox[0].value is False
    page.checkbox[0].check().run()
    page.button(key="business_execution_start").click().run()
    assert not page.exception
    assert ("PERMISSION", {"allow_local_image_adaptation": True}) in calls
    assert page.checkbox[0].value is True and page.checkbox[0].disabled
    page.button(key="business_execution_stop").click().run()
    processes[0].code = 0
    processes[0].done.set()
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        page.button(key="business_execution_refresh").click().run()
        if not page.button(key="business_execution_start").disabled:
            break
        time.sleep(0.01)
    assert not page.exception and page.checkbox[0].value is False
    assert not page.checkbox[0].disabled


def test_workbench_shows_failure_without_fake_running(execution, monkeypatch):
    http, _, content_id, _ = execution

    def failed(*args, **kwargs):
        raise OSError("PRIVATE test-admin")

    from app.business import conductor_launch
    monkeypatch.setattr(conductor_launch.subprocess, "Popen", failed)
    page, _ = page_for(http, content_id, monkeypatch)
    page.button(key="business_execution_start").click().run()
    assert not page.exception
    assert any("启动失败" in item.value for item in page.error)
    assert not any("正在执行" in item.value for item in page.info)
    assert "test-admin" not in str(page.error) and "PRIVATE" not in str(page.error)
