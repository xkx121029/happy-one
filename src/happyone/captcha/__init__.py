"""人机验证模块。

包内契约：
* :mod:`happyone.captcha.router` —— 模块级 ``router``，由骨架线挂载
* :mod:`happyone.captcha.service` —— 纯逻辑，不依赖 fastapi
* :mod:`happyone.captcha.models` —— 数据模型与 schema

注意：本 ``__init__`` 故意不导入 ``router``，避免在未安装 fastapi 的环境中
（如纯逻辑单测）导入包时就失败。
"""

from .models import (
    Challenge,
    ChallengeResponse,
    ErrorResponse,
    SolutionRequest,
    SolveResponse,
    VerifyRequest,
    VerifyResponse,
)
from .service import CaptchaError, create_challenge, submit_solution, verify_ticket

__all__ = [
    "CaptchaError",
    "Challenge",
    "ChallengeResponse",
    "ErrorResponse",
    "SolutionRequest",
    "SolveResponse",
    "VerifyRequest",
    "VerifyResponse",
    "create_challenge",
    "submit_solution",
    "verify_ticket",
]
