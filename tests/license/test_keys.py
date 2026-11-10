"""密钥生成 / 落盘 / 隔离性测试。"""

from __future__ import annotations

import os
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from happyone.license import codec, keys


def test_load_or_create_writes_key_and_keeps_kid(tmp_path):
    key_path = tmp_path / "keys" / "license_ed25519.pem"
    pair = keys.load_or_create(key_path)

    assert key_path.exists()
    assert pair.kid.startswith("k_")

    reloaded = keys.load_or_create(key_path)
    assert reloaded.kid == pair.kid
    assert (
        reloaded.public_key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        == pair.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


@pytest.mark.skipif(os.name != "posix", reason="Windows 无 POSIX 权限位，改用 ACL")
def test_private_key_permissions_are_600(tmp_path):
    key_path = tmp_path / "license_ed25519.pem"
    keys.load_or_create(key_path)
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600


def test_key_file_is_pem_pkcs8(tmp_path):
    key_path = tmp_path / "license_ed25519.pem"
    keys.load_or_create(key_path)
    content = key_path.read_text(encoding="ascii")
    assert content.startswith("-----BEGIN PRIVATE KEY-----")


def test_ensure_isolated_path_rejects_hub_secret_name(tmp_path):
    with pytest.raises(keys.KeyPathError, match="物理分离"):
        keys.ensure_isolated_path(tmp_path / "data" / ".secret")


def test_ensure_isolated_path_rejects_same_file_as_hub(tmp_path):
    hub_path = tmp_path / "hub" / "signing.key"
    with pytest.raises(keys.KeyPathError, match="物理分离"):
        keys.ensure_isolated_path(hub_path, hub_secret_path=hub_path)


def test_ensure_isolated_path_rejects_path_inside_hub_dir(tmp_path):
    hub_dir = tmp_path / "hub-secrets"
    with pytest.raises(keys.KeyPathError, match="物理分离"):
        keys.ensure_isolated_path(hub_dir / "license.pem", hub_secret_path=hub_dir / "signing.key")


def test_ensure_isolated_path_accepts_independent_path(tmp_path):
    path = keys.ensure_isolated_path(tmp_path / "license" / "ed25519.pem", hub_secret_path=tmp_path / "hub" / ".secret")
    assert path == (tmp_path / "license" / "ed25519.pem").resolve()


def test_public_key_info_can_verify_token_with_standard_library(keypair):
    token = codec.sign_payload({"hello": "世界"}, keypair.private_key)
    info = keys.public_key_info(keypair)

    raw = bytes.fromhex(str(info["public_key_hex"]))
    public_key = Ed25519PublicKey.from_public_bytes(raw)

    assert codec.verify_token(token, public_key) == {"hello": "世界"}
    assert info["kid"] == keypair.kid
    assert info["alg"] == "Ed25519"
    assert info["token_prefix"] == "hlic1"
