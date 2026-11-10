"""端到端最小链路：建用户 → 登录拿令牌 → 访问受保护接口。"""

from __future__ import annotations


def _login(client, username: str, password: str):
    return client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


def test_login_required_fields(client):
    response = client.post("/api/v1/auth/login", json={"username": "alice"})
    assert response.status_code == 422
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_VALIDATION"
    assert payload["request_id"].startswith("req_")


def test_create_login_me_logout(client, alice):
    anonymous = client.get("/api/v1/me")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "E_UNAUTHORIZED"

    wrong = _login(client, alice["username"], "wrong-password")
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "E_AUTH_INVALID_CREDENTIALS"

    login = _login(client, alice["username"], alice["password"])
    assert login.status_code == 200
    body = login.json()
    assert body["token"].startswith("ho_sess_")
    assert body["user"]["username"] == "alice"
    assert body["expires_at"]

    token = body["token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["auth_method"] == "session"
    assert me_body["user"]["id"] == alice["id"]
    assert "password_hash" not in me_body["user"]

    logout = client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 204
    after = client.get("/api/v1/me", headers=headers)
    assert after.status_code == 401


def test_admin_user_management_and_audit(client, alice):
    token = _login(client, alice["username"], alice["password"]).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "username": "bob",
            "password": "bob-password",
            "email": "bob@example.com",
            "display_name": "Bob",
        },
    )
    assert created.status_code == 201
    bob_id = created.json()["id"]

    duplicate = client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "bob", "password": "bob-password"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "E_CONFLICT"

    listed = client.get("/api/v1/users", headers=headers)
    assert listed.status_code == 200
    usernames = [item["username"] for item in listed.json()["items"]]
    assert usernames == ["alice", "bob"]

    updated = client.patch(
        f"/api/v1/users/{bob_id}",
        headers=headers,
        json={"display_name": "Bob Updated"},
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Bob Updated"

    events = client.get(
        "/api/v1/audit/events", headers=headers, params={"action": "user.create"}
    )
    assert events.status_code == 200
    assert len(events.json()["items"]) == 1
    assert events.json()["items"][0]["outcome"] == "success"


def test_non_admin_cannot_manage_users(client, alice):
    token = _login(client, alice["username"], alice["password"]).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "carol", "password": "carol-password"},
    )
    assert created.status_code == 201

    login = _login(client, "carol", "carol-password")
    carol_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    forbidden = client.get("/api/v1/users", headers=carol_headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "E_FORBIDDEN"
