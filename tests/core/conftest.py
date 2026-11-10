"""地基线测试公共夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from happyone.app import create_app
from happyone.config import Settings
from happyone.identity.service import IdentityService


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        issuer="http://testserver",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "test.db",
        oidc_key_path=tmp_path / "data" / "keys" / "oidc_ed25519.pem",
        license_key_path=tmp_path / "data" / "keys" / "license_ed25519.pem",
        session_ttl_sec=3600,
        access_token_ttl_sec=600,
        refresh_token_ttl_sec=86400,
        auth_code_ttl_sec=300,
        device_code_ttl_sec=300,
        device_poll_interval_sec=1,
        log_level="WARNING",
    )


@pytest.fixture()
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def alice(app) -> dict[str, str]:
    identity = IdentityService(app.state.db, app.state.settings)
    user = identity.create_user(
        username="alice",
        password="alice-password",
        email="alice@example.com",
        display_name="Alice",
        is_admin=True,
    )
    return {"id": user.id, "username": user.username, "password": "alice-password"}
