"""人机验证模块的数据模型。

本文件只依赖 pydantic，不依赖 fastapi，便于单测与在非 Web 进程内复用。
坐标统一为「画布左上角为原点、单位为逻辑像素」，(x, y) 指元素左上角。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LenientModel(BaseModel):
    """请求侧宽松模型：忽略前端多传的字段，避免因版本差异直接 422。"""

    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------- #
# 挑战描述（服务端出题，前端照此渲染）
# --------------------------------------------------------------------------- #


class Canvas(BaseModel):
    width: int
    height: int


class Interaction(BaseModel):
    """交互方式声明：主通道拖拽，等价通道按顺序点按（无障碍）。"""

    mode: Literal["drag"] = "drag"
    alt_modes: list[str] = Field(default_factory=lambda: ["tap"])
    drop_tolerance: int = 28
    events: dict[str, str] = Field(
        default_factory=lambda: {
            "grab": "pointerdown",
            "move": "pointermove",
            "drop": "pointerup",
        }
    )
    instructions: str = "把碎片拖到下方槽位；不便拖拽时，按顺序点按碎片即可"


class Item(BaseModel):
    id: str
    label: str
    x: float
    y: float
    w: float
    h: float
    shape: Literal["chip"] = "chip"


class Slot(BaseModel):
    id: str
    index: int  # 1 起，等于目标顺序中的第几格
    x: float
    y: float
    w: float
    h: float


class Challenge(BaseModel):
    id: str
    type: Literal["sequence"] = "sequence"
    title: str
    hint: str
    canvas: Canvas
    interaction: Interaction
    items: list[Item]
    slots: list[Slot]
    target: list[str]  # 目标顺序（item id，按槽位 1..n）
    issued_at: int
    expires_at: int
    expires_in: int


class ChallengeResponse(BaseModel):
    ok: Literal[True] = True
    challenge: Challenge


# --------------------------------------------------------------------------- #
# 提交解答（操作过程，而不是只交答案）
# --------------------------------------------------------------------------- #


class PathPoint(LenientModel):
    x: float
    y: float
    t: float = 0.0


class Step(LenientModel):
    n: int  # 步骤序号，1 起且必须连续
    item: str  # 该步操作的碎片 id
    x: float  # 拖拽模式的释放点 / 点按模式的点击点
    y: float
    t: float  # 相对毫秒时间戳（自进入本题起单调递增）
    dt: float  # 该步耗时（按下到释放，毫秒）
    path: list[PathPoint] | None = None  # 可选：该步的指针采样轨迹


class SolutionRequest(LenientModel):
    challenge_id: str
    mode: Literal["drag", "tap"] = "drag"
    steps: list[Step] = Field(default_factory=list)


class SolveResponse(BaseModel):
    ok: Literal[True] = True
    ticket: str
    purpose: str
    expires_in: int


# --------------------------------------------------------------------------- #
# 票据校验（内部接口）
# --------------------------------------------------------------------------- #


class VerifyRequest(LenientModel):
    ticket: str
    purpose: str = "captcha_pass"


class VerifyResponse(BaseModel):
    ok: Literal[True] = True
    purpose: str
    issued_at: int
    expires_at: int


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    code: str
    error: str
    attempts_left: int | None = None
