"""OIDC 授权码 + PKCE 与设备码流程。"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from happyone.authz.service import OAuthService

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REDIRECT_URI = "http://127.0.0.1:53682/callback"


def _login(client, alice) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": alice["username"], "password": alice["password"]},
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_client(app) -> None:
    oauth = OAuthService(
        app.state.db,
        app.state.settings,
        app.state.oidc_signer,
        users=None,
    )
    oauth.create_client(
        client_id="app_market",
        name="开心软件市场",
        client_type="public",
        redirect_uris=[REDIRECT_URI],
        scopes=["openid", "profile", "email", "offline_access"],
    )


def test_discovery_and_jwks(client):
    discovery = client.get("/.well-known/openid-configuration")
    assert discovery.status_code == 200
    body = discovery.json()
    assert body["issuer"] == "http://testserver"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["id_token_signing_alg_values_supported"] == ["EdDSA"]

    jwks = client.get("/.well-known/jwks.json").json()
    key = jwks["keys"][0]
    assert key["kty"] == "OKP"
    assert key["crv"] == "Ed25519"
    assert key["alg"] == "EdDSA"


def test_device_code_flow(client, app, alice):
    _create_client(app)
    headers = _login(client, alice)

    started = client.post(
        "/oauth/device_authorization",
        data={"client_id": "app_market", "scope": "openid profile email offline_access"},
    )
    assert started.status_code == 200
    device = started.json()
    assert device["user_code"].count("-") == 1
    assert device["expires_in"] > 0

    approved = client.post(
        "/oauth/device/approve",
        headers=headers,
        json={"user_code": device["user_code"], "action": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    exchanged = client.post(
        "/oauth/token",
        data={
            "grant_type": DEVICE_GRANT,
            "device_code": device["device_code"],
            "client_id": "app_market",
        },
    )
    assert exchanged.status_code == 200
    tokens = exchanged.json()
    assert tokens["access_token"].startswith("ho_at_")
    assert tokens["refresh_token"].startswith("ho_rt_")
    assert tokens["id_token"]

    claims = app.state.oidc_signer.verify_jwt(tokens["id_token"])
    assert claims["sub"] == alice["id"]
    assert claims["aud"] == "app_market"
    assert claims["preferred_username"] == "alice"

    userinfo = client.get(
        "/oauth/userinfo",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert userinfo.status_code == 200
    assert userinfo.json()["preferred_username"] == "alice"
    assert userinfo.json()["email"] == "alice@example.com"

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": "app_market",
        },
    )
    assert refreshed.status_code == 200
    new_access = refreshed.json()["access_token"]

    revoked = client.post(
        "/oauth/revoke",
        data={"token": new_access, "client_id": "app_market"},
    )
    assert revoked.status_code == 200
    after = client.get(
        "/oauth/userinfo", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert after.status_code == 401
    assert after.json()["error"] == "invalid_token"


def test_authorization_code_with_pkce(client, app, alice):
    _create_client(app)
    headers = _login(client, alice)

    verifier = "happy-one-pkce-verifier-" + "x" * 40
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    authorize = client.get(
        "/oauth/authorize",
        headers=headers,
        params={
            "response_type": "code",
            "client_id": "app_market",
            "redirect_uri": REDIRECT_URI,
            "scope": "openid profile",
            "state": "state-123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": "nonce-abc",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 302
    location = authorize.headers["location"]
    query = parse_qs(urlparse(location).query)
    assert query["state"] == ["state-123"]
    code = query["code"][0]

    exchanged = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": "app_market",
            "code_verifier": verifier,
        },
    )
    assert exchanged.status_code == 200
    claims = app.state.oidc_signer.verify_jwt(exchanged.json()["id_token"])
    assert claims["nonce"] == "nonce-abc"

    replay = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": "app_market",
            "code_verifier": verifier,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_authorize_rejects_unregistered_redirect(client, app, alice):
    _create_client(app)
    headers = _login(client, alice)
    response = client.get(
        "/oauth/authorize",
        headers=headers,
        params={
            "response_type": "code",
            "client_id": "app_market",
            "redirect_uri": "http://evil.example/callback",
            "scope": "openid",
            "code_challenge": "a" * 43,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
