"""设备绑定与设备数上限测试。"""

from __future__ import annotations

import pytest

from happyone.license.models import ErrorCode
from happyone.license.service import LicenseService, LicenseValidationError, hash_device_fingerprint


def test_bound_device_verifies_only_on_same_device(issue, service):
    issued = issue(device_fingerprint="  machine-A-fingerprint  ")

    ok = service.verify(issued.token, device_fingerprint="machine-A-fingerprint")
    assert ok.ok is True
    assert ok.payload.device.fp_hash == hash_device_fingerprint("machine-A-fingerprint")

    other = service.verify(issued.token, device_fingerprint="machine-B-fingerprint")
    assert other.ok is False
    assert other.code == ErrorCode.DEVICE_MISMATCH
    assert "设备指纹" in other.error


def test_bound_device_requires_fingerprint(issue, service):
    issued = issue(device_fingerprint="machine-A")
    result = service.verify(issued.token)
    assert result.ok is False
    assert result.code == ErrorCode.DEVICE_REQUIRED


def test_device_limit_is_enforced(issue, service, conn):
    issued = issue(max_devices=2)
    license_id = issued.payload.license_id

    assert service.verify(issued.token, device_fingerprint="dev-1").ok is True
    assert service.verify(issued.token, device_fingerprint="dev-2").ok is True

    third = service.verify(issued.token, device_fingerprint="dev-3")
    assert third.ok is False
    assert third.code == ErrorCode.DEVICE_LIMIT
    assert "2" in third.error

    again = service.verify(issued.token, device_fingerprint="dev-1")
    assert again.ok is True

    rows = conn.execute(
        "SELECT device_hash FROM license_device WHERE license_id = ? ORDER BY device_hash",
        (license_id,),
    ).fetchall()
    stored = {row[0] for row in rows}
    assert stored == {
        hash_device_fingerprint("dev-1"),
        hash_device_fingerprint("dev-2"),
    }
    # 数据库只存哈希，不存指纹原文
    assert len(stored) == 2
    assert all(len(item) == 64 for item in stored)


def test_device_limit_requires_fingerprint_when_online(issue, service):
    issued = issue(max_devices=3)
    result = service.verify(issued.token)
    assert result.ok is False
    assert result.code == ErrorCode.DEVICE_REQUIRED


def test_offline_mode_cannot_enforce_device_count(keypair, store, issue):
    issued = issue(max_devices=2)
    offline = LicenseService(keypair, store=None, grace_days=7, clock=lambda: 1_800_000_000)
    result = offline.verify(issued.token, device_fingerprint="dev-1")
    assert result.ok is True
    assert any("离线" in warning for warning in result.warnings)


def test_fingerprint_with_multiple_devices_is_rejected(issue):
    with pytest.raises(LicenseValidationError, match="max_devices"):
        issue(device_fingerprint="machine-A", max_devices=2)


def test_fingerprint_too_long_is_rejected(issue):
    with pytest.raises(LicenseValidationError, match="512"):
        issue(device_fingerprint="x" * 513)


def test_device_binding_is_visible_in_payload(issue, service):
    issued = issue(device_fingerprint="machine-A")
    result = service.verify(issued.token, device_fingerprint="machine-A")
    data = result.to_envelope()["data"]
    assert data["device"] == {
        "max_devices": 1,
        "fp_hash": hash_device_fingerprint("machine-A"),
    }
