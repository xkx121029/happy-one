"""tests/captcha 公共工具：路径自举、取题、构造轨迹。"""

from __future__ import annotations

import sys
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from happyone.captcha import service  # noqa: E402

DEFAULT_GAPS = (520, 610, 480)  # 步间空档（毫秒）
DEFAULT_DTS = (180, 240, 200, 260)  # 单步耗时（毫秒）


def fetch_challenge() -> dict:
    return service.create_challenge()["challenge"]


def build_solution(
    challenge: dict,
    *,
    mode: str = "drag",
    gaps: tuple[float, ...] = DEFAULT_GAPS,
    dts: tuple[float, ...] = DEFAULT_DTS,
    order: list[str] | None = None,
    coords: list[tuple[float, float]] | None = None,
) -> dict:
    """按给定节奏与顺序拼一条提交数据；默认是一条「像人」的合法轨迹。"""
    target = list(order) if order is not None else list(challenge["target"])
    items = {item["id"]: item for item in challenge["items"]}
    slots = sorted(challenge["slots"], key=lambda slot: slot["index"])

    steps: list[dict] = []
    start = 0.0
    for index, item_id in enumerate(target):
        if index > 0:
            start = start + dts[index - 1] + gaps[index - 1]

        if coords is not None:
            x, y = coords[index]
        elif mode == "tap":
            item = items[item_id]
            x, y = item["x"] + item["w"] / 2, item["y"] + item["h"] / 2
        else:
            slot = slots[index]
            x, y = slot["x"] + slot["w"] / 2, slot["y"] + slot["h"] / 2

        steps.append(
            {
                "n": index + 1,
                "item": item_id,
                "x": x,
                "y": y,
                "t": start + dts[index],
                "dt": dts[index],
            }
        )

    return {"challenge_id": challenge["id"], "mode": mode, "steps": steps}
