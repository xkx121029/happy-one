from __future__ import annotations

from happyone.channel.service import CODE_ALPHABET

SAFE_ALPHABET = set("23456789ABCDEFGHJKMNPQRSTUVWXYZ")


def test_create_code_uses_safe_alphabet_and_10min_ttl(create_pair) -> None:
    result = create_pair()
    code = result["code"]
    assert result["ok"] is True
    assert len(code) == 6
    assert set(code) <= SAFE_ALPHABET
    assert SAFE_ALPHABET == set(CODE_ALPHABET)
    assert not set(code) & set("01OIL")
    assert result["expires_in"] == 600
    assert result["state"] == "waiting_peer"
    assert result["direction"] == "mobile_to_desktop"
    assert result["transport"] == "direct"
    assert result["relay_available"] is False
    assert result["ws_path"] == "/ws/signal"


def test_join_establishes_session(create_pair, join_pair, client) -> None:
    created = create_pair("mobile", "iPhone")
    joined = join_pair(created["code"], "desktop", "MacBook")

    assert joined["session_id"] == created["session_id"]
    assert joined["token"] != created["token"]
    assert joined["peer"]["role"] == "mobile"
    assert joined["peer"]["device_name"] == "iPhone"
    assert joined["direction"] == "mobile_to_desktop"

    status = client.get(f"/channel/pair/{created['session_id']}").json()
    assert status["ok"] is True
    assert status["state"] == "paired"
    assert status["initiator"]["role"] == "mobile"
    assert status["joiner"]["role"] == "desktop"
    assert status["connection"]["initiator_online"] is False
    assert status["connection"]["joiner_online"] is False


def test_direction_is_derived_from_creator_role(create_pair, join_pair) -> None:
    created = create_pair("desktop")
    assert created["direction"] == "desktop_to_mobile"
    joined = join_pair(created["code"], "mobile")
    assert joined["direction"] == "desktop_to_mobile"
    assert joined["peer"]["role"] == "desktop"


def test_duplicate_code_use_is_rejected(create_pair, join_pair, client) -> None:
    created = create_pair()
    join_pair(created["code"])

    response = client.post(
        "/channel/pair/join", json={"code": created["code"], "role": "desktop"}
    )
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "code_used"
    assert body["error"]


def test_expired_code_is_rejected(create_pair, client, clock) -> None:
    created = create_pair()
    clock.advance(601)

    response = client.post(
        "/channel/pair/join", json={"code": created["code"], "role": "desktop"}
    )
    assert response.status_code == 410
    assert response.json()["code"] == "code_expired"

    status = client.get(f"/channel/pair/{created['session_id']}").json()
    assert status["state"] == "expired"
    assert status["close_reason"] == "code_expired"


def test_join_with_same_role_is_rejected(create_pair, client) -> None:
    created = create_pair("mobile")
    response = client.post(
        "/channel/pair/join", json={"code": created["code"], "role": "mobile"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "role_conflict"


def test_unknown_code_returns_404(create_pair, client) -> None:
    response = client.post(
        "/channel/pair/join", json={"code": "ABCDEF", "role": "desktop"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "code_not_found"


def test_code_format_is_validated(client) -> None:
    response = client.post("/channel/pair/join", json={"code": "ABC", "role": "desktop"})
    assert response.status_code == 400
    assert response.json()["code"] == "code_format"


def test_invalid_role_returns_chinese_error(client) -> None:
    response = client.post("/channel/pair", json={"role": "tablet"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_role"
    assert "mobile" in body["error"]


def test_broken_json_body_returns_structured_error(client) -> None:
    response = client.post(
        "/channel/pair",
        content=b"{not-json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json() == {"ok": False, "code": "bad_json", "error": "请求体不是合法的 JSON"}


def test_status_of_unknown_session_returns_404(client) -> None:
    response = client.get("/channel/pair/does-not-exist")
    assert response.status_code == 404
    assert response.json()["ok"] is False
    assert response.json()["code"] == "session_not_found"


def test_revoke_closes_session_and_invalidates_code(create_pair, join_pair, client) -> None:
    created = create_pair()
    join_pair(created["code"])

    response = client.post(
        f"/channel/pair/{created['session_id']}/revoke",
        json={"token": created["token"]},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "closed"

    status = client.get(f"/channel/pair/{created['session_id']}").json()
    assert status["state"] == "closed"
    assert status["close_reason"] == "revoked_by_mobile"

    retry = client.post(
        "/channel/pair/join", json={"code": created["code"], "role": "desktop"}
    )
    assert retry.status_code == 404
    assert retry.json()["code"] == "code_not_found"


def test_revoke_requires_member_token(create_pair, join_pair, client) -> None:
    created = create_pair()
    join_pair(created["code"])
    url = f"/channel/pair/{created['session_id']}/revoke"

    missing = client.post(url, json={})
    assert missing.status_code == 403
    assert missing.json()["code"] == "token_required"

    invalid = client.post(url, json={"token": "not-a-real-token"})
    assert invalid.status_code == 403
    assert invalid.json()["code"] == "token_invalid"


def test_joiner_token_can_revoke(create_pair, join_pair, client) -> None:
    created = create_pair()
    joined = join_pair(created["code"])
    response = client.post(
        f"/channel/pair/{created['session_id']}/revoke",
        json={"token": joined["token"]},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "closed"
