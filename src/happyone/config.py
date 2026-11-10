"""配置加载。

优先级：进程环境变量 > .env 文件 > 代码默认值。
不引入 pydantic-settings / python-dotenv，保持依赖面最小；.env 只解析
KEY=VALUE 形式，不支持变量插值。

对业务线的兼容：模块级属性 ``happyone.config.settings`` 惰性返回当前
Settings（PEP 562），由应用启动或 CLI 通过 ``set_global_settings`` 绑定。
"""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = Path("./data")


def _read_dotenv(path: Path) -> dict[str, str]:
    """读取 .env；文件不存在或格式异常时返回已解析部分。"""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


class Settings(BaseModel):
    """运行配置。实例不可变，避免请求期被意外改写。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    env: str = "dev"
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    log_level: str = "INFO"
    issuer: str = ""

    data_dir: Path = _DEFAULT_DATA_DIR
    db_path: Path | None = None
    oidc_key_path: Path | None = None

    session_ttl_sec: int = Field(default=7 * 24 * 3600, gt=0)
    access_token_ttl_sec: int = Field(default=3600, gt=0)
    refresh_token_ttl_sec: int = Field(default=30 * 24 * 3600, gt=0)
    auth_code_ttl_sec: int = Field(default=300, gt=0)
    device_code_ttl_sec: int = Field(default=600, gt=0)
    device_poll_interval_sec: int = Field(default=5, gt=0)

    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # 人机验证线：密钥仅从环境变量或持久化文件读取，绝不写日志
    captcha_secret: SecretStr | None = None

    # 许可证线
    license_key_path: Path | None = None
    license_grace_days: int = 7
    hub_secret_path: Path | None = None

    # 内容面（happy-hub）：由存储接入线消费，内核不直连
    hub_base_url: str | None = None
    hub_token: SecretStr | None = None
    hub_timeout_sec: float = 10.0
    hub_max_retries: int = 2
    storage_chunk_size: int = 8 * 1024 * 1024
    storage_multipart_threshold: int = 8 * 1024 * 1024
    storage_sign_ttl_sec: int = 300

    # 通道信令线
    channel_pair_code_ttl: float = 600.0
    channel_session_ttl: float = 1800.0
    channel_heartbeat_timeout: float = 30.0
    channel_sweep_interval: float = 30.0
    channel_max_sessions: int = 64
    channel_max_signal_bytes: int = 64 * 1024

    def model_post_init(self, __context: object) -> None:
        if self.license_key_path is None:
            object.__setattr__(
                self, "license_key_path", self.data_dir / "keys" / "license_ed25519.pem"
            )

    @property
    def database_path(self) -> Path:
        return self.db_path or (self.data_dir / "happyone.db")

    @property
    def oidc_signing_key_path(self) -> Path:
        return self.oidc_key_path or (self.data_dir / "keys" / "oidc_ed25519.pem")

    @property
    def captcha_secret_path(self) -> Path:
        return self.data_dir / "keys" / "captcha_secret"

    @property
    def hub_app_token(self) -> SecretStr | None:
        """兼容旧名称；存储线读取的是 hub_token。"""
        return self.hub_token

    @property
    def migrations_path(self) -> Path:
        return _PACKAGE_DIR / "migrations"

    def ensure_dirs(self) -> None:
        """创建运行期目录；迁移会创建数据库文件，这里只建父目录。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.oidc_signing_key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.license_key_path is not None:
            self.license_key_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> "Settings":
        dotenv = _read_dotenv(Path(dotenv_path))

        def raw(name: str, *aliases: str) -> str | None:
            for key in (name, *aliases):
                value = os.environ.get(key)
                if value:
                    return value
                value = dotenv.get(key)
                if value:
                    return value
            return None

        def text(name: str, default: str, *aliases: str) -> str:
            return raw(name, *aliases) or default

        def number(name: str, default: int, *aliases: str) -> int:
            value = raw(name, *aliases)
            if value is None:
                return default
            try:
                return int(value)
            except ValueError:
                return default

        def decimal(name: str, default: float, *aliases: str) -> float:
            value = raw(name, *aliases)
            if value is None:
                return default
            try:
                return float(value)
            except ValueError:
                return default

        def path_value(name: str, *aliases: str) -> Path | None:
            value = raw(name, *aliases)
            return Path(value) if value else None

        host = text("HAPPYONE_HOST", "127.0.0.1")
        port = number("HAPPYONE_PORT", 8787)
        issuer = text("HAPPYONE_ISSUER", f"http://{host}:{port}").rstrip("/")

        admin_password = raw("HAPPYONE_BOOTSTRAP_ADMIN_PASSWORD")
        hub_token = raw("HAPPYONE_HUB_TOKEN", "HAPPYONE_HUB_APP_TOKEN")
        captcha_secret = raw("HAPPYONE_CAPTCHA_SECRET", "CAPTCHA_SECRET")

        return cls(
            env=text("HAPPYONE_ENV", "dev"),
            host=host,
            port=port,
            log_level=text("HAPPYONE_LOG_LEVEL", "INFO").upper(),
            issuer=issuer,
            data_dir=path_value("HAPPYONE_DATA_DIR") or _DEFAULT_DATA_DIR,
            db_path=path_value("HAPPYONE_DB_PATH"),
            oidc_key_path=path_value("HAPPYONE_OIDC_KEY_PATH"),
            session_ttl_sec=number("HAPPYONE_SESSION_TTL_SEC", 7 * 24 * 3600),
            access_token_ttl_sec=number("HAPPYONE_ACCESS_TOKEN_TTL_SEC", 3600),
            refresh_token_ttl_sec=number("HAPPYONE_REFRESH_TOKEN_TTL_SEC", 30 * 24 * 3600),
            auth_code_ttl_sec=number("HAPPYONE_AUTH_CODE_TTL_SEC", 300),
            device_code_ttl_sec=number("HAPPYONE_DEVICE_CODE_TTL_SEC", 600),
            device_poll_interval_sec=number("HAPPYONE_DEVICE_POLL_INTERVAL_SEC", 5),
            bootstrap_admin_username=raw("HAPPYONE_BOOTSTRAP_ADMIN_USERNAME"),
            bootstrap_admin_password=(
                SecretStr(admin_password) if admin_password else None
            ),
            captcha_secret=SecretStr(captcha_secret) if captcha_secret else None,
            license_key_path=path_value(
                "HAPPYONE_LICENSE_KEY_PATH", "HAPPYONE_LICENSE_PRIVATE_KEY_PATH"
            ),
            license_grace_days=number("HAPPYONE_LICENSE_GRACE_DAYS", 7),
            hub_secret_path=path_value("HAPPYONE_HUB_SECRET_PATH"),
            hub_base_url=raw("HAPPYONE_HUB_BASE_URL"),
            hub_token=SecretStr(hub_token) if hub_token else None,
            hub_timeout_sec=decimal("HAPPYONE_HUB_TIMEOUT_SEC", 10.0),
            hub_max_retries=number("HAPPYONE_HUB_MAX_RETRIES", 2),
            storage_chunk_size=number(
                "HAPPYONE_STORAGE_CHUNK_SIZE", 8 * 1024 * 1024
            ),
            storage_multipart_threshold=number(
                "HAPPYONE_STORAGE_MULTIPART_THRESHOLD", 8 * 1024 * 1024
            ),
            storage_sign_ttl_sec=number("HAPPYONE_STORAGE_SIGN_TTL_SEC", 300),
            channel_pair_code_ttl=decimal("HAPPYONE_CHANNEL_PAIR_CODE_TTL", 600.0),
            channel_session_ttl=decimal("HAPPYONE_CHANNEL_SESSION_TTL", 1800.0),
            channel_heartbeat_timeout=decimal(
                "HAPPYONE_CHANNEL_HEARTBEAT_TIMEOUT", 30.0
            ),
            channel_sweep_interval=decimal("HAPPYONE_CHANNEL_SWEEP_INTERVAL", 30.0),
            channel_max_sessions=number("HAPPYONE_CHANNEL_MAX_SESSIONS", 64),
            channel_max_signal_bytes=number(
                "HAPPYONE_CHANNEL_MAX_SIGNAL_BYTES", 64 * 1024
            ),
        )


def with_persisted_secrets(settings: Settings) -> Settings:
    """确保 captcha 密钥在重启后仍然有效。

    环境变量优先；未配置时读取 ``<data_dir>/keys/captcha_secret``，
    文件不存在则生成并以 0600 落盘（Windows 尽力而为）。密钥不进入日志。
    """
    if settings.captcha_secret is not None:
        return settings

    path = settings.captcha_secret_path
    value = ""
    if path.is_file():
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
    if not value:
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(48)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return settings.model_copy(update={"captcha_secret": SecretStr(value)})


_global_settings: Settings | None = None
_global_lock = threading.Lock()


def set_global_settings(settings: Settings) -> None:
    """绑定当前进程的配置，供四条业务线的兼容接口读取。"""
    global _global_settings
    with _global_lock:
        _global_settings = settings


def get_global_settings() -> Settings:
    global _global_settings
    if _global_settings is None:
        with _global_lock:
            if _global_settings is None:
                _global_settings = Settings.from_env()
    return _global_settings


def __getattr__(name: str):
    """PEP 562：业务线通过 ``from happyone.config import settings`` 取配置。"""
    if name == "settings":
        return get_global_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
