"""key 规范与桶映射。"""

from __future__ import annotations

import pytest

from happyone.storage import keys


def test_namespace_buckets_are_fixed():
    assert keys.bucket_for("relay") == "relay"
    assert keys.bucket_for("license") == "license-artifacts"
    assert keys.bucket_for("BACKUP") == "backup"


def test_unknown_namespace_rejected():
    with pytest.raises(ValueError, match="未知的存储命名空间"):
        keys.bucket_for("public")


def test_empty_namespace_message_is_chinese():
    with pytest.raises(ValueError, match="存储命名空间不能为空"):
        keys.bucket_for("")


def test_content_addressed_key_uses_sha256():
    digest = "a" * 64
    assert keys.content_addressed_key("relay", digest) == f"relay/{digest}"


def test_content_addressed_key_rejects_bad_hash():
    with pytest.raises(ValueError, match="64 位"):
        keys.content_addressed_key("relay", "xyz")
    with pytest.raises(ValueError, match="64 位"):
        keys.content_addressed_key("relay", "g" * 64)


def test_content_addressed_key_normalizes_uppercase_hash():
    assert keys.content_addressed_key("relay", "A" * 64) == f"relay/{'a' * 64}"


def test_entity_key_normalizes_case_and_traversal():
    key = keys.entity_key("license", "License", "LIC-001", "../../授权书 v2.PDF")
    assert key.startswith("license/license/lic-001/")
    assert key.endswith(".pdf")
    assert ".." not in key
    assert key == key.lower()


def test_sanitize_filename_adds_digest_for_non_ascii():
    first = keys.sanitize_filename("授权书.pdf")
    second = keys.sanitize_filename("许可证.pdf")
    assert first.endswith(".pdf") and second.endswith(".pdf")
    assert first != second


def test_sanitize_filename_keeps_plain_ascii_clean():
    assert keys.sanitize_filename("Report-Final.PDF") == "report-final.pdf"


def test_sanitize_filename_rejects_empty():
    assert keys.sanitize_filename("...").startswith("file-")


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "/relay/x",
        "relay/",
        "relay//x",
        "relay/../x",
        "relay/./x",
        "relay/Upper",
        "relay/a b",
        "relay/a?b",
        "relay/a\\b",
    ],
)
def test_validate_key_rejects_invalid(bad_key):
    with pytest.raises(ValueError):
        keys.validate_key(bad_key)


def test_validate_key_accepts_entity_key():
    key = keys.entity_key("relay", "message", "MSG_01", "a.txt")
    assert keys.validate_key(key) == key
    assert not keys.is_content_addressed(key)


def test_is_content_addressed():
    assert keys.is_content_addressed(f"backup/{'b' * 64}")
    assert not keys.is_content_addressed("backup/entity/id/file.txt")
