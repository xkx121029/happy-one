"""路由层单测：验证挂载契约与统一错误格式（缺少 fastapi/httpx 时自动跳过）。"""

from __future__ import annotations

import unittest

from support import build_solution

try:  # 骨架线依赖可能尚未安装
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from happyone.captcha.router import router

    HAVE_STACK = True
except Exception:  # pragma: no cover - 环境缺少依赖时跳过
    HAVE_STACK = False


@unittest.skipUnless(HAVE_STACK, "未安装 fastapi/httpx，跳过路由测试")
class CaptchaRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        from happyone.captcha import deps

        deps.reset_test_state()
        deps._SCHEMA_TEXT = None
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_flow_through_http(self) -> None:
        response = self.client.post("/captcha/v1/challenge")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])

        challenge = body["challenge"]
        solve = self.client.post(
            "/captcha/v1/solve", json=build_solution(challenge)
        )
        self.assertEqual(solve.status_code, 200)
        ticket = solve.json()["ticket"]
        self.assertTrue(ticket)

        verify = self.client.post("/captcha/v1/verify", json={"ticket": ticket})
        self.assertEqual(verify.status_code, 200)
        self.assertTrue(verify.json()["ok"])

        replay = self.client.post("/captcha/v1/verify", json={"ticket": ticket})
        self.assertEqual(replay.status_code, 409)
        self.assertFalse(replay.json()["ok"])
        self.assertEqual(replay.json()["code"], "ticket_used")

    def test_business_error_format(self) -> None:
        challenge = self.client.post("/captcha/v1/challenge").json()["challenge"]
        payload = build_solution(challenge, order=list(reversed(challenge["target"])))
        response = self.client.post("/captcha/v1/solve", json=payload)
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "wrong_order")
        self.assertIn("顺序", body["error"])
        self.assertEqual(body["attempts_left"], 2)


if __name__ == "__main__":
    unittest.main()
