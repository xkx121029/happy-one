from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from happyone.channel import router as channel_router  # noqa: E402
from happyone.channel.service import PairingService  # noqa: E402


class FakeClock:
    """可手动推进的时钟，避免测试真的等待 10 分钟。"""

    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def service(clock: FakeClock) -> PairingService:
    return PairingService(
        clock=clock,
        pair_ttl=600.0,
        session_ttl=1800.0,
        heartbeat_timeout=30.0,
        # 清理任务间隔拉长，避免后台任务干扰用假时钟驱动的单测
        sweep_interval=3600.0,
    )


@pytest.fixture()
def app(service: PairingService) -> FastAPI:
    channel_router.configure(service)
    application = FastAPI()
    application.include_router(channel_router.router)
    return application


@pytest.fixture()
def client(app: FastAPI):
    # 必须用 with，让两个 WebSocket 共享同一个事件循环
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def create_pair(client: TestClient):
    def _create(role: str = "mobile", device_name: str = "iPhone") -> dict:
        response = client.post(
            "/channel/pair", json={"role": role, "device_name": device_name}
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _create


@pytest.fixture()
def join_pair(client: TestClient):
    def _join(code: str, role: str = "desktop", device_name: str = "MacBook") -> dict:
        response = client.post(
            "/channel/pair/join",
            json={"code": code, "role": role, "device_name": device_name},
        )
        assert response.status_code == 200, response.text
        return response.json()

    return _join
