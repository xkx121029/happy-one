"""令牌格式、验签与篡改检测测试。"""

from __future__ import annotations

import json

from happyone.license import codec
from happyone.license.keys import generate_keypair
from happyone.license.models import ErrorCode, PAYLOAD_TYPE_REVOCATION
from happyone.license.service import LicenseService, verify_revocation_snapshot


def _issue_token(service, issue):
    return issue()


def test_token_shape_is_three_segments_with_prefix(service, issue):
    token = _issue_token(service, issue).token
    parts = token.split(".")
    assert len(parts) == 3
    assert parts[0] == "hlic1"
    assert "=" not in token  # base64url 无填充


def test_canonical_json_is_order_independent():
    left = codec.canonical_json({"b": 1, "a": [1, 2]})
    right = codec.canonical_json({"a": [1, 2], "b": 1})
    assert left == right == b'{"a":[1,2],"b":1}'


def test_tampered_payload_must_fail_signature(service, issue):
    token = _issue_token(service, issue).token
    raw, signature, _signing_input = codec.decode_payload(token)

    raw["features"] = ["pro", "unlimited"]
    body = codec.b64u_encode(codec.canonical_json(raw))
    tampered = f"hlic1.{body}.{codec.b64u_encode(signature)}"
    assert tampered != token

    result = service.verify(tampered)
    assert result.ok is False
    assert result.code == ErrorCode.INVALID_SIGNATURE


def test_tampered_signature_must_fail(service, issue):
    token = _issue_token(service, issue).token
    prefix, body, signature = token.split(".")
    raw_signature = bytearray(codec.b64u_decode(signature))
    raw_signature[0] ^= 0xFF
    tampered = f"{prefix}.{body}.{codec.b64u_encode(bytes(raw_signature))}"

    result = service.verify(tampered)
    assert result.ok is False
    assert result.code == ErrorCode.INVALID_SIGNATURE


def test_signature_from_other_key_must_fail(service, issue):
    """公钥不匹配：kid 相同但返回的是另一把公钥。"""
    token = _issue_token(service, issue).token
    other = generate_keypair()

    mismatched = LicenseService(
        service.key_pair,
        store=None,
        grace_days=7,
        public_key_provider=lambda kid: other.public_key,
    )
    result = mismatched.verify(token)
    assert result.ok is False
    assert result.code == ErrorCode.INVALID_SIGNATURE


def test_unknown_kid_reports_unknown_key(service, issue):
    token = _issue_token(service, issue).token
    service_with_other_key = LicenseService(
        generate_keypair(),
        store=None,
        grace_days=7,
        public_key_provider=lambda kid: None,
    )
    result = service_with_other_key.verify(token)
    assert result.ok is False
    assert result.code == ErrorCode.UNKNOWN_KEY
    assert "kid" in result.error


def test_malformed_tokens_are_rejected_with_chinese_reason(service):
    for token in ["", "abc", "hlic1.only-two", "hlic2.abc.def", "hlic1.!!!.???"]:
        result = service.verify(token)
        assert result.ok is False
        assert result.code == ErrorCode.MALFORMED
        assert "许可证" in result.error


def test_snapshot_token_is_not_accepted_as_license(service, issue):
    _issue_token(service, issue)
    snapshot = service.revocation_snapshot()
    result = service.verify(snapshot.token)
    assert result.ok is False
    assert result.code == ErrorCode.WRONG_TYPE


def test_license_token_is_not_accepted_as_snapshot(service, issue):
    token = _issue_token(service, issue).token
    result = verify_revocation_snapshot(token, lambda kid: service.key_pair.public_key)
    assert result.ok is False
    assert result.code == ErrorCode.SNAPSHOT_WRONG_TYPE


def test_tampered_snapshot_must_fail_signature(service, issue):
    _issue_token(service, issue)
    snapshot = service.revocation_snapshot()
    raw, signature, _signing_input = codec.decode_payload(snapshot.token)

    raw["entries"] = [{"license_id": "lic_forged", "revoked_at": 1, "reason": "伪造"}]
    body = codec.b64u_encode(codec.canonical_json(raw))
    tampered = f"hlic1.{body}.{codec.b64u_encode(signature)}"

    result = verify_revocation_snapshot(tampered, lambda kid: service.key_pair.public_key)
    assert result.ok is False
    assert result.code == ErrorCode.SNAPSHOT_INVALID_SIGNATURE


def test_snapshot_typ_constant_matches_payload():
    assert PAYLOAD_TYPE_REVOCATION == "revocation_snapshot"
    assert json.dumps({"typ": PAYLOAD_TYPE_REVOCATION}) == '{"typ": "revocation_snapshot"}'
