"""FastAPI 应用装配。

职责：生命周期（目录、迁移、密钥、引导管理员）、请求追踪、统一错误处理、
内核三线静态挂载、四条业务线动态挂载、健康检查。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from happyone import __version__
from happyone.audit.router import router as audit_router
from happyone.audit.service import AuditService
from happyone.authz.router import router as authz_router
from happyone.authz.service import OAuthService
from happyone.config import Settings, set_global_settings, with_persisted_secrets
from happyone.core import errors
from happyone.core.errors import AppError, OAuthError
from happyone.core.ids import new_id
from happyone.core.plugins import mount_plugins
from happyone.core.security import Ed25519Signer
from happyone.db import Database, bind_database
from happyone.identity.router import router as identity_router
from happyone.identity.service import IdentityService

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_log = logging.getLogger("happyone")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value or new_id("req")


def create_app(settings: Settings | None = None) -> FastAPI:
    # captcha 密钥在构造期解析，保证插件 deps.configure 拿到完整配置
    app_settings = with_persisted_secrets(settings or Settings.from_env())
    set_global_settings(app_settings)
    _configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = time.monotonic()
        app_settings.ensure_dirs()
        database: Database = app.state.db

        # 迁移失败直接中止启动（fail-closed）
        migration = database.migrate(app_settings.migrations_path)
        _log.info(
            "迁移完成：本次执行 %s 个，目录共 %s 个",
            len(migration.applied),
            migration.total,
        )

        signer = Ed25519Signer.load_or_create(app_settings.oidc_signing_key_path)
        app.state.oidc_signer = signer
        _log.info("OIDC 签名密钥就绪，kid=%s", signer.kid)

        identity = IdentityService(database, app_settings)
        oauth = OAuthService(database, app_settings, signer, users=identity)
        identity.purge_expired()
        oauth.purge_expired()

        _bootstrap_admin(database, identity, app_settings)

        app.state.ready = True
        app.state.started_at = started
        AuditService(database).record(
            action="system.start",
            outcome="success",
            detail={
                "version": __version__,
                "plugins": app.state.plugins,
                "migrations_applied": migration.applied,
            },
        )
        _log.info(
            "happy-one 已就绪：issuer=%s，插件=%s",
            app_settings.issuer,
            app.state.plugins,
        )
        try:
            yield
        finally:
            app.state.ready = False
            await _close_plugin_resources(app)
            AuditService(database).record(action="system.stop", outcome="success")
            _log.info("happy-one 已停止")

    app = FastAPI(
        title="happy-one",
        version=__version__,
        description="个人自用统一枢纽：身份、授权、产物分发、许可证、通道信令",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.db = Database(app_settings.database_path)
    bind_database(app.state.db)
    app.state.ready = False
    app.state.started_at = time.monotonic()
    app.state.oidc_signer = None

    @app.middleware("http")
    async def request_tracking(request: Request, call_next) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = (
            incoming if _REQUEST_ID_RE.match(incoming) else new_id("req")
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    # ---- 异常处理 ----

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            _log.error("业务异常 %s：%s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_envelope(_request_id(request)),
        )

    @app.exception_handler(OAuthError)
    async def handle_oauth_error(request: Request, exc: OAuthError) -> JSONResponse:
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        return JSONResponse(
            status_code=exc.status_code, content=exc.to_payload(), headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "loc": ".".join(str(part) for part in item.get("loc", [])),
                "msg": item.get("msg", ""),
                "type": item.get("type", ""),
            }
            for item in exc.errors()
        ]
        error = errors.validation("请求参数不合法", {"fields": details})
        return JSONResponse(
            status_code=error.status_code, content=error.to_envelope(_request_id(request))
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            error = errors.not_found("接口不存在")
        elif exc.status_code == 405:
            error = AppError("E_METHOD_NOT_ALLOWED", "请求方法不被允许", 405)
        else:
            error = AppError("E_INTERNAL", "请求处理失败", exc.status_code)
        return JSONResponse(
            status_code=error.status_code, content=error.to_envelope(_request_id(request))
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def handle_sqlite(
        request: Request, exc: sqlite3.OperationalError
    ) -> JSONResponse:
        _log.error("数据库操作失败", exc_info=exc)
        error = errors.unavailable("数据库繁忙，请稍后重试")
        return JSONResponse(
            status_code=error.status_code, content=error.to_envelope(_request_id(request))
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        _log.exception("未处理异常：%s", type(exc).__name__)
        error = AppError("E_INTERNAL", "服务器内部错误", 500)
        return JSONResponse(
            status_code=error.status_code, content=error.to_envelope(_request_id(request))
        )

    # ---- 健康与元信息 ----

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_sec": int(time.monotonic() - app.state.started_at),
        }

    @app.get("/readyz", tags=["health"])
    def readyz(request: Request) -> JSONResponse:
        database: Database = app.state.db
        payload: dict[str, Any] = {
            "status": "ready",
            "db": "ok",
            "migrations": len(database.migration_status()),
            "plugins": app.state.plugins,
        }
        if not app.state.ready:
            error = AppError("E_NOT_READY", "内核尚未就绪", 503)
            return JSONResponse(
                status_code=503, content=error.to_envelope(_request_id(request))
            )
        try:
            database.ping()
        except sqlite3.Error:
            _log.error("就绪检查失败：数据库不可用", exc_info=True)
            error = errors.unavailable("数据库不可用")
            return JSONResponse(
                status_code=503, content=error.to_envelope(_request_id(request))
            )
        return JSONResponse(payload)

    @app.get("/api/v1/meta", tags=["health"])
    def meta() -> dict[str, Any]:
        core_plugins = ["identity", "authz", "audit"]
        loaded = [
            name for name, status in app.state.plugins.items() if status == "loaded"
        ]
        return {
            "name": "happy-one",
            "version": __version__,
            "issuer": app_settings.issuer,
            "plugins": core_plugins + loaded,
        }

    # ---- 路由 ----

    app.include_router(identity_router)
    app.include_router(authz_router)
    app.include_router(audit_router)

    app.state.plugins = mount_plugins(app, logging.getLogger("happyone.plugins"))
    for name, status in app.state.plugins.items():
        if status != "loaded":
            _log.warning("业务线 %s：%s（应用继续启动）", name, status)

    return app


async def _close_plugin_resources(app: FastAPI) -> None:
    """关闭业务线持有的外部连接；目前只有 storage 维护 hub 连接池。"""
    if app.state.plugins.get("storage") != "loaded":
        return
    try:
        from happyone.storage.deps import aclose_hub_client
    except Exception:  # noqa: BLE001 - 关闭阶段不得再抛异常
        _log.exception("storage 依赖不可用，跳过 hub 连接池释放")
        return
    try:
        await aclose_hub_client()
    except Exception:  # noqa: BLE001
        _log.exception("释放 storage hub 连接池失败")


def _bootstrap_admin(
    database: Database, identity: IdentityService, settings: Settings
) -> None:
    """仅当 users 表为空且配置了引导账号时创建管理员。"""
    username = settings.bootstrap_admin_username
    secret = settings.bootstrap_admin_password
    if not username or secret is None:
        return
    if identity.count_users() > 0:
        return
    user = identity.create_user(
        username=username,
        password=secret.get_secret_value(),
        display_name="管理员",
        is_admin=True,
    )
    AuditService(database).record(
        action="user.bootstrap",
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
    )
    _log.info("已创建引导管理员：%s", user.username)


# uvicorn 使用工厂模式启动，避免 import 时产生数据目录副作用：
#   uvicorn happyone.app:create_app --factory
