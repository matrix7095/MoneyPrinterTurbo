from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import httpx

from glance_agent.settings import AgentSettings


class AgentClient:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self.client = httpx.Client(
            base_url=self.settings.api_url.rstrip("/"),
            timeout=self.settings.timeout_seconds,
            follow_redirects=False,
        )

    def _client(self):
        return nullcontext(self.client)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.token.get_secret_value()}"}

    def whoami(self) -> dict[str, Any]:
        response = self.client.post(
            "/api/v1/agent/whoami",
            headers=self._headers(),
            json={
                "agent_id": self.settings.agent_id,
                "role_id": self.settings.role_id,
                "runtime": self.settings.runtime_report(),
            },
        )
        response.raise_for_status()
        return response.json()

    def deployment(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            "/api/v1/agent/deployment", headers=self._headers(), json=payload
        )
        response.raise_for_status()
        return response.json()

    def claim(self, wait: int = 0) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                "/api/v1/agent/tasks/claim",
                params={"wait": wait},
                headers={"Authorization": f"Bearer {self.settings.token.get_secret_value()}"},
                json={
                    "agent_id": self.settings.agent_id,
                    "role_id": self.settings.role_id,
                    "runtime": self.settings.runtime_report(),
                },
                # 长轮询读超时必须覆盖服务端 wait 窗口，否则客户端先断。
                timeout=httpx.Timeout(self.settings.timeout_seconds, read=wait + 60),
            )
            response.raise_for_status()
            return response.json()

    def dispatch(
        self,
        wait: int | None = None,
        *,
        launcher: str = "",
        task_type: str = "",
        task_id: str = "",
    ) -> dict[str, Any]:
        with self._client() as client:
            params: dict[str, Any] = {}
            if wait is not None:
                params["wait"] = wait
            response = client.post(
                "/api/v1/agent/dispatch",
                params=params,
                headers={"Authorization": f"Bearer {self.settings.token.get_secret_value()}"},
                json={
                    "agent_id": self.settings.agent_id,
                    "role_id": self.settings.role_id,
                    "task_type": task_type or None,
                    "task_id": task_id or None,
                    "runtime": self.settings.runtime_report(),
                    "workspace_dir": str(self.settings.workspace_dir.expanduser().resolve()),
                    "launcher": launcher,
                },
                # wait 缺省时由服务端配置决定，读超时按服务端上限上浮。
                timeout=httpx.Timeout(
                    self.settings.timeout_seconds, read=(wait if wait is not None else 840) + 60
                ),
            )
            response.raise_for_status()
            return response.json()

    def scheduler_plan(self, scheduler_id: str, contract_version: int = 1) -> dict[str, Any]:
        with self._client() as client:
            response = client.get(
                "/api/v1/agent/scheduler/plan",
                params={
                    "scheduler_id": scheduler_id,
                    **({"contract_version": 2} if contract_version == 2 else {}),
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    def confirm_claim(self, task_id: str, lease_token: str, agent_id: str) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"/api/v1/agent/tasks/{task_id}/confirm-claim",
                headers={"Authorization": f"Bearer {lease_token}"},
                json={"agent_id": agent_id},
            )
            response.raise_for_status()
            return response.json()

    def register_publish_intent(
        self, task_id: str, lease_token: str, agent_id: str
    ) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"/api/v1/agent/tasks/{task_id}/publish-intent",
                headers={"Authorization": f"Bearer {lease_token}"},
                json={"agent_id": agent_id},
            )
            response.raise_for_status()
            return response.json()

    def bundle(self, bundle_url: str, lease_token: str) -> dict[str, Any]:
        with self._client() as client:
            response = client.get(
                bundle_url,
                headers={"Authorization": f"Bearer {lease_token}"},
            )
            response.raise_for_status()
            return response.json()

    def download_context(self, url: str, lease_token: str, destination) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Keep the previous verified file usable until the new body is complete.
        # This matters on Windows, where a killed worker can otherwise leave a
        # zero-byte context file that looks present to the next dispatch.
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".download",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with self.client.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {lease_token}"},
                ) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        temporary.write(chunk)
                temporary.flush()
            temporary_path.replace(destination)
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def heartbeat(self, task_id: str, lease_token: str) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"/api/v1/agent/tasks/{task_id}/heartbeat",
                headers={"Authorization": f"Bearer {lease_token}"},
                json={"runtime": self.settings.runtime_report()},
            )
            response.raise_for_status()
            return response.json()

    def upload_artifact(
        self,
        task_id: str,
        lease_token: str,
        *,
        body: bytes,
        mime_type: str,
    ) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"/api/v1/agent/tasks/{task_id}/artifacts",
                headers={
                    "Authorization": f"Bearer {lease_token}",
                    "Content-Type": mime_type,
                },
                content=body,
            )
            response.raise_for_status()
            return response.json()

    def similar(
        self,
        *,
        task_id: str,
        lease_token: str,
        agent_id: str,
        query: str,
        url: str = "",
        direction_key: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                "/api/v1/agent/similar",
                headers={"Authorization": f"Bearer {lease_token}"},
                json={
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "query": query,
                    "url": url,
                    "direction_key": direction_key,
                    "limit": limit,
                },
            )
            response.raise_for_status()
            return response.json()

    def complete(
        self,
        task_id: str,
        lease_token: str,
        agent_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"/api/v1/agent/tasks/{task_id}/complete",
                headers={"Authorization": f"Bearer {lease_token}"},
                json={"agent_id": agent_id, "result": result},
            )
            response.raise_for_status()
            return response.json()

    def fail(
        self,
        task_id: str,
        lease_token: str,
        agent_id: str,
        *,
        retryable: bool,
        error_code: str,
        message: str,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"/api/v1/agent/tasks/{task_id}/fail",
                headers={"Authorization": f"Bearer {lease_token}"},
                json={
                    "agent_id": agent_id,
                    "retryable": retryable,
                    "error_code": error_code,
                    "message": message,
                    "attempt": attempt,
                },
            )
            response.raise_for_status()
            return response.json()
