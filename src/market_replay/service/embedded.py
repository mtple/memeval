"""Run the FastAPI app on a background uvicorn thread (used by the CLI demo and tests)."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn

from .app import cors_origins_from_env, create_app
from .runs import RunManager


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class EmbeddedServer:
    def __init__(self, manager: RunManager, admin_token: str, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.manager = manager
        self.admin_token = admin_token
        self.host = host
        self.port = port or free_port()
        self.app = create_app(manager, admin_token, cors_origins=cors_origins_from_env())
        self.manager.gateway_url = f"http://{host}:{self.port}"
        cfg = uvicorn.Config(self.app, host=host, port=self.port, log_level="warning", access_log=False)
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, name="market-replay-embedded", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 15.0) -> EmbeddedServer:
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = httpx.get(self.url + "/api/v1/health", timeout=1.0)
                if r.status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise RuntimeError("embedded server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def admin(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {self.admin_token}"}, timeout=600)

    def __enter__(self) -> EmbeddedServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
