"""签发、正常验签、过期与宽限期边界测试。"""

from __future__ import annotations

import pytest

from happyone.license.models import ErrorCode, IssueRequest, STATUS_IN_GRACE, STATUS_VALID
from happyone.license.service import LicenseService, LicenseValidationError

DAY = 86_400


def test_issue_and_verify_ok(service, issue):
    issued = issue()
    result = service.verify(issued.token)

    assert result.ok is True
    assert result.status == STATUS_VALID
    assert result.grace_days_left is None
    assert result.payload is not None
    assert result.payload.user_id == "user_001"
    assert result.payload.product_slug == "happy-one"
    assert result.payload.features == ("core", "export")
    assert result.payload.kid == service.key_pair.kid
    assert result.payload.expires_at == issued.payload.expires_at

    envelope = result.to_envelope()
    assert envelope["ok"] is True
    assert envelope["data"]["status"] == STATUS_VALID
    assert envelope["data"]["features"] == ["core", "export"]


def test_issued_license_is_persisted(service, issue):
    issued = issue()
    record = service.get(issued.payload.license_id)
    assert record is not None
    assert record.token == issued.token
    assert record.expires_at == issued.payload.expires_at

    described = service.describe(issued.payload.license_id)
    assert described["revoked"] is False
    assert described["token"] == issued.token


def test_features_are_normalized(service, issue):
    issued = issue(features=("  core ", "core", "beta"))
    assert issued.payload.features == ("core", "beta")


def test_expiry_boundary(service, issue):
    expires_at = 1_800_000_000 + 10 * DAY
    issued = issue(expires_at=expires_at)

    at_expiry = service.verify(issued.token, now=expires_at)
    assert at_expiry.ok is True
    assert at_expiry.status == STATUS_VALID

    one_second_after = service.verify(issued.token, now=expires_at + 1)
    assert one_second_after.ok is True
    assert one_second_after.status == STATUS_IN_GRACE
    assert 6.9 < (one_second_after.grace_days_left or 0) <= 7
    assert any("宽限期" in warning for warning in one_second_after.warnings)

    last_grace_second = service.verify(issued.token, now=expires_at + 7 * DAY)
    assert last_grace_second.ok is True
    assert last_grace_second.status == STATUS_IN_GRACE
    assert last_grace_second.grace_days_left == 0

    beyond_grace = service.verify(issued.token, now=expires_at + 7 * DAY + 1)
    assert beyond_grace.ok is False
    assert beyond_grace.code == ErrorCode.EXPIRED
    assert "过期" in beyond_grace.error
    assert "宽限期" in beyond_grace.error


def test_zero_grace_days_expires_immediately(keypair, store):
    strict = LicenseService(keypair, store=store, grace_days=0, clock=lambda: 1_800_000_000)
    issued = strict.issue(
        IssueRequest(user_id="u", product_slug="p", features=(), expires_in_days=1),
        now=1_800_000_000,
    )
    assert strict.verify(issued.token, now=1_800_000_000 + DAY).ok is True
    result = strict.verify(issued.token, now=1_800_000_000 + DAY + 1)
    assert result.ok is False
    assert result.code == ErrorCode.EXPIRED


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"expires_in_days": None}, "expires_at 或 expires_in_days"),
        ({"expires_at": 1, "expires_in_days": 1}, "只能二选一"),
        ({"expires_at": 1}, "必须晚于签发时间"),
        ({"expires_in_days": 0}, "1 到 3650"),
        ({"expires_in_days": 4000}, "1 到 3650"),
        ({"max_devices": 0}, "1 到 1000"),
        ({"max_devices": 1001}, "1 到 1000"),
        ({"user_id": "   "}, "user_id"),
        ({"product_slug": ""}, "product_slug"),
    ],
)
def test_issue_validation(issue, overrides, message):
    with pytest.raises(LicenseValidationError) as excinfo:
        issue(**overrides)
    assert message in excinfo.value.message


def test_duplicate_license_id_is_rejected(service, issue):
    issue(license_id="lic_custom_001")
    with pytest.raises(LicenseValidationError, match="已存在"):
        issue(license_id="lic_custom_001")


def test_invalid_license_id_is_rejected(issue):
    with pytest.raises(LicenseValidationError, match="license_id"):
        issue(license_id="含中文的编号")
