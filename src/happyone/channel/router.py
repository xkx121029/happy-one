"""通道层 HTTP + WebSocket 适配。

挂载方式（骨架线负责）：`app.include_router(channel.router.router)`。
所有异常在本模块内收敛为 `{"ok": false, "code": ..., "error": "中文"}`，
不会向 FastAPI 抛出 500 默认页，也不会影响登录、授权、下载等其他路由。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import deps
from .models import (
    FORWARDABLE_EVENTS,
    ClientEvent,
    PairCreateRequest,
    PairJoinRequest,
    ServerEvent,
)
from .service import ChannelError, PairingService

logger = logging.getLogger("happyone.channel")

router = APIRouter(tags=["channel"])

_QUEUE_MAXSIZE = 256


def configure(service: PairingService | None = None) -> PairingService:
    """测试或骨架需要自定义实例时注入；传 None 表示按配置重建。"""
    return deps.set_service(service)


# --------------------------------------------------------------------- HTTP


@router.post("/channel/pair")
async def create_pair(request: Request) -> JSONResponse:
    try:
        body = await _read_json(request)
        payload = PairCreateRequest.model_validate(body)
        service = deps.get_service()
        _ensure_reaper(service)
        result = service.create_pair(payload.role, payload.device_name)
        return JSONResponse(status_code=201, content=result.model_dump(mode="json"))
    except ChannelError as exc:
        return _error_response(exc)
    except ValidationError:
        return _error_response(ChannelError("bad_request"))
    except Exception:
        logger.exception("channel.create_pair failed")
        return _error_response(ChannelError("internal", status=500))


@router.post("/channel/pair/join")
async def join_pair(request: Request) -> JSONResponse:
    try:
        body = await _read_json(request)
        payload = PairJoinRequest.model_validate(body)
        service = deps.get_service()
        _ensure_reaper(service)
        result = service.join_pair(payload.code, payload.role, payload.device_name)
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))
    except ChannelError as exc:
        return _error_response(exc)
    except ValidationError:
        return _error_response(ChannelError("bad_request"))
    except Exception:
        logger.exception("channel.join_pair failed")
        return _error_response(ChannelError("internal", status=500))


@router.get("/channel/pair/{session_id}")
async def pair_status(session_id: str) -> JSONResponse:
    try:
        service = deps.get_service()
        _ensure_reaper(service)
        result = service.get_status(session_id)
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))
    except ChannelError as exc:
        return _error_response(exc)
    except Exception:
        logger.exception("channel.pair_status failed")
        return _error_response(ChannelError("internal", status=500))


@router.post("/channel/pair/{session_id}/revoke")
async def revoke_pair(session_id: str, request: Request) -> JSONResponse:
    try:
        body = await _read_json(request)
        token = body.get("token")
        if token is not None and not isinstance(token, str):
            raise ChannelError("token_invalid", status=403)
        service = deps.get_service()
        _ensure_reaper(service)
        result = service.revoke(session_id, token)
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))
    except ChannelError as exc:
        return _error_response(exc)
    except Exception:
        logger.exception("channel.revoke_pair failed")
        return _error_response(ChannelError("internal", status=500))


# ---------------------------------------------------------------- WebSocket


@router.websocket("/ws/signal")
async def signal_channel(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        service = deps.get_service()
        _ensure_reaper(service)
    except Exception:
        logger.exception("channel service unavailable")
        await _reject(websocket, ChannelError("internal", status=500))
        return

    params = websocket.query_params
    session_id = (params.get("session_id") or "").strip()
    role = (params.get("role") or "").strip().lower()
    token = params.get("token") or ""
    if not session_id or not role or not token:
        await _reject(websocket, ChannelError("token_required"))
        return

    queue: "asyncio.Queue[dict[str, Any] | None]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    try:
        outcome = service.attach(session_id, role, token, queue)
    except ChannelError as exc:
        await _reject(websocket, exc)
        return
    except Exception:
        logger.exception("channel.attach failed")
        await _reject(websocket, ChannelError("internal", status=500))
        return

    connection = outcome.connection
    sender = asyncio.create_task(_send_loop(websocket, queue))
    service.notify(connection, outcome.ready_event)
    service.notify(outcome.peer_conn, outcome.peer_notice)

    reason = "disconnected"
    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=service.heartbeat_timeout
                )
            except asyncio.TimeoutError:
                # 半开连接：明确告知客户端原因，而不是静默断开。
                reason = "heartbeat_timeout"
                service.notify(connection, service.error_event(session_id, "heartbeat_timeout"))
                break

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                service.notify(connection, service.error_event(session_id, "bad_json"))
                continue
            if not isinstance(message, dict):
                service.notify(connection, service.error_event(session_id, "bad_event"))
                continue

            event_type = message.get("type")
            if not isinstance(event_type, str):
                service.notify(connection, service.error_event(session_id, "bad_event"))
                continue
            if event_type == ClientEvent.PING:
                service.touch(session_id, role)
                service.notify(connection, service.pong_event(session_id))
                continue
            if event_type == ClientEvent.BYE:
                reason = "peer_bye"
                break
            if event_type in FORWARDABLE_EVENTS:
                try:
                    service.route_signal(session_id, role, event_type, message.get("payload"))
                except ChannelError as exc:
                    service.notify(
                        connection, service.error_event(session_id, exc.code, exc.message)
                    )
                continue
            service.notify(connection, service.error_event(session_id, "unknown_event"))
    except WebSocketDisconnect:
        reason = "disconnected"
    except Exception:
        logger.exception("channel websocket loop failed")
        reason = "internal_error"
        service.notify(connection, service.error_event(session_id, "internal"))
    finally:
        result = service.detach(session_id, role, reason, connection=connection)
        if result.peer_conn is not None and result.peer_event is not None:
            service.notify(result.peer_conn, result.peer_event)
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(sender, timeout=1.0)
        except asyncio.TimeoutError:
            sender.cancel()
        except Exception:
            pass
        await _safe_close(websocket)


# -------------------------------------------------------------------- 工具


async def _read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        # 允许空 body：由具体接口决定是 token_required 还是 bad_request
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        raise ChannelError("bad_json") from None
    if not isinstance(data, dict):
        raise ChannelError("bad_request")
    return data


def _error_response(exc: ChannelError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"ok": False, "code": exc.code, "error": exc.message},
    )


async def _send_loop(
    websocket: WebSocket, queue: "asyncio.Queue[dict[str, Any] | None]"
) -> None:
    while True:
        event = await queue.get()
        if event is None:
            # 先把已排队的事件发完再关流，保证 session_closed / heartbeat_timeout 不丢。
            await _safe_close(websocket)
            return
        try:
            await websocket.send_json(event)
        except Exception:
            return


async def _reject(websocket: WebSocket, exc: ChannelError) -> None:
    await _safe_send(
        websocket,
        {
            "type": ServerEvent.ERROR,
            "ok": False,
            "code": exc.code,
            "error": exc.message,
        },
    )
    await _safe_close(websocket, code=1008)


async def _safe_send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await websocket.send_json(payload)
    except Exception:
        pass


async def _safe_close(websocket: WebSocket, code: int = 1000) -> None:
    try:
        await websocket.close(code=code)
    except Exception:
        pass


def _ensure_reaper(service: PairingService) -> None:
    """懒启动 TTL 清理任务；每个事件循环各自持有，重复调用无副作用。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = service.reaper_task
    if task is not None and not task.done():
        return
    service.reaper_task = loop.create_task(_reaper_loop(service))


async def _reaper_loop(service: PairingService) -> None:
    try:
        while True:
            await asyncio.sleep(max(service.sweep_interval, 1.0))
            try:
                service.sweep()
            except Exception:
                logger.exception("channel sweep failed")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("channel reaper stopped")


__all__ = ["configure", "router"]
