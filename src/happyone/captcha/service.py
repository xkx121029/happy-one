"""人机验证服务：出题 → 校验操作过程 → 签发一次性票据。

设计目标：两到三秒完成、不烦人、脚本难过。
判定重心是「过程像不像人」（步间节奏、单步耗时、落点、可选轨迹采样），
而不是单纯校验答案；因此即使答案完全正确，过程失真也会被拒。

本文件是纯逻辑，**禁止 import fastapi**。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import random
import secrets
import time
from typing import Any

from . import deps, store
from .models import (
    Canvas,
    Challenge,
    Interaction,
    Item,
    PathPoint,
    Slot,
    SolutionRequest,
)

# --------------------------------------------------------------------------- #
# 可调参数（README 中同步说明；阈值改动请同步改文档与用例）
# --------------------------------------------------------------------------- #

PURPOSE = "captcha_pass"

CHALLENGE_TTL = 300  # 挑战 5 分钟过期
TICKET_TTL = 600  # 票据 10 分钟有效
MAX_ATTEMPTS = 3  # 同一题最多 3 次失败，之后必须换题

ITEM_COUNT = 4
CANVAS_W, CANVAS_H = 360, 220
ITEM_SIZE = 48
SLOT_SIZE = 52
ITEM_CENTER_Y = 62
SLOT_CENTER_Y = 172
SIDE_PADDING = 12

MIN_STEP_MS = 60.0  # 单步（按下→释放）不得低于 60ms
MAX_STEP_MS = 6000.0  # 单步不得高于 6s
MIN_GAP_MS = 100.0  # 上一步释放到下一步按下至少 100ms（防瞬移）
MIN_TOTAL_MS = 800.0  # 整题不得快于 0.8s（4 步）
MAX_TOTAL_MS = 30000.0  # 整题不得超过 30s
UNIFORM_CV = 0.08  # 步间间隔变异系数低于该值…
UNIFORM_RANGE_MS = 35.0  # …且极差小于 35ms，判为「过于均匀」（脚本特征）

POSITION_TOLERANCE = 40.0  # 落点距离目标中心允许的偏差
MAX_PATH_JUMP = 400.0  # 轨迹相邻采样最大跳变（防瞬移）
MIN_PATH_SAMPLES = 3  # 单步轨迹最少采样数


class CaptchaError(Exception):
    """统一业务错误：``{"ok": false, "code": ..., "error": "中文"}``。"""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"ok": False, "code": self.code, "error": self.message}
        body.update(self.extra)
        return body


# --------------------------------------------------------------------------- #
# 出题
# --------------------------------------------------------------------------- #


def _build_challenge(now: float) -> Challenge:
    rng = random.SystemRandom()
    values = rng.sample([2, 3, 4, 5, 6, 7, 8, 9], ITEM_COUNT)
    rng.shuffle(values)

    usable_w = CANVAS_W - SIDE_PADDING * 2
    column_w = usable_w / ITEM_COUNT

    items: list[Item] = []
    for index in range(ITEM_COUNT):
        center_x = SIDE_PADDING + column_w * (index + 0.5) + rng.uniform(-8, 8)
        center_y = ITEM_CENTER_Y + rng.uniform(-18, 18)
        items.append(
            Item(
                id=secrets.token_hex(3),
                label=str(values[index]),
                x=round(center_x - ITEM_SIZE / 2, 1),
                y=round(center_y - ITEM_SIZE / 2, 1),
                w=ITEM_SIZE,
                h=ITEM_SIZE,
            )
        )

    slots: list[Slot] = [
        Slot(
            id=f"s{index + 1}",
            index=index + 1,
            x=round(
                SIDE_PADDING + column_w * (index + 0.5) - SLOT_SIZE / 2, 1
            ),
            y=round(SLOT_CENTER_Y - SLOT_SIZE / 2, 1),
            w=SLOT_SIZE,
            h=SLOT_SIZE,
        )
        for index in range(ITEM_COUNT)
    ]

    target = [item.id for item in sorted(items, key=lambda it: int(it.label))]

    issued_at = int(now)
    return Challenge(
        id=f"c_{secrets.token_hex(12)}",
        title="按数字从小到大依次归位",
        hint="把每块碎片拖到下方对应序号的槽位；也可以直接按顺序点按碎片",
        canvas=Canvas(width=CANVAS_W, height=CANVAS_H),
        interaction=Interaction(
            mode="drag",
            alt_modes=["tap"],
            drop_tolerance=28,
            instructions="拖动碎片到槽位；无障碍模式：按顺序点按碎片",
        ),
        items=items,
        slots=slots,
        target=target,
        issued_at=issued_at,
        expires_at=issued_at + CHALLENGE_TTL,
        expires_in=CHALLENGE_TTL,
    )


def create_challenge() -> dict[str, Any]:
    now = time.time()
    challenge = _build_challenge(now)
    store.insert_challenge(
        challenge.id,
        challenge.model_dump_json(),
        now,
        float(challenge.expires_at),
        kind=challenge.type,
    )
    store.purge_expired(now)
    return {"ok": True, "challenge": challenge.model_dump()}


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #


def _parse_solution(payload: SolutionRequest | dict[str, Any]) -> SolutionRequest:
    if isinstance(payload, SolutionRequest):
        return payload
    try:
        return SolutionRequest.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError 及子类
        raise CaptchaError("invalid_request", "提交数据格式不正确", 422) from exc


def _check_structure(challenge: Challenge, solution: SolutionRequest) -> None:
    allowed = [challenge.interaction.mode, *challenge.interaction.alt_modes]
    if solution.mode not in allowed:
        raise CaptchaError("invalid_request", "不支持的交互方式", 422)

    steps = solution.steps
    if len(steps) != len(challenge.items):
        raise CaptchaError("invalid_request", "步骤数量与题目不符", 422)

    if [step.n for step in steps] != list(range(1, len(steps) + 1)):
        raise CaptchaError("invalid_request", "步骤序号不连续", 422)

    item_ids = {item.id for item in challenge.items}
    submitted = [step.item for step in steps]
    if len(set(submitted)) != len(submitted) or set(submitted) != item_ids:
        raise CaptchaError("invalid_request", "碎片与题目不匹配", 422)


def _check_timing(steps: list[Any]) -> None:
    prev_release: float | None = None
    gaps: list[float] = []

    for step in steps:
        if not math.isfinite(step.t) or not math.isfinite(step.dt):
            raise CaptchaError("invalid_request", "时间数据不合法", 422)
        if step.dt < MIN_STEP_MS:
            # 真实人类按下到释放不可能低于 60ms，零耗时直接判为脚本
            raise CaptchaError("timing_zero_step", "操作耗时为 0 或过短，请重试")
        if step.dt > MAX_STEP_MS:
            raise CaptchaError("timing_step_too_long", "单步耗时异常，请重试")

        start = step.t - step.dt
        if start < 0:
            raise CaptchaError("timing_invalid", "时间戳不合法，请重试")
        if prev_release is not None:
            gap = start - prev_release
            if gap < MIN_GAP_MS:
                raise CaptchaError("timing_too_fast", "步间间隔过短，请重试")
            gaps.append(gap)
        prev_release = step.t

    total = steps[-1].t
    if total < MIN_TOTAL_MS:
        raise CaptchaError("timing_too_fast", "整体操作过快，请放慢一点重试")
    if total > MAX_TOTAL_MS:
        raise CaptchaError("timing_too_slow", "整体操作超时，请刷新挑战重试")

    # 均匀到不真实：脚本常用固定 sleep；两个条件同时成立才判，避免误伤节奏稳定的人
    if len(gaps) >= 2:
        mean = sum(gaps) / len(gaps)
        if mean > 0:
            variance = sum((gap - mean) ** 2 for gap in gaps) / len(gaps)
            cv = math.sqrt(variance) / mean
            spread = max(gaps) - min(gaps)
            if cv < UNIFORM_CV and spread < UNIFORM_RANGE_MS:
                raise CaptchaError("timing_uniform", "操作节奏过于均匀，请重试")


def _check_positions(challenge: Challenge, solution: SolutionRequest) -> None:
    items = {item.id: item for item in challenge.items}
    slots = sorted(challenge.slots, key=lambda slot: slot.index)

    for index, step in enumerate(solution.steps):
        if not (math.isfinite(step.x) and math.isfinite(step.y)):
            raise CaptchaError("invalid_request", "坐标数据不合法", 422)

        if solution.mode == "tap":
            item = items[step.item]
            center_x, center_y = item.x + item.w / 2, item.y + item.h / 2
        else:
            slot = slots[index]
            center_x, center_y = slot.x + slot.w / 2, slot.y + slot.h / 2

        if not (-10 <= step.x <= CANVAS_W + 10 and -10 <= step.y <= CANVAS_H + 10):
            raise CaptchaError("position_invalid", "落点超出画布，请重试")

        distance = math.hypot(step.x - center_x, step.y - center_y)
        if distance > POSITION_TOLERANCE:
            raise CaptchaError("position_invalid", "落点不在目标区域内，请重试")


def _check_paths(steps: list[Any]) -> None:
    """轨迹采样是可选增强：一旦提交就必须自洽（采样足够、不瞬移、终点吻合）。"""
    for step in steps:
        path = step.path
        if not path:
            continue
        if len(path) < MIN_PATH_SAMPLES:
            raise CaptchaError("track_invalid", "轨迹采样过少，请重试")

        previous: PathPoint = path[0]
        for point in path[1:]:
            if point.t < previous.t:
                raise CaptchaError("track_invalid", "轨迹时间戳不合法，请重试")
            jump = math.hypot(point.x - previous.x, point.y - previous.y)
            if jump > MAX_PATH_JUMP:
                raise CaptchaError("track_invalid", "轨迹存在瞬移，请重试")
            previous = point

        tail = path[-1]
        if math.hypot(tail.x - step.x, tail.y - step.y) > 60:
            raise CaptchaError("track_invalid", "轨迹终点与落点不一致，请重试")


def _check_answer(challenge: Challenge, solution: SolutionRequest) -> None:
    submitted = [step.item for step in solution.steps]
    if submitted != challenge.target:
        raise CaptchaError("wrong_order", "顺序不正确，请重新排列")


def _verify_solution(challenge: Challenge, solution: SolutionRequest) -> None:
    _check_structure(challenge, solution)
    _check_timing(solution.steps)
    _check_positions(challenge, solution)
    _check_paths(solution.steps)
    _check_answer(challenge, solution)


# --------------------------------------------------------------------------- #
# 票据
# --------------------------------------------------------------------------- #


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(body: str) -> str:
    digest = hmac.new(deps.get_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def _ip_prefix(ip: str | None) -> str | None:
    """可选的 IP 绑定：IPv4 取 /24，IPv6 取 /48。解析失败则不绑定。"""
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    bits = 24 if address.version == 4 else 48
    network = ipaddress.ip_network(f"{address}/{bits}", strict=False)
    return f"{network.network_address}/{bits}"


def _issue_ticket(now: float, purpose: str, ip_prefix: str | None) -> tuple[str, int]:
    nonce = secrets.token_hex(16)
    issued_at = int(now)
    expires_at = issued_at + TICKET_TTL
    payload = {
        "v": 1,
        "p": purpose,
        "iat": issued_at,
        "exp": expires_at,
        "n": nonce,
        "ip": ip_prefix or "",
    }
    body = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _sign(body)
    store.insert_ticket(
        nonce, purpose, float(issued_at), float(expires_at), ip_prefix, signature
    )
    return f"v1.{body}.{signature}", TICKET_TTL


def _parse_ticket(ticket: str) -> dict[str, Any]:
    parts = (ticket or "").split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise CaptchaError("ticket_invalid", "票据格式不正确", 401)
    _, body, signature = parts
    if not hmac.compare_digest(_sign(body), signature):
        raise CaptchaError("ticket_invalid", "票据签名校验失败", 401)
    try:
        payload = json.loads(_b64decode(body))
    except Exception as exc:
        raise CaptchaError("ticket_invalid", "票据内容无法解析", 401) from exc
    if not isinstance(payload, dict) or not payload.get("n") or not payload.get("exp"):
        raise CaptchaError("ticket_invalid", "票据内容不完整", 401)
    return payload


def verify_ticket(
    ticket: str, purpose: str = PURPOSE, client_ip: str | None = None
) -> dict[str, Any]:
    payload = _parse_ticket(ticket)
    now = time.time()

    if payload.get("p") != purpose:
        raise CaptchaError("ticket_purpose", "票据用途不匹配", 401)
    if float(payload["exp"]) <= now:
        raise CaptchaError("ticket_expired", "票据已过期，请重新验证", 401)

    record = store.get_ticket(str(payload["n"]))
    if record is None:
        raise CaptchaError("ticket_invalid", "票据不存在或已被清理", 401)
    if record.used:
        raise CaptchaError("ticket_used", "票据已被使用，请重新验证", 409)
    if record.purpose != purpose:
        raise CaptchaError("ticket_purpose", "票据用途不匹配", 401)

    current_prefix = _ip_prefix(client_ip)
    if record.ip_prefix and current_prefix and record.ip_prefix != current_prefix:
        raise CaptchaError("ip_mismatch", "票据与当前网络环境不匹配", 403)

    if not store.consume_ticket(str(payload["n"]), now):
        raise CaptchaError("ticket_used", "票据已被使用，请重新验证", 409)

    return {
        "ok": True,
        "purpose": purpose,
        "issued_at": int(payload["iat"]),
        "expires_at": int(payload["exp"]),
    }


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def submit_solution(
    payload: SolutionRequest | dict[str, Any],
    client_ip: str | None = None,
) -> dict[str, Any]:
    solution = _parse_solution(payload)
    now = time.time()

    record = store.get_challenge(solution.challenge_id)
    if record is None:
        raise CaptchaError("challenge_not_found", "挑战不存在或已失效，请刷新", 404)
    if record.used:
        raise CaptchaError("challenge_used", "该挑战已使用过，请刷新", 409)
    if record.expires_at <= now:
        raise CaptchaError("challenge_expired", "挑战已过期，请刷新", 410)
    if record.attempts >= MAX_ATTEMPTS:
        raise CaptchaError(
            "too_many_attempts",
            "失败次数已达上限，请刷新挑战",
            429,
            {"attempts_left": 0},
        )

    try:
        challenge = Challenge.model_validate_json(record.payload)
        _verify_solution(challenge, solution)
    except CaptchaError as exc:
        attempts = store.bump_attempts(solution.challenge_id)
        left = max(MAX_ATTEMPTS - attempts, 0)
        if left <= 0:
            message = f"{exc.message}（已连续失败 {attempts} 次，请刷新挑战）"
        else:
            message = f"{exc.message}（剩余 {left} 次机会）"
        raise CaptchaError(exc.code, message, exc.status, {"attempts_left": left}) from None

    if not store.mark_solved(solution.challenge_id, now):
        raise CaptchaError("challenge_used", "该挑战已使用过，请刷新", 409)

    ticket, ttl = _issue_ticket(now, PURPOSE, _ip_prefix(client_ip))
    return {"ok": True, "ticket": ticket, "purpose": PURPOSE, "expires_in": ttl}
