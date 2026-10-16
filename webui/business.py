"""MPT business workbench: HTTP operations and presentation, no local workflow state."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


class BusinessAPIError(Exception):
    """User-visible errors contain neither credentials nor request internals."""


@dataclass
class BusinessConnection:
    base_url: str
    token: str = field(repr=False)

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        address = urlsplit(self.base_url)
        if address.scheme not in {"http", "https"} or not address.hostname or address.username or address.password or address.query or address.fragment:
            raise BusinessAPIError("请输入完整的 HTTP/HTTPS 服务地址，地址中不能包含凭据。")

    def request(self, method, path, *, payload=None, params=None):
        if not path.startswith("/api/v1/"):
            raise BusinessAPIError("请求必须使用业务 API 路径。")
        action = path.rsplit("/", 1)[-1]
        media_action = method == "POST" and path.startswith("/api/v1/contents/") and action in {"prepare", "approve-script", "render", "approve-final", "resume-acceptance"}
        try:
            response = requests.request(
                method, self.base_url + path, headers={"Authorization": f"Bearer {self.token}"},
                json=payload, params=params, timeout=(3, 600 if media_action else 30), allow_redirects=False,
            )
        except requests.Timeout:
            message = "操作可能仍在服务端执行；请刷新内容状态，确认完成或失败后再决定重试。" if method == "POST" else "查询超时，请刷新业务状态。"
            raise BusinessAPIError(message) from None
        except requests.RequestException:
            raise BusinessAPIError("无法连接业务服务，请检查地址和服务状态。") from None
        if response.status_code not in {200, 202}:
            # Do not expose arbitrary remote response bodies or a credential-bearing URL.
            raise BusinessAPIError(f"服务拒绝操作（HTTP {response.status_code}）。请查看内容任务和质量闸门。")
        try:
            return response.status_code, response.json()
        except ValueError:
            raise BusinessAPIError("业务服务返回了无效 JSON。") from None

    def artifact(self, content_id, artifact_id):
        path = content_path(content_id) + f"/artifacts/{quote(artifact_id, safe='')}"
        try:
            with requests.get(
                self.base_url + path, headers={"Authorization": f"Bearer {self.token}"},
                timeout=(3, 30), allow_redirects=False, stream=True,
            ) as response:
                if response.status_code != 200:
                    raise BusinessAPIError(f"产物下载被拒绝（HTTP {response.status_code}）。")
                chunks = []
                size = 0
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > 200 * 1024 * 1024:
                        raise BusinessAPIError("产物超过工作台单次预览的 200 MiB 上限。")
                    chunks.append(chunk)
                return b"".join(chunks)
        except requests.RequestException:
            raise BusinessAPIError("产物下载失败，请检查业务服务。") from None


def content_path(content_id):
    return f"/api/v1/contents/{quote(content_id, safe='')}"


def connection_defaults(configuration, root_dir):
    options = configuration.get("business", {})
    base = os.environ.get("MPT_BUSINESS_API_URL") or os.environ.get("MPT_BUSINESS_PUBLIC_BASE_URL") or options.get("public_base_url") or "http://127.0.0.1:8010"
    token = os.environ.get("MPT_BUSINESS_ADMIN_TOKEN", "")
    token_file = os.environ.get("MPT_BUSINESS_ADMIN_TOKEN_FILE") or options.get("admin_token_file")
    if not token and token_file:
        import json

        path = Path(token_file)
        if not path.is_absolute():
            path = Path(root_dir) / path
        try:
            token = json.loads(path.read_text(encoding="utf-8"))["admin_token"]
            if not isinstance(token, str):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise BusinessAPIError("配置的私有管理员凭据文件不可用。") from None
    return base, token


def available_actions(status, stage=""):
    if status == "WAITING_AGENT" and stage == "audio_evidence_waiting":
        return ("resume-acceptance",)
    return {
        "PREPARING_CONTEXT": ("prepare",),
        "SCRIPT_REVIEW": ("approve-script", "reject-script"),
        "PRODUCING": ("render",),
        "FINAL_REVIEW": ("render", "approve-final"),
        "FAILED": ("prepare", "render"),
        "PACKAGED": ("publications/douyin/publish",),
    }.get(status, ())


def publication_view(record):
    published = record.get("status") == "PUBLISHED"
    url = record.get("external_url", "")
    verified = published and bool(record.get("external_id")) and urlsplit(url).scheme in {"http", "https"}
    return {
        "platform": record.get("platform"), "status": record.get("status"),
        "verified": verified,
        "external_id": record.get("external_id") if verified else None,
        "external_url": url if verified else None,
        "note": record.get("note"),
    }


def _semantics(st, connection, content_id):
    for endpoint, label in (("scripts", "读取已质检脚本"), ("terms", "读取脚本素材关键词"), ("social-metadata", "读取发布元数据")):
        if st.button(label, key=f"business_{endpoint}"):
            status, result = connection.request("POST", f"/api/v1/{endpoint}", payload={"content_id": content_id})
            if status == 202:
                st.info("任务尚未完成消费；请查看下方任务状态，完成当前审核阶段后刷新。")
            st.json(result["data"])


def _execution(st, connection, content_id, content_status):
    st.subheader("按需生产")
    st.caption("点击运行一次，从当前阶段继续研究、制作、独立质量审核与发布；运行结束后停止。")
    if st.button("刷新运行状态", key="business_execution_refresh"):
        st.rerun()
    try:
        _, view = connection.request("GET", content_path(content_id) + "/execution")
    except BusinessAPIError:
        st.warning("运行状态暂不可用，请刷新后再试。")
        return
    state = view["state"]
    if view.get("stop_requested") and view.get("run_state") == "draining":
        st.info("已请求停止，等待现有工作结束。")
    elif state == "STARTING":
        st.info("正在启动，请稍后刷新运行状态。")
    elif state == "RUNNING":
        st.info("正在执行，请查看下方任务、质量审核和发布记录。")
    elif state == "DRAINING" or view.get("stop_requested") and state in {"STARTING", "RUNNING"}:
        st.info("已请求停止，等待本次运行结束。")
    elif state == "UNKNOWN":
        st.warning("暂时无法确认运行结果。请查看任务和发布记录；系统不会重复启动。")
    elif state == "FAILED":
        st.error("启动失败，请检查业务服务后再试。" if view.get("error_code") == "CONTENT_EXECUTION_START_FAILED" else "本次运行失败，请查看任务和质量审核结果后再试。")
    elif state == "FINISHED":
        st.info("本次运行已结束。请查看下方质量审核与平台发布记录。")
    else:
        st.info("当前内容尚未运行。")
    disabled = state in {"STARTING", "RUNNING", "DRAINING", "UNKNOWN"} or view.get("run_state") == "draining" or content_status in {"PUBLISHED", "ARCHIVED", "REJECTED"}
    permission = st.checkbox(
        "仅允许本次新 AI 概念图片按冻结最低尺寸在本机适配，保留原图和处理记录，并继续独立质检",
        value=bool(view.get("allow_local_image_adaptation")) if disabled else False,
        disabled=disabled, key=f"business_image_adaptation_{content_id}_{view.get('run_id', 'new')}_{state}",
    )
    if st.button("运行一次", disabled=disabled, key="business_execution_start"):
        try:
            connection.request("POST", content_path(content_id) + "/run/execute", payload={"allow_local_image_adaptation": permission})
            st.rerun()
        except BusinessAPIError as exc:
            st.error(str(exc))
    if view.get("can_stop") and st.button("停止新增工作", key="business_execution_stop"):
        try:
            connection.request("POST", content_path(content_id) + "/execution/stop")
            st.rerun()
        except BusinessAPIError as exc:
            st.error(str(exc))


def _content(st, connection, contents):
    if not contents:
        st.info("先从候选列表选择一条内容。")
        return
    by_id = {row["id"]: row for row in contents}
    # The server remains the only state authority; this session stores only selection.
    selected = st.session_state.get("business_selected_content")
    if selected not in by_id:
        st.session_state["business_selected_content"] = next(iter(by_id))
    content_id = st.selectbox(
        "内容", list(by_id), key="business_selected_content",
        format_func=lambda key: f"{by_id[key].get('title', key)} · {by_id[key]['business_status']}",
    )
    _, detail = connection.request("GET", content_path(content_id))
    content = detail["content"]
    st.write(f"状态：{content['business_status']} · 阶段：{content.get('current_stage', '')}")
    if content.get("error_code"):
        st.error(f"{content['error_code']}：{content.get('error_message', '')}")
    _execution(st, connection, content_id, content["business_status"])
    with st.expander("内容与来源上下文"):
        st.json(content)
    st.subheader("研究、脚本、视觉计划与质量审核")
    tasks = detail["tasks"]
    if tasks:
        st.dataframe([{key: task.get(key) for key in ("id", "task_type", "status", "attempt", "updated_at")} for task in tasks], hide_index=True)
    for task in tasks:
        with st.expander(f"{task['task_type']} · {task['status']} · {task['id']}"):
            st.json({"input": task.get("input_data"), "result": task.get("result_data"), "error": task.get("last_error")})
    _semantics(st, connection, content_id)
    st.subheader("产物与成片")
    for artifact in detail["artifacts"]:
        st.write(f"{artifact['artifact_type']} · {artifact['relative_path']} · SHA256 {artifact['sha256']}")
        if st.button("读取产物", key=f"business_artifact_{artifact['id']}"):
            data = connection.artifact(content_id, artifact["id"])
            if artifact["artifact_type"] in {"final_video", "preview_video"}:
                st.video(data)
            elif artifact["artifact_type"] in {"cover", "image", "generated_image"}:
                st.image(data)
            st.download_button("下载原文件", data, file_name=Path(artifact["relative_path"]).name, key=f"business_download_{artifact['id']}")
    st.subheader("人工审核动作")
    labels = {"prepare": "准备内容 / 重试准备", "approve-script": "批准当前脚本并进入配音", "reject-script": "驳回脚本并要求修改", "render": "合成成片 / 重试合成", "approve-final": "批准当前成片并准备发布包", "publications/douyin/publish": "签发抖音发布任务", "resume-acceptance": "补齐真实音频证据并继续成片验收"}
    for action in available_actions(content["business_status"], content.get("current_stage", "")):
        payload = None
        enabled = True
        if action == "reject-script":
            note = st.text_area("脚本修改要求", key="business_reject_note")
            payload = {"note": note}
            enabled = bool(note.strip())
        elif action in {"approve-script", "approve-final", "publications/douyin/publish"}:
            enabled = st.checkbox(f"已查看相关内容与 QC，确认{labels[action]}", key=f"business_confirm_{content_id}_{action}")
        if st.button(labels[action], disabled=not enabled, key=f"business_action_{action}"):
            _, result = connection.request("POST", content_path(content_id) + "/" + action, payload=payload)
            st.json(result)
            st.rerun()
    if detail["publications"]:
        st.subheader("平台发布记录")
        for record in detail["publications"]:
            view = publication_view(record)
            if view["verified"]:
                st.write(f"{view['platform']} · PUBLISHED · 平台 ID：{view['external_id']}")
                st.link_button("查看平台发布", view["external_url"])
            else:
                st.warning(f"{view['platform']} · {view['status']}：尚无已核实发布结果；请查看发布任务和证据。")
    elif content["business_status"] == "PACKAGED":
        st.info("发布包已准备，尚未获得真实平台发布结果。")


def render_business_page(configuration, root_dir):
    import streamlit as st

    st.title("MoneyPrinterTurbo 业务工作台")
    st.caption("发现项目 → 研究与脚本 → 素材与成片 → 质量审核 → 抖音发布；状态与结果由业务服务持久化。")
    try:
        base, token = connection_defaults(configuration, root_dir)
        with st.expander("业务服务连接", expanded=not bool(token)):
            base = st.text_input("服务地址", value=base, key="business_api_url")
            token = st.text_input("管理员令牌", value=token, type="password", key="business_admin_token")
        if not token:
            st.info("配置独立业务管理员令牌或私有凭据文件后连接。")
            return
        connection = BusinessConnection(base, token)
        _, contents = connection.request("GET", "/api/v1/contents")
        st.button("刷新业务状态", key="business_refresh")
        with st.expander("发现新项目"):
            direction = st.text_input("发现方向", key="business_direction")
            query = st.text_input("检索要求", key="business_query")
            count = st.number_input("候选数量上限", min_value=1, max_value=30, value=10, key="business_count")
            if st.button("签发发现任务", disabled=not bool(direction.strip()), key="business_search"):
                _, result = connection.request("POST", "/api/v1/discovery/search-tasks", payload={"direction": direction, "query": query, "max_candidates": count})
                st.session_state["business_search_task_id"] = result["task_id"]
                st.json(result)
            task_id = st.session_state.get("business_search_task_id")
            if task_id:
                _, task = connection.request("GET", f"/api/v1/discovery/tasks/{quote(task_id, safe='')}")
                st.json({key: task.get(key) for key in ("id", "status", "task_type", "result_data")})
        _, candidates = connection.request("GET", "/api/v1/candidates")
        with st.expander("来源候选", expanded=not bool(contents)):
            if candidates:
                by_id = {row["id"]: row for row in candidates}
                candidate_id = st.selectbox("候选项目", list(by_id), key="business_candidate_id", format_func=lambda key: by_id[key].get("title_zh") or by_id[key].get("original_title", key))
                candidate = by_id[candidate_id]
                st.write(candidate.get("title_zh") or candidate["original_title"])
                st.write(f"来源：{candidate['source_url']}")
                st.write(f"权益：{candidate.get('rights_status', '')} · {candidate.get('rights_note') or '尚无权益说明'}")
                st.write(f"状态：{candidate['status']}")
                if candidate.get("summary"):
                    st.write(candidate["summary"])
                with st.expander("候选技术详情", expanded=False):
                    st.json(candidate)
                mode = st.selectbox("制作模式", ["footage", "image_only"], key="business_production_mode")
                if st.button("选择候选并准备内容", key="business_select_candidate"):
                    _, result = connection.request("POST", f"/api/v1/candidates/{quote(candidate_id, safe='')}/select", params={"production_mode": mode})
                    st.session_state["business_selected_content"] = result["id"]
                    st.rerun()
            else:
                st.info("发现任务被 Agent 完成并消费后，候选项目会出现在这里。")
        _content(st, connection, contents)
    except BusinessAPIError as exc:
        st.error(str(exc))
