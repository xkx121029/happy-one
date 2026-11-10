"""插件挂载：四条业务线就位、失败隔离、密钥持久化。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from happyone.config import Settings, with_persisted_secrets
from happyone.core import plugins
from happyone.db import Database


def test_all_plugins_loaded(client, app):
    assert app.state.plugins == {
        "license": "loaded",
        "storage": "loaded",
        "channel": "loaded",
        "captcha": "loaded",
    }

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["plugins"]["captcha"] == "loaded"

    meta = client.get("/api/v1/meta")
    assert "captcha" in meta.json()["plugins"]

    # 人机验证线作为第一个挂载样本，实际发起一次请求
    challenge = client.post("/captcha/v1/challenge")
    assert challenge.status_code == 200
    assert challenge.json()


def test_missing_plugin_is_skipped(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(plugins, "PLUGIN_PACKAGES", ("not_a_real_line",))
    app = FastAPI()
    app.state.db = Database(tmp_path / "test.db")
    app.state.settings = Settings(data_dir=tmp_path)

    result = plugins.mount_plugins(app)
    assert result == {"not_a_real_line": "skipped"}
    assert app.routes  # FastAPI 自带路由仍在，应用未被拖垮


def test_captcha_secret_is_persisted_and_masked(tmp_path: Path):
    base = Settings(data_dir=tmp_path / "data")

    first = with_persisted_secrets(base)
    assert first.captcha_secret is not None
    first_value = first.captcha_secret.get_secret_value()
    assert len(first_value) >= 32
    assert "**********" in repr(first)
    assert first_value not in repr(first)
    assert (tmp_path / "data" / "keys" / "captcha_secret").is_file()

    # 重启（重新解析）后密钥保持一致，未核销票据仍可校验
    second = with_persisted_secrets(Settings(data_dir=tmp_path / "data"))
    assert second.captcha_secret is not None
    assert second.captcha_secret.get_secret_value() == first_value


def test_global_settings_binding(app):
    from happyone.config import settings as global_settings

    assert global_settings.issuer == app.state.settings.issuer
    assert global_settings.captcha_secret is not None
