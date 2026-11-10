"""默认装配（deps 兜底 + 路由单例）集成测试。

验证骨架线尚未提供 happyone.config / happyone.db 时，模块仍能：
用环境变量指定私钥与数据库位置、自动建密钥、自动建表、签发与验签闭环。
"""

from __future__ import annotations

import pytest

import happyone.config as config_module
from happyone.license import deps
from happyone.license import router as router_module
from happyone.license.models import IssueRequest


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    key_path = tmp_path / "keys" / "license_ed25519.pem"
    db_path = tmp_path / "data" / "happyone.db"
    monkeypatch.setenv("HAPPYONE_LICENSE_KEY_PATH", str(key_path))
    monkeypatch.setenv("HAPPYONE_DB_PATH", str(db_path))
    monkeypatch.delenv("HAPPYONE_LICENSE_GRACE_DAYS", raising=False)
    # 配置模块有进程级缓存，测试之间需要清空才能读到新的环境变量
    monkeypatch.setattr(config_module, "_global_settings", None, raising=False)
    # 骨架线 db.get_conn 由其自身维护，这里单独验证本模块的兜底路径
    monkeypatch.setattr(deps, "get_db_conn", deps._fallback_db_conn)
    router_module.reset_license_service()
    try:
        yield key_path, db_path
    finally:
        router_module.reset_license_service()


def test_default_service_creates_key_and_schema(isolated_env):
    key_path, db_path = isolated_env
    service = router_module.build_license_service()

    assert key_path.exists()
    assert service.grace_days == 7

    issued = service.issue(IssueRequest(user_id="u1", product_slug="happy-one", expires_in_days=10))
    assert service.verify(issued.token).ok is True
    assert db_path.exists()
    assert service.get(issued.payload.license_id) is not None


def test_rebuild_reuses_same_key(isolated_env):
    first = router_module.build_license_service()
    kid = first.key_pair.kid
    issued = first.issue(IssueRequest(user_id="u1", product_slug="happy-one", expires_in_days=10))

    router_module.reset_license_service()
    second = router_module.get_license_service()
    assert second is not None
    assert second.key_pair.kid == kid
    # 重启后旧许可证仍然可通过验签
    assert second.verify(issued.token).ok is True


def test_key_path_conflict_disables_service(tmp_path, monkeypatch):
    monkeypatch.setenv("HAPPYONE_LICENSE_KEY_PATH", str(tmp_path / "data" / ".secret"))
    monkeypatch.setenv("HAPPYONE_HUB_SECRET_PATH", str(tmp_path / "hub" / ".secret"))
    monkeypatch.setenv("HAPPYONE_DB_PATH", str(tmp_path / "happyone.db"))
    monkeypatch.setattr(config_module, "_global_settings", None, raising=False)
    monkeypatch.setattr(deps, "get_db_conn", deps._fallback_db_conn)
    router_module.reset_license_service()

    assert router_module.get_license_service() is None
    router_module.reset_license_service()


def test_get_db_conn_prefers_skeleton(monkeypatch):
    import happyone.db as skeleton_db

    sentinel = object()
    monkeypatch.setattr(skeleton_db, "get_conn", lambda: sentinel)
    assert deps.get_db_conn() is sentinel
