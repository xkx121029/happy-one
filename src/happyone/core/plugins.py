"""业务线插件挂载。

契约（见 docs/ARCHITECTURE.md 2.2 / 2.3）：
- 每个业务线包提供模块级 `router: APIRouter`。
- 可选提供 `deps.configure(*, database, settings)` 作为依赖注入点，挂载前调用。
- 包缺失、导入失败、注入失败或 router 缺失时记录日志并跳过，绝不让应用启动失败。
"""

from __future__ import annotations

import importlib
import logging

from fastapi import APIRouter, FastAPI

PLUGIN_PACKAGES: tuple[str, ...] = ("license", "storage", "channel", "captcha")

LOADED = "loaded"
SKIPPED = "skipped"


def mount_plugins(app: FastAPI, logger: logging.Logger | None = None) -> dict[str, str]:
    """扫描并挂载可选业务线，返回 {包名: loaded|skipped}。"""
    log = logger or logging.getLogger("happyone.plugins")
    results: dict[str, str] = {}

    for name in PLUGIN_PACKAGES:
        module_path = f"happyone.{name}.router"
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            if exc.name == f"happyone.{name}":
                log.info("业务线 %s 未就绪，跳过挂载", name)
            else:
                log.error(
                    "业务线 %s 的 router 依赖缺失（%s），已跳过挂载",
                    name,
                    exc.name,
                    exc_info=True,
                )
            results[name] = SKIPPED
            continue
        except Exception:  # noqa: BLE001 - 任何导入错误都必须被隔离
            log.error("业务线 %s 的 router 导入失败，已跳过挂载", name, exc_info=True)
            results[name] = SKIPPED
            continue

        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            log.error("业务线 %s 未暴露模块级 router: APIRouter，已跳过挂载", name)
            results[name] = SKIPPED
            continue

        if not _inject_dependencies(name, app, log):
            results[name] = SKIPPED
            continue

        try:
            app.include_router(router)
        except Exception:  # noqa: BLE001 - 路由冲突等错误不得拖垮应用
            log.error("业务线 %s 的 router 挂载失败，已跳过", name, exc_info=True)
            results[name] = SKIPPED
            continue

        results[name] = LOADED
        log.info("业务线 %s 已挂载", name)

    return results


def _inject_dependencies(name: str, app: FastAPI, log: logging.Logger) -> bool:
    """调用可选的 deps.configure(database=..., settings=...)。"""
    module_path = f"happyone.{name}.deps"
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name == module_path:
            log.info("业务线 %s 未提供 deps 注入点，使用模块级兼容配置", name)
            return True
        log.error(
            "业务线 %s 的 deps 依赖缺失（%s），已跳过挂载",
            name,
            exc.name,
            exc_info=True,
        )
        return False
    except Exception:  # noqa: BLE001
        log.error("业务线 %s 的 deps 导入失败，已跳过挂载", name, exc_info=True)
        return False

    configure = getattr(module, "configure", None)
    if not callable(configure):
        log.info("业务线 %s 的 deps 未暴露 configure，使用模块级兼容配置", name)
        return True

    try:
        configure(database=app.state.db, settings=app.state.settings)
    except Exception:  # noqa: BLE001 - 注入失败时不挂载半成品路由
        log.error("业务线 %s 的 deps.configure 注入失败，已跳过挂载", name, exc_info=True)
        return False

    log.info("业务线 %s 已注入 database/settings", name)
    return True
