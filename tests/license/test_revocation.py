"""吊销与吊销快照测试。"""

from __future__ import annotations

import pytest

from happyone.license.keys import generate_keypair
from happyone.license.models import ErrorCode
from happyone.license.service import (
    LicenseAlreadyRevokedError,
    LicenseNotFoundError,
    LicenseService,
    LicenseServiceUnavailable,
    verify_revocation_snapshot,
)

FIXED_NOW = 1_800_000_000


def test_revoked_license_fails_verification(service, issue):
    issued = issue()
    entry = service.revoke(issued.payload.license_id, reason="用户申请退款", revoked_by="admin")

    assert entry.reason == "用户申请退款"

    result = service.verify(issued.token)
    assert result.ok is False
    assert result.code == ErrorCode.REVOKED
    assert "吊销" in result.error
    assert "用户申请退款" in result.error

    described = service.describe(issued.payload.license_id)
    assert described["revoked"] is True
    assert described["revoked_reason"] == "用户申请退款"


def test_revoke_unknown_license_fails(service):
    with pytest.raises(LicenseNotFoundError, match="未找到"):
        service.revoke("lic_not_exists")


def test_double_revoke_is_rejected(service, issue):
    issued = issue()
    service.revoke(issued.payload.license_id)
    with pytest.raises(LicenseAlreadyRevokedError):
        service.revoke(issued.payload.license_id)


def test_revocation_requires_store(keypair):
    offline = LicenseService(keypair, store=None, grace_days=7, clock=lambda: FIXED_NOW)
    with pytest.raises(LicenseServiceUnavailable):
        offline.revoke("lic_x")
    with pytest.raises(LicenseServiceUnavailable):
        offline.revocation_snapshot()


def test_snapshot_is_signed_and_verifiable_offline(service, issue):
    first = issue()
    second = issue(user_id="user_002")
    service.revoke(first.payload.license_id, reason="违规")

    snapshot = service.revocation_snapshot(now=FIXED_NOW)
    assert snapshot.ok is True
    assert snapshot.kid == service.key_pair.kid
    assert len(snapshot.entries) == 1
    assert snapshot.payload["count"] == 1
    assert snapshot.payload["entries"][0]["license_id"] == first.payload.license_id

    # 客户端视角：只拿公钥 + 快照字符串
    verified = verify_revocation_snapshot(snapshot.token, lambda kid: service.key_pair.public_key)
    assert verified.ok is True
    assert [entry.license_id for entry in verified.entries] == [first.payload.license_id]
    assert service.snapshot_contains(verified, first.payload.license_id) is True
    assert service.snapshot_contains(verified, second.payload.license_id) is False


def test_snapshot_with_wrong_public_key_fails(service, issue):
    issue()
    snapshot = service.revocation_snapshot(now=FIXED_NOW)
    other = generate_keypair()

    result = verify_revocation_snapshot(snapshot.token, lambda kid: other.public_key)
    assert result.ok is False
    assert result.code == ErrorCode.SNAPSHOT_INVALID_SIGNATURE

    unknown = verify_revocation_snapshot(snapshot.token, lambda kid: None)
    assert unknown.ok is False
    assert unknown.code == ErrorCode.SNAPSHOT_UNKNOWN_KEY


def test_snapshot_reflects_new_revocations(service, issue):
    issued = issue()
    empty = service.revocation_snapshot(now=FIXED_NOW)
    assert empty.payload["count"] == 0

    service.revoke(issued.payload.license_id)
    filled = service.revocation_snapshot(now=FIXED_NOW + 60)
    assert filled.payload["count"] == 1
    assert filled.issued_at == FIXED_NOW + 60


def test_revocation_is_isolated_per_license(service, issue):
    first = issue()
    second = issue(user_id="user_003")
    service.revoke(first.payload.license_id)

    assert service.verify(first.token).code == ErrorCode.REVOKED
    unaffected = service.verify(second.token)
    assert unaffected.ok is True
