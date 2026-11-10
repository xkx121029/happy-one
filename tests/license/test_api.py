"""HTTP 接口与统一包络测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from happyone.license import router as license_router_module
from happyone.license.models import ErrorCode
from happyone.license.router import get_license_service
from happyone.license.service import verify_revocation_snapshot

FIXED_NOW = 1_800_000_000


@pytest.fixture
def app(service):
    application = FastAPI()
    application.include_router(license_router_module.router)
    application.dependency_overrides[get_license_service] = lambda: service
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _issue(client, **overrides):
    body = {"user_id": "user_001", "product_slug": "happy-one", "features": ["core"], "expires_in_days": 30}
    body.update(overrides)
    response = client.post("/license/issue", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_issue_returns_envelope(client):
    data = _issue(client)
    assert data["license_id"].startswith("lic_")
    assert data["token"].startswith("hlic1.")
    assert data["payload"]["user_id"] == "user_001"


def test_verify_ok(client):
    data = _issue(client)
    response = client.post("/license/verify", json={"license": data["token"]})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["status"] == "valid"
    assert body["data"]["license_id"] == data["license_id"]


def test_verify_failure_returns_200_and_chinese_reason(client):
    data = _issue(client)
    prefix, _body, signature = data["token"].split(".")
    response = client.post("/license/verify", json={"license": f"{prefix}.{_body}.{signature[:-4]}AAAA"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == ErrorCode.INVALID_SIGNATURE
    assert "篡改" in body["error"]


def test_query_license(client):
    data = _issue(client)
    response = client.get(f"/license/{data['license_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["revoked"] is False
    assert body["data"]["token"] == data["token"]


def test_query_missing_license(client):
    response = client.get("/license/lic_missing")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"ok", "code", "error"}
    assert body["ok"] is False
    assert body["code"] == ErrorCode.NOT_FOUND
    assert "未找到" in body["error"]


def test_revoke_and_double_revoke(client):
    data = _issue(client)
    response = client.post("/license/revoke", json={"license_id": data["license_id"], "reason": "退款"})
    assert response.status_code == 200
    assert response.json()["data"]["reason"] == "退款"

    again = client.post("/license/revoke", json={"license_id": data["license_id"]})
    assert again.status_code == 409
    assert again.json()["code"] == ErrorCode.ALREADY_REVOKED

    verify_body = client.post("/license/verify", json={"license": data["token"]}).json()
    assert verify_body["ok"] is False
    assert verify_body["code"] == ErrorCode.REVOKED


def test_revoke_unknown_license(client):
    response = client.post("/license/revoke", json={"license_id": "lic_unknown"})
    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.NOT_FOUND


def test_public_key(client):
    response = client.get("/license/public-key")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["alg"] == "Ed25519"
    assert data["kid"].startswith("k_")
    assert len(data["public_key_hex"]) == 64
    assert data["grace_days"] == 7


def test_revocation_snapshot(client, service):
    data = _issue(client)
    client.post("/license/revoke", json={"license_id": data["license_id"]})

    response = client.get("/license/revocations/snapshot")
    assert response.status_code == 200
    snapshot = response.json()["data"]
    assert snapshot["count"] == 1
    assert snapshot["token"].startswith("hlic1.")

    verified = verify_revocation_snapshot(snapshot["token"], lambda kid: service.key_pair.public_key)
    assert verified.ok is True
    assert verified.entries[0].license_id == data["license_id"]


def test_device_fingerprint_through_api(client):
    data = _issue(client, device_fingerprint="machine-A")
    ok = client.post("/license/verify", json={"license": data["token"], "device_fingerprint": "machine-A"})
    assert ok.json()["ok"] is True

    bad = client.post("/license/verify", json={"license": data["token"], "device_fingerprint": "machine-B"})
    assert bad.json()["code"] == ErrorCode.DEVICE_MISMATCH


def test_invalid_json_body(client):
    response = client.post(
        "/license/issue",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert "JSON" in body["error"]


def test_missing_required_field(client):
    response = client.post("/license/issue", json={"product_slug": "happy-one", "expires_in_days": 3})
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.INVALID_REQUEST
    assert "user_id" in response.json()["error"]


def test_service_unavailable_envelope(app):
    app.dependency_overrides[get_license_service] = lambda: None
    with TestClient(app) as client:
        response = client.get("/license/public-key")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == ErrorCode.SERVICE_UNAVAILABLE
    assert "初始化失败" in body["error"]
